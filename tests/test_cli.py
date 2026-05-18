"""Tests for cli.py — Typer dispatch and every verb handler."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from beetroot import cli, config, paths, ports, registry, snapshot

runner = CliRunner()


def _ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _patched_subprocess() -> Any:
    """Patch subprocess.run inside compose.* so no real docker is invoked."""
    return patch("subprocess.run", return_value=_ok_proc())


# ---------------------------------------------------------------------------
# beetroot build
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_default_gapps_is_lite(self) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bs:
            mock_bs.return_value = "redroid/redroid:14.0.0_litegapps_houdini_magisk"
            result = runner.invoke(cli.app, ["build"])
        assert result.exit_code == 0
        mock_bs.assert_called_once_with(gapps="lite")

    @pytest.mark.parametrize("variant", ["none", "lite", "full", "mindthegapps"])
    def test_each_variant_parses(self, variant: str) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bs:
            mock_bs.return_value = f"redroid/redroid:14.0.0_{variant}_houdini_magisk"
            result = runner.invoke(cli.app, ["build", variant])
        assert result.exit_code == 0
        mock_bs.assert_called_once_with(gapps=variant)

    def test_invalid_variant_exits(self) -> None:
        result = runner.invoke(cli.app, ["build", "blah"])
        assert result.exit_code != 0
        err = result.stderr
        assert "blah" in err
        for variant in ("none", "lite", "full", "mindthegapps"):
            assert variant in err

    def test_help_lists_variants(self) -> None:
        result = runner.invoke(cli.app, ["build", "--help"])
        assert result.exit_code == 0
        out = result.stdout
        for variant in ("none", "lite", "full", "mindthegapps"):
            assert variant in out


class TestBuildDispatch:
    def test_cmd_build_invokes_bootstrap_with_lite_default(self) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bs:
            mock_bs.return_value = "redroid/redroid:14.0.0_litegapps_houdini_magisk"
            result = runner.invoke(cli.app, ["build"])
        mock_bs.assert_called_once_with(gapps="lite")
        assert result.exit_code == 0
        assert "redroid/redroid:14.0.0_litegapps_houdini_magisk" in result.stdout

    @pytest.mark.parametrize("variant", ["none", "lite", "full", "mindthegapps"])
    def test_cmd_build_forwards_each_variant(self, variant: str) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bs:
            mock_bs.return_value = f"redroid/redroid:14.0.0_{variant}_houdini_magisk"
            result = runner.invoke(cli.app, ["build", variant])
        assert result.exit_code == 0
        mock_bs.assert_called_once_with(gapps=variant)

    def test_main_dispatches_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "build", "full"])
        with patch("beetroot.cli.builder.build_image") as mock_bs:
            mock_bs.return_value = "redroid/redroid:14.0.0_gapps_houdini_magisk"
            # Typer's standalone-mode invocation exits via SystemExit even
            # on the happy path (Click semantics).
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 0
        mock_bs.assert_called_once_with(gapps="full")


# ---------------------------------------------------------------------------
# _ensure_exists
# ---------------------------------------------------------------------------


class TestEnsureExists:
    def test_exits_when_instance_missing(self, cli_root: Path) -> None:
        import typer as _typer

        with pytest.raises(_typer.Exit) as exc:
            cli._ensure_exists("ghost")
        assert exc.value.exit_code == 1

    def test_returns_none_when_instance_exists(self, cli_root: Path) -> None:
        registry.add("alpha", cli_root / "alpha", 0)
        cli._ensure_exists("alpha")


# ---------------------------------------------------------------------------
# _resolve_names
# ---------------------------------------------------------------------------


class TestResolveNames:
    def test_explicit_names(self, cli_root: Path) -> None:
        assert cli._resolve_names(["alpha", "bravo"], all_flag=False) == ["alpha", "bravo"]

    def test_all_returns_sorted_registry(self, cli_root: Path) -> None:
        registry.add("bravo", cli_root / "bravo", 1)
        registry.add("alpha", cli_root / "alpha", 0)
        assert cli._resolve_names([], all_flag=True) == ["alpha", "bravo"]

    def test_all_and_names_mutex(self, cli_root: Path) -> None:
        import typer as _typer

        with pytest.raises(_typer.Exit) as exc:
            cli._resolve_names(["alpha"], all_flag=True)
        assert exc.value.exit_code == 1

    def test_no_names_no_all_errors(self, cli_root: Path) -> None:
        import typer as _typer

        with pytest.raises(_typer.Exit) as exc:
            cli._resolve_names([], all_flag=False)
        assert exc.value.exit_code == 1

    def test_all_with_empty_registry_exits_zero(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import typer as _typer

        with pytest.raises(_typer.Exit) as exc_info:
            cli._resolve_names([], all_flag=True)
        assert exc_info.value.exit_code == 0
        assert "no instances" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_create
# ---------------------------------------------------------------------------


class TestCmdCreate:
    def test_create_basic(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is not None
        root = registry.instance_path("alpha")
        assert paths.instance_yaml(root).exists()
        assert paths.instance_env(root).exists()

    def test_create_default_path_is_cwd_subdir(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert registry.instance_path("alpha") == (cli_root / "alpha").resolve()

    def test_create_explicit_path(self, cli_root: Path) -> None:
        target = cli_root / "deep" / "nested" / "alpha-dir"
        result = runner.invoke(cli.app, ["create", "alpha", "--path", str(target)])
        assert result.exit_code == 0, result.stderr
        assert registry.instance_path("alpha") == target.resolve()
        assert (target / "beetroot.yaml").exists()

    def test_create_duplicate_exits(self, cli_root: Path) -> None:
        registry.add("alpha", cli_root / "alpha", 0)
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 1
        assert "already exists" in result.stderr

    def test_create_refuses_existing_yaml(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        target.mkdir()
        (target / "beetroot.yaml").write_text("api_version: 2\n")
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 1
        assert "register" in result.stderr

    def test_create_with_from_data_copies(self, cli_root: Path) -> None:
        src = cli_root / "old-data"
        src.mkdir()
        (src / "marker.txt").write_text("hello")
        result = runner.invoke(cli.app, ["create", "alpha", "--from-data", str(src)])
        assert result.exit_code == 0, result.stderr
        root = registry.instance_path("alpha")
        assert (paths.instance_data(root) / "marker.txt").read_text() == "hello"

    def test_create_with_from_data_overwrites_existing(self, cli_root: Path) -> None:
        src = cli_root / "old-data"
        src.mkdir()
        (src / "marker.txt").write_text("new")
        target = cli_root / "alpha"
        target.mkdir()
        dst = target / "data"
        dst.mkdir()
        (dst / "stale.txt").write_text("stale")
        result = runner.invoke(
            cli.app,
            ["create", "alpha", "--from-data", str(src), "--path", str(target)],
        )
        assert result.exit_code == 0, result.stderr
        assert (target / "data" / "marker.txt").read_text() == "new"
        assert not (target / "data" / "stale.txt").exists()

    def test_create_with_invalid_from_data_exits(self, cli_root: Path) -> None:
        result = runner.invoke(
            cli.app, ["create", "alpha", "--from-data", str(cli_root / "missing")]
        )
        assert result.exit_code == 1
        assert "not a directory" in result.stderr

    def test_create_writes_exact_minimal_yaml_bytes(self, cli_root: Path) -> None:
        """Behavior test — drive create end-to-end and pin the YAML bytes.

        The whole point of T3 is that a fresh ``beetroot create`` produces
        a minimal, hand-readable beetroot.yaml — not the schema's full
        defaulted dump. Any change to that artifact (extra fields, key
        reordering, comment leakage) breaks this assertion deliberately.
        """
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        root = registry.instance_path("alpha")
        assert paths.instance_yaml(root).read_bytes() == (
            b"api_version: 2\nandroid:\n  version: 14\n"
        )

    def test_create_default_no_frida(self, cli_root: Path) -> None:
        # v0.3 (T2): the default minimal YAML omits the `frida:` block, so
        # the staged frida-server is a 0-byte non-executable placeholder.
        # entrypoint.sh's `[ -x ]` check skips the launch in that case.
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        root = registry.instance_path("alpha")
        staged = paths.instance_frida(root)
        assert staged.exists()
        assert staged.stat().st_size == 0
        assert staged.stat().st_mode & 0o111 == 0

    def test_apply_with_frida_yaml_stages_executable(self, cli_root: Path) -> None:
        # End-to-end opt-in path that survived T3 removing --preset:
        # create with the minimal YAML, then overwrite with the with-frida
        # example body (mirroring `cp examples/with-frida.yaml ./beetroot.yaml`),
        # then apply. The staged frida-server should now be executable.
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        root = registry.instance_path("alpha")
        paths.instance_yaml(root).write_text(
            "api_version: 2\n"
            "android:\n"
            "  version: 14\n"
            'frida:\n  version: "16.4.10"\n'
        )
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        staged = paths.instance_frida(root)
        assert staged.exists()
        assert staged.stat().st_size > 0
        assert staged.stat().st_mode & 0o111 != 0


# ---------------------------------------------------------------------------
# cmd_register
# ---------------------------------------------------------------------------


class TestCmdRegister:
    def test_register_adopts_existing_dir(self, cli_root: Path) -> None:
        target = cli_root / "external-instance"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        result = runner.invoke(cli.app, ["register", str(target)])
        assert result.exit_code == 0, result.stderr
        assert registry.instance_path("external-instance") == target.resolve()

    def test_register_with_explicit_name(self, cli_root: Path) -> None:
        target = cli_root / "external"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        result = runner.invoke(cli.app, ["register", str(target), "--name", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert registry.instance_path("alpha") == target.resolve()

    def test_register_missing_yaml_exits(self, cli_root: Path) -> None:
        target = cli_root / "empty-dir"
        target.mkdir()
        result = runner.invoke(cli.app, ["register", str(target)])
        assert result.exit_code == 1
        assert "no beetroot.yaml" in result.stderr

    def test_register_duplicate_name_exits(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        result = runner.invoke(cli.app, ["register", str(target)])
        assert result.exit_code == 0, result.stderr
        # Re-registering under the same name should fail.
        result = runner.invoke(cli.app, ["register", str(target)])
        assert result.exit_code == 1
        assert "already registered" in result.stderr


# ---------------------------------------------------------------------------
# cmd_apply
# ---------------------------------------------------------------------------


class TestCmdApply:
    def test_apply_re_renders_env(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        root = registry.instance_path("alpha")
        paths.instance_env(root).unlink()
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert paths.instance_env(root).exists()

    def test_apply_missing_instance_exits(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["apply", "ghost"])
        assert result.exit_code == 1
        assert "no instance named" in result.stderr

    def test_apply_frida_none_path(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        root = registry.instance_path("alpha")
        (root / "beetroot.yaml").write_text(
            "api_version: 2\nandroid:\n  version: 14\nfrida: null\n"
        )
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert paths.instance_frida(root).exists()
        assert paths.instance_frida(root).stat().st_size == 0


# ---------------------------------------------------------------------------
# cmd_up / cmd_down / cmd_restart
# ---------------------------------------------------------------------------


class TestCmdUp:
    def test_up_invokes_compose(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["up", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert mock_run.called

    def test_up_does_not_pass_build_flag(self, cli_root: Path) -> None:
        """`beetroot up` never adds `--build` to the compose argv (T5)."""
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["up", "alpha"])
        assert result.exit_code == 0, result.stderr
        cmd = mock_run.call_args[0][0]
        assert "--build" not in cmd

    def test_up_rejects_build_flag(self, cli_root: Path) -> None:
        """The `--build` option is removed; Typer must reject it."""
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["up", "alpha", "--build"])
        assert result.exit_code != 0

    def test_up_all(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["up", "--all"])
        assert result.exit_code == 0, result.stderr
        assert mock_run.call_count == 2


class TestCmdDown:
    def test_down_invokes_compose(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["down", "alpha"])
        assert result.exit_code == 0, result.stderr
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd


class TestCmdRestart:
    def test_restart_invokes_down_then_up(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["restart", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("down" in c for c in cmds)
        assert any("up" in c for c in cmds)


# ---------------------------------------------------------------------------
# cmd_destroy
# ---------------------------------------------------------------------------


class TestCmdDestroy:
    def test_destroy_with_yes_skips_prompt(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        root = registry.instance_path("alpha")
        with _patched_subprocess():
            result = runner.invoke(cli.app, ["destroy", "alpha", "-y"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is None
        assert not root.exists()

    def test_destroy_prompt_yes(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess(), patch("builtins.input", return_value="y"):
            result = runner.invoke(cli.app, ["destroy", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is None

    def test_destroy_prompt_no_aborts(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess(), patch("builtins.input", return_value="n"):
            result = runner.invoke(cli.app, ["destroy", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is not None
        assert "aborted" in result.stdout

    def test_destroy_compose_error_is_caught(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        from beetroot import compose

        def _boom(name: str, root: Path, *, volumes: bool = False) -> None:
            raise compose.ComposeError("simulated failure")

        with patch.object(compose, "down", side_effect=_boom):
            result = runner.invoke(cli.app, ["destroy", "alpha", "-y"])
        assert result.exit_code == 0, result.stderr
        assert "continuing" in result.stdout
        assert registry.get("alpha") is None

    def test_destroy_missing_instance_exits(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["destroy", "ghost", "-y"])
        assert result.exit_code == 1
        assert "no instance named" in result.stderr

    def test_destroy_without_instance_dir(self, cli_root: Path) -> None:
        # Registry-only entry (path absent). Exercises the "if not exists" branch.
        registry.add("alpha", cli_root / "alpha-missing", 0)
        with _patched_subprocess():
            result = runner.invoke(cli.app, ["destroy", "alpha", "-y"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is None


# ---------------------------------------------------------------------------
# cmd_ls
# ---------------------------------------------------------------------------


class TestCmdLs:
    def test_ls_empty_human(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["ls"])
        assert result.exit_code == 0, result.stderr
        assert "no instances" in result.stdout

    def test_ls_human_with_entries(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess():
            result = runner.invoke(cli.app, ["ls"])
        assert result.exit_code == 0, result.stderr
        assert "alpha" in result.stdout
        assert "5555" in result.stdout

    def test_ls_json(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess():
            result = runner.invoke(cli.app, ["ls", "--json"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert "alpha" in data
        assert data["alpha"]["adb"] == "localhost:5555"
        assert data["alpha"]["path"] == str(registry.instance_path("alpha"))


# ---------------------------------------------------------------------------
# cmd_logs
# ---------------------------------------------------------------------------


class TestCmdLogs:
    def test_logs_invokes_compose(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["logs", "alpha"])
        assert result.exit_code == 0, result.stderr
        cmd = mock_run.call_args[0][0]
        assert "logs" in cmd

    def test_logs_with_follow(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with _patched_subprocess() as mock_run:
            result = runner.invoke(cli.app, ["logs", "alpha", "-f"])
        assert result.exit_code == 0, result.stderr
        cmd = mock_run.call_args[0][0]
        logs_idx = cmd.index("logs")
        assert "-f" in cmd[logs_idx:]


# ---------------------------------------------------------------------------
# cmd_shell
# ---------------------------------------------------------------------------


class TestCmdShell:
    def test_shell_invokes_adb(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            result = runner.invoke(cli.app, ["shell", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert cmds[0][0] == "adb"
        assert cmds[1][0] == "adb"
        assert "shell" in cmds[1]

    def test_shell_no_adb_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        import shutil

        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        result = runner.invoke(cli.app, ["shell", "alpha"])
        assert result.exit_code == 1
        assert "adb not found" in result.stderr


# ---------------------------------------------------------------------------
# cmd_env
# ---------------------------------------------------------------------------


class TestCmdEnv:
    def test_env_prints_exports(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["env", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert "ANDROID_DEVICE=localhost:5555" in result.stdout
        assert "FRIDA_DEVICE=localhost:27042" in result.stdout


# ---------------------------------------------------------------------------
# cmd_frida
# ---------------------------------------------------------------------------


class TestCmdFrida:
    def test_frida_invokes_frida_cli(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            result = runner.invoke(cli.app, ["frida", "alpha", "-n", "com.app"])
        assert result.exit_code == 0, result.stderr
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "frida"
        assert "-H" in cmd
        assert "localhost:27042" in cmd
        assert "com.app" in cmd

    def test_frida_no_frida_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        import shutil

        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        result = runner.invoke(cli.app, ["frida", "alpha"])
        assert result.exit_code == 1
        assert "frida CLI not found" in result.stderr
        assert "beetroot[frida]" in result.stderr

    def test_frida_forwards_remainder_args_verbatim(self, cli_root: Path) -> None:
        """T4 behavior test — `beetroot frida alpha -- -l script.js` round-trips verbatim.

        Mirrors the legacy argparse ``REMAINDER`` semantics: Beetroot consumes
        only the instance name and a possible ``--`` separator, then forwards
        every remaining argv token to the underlying ``frida`` CLI after
        prepending ``-H localhost:<frida_port>``. The forwarded tokens must
        preserve order and not be touched by Typer's option-parser.
        """
        runner.invoke(cli.app, ["create", "alpha"])
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            result = runner.invoke(
                cli.app, ["frida", "alpha", "--", "-l", "script.js"]
            )
        assert result.exit_code == 0, result.stderr
        cmd = mock_run.call_args[0][0]
        # Beetroot prepends frida -H localhost:<port>, then the user args.
        assert cmd[0] == "frida"
        assert cmd[1] == "-H"
        assert cmd[2] == "localhost:27042"
        # The remainder is forwarded in order, verbatim.
        assert cmd[3:] == ["-l", "script.js"]


# ---------------------------------------------------------------------------
# cmd_module
# ---------------------------------------------------------------------------


class TestCmdModule:
    def test_module_url_branch(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])

        def _resp(url: str, **kwargs: object) -> MagicMock:
            r = MagicMock()
            r.read.return_value = b"PK\x03\x04zip"
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=_resp):
            result = runner.invoke(
                cli.app, ["module", "alpha", "https://example.com/mod.zip"]
            )
        assert result.exit_code == 0, result.stderr
        cfg = config.load_yaml(paths.instance_yaml(registry.instance_path("alpha")))
        assert len(cfg.modules) == 1
        assert cfg.modules[0].url == "https://example.com/mod.zip"
        assert cfg.modules[0].path is None

    def test_module_path_branch(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        root = registry.instance_path("alpha")
        local = root / "local-mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        result = runner.invoke(cli.app, ["module", "alpha", "local-mod.zip"])
        assert result.exit_code == 0, result.stderr
        cfg = config.load_yaml(paths.instance_yaml(root))
        assert len(cfg.modules) == 1
        assert cfg.modules[0].path == "local-mod.zip"

    def test_module_with_sha256(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        root = registry.instance_path("alpha")
        local = root / "local-mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        import hashlib

        sha = hashlib.sha256(local.read_bytes()).hexdigest()
        result = runner.invoke(
            cli.app, ["module", "alpha", "local-mod.zip", "--sha256", sha]
        )
        assert result.exit_code == 0, result.stderr
        cfg = config.load_yaml(paths.instance_yaml(root))
        assert cfg.modules[0].sha256 == sha


# ---------------------------------------------------------------------------
# top-level app and main()
# ---------------------------------------------------------------------------


class TestTopLevelApp:
    def test_app_help_lists_every_verb(self) -> None:
        result = runner.invoke(cli.app, ["--help"])
        assert result.exit_code == 0
        for verb in (
            "create",
            "register",
            "apply",
            "up",
            "down",
            "restart",
            "destroy",
            "ls",
            "logs",
            "shell",
            "env",
            "frida",
            "module",
            "build",
        ):
            assert verb in result.stdout

    def test_create_help_lists_flags(self) -> None:
        result = runner.invoke(cli.app, ["create", "--help"])
        assert result.exit_code == 0
        assert "--path" in result.stdout
        assert "--from-data" in result.stdout

    def test_register_help_shows_path_positional(self) -> None:
        result = runner.invoke(cli.app, ["register", "--help"])
        assert result.exit_code == 0
        assert "path" in result.stdout.lower()

    def test_up_all_flag_help(self) -> None:
        result = runner.invoke(cli.app, ["up", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.stdout
        assert "--build" not in result.stdout

    def test_frida_help_describes_passthrough(self) -> None:
        result = runner.invoke(cli.app, ["frida", "--help"])
        assert result.exit_code == 0
        assert "frida" in result.stdout.lower()


class TestMain:
    def test_main_dispatches_create(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "create", "alpha"])
        # Typer's standalone-mode app() exits via SystemExit even on success.
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert registry.get("alpha") is not None

    def test_main_no_subcommand_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot"])
        with pytest.raises(SystemExit):
            cli.main()

    def test_main_wraps_instance_root_not_found(
        self,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _raise() -> None:
            raise paths.InstanceRootNotFoundError("no beetroot.yaml in /nowhere")

        monkeypatch.setattr(cli, "app", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error: no beetroot.yaml in /nowhere" in err

    def test_main_wraps_port_collision_error(
        self,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _raise() -> None:
            raise ports.PortCollisionError("frida and frida2 both resolved to 27043")

        monkeypatch.setattr(cli, "app", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error: frida and frida2 both resolved to 27043" in err


class TestCmdSnapshot:
    def test_snapshot_default_output_in_cwd(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr

        result = runner.invoke(cli.app, ["snapshot", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert (cli_root / "alpha.tar.zst").is_file()

    def test_snapshot_explicit_output(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        out = cli_root / "snapshots" / "alpha-clean"
        result = runner.invoke(cli.app, ["snapshot", "alpha", "-o", str(out)])
        assert result.exit_code == 0, result.stderr
        assert (cli_root / "snapshots" / "alpha-clean.tar.zst").is_file()

    def test_snapshot_unknown_instance_exits(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["snapshot", "ghost"])
        assert result.exit_code == 1
        assert "no instance named" in result.stderr

    def test_snapshot_surfaces_module_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])

        def _boom(root: Path, dest: Path) -> Path:
            raise snapshot.SnapshotError("disk on fire")

        monkeypatch.setattr(snapshot, "snapshot", _boom)
        result = runner.invoke(cli.app, ["snapshot", "alpha"])
        assert result.exit_code == 1
        assert "disk on fire" in result.stderr


class TestCmdRestore:
    def test_restore_round_trips_through_cli(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        src = registry.instance_path("alpha")
        (paths.instance_data(src) / "marker.txt").write_bytes(b"survives")

        snap = runner.invoke(cli.app, ["snapshot", "alpha"])
        assert snap.exit_code == 0, snap.stderr

        runner.invoke(cli.app, ["destroy", "-y", "alpha"])
        assert registry.get("alpha") is None

        result = runner.invoke(
            cli.app,
            ["restore", str(cli_root / "alpha.tar.zst"), "--as", "beta",
             "--path", str(cli_root / "beta-dir")],
        )
        assert result.exit_code == 0, result.stderr
        beta = registry.get("beta")
        assert beta is not None
        assert Path(beta["absolute_path"]) == (cli_root / "beta-dir").resolve()
        assert (
            Path(beta["absolute_path"]) / "data" / "marker.txt"
        ).read_bytes() == b"survives"

    def test_restore_default_name_from_manifest(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        snap = runner.invoke(cli.app, ["snapshot", "alpha"])
        assert snap.exit_code == 0, snap.stderr
        runner.invoke(cli.app, ["destroy", "-y", "alpha"])

        result = runner.invoke(
            cli.app, ["restore", str(cli_root / "alpha.tar.zst")]
        )
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is not None

    def test_restore_invalid_archive_exits(self, cli_root: Path) -> None:
        bogus = cli_root / "garbage.tar.zst"
        bogus.write_bytes(b"this is not a zstd stream")
        result = runner.invoke(
            cli.app, ["restore", str(bogus), "--as", "beta"]
        )
        assert result.exit_code == 1
        assert "error:" in result.stderr

    def test_restore_surfaces_restore_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        snap = runner.invoke(cli.app, ["snapshot", "alpha"])
        assert snap.exit_code == 0, snap.stderr

        def _boom(archive: Path, *, dest_name: str, dest_path: Path,
                  force: bool = False) -> Path:
            raise snapshot.SnapshotError("registry locked")

        monkeypatch.setattr(snapshot, "restore", _boom)
        result = runner.invoke(
            cli.app,
            ["restore", str(cli_root / "alpha.tar.zst"), "--as", "beta"],
        )
        assert result.exit_code == 1
        assert "registry locked" in result.stderr

    def test_restore_force_flag_overwrites(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        snap = runner.invoke(cli.app, ["snapshot", "alpha"])
        assert snap.exit_code == 0, snap.stderr
        runner.invoke(cli.app, ["destroy", "-y", "alpha"])

        target = cli_root / "fresh"
        target.mkdir()
        (target / "stale.txt").write_bytes(b"go away")

        result = runner.invoke(
            cli.app,
            ["restore", str(cli_root / "alpha.tar.zst"), "--as", "alpha",
             "--path", str(target), "--force"],
        )
        assert result.exit_code == 0, result.stderr
        assert not (target / "stale.txt").exists()
