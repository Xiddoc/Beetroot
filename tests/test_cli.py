"""Tests for cli.py — argparse dispatch and every verb handler."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from beetroot import cli, paths, ports, registry


class TestSetupParser:
    def test_default_gapps_is_lite(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["setup"])
        assert args.gapps == "lite"

    @pytest.mark.parametrize("variant", ["none", "lite", "full", "mindthegapps"])
    def test_each_variant_parses(self, variant: str) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["setup", variant])
        assert args.gapps == variant

    def test_invalid_variant_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["setup", "blah"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        # argparse mentions the allowed choices on `choices=` mismatch
        assert "blah" in err
        for variant in ("none", "lite", "full", "mindthegapps"):
            assert variant in err

    def test_help_lists_variants(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["setup", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for variant in ("none", "lite", "full", "mindthegapps"):
            assert variant in out


class TestSetupDispatch:
    def test_cmd_setup_invokes_bootstrap_with_lite_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("beetroot.cli.setup_runner.bootstrap_base_image") as mock_bs:
            mock_bs.return_value = "redroid/redroid:14.0.0_litegapps_houdini_magisk"
            parser = cli.build_parser()
            args = parser.parse_args(["setup"])
            args.func(args)
        mock_bs.assert_called_once_with(gapps="lite")
        out = capsys.readouterr().out
        assert "redroid/redroid:14.0.0_litegapps_houdini_magisk" in out

    @pytest.mark.parametrize("variant", ["none", "lite", "full", "mindthegapps"])
    def test_cmd_setup_forwards_each_variant(self, variant: str) -> None:
        with patch("beetroot.cli.setup_runner.bootstrap_base_image") as mock_bs:
            mock_bs.return_value = f"redroid/redroid:14.0.0_{variant}_houdini_magisk"
            parser = cli.build_parser()
            args = parser.parse_args(["setup", variant])
            args.func(args)
        mock_bs.assert_called_once_with(gapps=variant)

    def test_main_dispatches_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "setup", "full"])
        with patch("beetroot.cli.setup_runner.bootstrap_base_image") as mock_bs:
            mock_bs.return_value = "redroid/redroid:14.0.0_gapps_houdini_magisk"
            cli.main()
        mock_bs.assert_called_once_with(gapps="full")


def _ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


@pytest.fixture
def cli_root(isolated_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up an isolated repo with a default preset and stubbed external commands.

    Every subprocess invocation is short-circuited, frida_dl.download is
    no-op'd, and urllib is never reached. Tests still get a real filesystem
    under tmp so paths.* keep working.
    """
    presets = isolated_root / "presets"
    presets.mkdir()
    (presets / "default.yaml").write_text("android:\n  version: 14\n")
    (presets / "stealth.yaml").write_text("android:\n  version: 14\n")

    # Force compose._ensure_docker() and shutil.which lookups to succeed
    # by default. Individual tests override shutil.which when they need to.
    import shutil

    def _which(name: str) -> str | None:
        if name in {"docker", "adb", "frida"}:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(shutil, "which", _which)

    # Pin Frida download to a deterministic no-op: write a placeholder file
    # at the cache path so stage_for_instance can copy it.
    from beetroot import frida_dl

    def _fake_download(version: str) -> Path:
        out = frida_dl.cached_binary(version)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_bytes(b"fake-frida")
            out.chmod(0o755)
        return out

    monkeypatch.setattr(frida_dl, "download", _fake_download)

    return isolated_root


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _patched_subprocess() -> Any:
    """Patch subprocess.run inside compose.* so no real docker is invoked."""
    return patch("subprocess.run", return_value=_ok_proc())


# ---------------------------------------------------------------------------
# _ensure_exists
# ---------------------------------------------------------------------------


class TestEnsureExists:
    def test_exits_when_instance_missing(self, cli_root: Path) -> None:
        with pytest.raises(SystemExit, match="no instance named"):
            cli._ensure_exists("ghost")

    def test_returns_none_when_instance_exists(self, cli_root: Path) -> None:
        registry.add("alpha", 0)
        # Should not raise.
        cli._ensure_exists("alpha")


# ---------------------------------------------------------------------------
# _resolve_names
# ---------------------------------------------------------------------------


class TestResolveNames:
    def test_explicit_names(self, cli_root: Path) -> None:
        args = _ns(all=False, names=["alpha", "bravo"])
        assert cli._resolve_names(args) == ["alpha", "bravo"]

    def test_all_returns_sorted_registry(self, cli_root: Path) -> None:
        registry.add("bravo", 1)
        registry.add("alpha", 0)
        args = _ns(all=True, names=[])
        assert cli._resolve_names(args) == ["alpha", "bravo"]

    def test_all_and_names_mutex(self, cli_root: Path) -> None:
        args = _ns(all=True, names=["alpha"])
        with pytest.raises(SystemExit, match="mutually exclusive"):
            cli._resolve_names(args)

    def test_no_names_no_all_errors(self, cli_root: Path) -> None:
        args = _ns(all=False, names=[])
        with pytest.raises(SystemExit, match="provide at least one"):
            cli._resolve_names(args)

    def test_all_with_empty_registry_exits_zero(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _ns(all=True, names=[])
        with pytest.raises(SystemExit) as exc_info:
            cli._resolve_names(args)
        assert exc_info.value.code == 0
        assert "no instances" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_create
# ---------------------------------------------------------------------------


class TestCmdCreate:
    def test_create_basic(self, cli_root: Path) -> None:
        args = _ns(name="alpha", preset="default", from_data=None)
        cli.cmd_create(args)
        assert registry.get("alpha") is not None
        assert paths.instance_yaml("alpha").exists()
        assert paths.instance_env("alpha").exists()

    def test_create_duplicate_exits(self, cli_root: Path) -> None:
        registry.add("alpha", 0)
        args = _ns(name="alpha", preset="default", from_data=None)
        with pytest.raises(SystemExit, match="already exists"):
            cli.cmd_create(args)

    def test_create_with_from_data_copies(self, cli_root: Path) -> None:
        src = cli_root / "old-data"
        src.mkdir()
        (src / "marker.txt").write_text("hello")
        args = _ns(name="alpha", preset="default", from_data="old-data")
        cli.cmd_create(args)
        assert (paths.instance_data("alpha") / "marker.txt").read_text() == "hello"

    def test_create_with_from_data_overwrites_existing(self, cli_root: Path) -> None:
        src = cli_root / "old-data"
        src.mkdir()
        (src / "marker.txt").write_text("new")
        # Pre-create the destination data dir to exercise the rmtree branch.
        dst = paths.instance_data("alpha")
        dst.mkdir(parents=True)
        (dst / "stale.txt").write_text("stale")
        args = _ns(name="alpha", preset="default", from_data="old-data")
        cli.cmd_create(args)
        assert (paths.instance_data("alpha") / "marker.txt").read_text() == "new"
        assert not (paths.instance_data("alpha") / "stale.txt").exists()

    def test_create_with_invalid_from_data_exits(self, cli_root: Path) -> None:
        args = _ns(name="alpha", preset="default", from_data="does-not-exist")
        with pytest.raises(SystemExit, match="not a directory"):
            cli.cmd_create(args)


# ---------------------------------------------------------------------------
# cmd_apply
# ---------------------------------------------------------------------------


class TestCmdApply:
    def test_apply_re_renders_env(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        # Delete the .env to prove apply re-renders.
        paths.instance_env("alpha").unlink()
        cli.cmd_apply(_ns(name="alpha"))
        assert paths.instance_env("alpha").exists()

    def test_apply_missing_instance_exits(self, cli_root: Path) -> None:
        with pytest.raises(SystemExit, match="no instance named"):
            cli.cmd_apply(_ns(name="ghost"))

    def test_apply_frida_none_path(self, cli_root: Path) -> None:
        # Stage with frida=null path through _stage_instance.
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        yaml_path = paths.instance_yaml("alpha")
        yaml_path.write_text("android:\n  version: 14\nfrida: null\n")
        cli.cmd_apply(_ns(name="alpha"))
        # An empty placeholder file means stage_empty was used.
        assert paths.instance_frida("alpha").exists()
        assert paths.instance_frida("alpha").stat().st_size == 0


# ---------------------------------------------------------------------------
# cmd_up / cmd_down / cmd_restart
# ---------------------------------------------------------------------------


class TestCmdUp:
    def test_up_invokes_compose(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_up(_ns(names=["alpha"], all=False, build=False))
        assert mock_run.called

    def test_up_with_build_flag(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_up(_ns(names=["alpha"], all=False, build=True))
        cmd = mock_run.call_args[0][0]
        assert "--build" in cmd

    def test_up_all(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        cli.cmd_create(_ns(name="bravo", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_up(_ns(names=[], all=True, build=False))
        # Once per instance.
        assert mock_run.call_count == 2


class TestCmdDown:
    def test_down_invokes_compose(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_down(_ns(names=["alpha"], all=False))
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd


class TestCmdRestart:
    def test_restart_invokes_down_then_up(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_restart(_ns(names=["alpha"], all=False))
        # down + up => two compose invocations.
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("down" in c for c in cmds)
        assert any("up" in c for c in cmds)


# ---------------------------------------------------------------------------
# cmd_destroy
# ---------------------------------------------------------------------------


class TestCmdDestroy:
    def test_destroy_with_yes_skips_prompt(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess():
            cli.cmd_destroy(_ns(name="alpha", yes=True))
        assert registry.get("alpha") is None
        assert not paths.instance_dir("alpha").exists()

    def test_destroy_prompt_yes(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess(), patch("builtins.input", return_value="y"):
            cli.cmd_destroy(_ns(name="alpha", yes=False))
        assert registry.get("alpha") is None

    def test_destroy_prompt_no_aborts(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess(), patch("builtins.input", return_value="n"):
            cli.cmd_destroy(_ns(name="alpha", yes=False))
        assert registry.get("alpha") is not None
        assert "aborted" in capsys.readouterr().out

    def test_destroy_compose_error_is_caught(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        from beetroot import compose

        def _boom(name: str, *, volumes: bool = False) -> None:
            raise compose.ComposeError("simulated failure")

        with patch.object(compose, "down", side_effect=_boom):
            cli.cmd_destroy(_ns(name="alpha", yes=True))
        out = capsys.readouterr().out
        assert "continuing" in out
        # The instance should still be removed despite the compose failure.
        assert registry.get("alpha") is None

    def test_destroy_missing_instance_exits(self, cli_root: Path) -> None:
        with pytest.raises(SystemExit, match="no instance named"):
            cli.cmd_destroy(_ns(name="ghost", yes=True))

    def test_destroy_without_instance_dir(self, cli_root: Path) -> None:
        # Registry-only entry (no instance dir). Exercises the "if exists"
        # false branch in cmd_destroy.
        registry.add("alpha", 0)
        with _patched_subprocess():
            cli.cmd_destroy(_ns(name="alpha", yes=True))
        assert registry.get("alpha") is None


# ---------------------------------------------------------------------------
# cmd_ls
# ---------------------------------------------------------------------------


class TestCmdLs:
    def test_ls_empty_human(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_ls(_ns(json=False))
        assert "no instances" in capsys.readouterr().out

    def test_ls_human_with_entries(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess():
            cli.cmd_ls(_ns(json=False))
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "5555" in out

    def test_ls_json(self, cli_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        capsys.readouterr()  # drop create() chatter
        with _patched_subprocess():
            cli.cmd_ls(_ns(json=True))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "alpha" in data
        assert data["alpha"]["adb"] == "localhost:5555"


# ---------------------------------------------------------------------------
# cmd_logs
# ---------------------------------------------------------------------------


class TestCmdLogs:
    def test_logs_invokes_compose(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_logs(_ns(name="alpha", follow=False))
        cmd = mock_run.call_args[0][0]
        assert "logs" in cmd

    def test_logs_with_follow(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with _patched_subprocess() as mock_run:
            cli.cmd_logs(_ns(name="alpha", follow=True))
        cmd = mock_run.call_args[0][0]
        logs_idx = cmd.index("logs")
        assert "-f" in cmd[logs_idx:]


# ---------------------------------------------------------------------------
# cmd_shell
# ---------------------------------------------------------------------------


class TestCmdShell:
    def test_shell_invokes_adb(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            cli.cmd_shell(_ns(name="alpha"))
        # adb connect + adb shell, two subprocess.run calls.
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert cmds[0][0] == "adb"
        assert cmds[1][0] == "adb"
        assert "shell" in cmds[1]

    def test_shell_no_adb_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        import shutil

        # adb missing, docker still present so cmd_create above worked.
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        with pytest.raises(SystemExit, match="adb not found"):
            cli.cmd_shell(_ns(name="alpha"))


# ---------------------------------------------------------------------------
# cmd_env
# ---------------------------------------------------------------------------


class TestCmdEnv:
    def test_env_prints_exports(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        cli.cmd_env(_ns(name="alpha"))
        out = capsys.readouterr().out
        assert "ANDROID_DEVICE=localhost:5555" in out
        assert "FRIDA_DEVICE=localhost:27042" in out


# ---------------------------------------------------------------------------
# cmd_frida
# ---------------------------------------------------------------------------


class TestCmdFrida:
    def test_frida_invokes_frida_cli(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            cli.cmd_frida(_ns(name="alpha", frida_args=["-n", "com.app"]))
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "frida"
        assert "-H" in cmd
        assert "localhost:27042" in cmd
        assert "com.app" in cmd

    def test_frida_no_frida_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        import shutil

        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        with pytest.raises(SystemExit, match=r"frida CLI not found.*beetroot\[frida\]"):
            cli.cmd_frida(_ns(name="alpha", frida_args=[]))


# ---------------------------------------------------------------------------
# cmd_module
# ---------------------------------------------------------------------------


class TestCmdModule:
    def test_module_url_branch(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))

        def _resp(url: str, **kwargs: object) -> MagicMock:
            r = MagicMock()
            r.read.return_value = b"PK\x03\x04zip"
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=_resp):
            cli.cmd_module(
                _ns(name="alpha", source="https://example.com/mod.zip", sha256=None)
            )

        from beetroot import config
        cfg = config.load_instance("alpha")
        assert len(cfg.modules) == 1
        assert cfg.modules[0].url == "https://example.com/mod.zip"
        assert cfg.modules[0].path is None

    def test_module_path_branch(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        # Make a real zip path under the repo root so _resolve finds it.
        local = cli_root / "local-mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        cli.cmd_module(_ns(name="alpha", source="local-mod.zip", sha256=None))
        from beetroot import config
        cfg = config.load_instance("alpha")
        assert len(cfg.modules) == 1
        assert cfg.modules[0].path == "local-mod.zip"
        assert cfg.modules[0].url is None

    def test_module_with_sha256(self, cli_root: Path) -> None:
        cli.cmd_create(_ns(name="alpha", preset="default", from_data=None))
        local = cli_root / "local-mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        import hashlib

        sha = hashlib.sha256(local.read_bytes()).hexdigest()
        cli.cmd_module(_ns(name="alpha", source="local-mod.zip", sha256=sha))
        from beetroot import config
        cfg = config.load_instance("alpha")
        assert cfg.modules[0].sha256 == sha


# ---------------------------------------------------------------------------
# build_parser / main
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_build_parser_returns_parser(self) -> None:
        p = cli.build_parser()
        assert isinstance(p, argparse.ArgumentParser)

    def test_subcommands_all_wire_a_func(self) -> None:
        p = cli.build_parser()
        for verb, name in [
            ("create", "alpha"),
            ("apply", "alpha"),
            ("destroy", "alpha"),
            ("logs", "alpha"),
            ("shell", "alpha"),
            ("env", "alpha"),
        ]:
            ns = p.parse_args([verb, name])
            assert callable(ns.func)

    def test_parse_create_default_preset(self) -> None:
        p = cli.build_parser()
        ns = p.parse_args(["create", "alpha"])
        assert ns.preset == "default"
        assert ns.func is cli.cmd_create

    def test_parse_up_all_flag(self) -> None:
        p = cli.build_parser()
        ns = p.parse_args(["up", "--all"])
        assert ns.all is True

    def test_parse_frida_remainder(self) -> None:
        p = cli.build_parser()
        ns = p.parse_args(["frida", "alpha", "-n", "com.app"])
        assert ns.frida_args == ["-n", "com.app"]

    def test_parse_module_with_sha(self) -> None:
        p = cli.build_parser()
        ns = p.parse_args(
            ["module", "alpha", "https://example.com/mod.zip", "--sha256", "abc"]
        )
        assert ns.sha256 == "abc"

class TestMain:
    def test_main_dispatches_create(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "create", "alpha"])
        cli.main()
        assert registry.get("alpha") is not None

    def test_main_no_subcommand_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot"])
        with pytest.raises(SystemExit):
            cli.main()

    def test_main_wraps_project_root_not_found(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "ls"])

        def _raise(_args: argparse.Namespace) -> None:
            raise paths.ProjectRootNotFoundError("no compose.yaml in /nowhere")

        monkeypatch.setattr(cli, "cmd_ls", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert "error: no compose.yaml in /nowhere" in str(exc.value)

    def test_main_wraps_port_collision_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Resolver-side self-collision (e.g. partial override colliding with
        # an un-overridden stride sibling) must surface as a friendly
        # `error: ...` line rather than a Python traceback.
        monkeypatch.setattr("sys.argv", ["beetroot", "ls"])

        def _raise(_args: argparse.Namespace) -> None:
            raise ports.PortCollisionError("frida and frida2 both resolved to 27043")

        monkeypatch.setattr(cli, "cmd_ls", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert "error: frida and frida2 both resolved to 27043" in str(exc.value)
