"""Tests for the OOP api.py — Instance, Manager, DeviceBackend."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from beetroot import api, compose, config, paths, registry, snapshot


def _ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _patched_subprocess() -> Any:
    return patch("subprocess.run", return_value=_ok_proc())


# ---------------------------------------------------------------------------
# DeviceBackend Protocol
# ---------------------------------------------------------------------------


class TestDeviceBackend:
    def test_instance_satisfies_protocol(self, cli_root: Path) -> None:
        # An Instance must structurally satisfy the DeviceBackend Protocol —
        # adb_address, frida_address, is_available, install_frida.
        inst = api.Instance.create("alpha")
        # Protocol is @runtime_checkable, so isinstance works.
        assert isinstance(inst, api.DeviceBackend)

    def test_protocol_attrs_present_on_instance(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        assert isinstance(inst.adb_address, str)
        assert isinstance(inst.frida_address, str)
        assert isinstance(inst.is_available, bool)

    def test_partial_object_is_not_devicebackend(self) -> None:
        # A class that satisfies most of the Protocol but is missing
        # one method must NOT pass isinstance(obj, DeviceBackend).
        # @runtime_checkable Protocols on Python 3.12+ check method
        # presence too, not just attribute presence.
        class _Stub:
            adb_address = "host:1"
            frida_address = "host:2"
            is_available = True
            # install_frida missing on purpose.

        stub = _Stub()
        assert isinstance(stub, api.DeviceBackend) is False


# ---------------------------------------------------------------------------
# Instance.create
# ---------------------------------------------------------------------------


class TestInstanceCreate:
    def test_create_default_path_is_cwd_subdir(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        assert inst.name == "alpha"
        assert inst.root == (cli_root / "alpha").resolve()
        assert registry.get("alpha") is not None

    def test_create_explicit_path(self, cli_root: Path) -> None:
        target = cli_root / "deep" / "nested" / "alpha-dir"
        inst = api.Instance.create("alpha", path=target)
        assert inst.root == target.resolve()
        assert (target / "beetroot.yaml").is_file()

    def test_create_writes_minimal_yaml_when_cfg_omitted(self, cli_root: Path) -> None:
        # T8 behavior: api.Instance.create with no explicit cfg writes
        # the same minimal byte string the CLI's `beetroot create` does.
        # This is the byte-pinned guarantee from T3 that the CLI test
        # already verifies; the OOP path must match.
        inst = api.Instance.create("alpha")
        assert paths.instance_yaml(inst.root).read_bytes() == (
            b"api_version: 3\nandroid:\n  version: 14\n"
        )

    def test_create_with_explicit_cfg_serialises_full_model(
        self, cli_root: Path
    ) -> None:
        cfg = config.InstanceConfig(frida=config.Frida(version="16.4.10"))
        inst = api.Instance.create("alpha", cfg=cfg)
        text = paths.instance_yaml(inst.root).read_text()
        # Explicit cfg goes through config.write_yaml — emits the full
        # schema dump, not the minimal hand-readable form.
        assert "16.4.10" in text
        assert "resources:" in text

    def test_create_stages_env_and_frida_placeholder(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        assert paths.instance_env(inst.root).is_file()
        # Default cfg has frida=None → empty placeholder.
        staged = paths.instance_frida(inst.root)
        assert staged.exists()
        assert staged.stat().st_size == 0

    def test_create_stages_executable_frida_when_pinned(
        self, cli_root: Path
    ) -> None:
        cfg = config.InstanceConfig(frida=config.Frida(version="16.4.10"))
        inst = api.Instance.create("alpha", cfg=cfg)
        staged = paths.instance_frida(inst.root)
        assert staged.stat().st_size > 0
        assert staged.stat().st_mode & 0o111

    def test_create_duplicate_name_raises(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        with pytest.raises(ValueError, match="already exists"):
            api.Instance.create("alpha")

    def test_create_existing_yaml_raises_fileexists(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        target.mkdir()
        (target / "beetroot.yaml").write_text("api_version: 3\n")
        with pytest.raises(FileExistsError, match="already exists"):
            api.Instance.create("alpha", path=target)

    def test_create_collision_raises_value_error(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        # Pre-pin alpha's beetroot.yaml so bravo's stride-default ADB (5565)
        # collides with alpha's pinned ADB.
        cfg = config.InstanceConfig(ports=config.Ports(adb=5565))
        config.write_yaml(paths.instance_yaml(registry.instance_path("alpha")), cfg)
        with pytest.raises(ValueError, match="5565"):
            api.Instance.create("bravo")

    @pytest.mark.parametrize(
        "bad",
        [
            "Alpha",              # uppercase
            "alpha bravo",        # space
            "alpha.bravo",        # dot
            "alpha/bravo",        # slash
            "alpha:bravo",        # colon
            "",                   # empty
            "alpha!",             # punctuation
        ],
    )
    def test_create_invalid_name_raises_before_side_effects(
        self, bad: str, cli_root: Path,
    ) -> None:
        # T2 (v0.3.1 deferred): instance names must match the Docker
        # compose project-name grammar (``[a-z0-9_-]+``). A bad name
        # MUST raise BEFORE any side effect runs — no mkdir, no
        # registry write, no port allocation.
        with pytest.raises(ValueError, match="instance name"):
            api.Instance.create(bad)
        assert registry.get(bad) is None
        # No stray ``<bad>`` dir was created either.
        if bad:
            assert not (cli_root / bad).exists()


# ---------------------------------------------------------------------------
# Instance.register
# ---------------------------------------------------------------------------


class TestInstanceRegister:
    def test_register_adopts_existing_dir(self, cli_root: Path) -> None:
        target = cli_root / "external"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        inst = api.Instance.register(target)
        assert inst.name == "external"
        assert registry.instance_path("external") == target.resolve()

    def test_register_with_explicit_name(self, cli_root: Path) -> None:
        target = cli_root / "external"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        inst = api.Instance.register(target, name="custom")
        assert inst.name == "custom"

    def test_register_missing_yaml_raises(self, cli_root: Path) -> None:
        empty = cli_root / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError, match=r"no beetroot\.yaml"):
            api.Instance.register(empty)

    def test_register_duplicate_raises(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        api.Instance.register(target)
        with pytest.raises(ValueError, match="already in registry"):
            api.Instance.register(target)

    def test_register_invalid_explicit_name_raises(
        self, cli_root: Path,
    ) -> None:
        # T2 (v0.3.1 deferred): explicit ``name`` to register must
        # match the same grammar create checks.
        target = cli_root / "alpha"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        with pytest.raises(ValueError, match="instance name"):
            api.Instance.register(target, name="Bad Name")
        assert registry.get("Bad Name") is None

    def test_register_invalid_basename_default_raises(
        self, cli_root: Path,
    ) -> None:
        # When ``name=`` is omitted, ``register`` falls back to the
        # directory's basename. If that basename violates the
        # grammar, the same validation must fire.
        target = cli_root / "Bad-Name"  # uppercase
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        with pytest.raises(ValueError, match="instance name"):
            api.Instance.register(target)


# ---------------------------------------------------------------------------
# Instance.load
# ---------------------------------------------------------------------------


class TestInstanceLoad:
    def test_load_returns_equivalent_object_after_create(
        self, cli_root: Path
    ) -> None:
        # Behavior: Instance.create("alpha", path=<tmp>) → Instance.load("alpha")
        # returns an equivalent object (same name, root, config).
        target = cli_root / "alpha-here"
        created = api.Instance.create("alpha", path=target)
        loaded = api.Instance.load("alpha")
        assert loaded.name == created.name
        assert loaded.root == created.root
        assert loaded.index == created.index
        assert loaded.config.android.version == created.config.android.version

    def test_load_missing_raises_not_found(self, cli_root: Path) -> None:
        with pytest.raises(api.InstanceNotFoundError, match="no instance named"):
            api.Instance.load("ghost")


# ---------------------------------------------------------------------------
# Instance.from_path — required by T8: walk-up test
# ---------------------------------------------------------------------------


class TestInstanceFromPath:
    def test_from_path_walks_up_from_subdir(self, cli_root: Path) -> None:
        # T8 behavior: from_path on a subdir walks up to find beetroot.yaml,
        # then matches the registry by resolved path.
        target = cli_root / "alpha-walkup"
        created = api.Instance.create("alpha", path=target)
        deep = target / "data" / "nested" / "deep"
        deep.mkdir(parents=True, exist_ok=True)
        loaded = api.Instance.from_path(deep)
        assert loaded.name == "alpha"
        assert loaded.root == created.root

    def test_from_path_at_root_works(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        api.Instance.create("alpha", path=target)
        loaded = api.Instance.from_path(target)
        assert loaded.name == "alpha"

    def test_from_path_unregistered_raises(self, cli_root: Path) -> None:
        # Has beetroot.yaml on disk but not in registry.
        target = cli_root / "orphan"
        target.mkdir()
        (target / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
        with pytest.raises(api.InstanceNotFoundError, match="not registered"):
            api.Instance.from_path(target)

    def test_from_path_unregistered_raises_with_other_entries(
        self, cli_root: Path
    ) -> None:
        # Registry has entries (so the loop iterates), but none match the
        # path being looked up — exercises the loop-without-match branch.
        api.Instance.create("alpha", path=cli_root / "alpha")
        orphan = cli_root / "orphan"
        orphan.mkdir()
        (orphan / "beetroot.yaml").write_text(
            "api_version: 3\nandroid:\n  version: 14\n"
        )
        with pytest.raises(api.InstanceNotFoundError, match="not registered"):
            api.Instance.from_path(orphan)


# ---------------------------------------------------------------------------
# Instance lifecycle: up / down / restart / apply
# ---------------------------------------------------------------------------


class TestInstanceLifecycle:
    def test_up_invokes_compose_up(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess() as mock_run:
            inst.up()
        # docker compose ... up -d
        cmd = mock_run.call_args[0][0]
        assert "up" in cmd
        assert "-d" in cmd

    def test_down_invokes_compose_down(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess() as mock_run:
            inst.down()
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd

    def test_restart_invokes_down_then_up(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess() as mock_run:
            inst.restart()
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("down" in c for c in cmds)
        assert any("up" in c for c in cmds)

    def test_apply_rerenders_env(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        paths.instance_env(inst.root).unlink()
        inst.apply()
        assert paths.instance_env(inst.root).is_file()

    def test_apply_picks_up_external_yaml_edits(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        # The minimal create writes no frida block; the in-memory cfg agrees.
        assert inst.config.frida is None
        # Externally edit the yaml — apply re-reads it.
        paths.instance_yaml(inst.root).write_text(
            "api_version: 3\n"
            "android:\n  version: 14\n"
            'frida:\n  version: "16.4.10"\n'
        )
        inst.apply()
        # Re-read the on-disk config (the apply() refresh re-binds inst.config
        # too, but mypy can't see across the property load — use the explicit
        # round-trip to assert).
        reloaded = config.load_yaml(paths.instance_yaml(inst.root))
        assert reloaded.frida is not None
        assert reloaded.frida.version == "16.4.10"

    def test_apply_collision_raises(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        bravo = api.Instance.create("bravo")
        # Externally rewrite bravo to collide with alpha's ADB.
        paths.instance_yaml(bravo.root).write_text(
            "api_version: 3\nandroid:\n  version: 14\n"
            "ports:\n  adb: 5555\n"
        )
        with pytest.raises(ValueError, match="5555"):
            bravo.apply()


# ---------------------------------------------------------------------------
# Instance.destroy
# ---------------------------------------------------------------------------


class TestInstanceDestroy:
    def test_destroy_with_yes_clears_registry_and_dir(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        root = inst.root
        with _patched_subprocess():
            inst.destroy(yes=True)
        assert registry.get("alpha") is None
        assert not root.exists()

    def test_destroy_prompt_yes(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess(), patch("builtins.input", return_value="y"):
            inst.destroy()
        assert registry.get("alpha") is None

    def test_destroy_prompt_no_raises(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess(), patch("builtins.input", return_value="n"):
            with pytest.raises(RuntimeError, match="aborted"):
                inst.destroy()
        # Registry entry survives.
        assert registry.get("alpha") is not None

    def test_destroy_when_root_already_gone(self, cli_root: Path) -> None:
        # Registry has the entry but the on-disk dir is missing — destroy
        # must still clear the registry. Exercises the `if root.exists()`
        # false branch.
        registry.add("alpha", cli_root / "alpha-missing", 0)
        # Need to manually build an Instance — Instance.load would fail.
        cfg = config.InstanceConfig()
        inst = api.Instance(name="alpha", root=cli_root / "alpha-missing", cfg=cfg)
        with _patched_subprocess():
            inst.destroy(yes=True)
        assert registry.get("alpha") is None

    def test_destroy_propagates_compose_error_after_cleanup(
        self, cli_root: Path
    ) -> None:
        inst = api.Instance.create("alpha")
        root = inst.root

        def _boom(name: str, root: Path, *, volumes: bool = False) -> None:
            raise compose.ComposeError("simulated")

        with patch.object(compose, "down", side_effect=_boom):
            with pytest.raises(compose.ComposeError, match="simulated"):
                inst.destroy(yes=True)
        # Cleanup still ran despite the error.
        assert registry.get("alpha") is None
        assert not root.exists()

    def test_destroy_ctrlc_after_registry_remove_leaves_no_orphan(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # T2 Agent 2 B-4: ``Instance.destroy`` used to ``rmtree`` BEFORE
        # ``registry.remove``. A ``^C`` between the two steps stranded
        # a registry row pointing at a now-gone directory — an orphan
        # the user could only fix by re-creating the dir then running
        # destroy again (``Instance.load`` trips on the missing yaml).
        # v0.4 reorders to ``compose.down`` → ``registry.remove`` →
        # ``rmtree`` so a ``^C`` between the last two steps leaves a
        # tidy registry and the user just rm -rf's the stale dir.
        inst = api.Instance.create("alpha")
        root = inst.root

        def _ctrl_c(target: Path) -> None:
            raise KeyboardInterrupt

        # Patch the ``shutil`` module that ``api.py`` calls — every
        # module that ``import shutil``s sees the same module object,
        # so monkeypatching ``shutil.rmtree`` propagates to api.
        import shutil as _shutil
        monkeypatch.setattr(_shutil, "rmtree", _ctrl_c)
        with _patched_subprocess():
            with pytest.raises(KeyboardInterrupt):
                inst.destroy(yes=True)
        # Registry row IS gone — we got past ``registry.remove`` before
        # the ^C fired. The on-disk dir survives because rmtree raised
        # before doing any work; the user can wipe it manually.
        assert registry.get("alpha") is None
        assert root.exists()


# ---------------------------------------------------------------------------
# Instance operations: shell, frida_cli, logs, add_module, snapshot
# ---------------------------------------------------------------------------


class TestInstanceShell:
    def test_shell_invokes_adb(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            rc = inst.shell()
        assert rc == 0
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert cmds[0][0] == "adb"
        assert cmds[1][0] == "adb"
        assert "shell" in cmds[1]

    def test_shell_no_adb_raises(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inst = api.Instance.create("alpha")
        import shutil as _shutil

        monkeypatch.setattr(
            _shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        with pytest.raises(api.AdbNotInstalledError, match="adb not found"):
            inst.shell()


class TestInstanceInstallFrida:
    def test_install_frida_stages_executable(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        # Default create leaves a 0-byte placeholder; install_frida
        # replaces it with an executable copy.
        inst.install_frida("16.4.10")
        staged = paths.instance_frida(inst.root)
        assert staged.stat().st_size > 0
        assert staged.stat().st_mode & 0o111


class TestInstanceFridaCli:
    def test_frida_cli_invokes_with_address(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            rc = inst.frida_cli(["-n", "com.app"])
        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "frida"
        assert "-H" in cmd
        assert "localhost:27042" in cmd
        assert "com.app" in cmd

    def test_frida_cli_no_frida_raises(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inst = api.Instance.create("alpha")
        import shutil as _shutil

        monkeypatch.setattr(
            _shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        with pytest.raises(api.FridaNotInstalledError, match="frida CLI not found"):
            inst.frida_cli([])


class TestInstanceLogs:
    def test_logs_invokes_compose_logs(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess() as mock_run:
            inst.logs()
        cmd = mock_run.call_args[0][0]
        assert "logs" in cmd
        # -f should NOT appear after the `logs` token.
        logs_idx = cmd.index("logs")
        assert "-f" not in cmd[logs_idx:]

    def test_logs_follow(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        with _patched_subprocess() as mock_run:
            inst.logs(follow=True)
        cmd = mock_run.call_args[0][0]
        logs_idx = cmd.index("logs")
        assert "-f" in cmd[logs_idx:]


class TestInstanceAddModule:
    def test_add_module_url(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")

        def _resp(url: str, **kwargs: object) -> MagicMock:
            r = MagicMock()
            r.read.return_value = b"PK\x03\x04zip"
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=_resp):
            inst.add_module("https://example.com/mod.zip")
        cfg = config.load_yaml(paths.instance_yaml(inst.root))
        assert len(cfg.modules) == 1
        assert cfg.modules[0].url == "https://example.com/mod.zip"

    def test_add_module_path(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        local = inst.root / "mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        inst.add_module("mod.zip")
        cfg = config.load_yaml(paths.instance_yaml(inst.root))
        assert len(cfg.modules) == 1
        assert cfg.modules[0].path == "mod.zip"
        assert cfg.modules[0].url is None

    def test_add_module_with_sha256(self, cli_root: Path) -> None:
        import hashlib

        inst = api.Instance.create("alpha")
        local = inst.root / "mod.zip"
        local.write_bytes(b"PK\x03\x04hashable")
        sha = hashlib.sha256(local.read_bytes()).hexdigest()
        inst.add_module("mod.zip", sha256=sha)
        cfg = config.load_yaml(paths.instance_yaml(inst.root))
        assert cfg.modules[0].sha256 == sha

    def test_add_module_url_failure_leaves_yaml_unchanged(
        self, cli_root: Path
    ) -> None:
        # T2 Agent 2 B-6 / Agent 3 1.6: a failed stage MUST NOT leave
        # the YAML mutated. Pre-T2, ``add_module`` appended the model +
        # wrote YAML THEN tried to stage — a 404 left the user's
        # beetroot.yaml polluted with a module they couldn't reach.
        inst = api.Instance.create("alpha")
        yaml_before = paths.instance_yaml(inst.root).read_text()
        modules_before = list(inst.config.modules)

        import urllib.error

        def _boom(url: str, **kwargs: object) -> object:
            raise urllib.error.HTTPError(
                url=url, code=404, msg="Not Found",
                hdrs=None, fp=None,  # type: ignore[arg-type]
            )

        with patch("urllib.request.urlopen", side_effect=_boom):
            with pytest.raises(Exception):  # noqa: B017, PT011
                inst.add_module("https://example.com/broken.zip")

        # YAML is byte-identical to before (no stale write).
        assert paths.instance_yaml(inst.root).read_text() == yaml_before
        # In-memory model is also untouched.
        assert list(inst.config.modules) == modules_before
        cfg = config.load_yaml(paths.instance_yaml(inst.root))
        assert cfg.modules == modules_before


class TestInstanceSnapshot:
    def test_snapshot_creates_archive(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        out = inst.snapshot(cli_root / "alpha")
        # Behavior assertion on observable side-effect: archive exists at
        # the expected path with .tar.zst appended.
        assert out == (cli_root / "alpha.tar.zst").resolve() or out == (
            cli_root / "alpha.tar.zst"
        )
        assert out.is_file()
        # The archive should have a manifest readable by the snapshot module.
        manifest = snapshot.read_manifest(out)
        assert manifest.name == "alpha"


# ---------------------------------------------------------------------------
# Instance properties: name, root, ports, status, addresses
# ---------------------------------------------------------------------------


class TestInstanceProperties:
    def test_properties_match_resolved_ports(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        assert inst.index == 0
        assert inst.ports == {"adb": 5555, "frida": 27042, "frida2": 27043}
        assert inst.adb_address == "localhost:5555"
        assert inst.frida_address == "localhost:27042"

    def test_status_queries_compose(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"State": "running"}\n',
            stderr="",
        )
        with patch("subprocess.run", return_value=fake):
            assert inst.status == "running"
            assert inst.is_available is True

    def test_status_not_created(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch("subprocess.run", return_value=fake):
            assert inst.status == "not-created"
            assert inst.is_available is False

    def test_meta_raises_when_registry_loses_entry(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha")
        registry.remove("alpha")
        with pytest.raises(api.InstanceNotFoundError, match="disappeared"):
            _ = inst.index


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class TestManager:
    def test_list_returns_all_sorted(self, cli_root: Path) -> None:
        api.Instance.create("bravo")
        api.Instance.create("alpha")
        names = [i.name for i in api.Manager.list()]
        assert names == ["alpha", "bravo"]

    def test_list_empty(self, cli_root: Path) -> None:
        assert api.Manager.list() == []

    def test_get_present(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        inst = api.Manager.get("alpha")
        assert inst is not None
        assert inst.name == "alpha"

    def test_get_missing_returns_none(self, cli_root: Path) -> None:
        assert api.Manager.get("ghost") is None

    def test_allocate_port_index_is_module_private(self, cli_root: Path) -> None:
        # T1 retired the public ``Manager.allocate_port_index`` per Agent
        # 2 F-4: the index is not reserved by this call, so calling it
        # without an immediate follow-up ``registry.add`` is a footgun.
        # The module-private helper remains for in-tree callers.
        assert not hasattr(api.Manager, "allocate_port_index")
        assert api._allocate_port_index() == 0
        api.Instance.create("alpha")
        assert api._allocate_port_index() == 1


# ---------------------------------------------------------------------------
# CLI ↔ OOP dispatch assertions — required by T8
# ---------------------------------------------------------------------------


class TestCliDispatchesToApi:
    def test_up_verb_calls_instance_up(self, cli_root: Path) -> None:
        # Required dispatch assertion: mock Instance.up and verify the
        # Typer command actually calls into the OOP layer.
        from typer.testing import CliRunner

        from beetroot import cli

        runner = CliRunner()
        runner.invoke(cli.app, ["create", "alpha"])
        with patch.object(api.Instance, "up") as mock_up:
            with _patched_subprocess():
                result = runner.invoke(cli.app, ["up", "alpha"])
        assert result.exit_code == 0, result.stderr
        mock_up.assert_called_once_with()

    def test_down_verb_calls_instance_down(self, cli_root: Path) -> None:
        from typer.testing import CliRunner

        from beetroot import cli

        runner = CliRunner()
        runner.invoke(cli.app, ["create", "alpha"])
        with patch.object(api.Instance, "down") as mock_down:
            with _patched_subprocess():
                result = runner.invoke(cli.app, ["down", "alpha"])
        assert result.exit_code == 0, result.stderr
        mock_down.assert_called_once_with()

    def test_apply_verb_calls_instance_apply(self, cli_root: Path) -> None:
        from typer.testing import CliRunner

        from beetroot import cli

        runner = CliRunner()
        runner.invoke(cli.app, ["create", "alpha"])
        with patch.object(api.Instance, "apply") as mock_apply:
            result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        mock_apply.assert_called_once_with()

    def test_ls_verb_calls_manager_list(self, cli_root: Path) -> None:
        from typer.testing import CliRunner

        from beetroot import cli

        runner = CliRunner()
        runner.invoke(cli.app, ["create", "alpha"])
        with patch.object(api.Manager, "list", wraps=api.Manager.list) as mock_list:
            with _patched_subprocess():
                result = runner.invoke(cli.app, ["ls"])
        assert result.exit_code == 0, result.stderr
        mock_list.assert_called_once_with()
