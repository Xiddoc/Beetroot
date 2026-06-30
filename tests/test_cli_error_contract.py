"""H3 guardrail — verbs surface domain errors as `error: ...` + exit 1.

The CR root-cause was that v0.3 verbs (up/down/restart/logs/apply/build)
let `compose.ComposeError` / `builder.BootstrapError` propagate as
tracebacks, breaking the v0.2 contract of "every failure mode prints
`error: <message>` on stderr and exits 1".

These tests patch the deep call sites of each verb to raise a domain
exception, then assert the CLI's user-visible contract:

* exit code 1
* stderr starts with "error:"
* stderr does NOT contain "Traceback"
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from beetroot import builder, cli, compose, console, registry

_CORPUS_DIR = Path(__file__).parent / "corpus"
_CORPUS_FILES = sorted(_CORPUS_DIR.glob("*.yaml"))


def _run_main_with_argv(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """Drive cli.main() under a faked argv. Returns (exit_code, stderr)."""
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    console.set_consoles(stderr=Console(file=buf, force_terminal=False))
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0), buf.getvalue()
    return 0, buf.getvalue()


class TestComposeErrorSurfacing:
    def test_up_surfaces_compose_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        CliRunner().invoke(cli.app, ["create", "alpha"])

        def _boom(name: str, root: Path) -> None:
            raise compose.ComposeError("simulated compose up failure")

        monkeypatch.setattr(compose, "up", _boom)
        code, err = _run_main_with_argv(["beetroot", "up", "alpha"], monkeypatch)
        assert code == 1
        assert "error:" in err
        assert "simulated compose up failure" in err
        assert "Traceback" not in err

    def test_down_surfaces_compose_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        CliRunner().invoke(cli.app, ["create", "alpha"])

        def _boom(name: str, root: Path, *, volumes: bool = False) -> None:
            raise compose.ComposeError("simulated down")

        # `destroy` deliberately catches ComposeError inside the verb;
        # the `down` verb does NOT, so it should surface here.
        monkeypatch.setattr(compose, "down", _boom)
        code, err = _run_main_with_argv(["beetroot", "down", "alpha"], monkeypatch)
        assert code == 1
        assert "error:" in err
        assert "simulated down" in err
        assert "Traceback" not in err

    def test_restart_surfaces_compose_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        CliRunner().invoke(cli.app, ["create", "alpha"])

        def _boom(name: str, root: Path, *, volumes: bool = False) -> None:
            raise compose.ComposeError("restart kaboom")

        monkeypatch.setattr(compose, "down", _boom)
        code, err = _run_main_with_argv(["beetroot", "restart", "alpha"], monkeypatch)
        assert code == 1
        assert "error:" in err
        assert "restart kaboom" in err
        assert "Traceback" not in err

    def test_apply_compose_error_not_raised_by_apply_itself(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # apply doesn't talk to compose, but does load frida_download which
        # can fail. Simulate by making frida_download.stage_for_instance
        # raise RuntimeError — verify it's caught by cli.main(). The
        # spec says BootstrapError too, but the apply path raises
        # bare RuntimeError if e.g. frida download fails. So this
        # just pins that the apply path doesn't tracebackfly out:
        # build verb is exercised below.
        CliRunner().invoke(cli.app, ["create", "alpha"])
        # No exception expected for plain apply.
        code, err = _run_main_with_argv(["beetroot", "apply", "alpha"], monkeypatch)
        assert code == 0
        assert "error:" not in err

    def test_apply_surfaces_frida_fetch_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #167: apply routes _stage_network non-softly; a frida resolve /
        # download failure raises FridaFetchError (a RuntimeError), which
        # apply's inline ValueError catch misses. cli.main() must map it to
        # the friendly error: ... + exit 1 contract, never a raw traceback.
        from beetroot import config, frida_download, registry

        CliRunner().invoke(cli.app, ["create", "alpha"])
        # A frida block makes apply route through frida_download.stage_for_instance
        # (the default no-frida config stages an empty placeholder and never
        # touches the network).
        yaml_path = registry.instance_path("alpha") / "beetroot.yaml"
        yaml_path.write_text(
            f"api_version: {config.SUPPORTED_API_VERSION}\n"
            "android:\n  version: 14\nfrida:\n  version: 16.4.10\n",
            encoding="utf-8",
        )

        def _boom(root: Path, version: str, *, expected_sha256: str | None = None) -> None:
            raise frida_download.FridaFetchError("simulated frida download failure")

        monkeypatch.setattr(frida_download, "stage_for_instance", _boom)
        code, err = _run_main_with_argv(["beetroot", "apply", "alpha"], monkeypatch)
        assert code == 1
        assert "error:" in err
        assert "simulated frida download failure" in err
        assert "Traceback" not in err


class TestBootstrapErrorSurfacing:
    def test_build_surfaces_bootstrap_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(
            *, gapps: str, gapps_vendor: str | None = None, build_context: Path | None = None
        ) -> str:
            raise builder.BootstrapError("simulated bootstrap failure")

        monkeypatch.setattr(builder, "build_image", _boom)
        code, err = _run_main_with_argv(["beetroot", "build", "minimal"], monkeypatch)
        assert code == 1
        assert "error:" in err
        assert "simulated bootstrap failure" in err
        assert "Traceback" not in err


class TestHostileConfigSurfacing:
    """A corpus of hostile ``beetroot.yaml`` files must never traceback.

    Issue #21 ("adversarial config corpus"): every malformed or
    unsupported config — wrong field types, out-of-range geometry,
    unsupported / non-numeric ``api_version``, the removed ``stealth:``
    section, a top-level sequence, an invalid denylist package, and
    syntactically broken YAML — must reach the user as ``error: ...`` +
    exit 1, never a Rich-rendered traceback.

    The malformed-syntax file is the regression guard for the bug this
    slice fixed: ``yaml.YAMLError`` is *not* a ``ValueError``, so even the
    ``register``/``adopt`` verbs (which catch ``ValueError`` inline) let it
    propagate as a traceback until ``cli.main`` learned to catch it.
    """

    def test_corpus_is_non_empty(self) -> None:
        # Guard against a silently-empty parametrization (e.g. the corpus
        # dir vanishing from the wheel) making every case below a no-op.
        assert _CORPUS_FILES, f"no corpus files under {_CORPUS_DIR}"

    @pytest.mark.parametrize("corpus_file", _CORPUS_FILES, ids=[p.stem for p in _CORPUS_FILES])
    def test_register_hostile_yaml_is_friendly(
        self,
        corpus_file: Path,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = cli_root / corpus_file.stem
        target.mkdir()
        (target / "beetroot.yaml").write_text(
            corpus_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        code, err = _run_main_with_argv(
            ["beetroot", "register", str(target), "--name", "victim"],
            monkeypatch,
        )
        assert code == 1
        assert err.startswith("error:")
        assert "Traceback" not in err

    def test_validation_error_via_name_resolved_verb_is_friendly(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A *registered* instance whose YAML is corrupted out-of-band is
        # loaded deep in a name-resolved verb (``status``), which — unlike
        # ``register`` — does not catch ``ValueError`` inline. Pre-fix this
        # ``pydantic.ValidationError`` tracebacked; ``cli.main`` now nets it.
        CliRunner().invoke(cli.app, ["create", "alpha"])
        (cli_root / "alpha" / "beetroot.yaml").write_text(
            'api_version: 8\nresources:\n  cpus: "lots"\n'
        )
        code, err = _run_main_with_argv(["beetroot", "status", "alpha"], monkeypatch)
        assert code == 1
        assert err.startswith("error:")
        assert "Traceback" not in err


class TestRegistryErrorSurfacing:
    """T2 Agent 3 1.9: registry.RegistryError catches in cli.main."""

    def test_registry_error_surfaced_via_main(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Inject a verb that raises ``RegistryError``. Pre-T2 the
        # CLI would have propagated this as a Rich-rendered
        # traceback; post-T2 ``cli.main`` catches it alongside
        # ComposeError / BootstrapError / etc.
        def _boom() -> None:
            raise registry.RegistryError("simulated registry inconsistency")

        monkeypatch.setattr(cli, "app", _boom)
        code, err = _run_main_with_argv(["beetroot", "ls"], monkeypatch)
        assert code == 1
        assert "error:" in err
        assert "simulated registry inconsistency" in err
        assert "Traceback" not in err


class TestFromDataCopyErrorSurfacing:
    """#180: a non-FileNotFoundError OSError from the --from-data copy.

    rmtree/copytree can raise PermissionError, shutil.Error, or
    OSError(ENOSPC) — none of which is the FileNotFoundError cli.main()
    catches. The create verb must wrap the copy and name the failure as a
    friendly ``error: --from-data copy failed: ...`` line + exit 1.
    """

    def test_from_data_copytree_oserror_friendly(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = cli_root / "donor"
        src.mkdir()
        (src / "marker").write_text("x", encoding="utf-8")

        def _boom(*args: object, **kwargs: object) -> None:
            raise PermissionError("permission denied")

        monkeypatch.setattr(shutil, "copytree", _boom)
        code, err = _run_main_with_argv(
            ["beetroot", "create", "alpha", "--from-data", str(src)],
            monkeypatch,
        )
        assert code == 1
        assert "error:" in err
        assert "--from-data copy failed" in err
        assert "Traceback" not in err

    def test_from_data_rmtree_oserror_friendly(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from beetroot import paths

        src = cli_root / "donor"
        src.mkdir()
        (src / "marker").write_text("x", encoding="utf-8")
        # Pre-populate the target's data dir so the rmtree branch runs.
        target_root = (cli_root / "alpha").resolve()
        paths.instance_data(target_root).mkdir(parents=True)

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("device busy")

        monkeypatch.setattr(shutil, "rmtree", _boom)
        code, err = _run_main_with_argv(
            ["beetroot", "create", "alpha", "--from-data", str(src)],
            monkeypatch,
        )
        assert code == 1
        assert "error:" in err
        assert "--from-data copy failed" in err
        assert "Traceback" not in err
