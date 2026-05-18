"""Tests for cli.py — argparse dispatch and every verb handler."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from beetroot import cli, config, paths, ports, registry


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


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _create_ns(name: str, **overrides: Any) -> argparse.Namespace:
    """Build a ``cmd_create``-shaped namespace with sensible defaults."""
    defaults: dict[str, Any] = {
        "name": name,
        "from_data": None,
        "path": None,
    }
    defaults.update(overrides)
    return _ns(**defaults)


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
        registry.add("alpha", cli_root / "alpha", 0)
        cli._ensure_exists("alpha")


# ---------------------------------------------------------------------------
# _resolve_names
# ---------------------------------------------------------------------------


class TestResolveNames:
    def test_explicit_names(self, cli_root: Path) -> None:
        args = _ns(all=False, names=["alpha", "bravo"])
        assert cli._resolve_names(args) == ["alpha", "bravo"]

    def test_all_returns_sorted_registry(self, cli_root: Path) -> None:
        registry.add("bravo", cli_root / "bravo", 1)
        registry.add("alpha", cli_root / "alpha", 0)
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
        cli.cmd_create(_create_ns("alpha"))
        assert registry.get("alpha") is not None
        root = registry.instance_path("alpha")
        assert paths.instance_yaml(root).exists()
        assert paths.instance_env(root).exists()

    def test_create_default_path_is_cwd_subdir(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        assert registry.instance_path("alpha") == (cli_root / "alpha").resolve()

    def test_create_explicit_path(self, cli_root: Path) -> None:
        target = cli_root / "deep" / "nested" / "alpha-dir"
        cli.cmd_create(_create_ns("alpha", path=str(target)))
        assert registry.instance_path("alpha") == target.resolve()
        assert (target / "beetroot.yaml").exists()

    def test_create_duplicate_exits(self, cli_root: Path) -> None:
        registry.add("alpha", cli_root / "alpha", 0)
        with pytest.raises(SystemExit, match="already exists"):
            cli.cmd_create(_create_ns("alpha"))

    def test_create_refuses_existing_yaml(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        target.mkdir()
        (target / "beetroot.yaml").write_text("api_version: 2\n")
        with pytest.raises(SystemExit, match="register"):
            cli.cmd_create(_create_ns("alpha"))

    def test_create_with_from_data_copies(self, cli_root: Path) -> None:
        src = cli_root / "old-data"
        src.mkdir()
        (src / "marker.txt").write_text("hello")
        cli.cmd_create(_create_ns("alpha", from_data=str(src)))
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
        cli.cmd_create(_create_ns("alpha", from_data=str(src), path=str(target)))
        assert (target / "data" / "marker.txt").read_text() == "new"
        assert not (target / "data" / "stale.txt").exists()

    def test_create_with_invalid_from_data_exits(self, cli_root: Path) -> None:
        with pytest.raises(SystemExit, match="not a directory"):
            cli.cmd_create(_create_ns("alpha", from_data=str(cli_root / "missing")))

    def test_create_writes_exact_minimal_yaml_bytes(self, cli_root: Path) -> None:
        """Behavior test — drive cmd_create end-to-end and pin the YAML bytes.

        The whole point of T3 is that a fresh ``beetroot create`` produces
        a minimal, hand-readable beetroot.yaml — not the schema's full
        defaulted dump. Any change to that artifact (extra fields, key
        reordering, comment leakage) breaks this assertion deliberately.
        """
        cli.cmd_create(_create_ns("alpha"))
        root = registry.instance_path("alpha")
        assert paths.instance_yaml(root).read_bytes() == (
            b"api_version: 2\nandroid:\n  version: 14\n"
        )


# ---------------------------------------------------------------------------
# cmd_register
# ---------------------------------------------------------------------------


class TestCmdRegister:
    def test_register_adopts_existing_dir(self, cli_root: Path) -> None:
        target = cli_root / "external-instance"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        cli.cmd_register(_ns(path=str(target), name=None))
        assert registry.instance_path("external-instance") == target.resolve()

    def test_register_with_explicit_name(self, cli_root: Path) -> None:
        target = cli_root / "external"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        cli.cmd_register(_ns(path=str(target), name="alpha"))
        assert registry.instance_path("alpha") == target.resolve()

    def test_register_missing_yaml_exits(self, cli_root: Path) -> None:
        target = cli_root / "empty-dir"
        target.mkdir()
        with pytest.raises(SystemExit, match=r"no beetroot\.yaml"):
            cli.cmd_register(_ns(path=str(target), name=None))

    def test_register_duplicate_name_exits(self, cli_root: Path) -> None:
        target = cli_root / "alpha"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        cli.cmd_register(_ns(path=str(target), name=None))
        # Re-registering under the same name should fail.
        with pytest.raises(SystemExit, match="already registered"):
            cli.cmd_register(_ns(path=str(target), name=None))


# ---------------------------------------------------------------------------
# cmd_apply
# ---------------------------------------------------------------------------


class TestCmdApply:
    def test_apply_re_renders_env(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        root = registry.instance_path("alpha")
        paths.instance_env(root).unlink()
        cli.cmd_apply(_ns(name="alpha"))
        assert paths.instance_env(root).exists()

    def test_apply_missing_instance_exits(self, cli_root: Path) -> None:
        with pytest.raises(SystemExit, match="no instance named"):
            cli.cmd_apply(_ns(name="ghost"))

    def test_apply_frida_none_path(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        root = registry.instance_path("alpha")
        (root / "beetroot.yaml").write_text(
            "api_version: 2\nandroid:\n  version: 14\nfrida: null\n"
        )
        cli.cmd_apply(_ns(name="alpha"))
        assert paths.instance_frida(root).exists()
        assert paths.instance_frida(root).stat().st_size == 0


# ---------------------------------------------------------------------------
# cmd_up / cmd_down / cmd_restart
# ---------------------------------------------------------------------------


class TestCmdUp:
    def test_up_invokes_compose(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess() as mock_run:
            cli.cmd_up(_ns(names=["alpha"], all=False, build=False))
        assert mock_run.called

    def test_up_with_build_flag(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess() as mock_run:
            cli.cmd_up(_ns(names=["alpha"], all=False, build=True))
        cmd = mock_run.call_args[0][0]
        assert "--build" in cmd

    def test_up_all(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        cli.cmd_create(_create_ns("bravo"))
        with _patched_subprocess() as mock_run:
            cli.cmd_up(_ns(names=[], all=True, build=False))
        assert mock_run.call_count == 2


class TestCmdDown:
    def test_down_invokes_compose(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess() as mock_run:
            cli.cmd_down(_ns(names=["alpha"], all=False))
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd


class TestCmdRestart:
    def test_restart_invokes_down_then_up(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess() as mock_run:
            cli.cmd_restart(_ns(names=["alpha"], all=False))
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("down" in c for c in cmds)
        assert any("up" in c for c in cmds)


# ---------------------------------------------------------------------------
# cmd_destroy
# ---------------------------------------------------------------------------


class TestCmdDestroy:
    def test_destroy_with_yes_skips_prompt(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        root = registry.instance_path("alpha")
        with _patched_subprocess():
            cli.cmd_destroy(_ns(name="alpha", yes=True))
        assert registry.get("alpha") is None
        assert not root.exists()

    def test_destroy_prompt_yes(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess(), patch("builtins.input", return_value="y"):
            cli.cmd_destroy(_ns(name="alpha", yes=False))
        assert registry.get("alpha") is None

    def test_destroy_prompt_no_aborts(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess(), patch("builtins.input", return_value="n"):
            cli.cmd_destroy(_ns(name="alpha", yes=False))
        assert registry.get("alpha") is not None
        assert "aborted" in capsys.readouterr().out

    def test_destroy_compose_error_is_caught(
        self, cli_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.cmd_create(_create_ns("alpha"))
        from beetroot import compose

        def _boom(name: str, root: Path, *, volumes: bool = False) -> None:
            raise compose.ComposeError("simulated failure")

        with patch.object(compose, "down", side_effect=_boom):
            cli.cmd_destroy(_ns(name="alpha", yes=True))
        out = capsys.readouterr().out
        assert "continuing" in out
        assert registry.get("alpha") is None

    def test_destroy_missing_instance_exits(self, cli_root: Path) -> None:
        with pytest.raises(SystemExit, match="no instance named"):
            cli.cmd_destroy(_ns(name="ghost", yes=True))

    def test_destroy_without_instance_dir(self, cli_root: Path) -> None:
        # Registry-only entry (path absent). Exercises the "if not exists" branch.
        registry.add("alpha", cli_root / "alpha-missing", 0)
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
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess():
            cli.cmd_ls(_ns(json=False))
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "5555" in out

    def test_ls_json(self, cli_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cli.cmd_create(_create_ns("alpha"))
        capsys.readouterr()
        with _patched_subprocess():
            cli.cmd_ls(_ns(json=True))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "alpha" in data
        assert data["alpha"]["adb"] == "localhost:5555"
        assert data["alpha"]["path"] == str(registry.instance_path("alpha"))


# ---------------------------------------------------------------------------
# cmd_logs
# ---------------------------------------------------------------------------


class TestCmdLogs:
    def test_logs_invokes_compose(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        with _patched_subprocess() as mock_run:
            cli.cmd_logs(_ns(name="alpha", follow=False))
        cmd = mock_run.call_args[0][0]
        assert "logs" in cmd

    def test_logs_with_follow(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
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
        cli.cmd_create(_create_ns("alpha"))
        with patch("subprocess.run", return_value=_ok_proc()) as mock_run:
            cli.cmd_shell(_ns(name="alpha"))
        assert mock_run.call_count == 2
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert cmds[0][0] == "adb"
        assert cmds[1][0] == "adb"
        assert "shell" in cmds[1]

    def test_shell_no_adb_exits(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli.cmd_create(_create_ns("alpha"))
        import shutil

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
        cli.cmd_create(_create_ns("alpha"))
        cli.cmd_env(_ns(name="alpha"))
        out = capsys.readouterr().out
        assert "ANDROID_DEVICE=localhost:5555" in out
        assert "FRIDA_DEVICE=localhost:27042" in out


# ---------------------------------------------------------------------------
# cmd_frida
# ---------------------------------------------------------------------------


class TestCmdFrida:
    def test_frida_invokes_frida_cli(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
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
        cli.cmd_create(_create_ns("alpha"))
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
        cli.cmd_create(_create_ns("alpha"))

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

        cfg = config.load_yaml(paths.instance_yaml(registry.instance_path("alpha")))
        assert len(cfg.modules) == 1
        assert cfg.modules[0].url == "https://example.com/mod.zip"
        assert cfg.modules[0].path is None

    def test_module_path_branch(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        root = registry.instance_path("alpha")
        local = root / "local-mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        cli.cmd_module(_ns(name="alpha", source="local-mod.zip", sha256=None))
        cfg = config.load_yaml(paths.instance_yaml(root))
        assert len(cfg.modules) == 1
        assert cfg.modules[0].path == "local-mod.zip"

    def test_module_with_sha256(self, cli_root: Path) -> None:
        cli.cmd_create(_create_ns("alpha"))
        root = registry.instance_path("alpha")
        local = root / "local-mod.zip"
        local.write_bytes(b"PK\x03\x04local")
        import hashlib

        sha = hashlib.sha256(local.read_bytes()).hexdigest()
        cli.cmd_module(_ns(name="alpha", source="local-mod.zip", sha256=sha))
        cfg = config.load_yaml(paths.instance_yaml(root))
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

    def test_parse_create_has_no_preset_flag(self) -> None:
        p = cli.build_parser()
        ns = p.parse_args(["create", "alpha"])
        assert not hasattr(ns, "preset")
        assert ns.func is cli.cmd_create

    def test_parse_create_rejects_preset_flag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = cli.build_parser()
        with pytest.raises(SystemExit) as exc:
            p.parse_args(["create", "alpha", "--preset", "default"])
        assert exc.value.code == 2
        assert "--preset" in capsys.readouterr().err

    def test_parse_register_path_positional(self) -> None:
        p = cli.build_parser()
        ns = p.parse_args(["register", "/tmp/alpha"])
        assert ns.path == "/tmp/alpha"
        assert ns.name is None
        assert ns.func is cli.cmd_register

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

    def test_main_wraps_instance_root_not_found(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "ls"])

        def _raise(_args: argparse.Namespace) -> None:
            raise paths.InstanceRootNotFoundError("no beetroot.yaml in /nowhere")

        monkeypatch.setattr(cli, "cmd_ls", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert "error: no beetroot.yaml in /nowhere" in str(exc.value)

    def test_main_wraps_port_collision_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["beetroot", "ls"])

        def _raise(_args: argparse.Namespace) -> None:
            raise ports.PortCollisionError("frida and frida2 both resolved to 27043")

        monkeypatch.setattr(cli, "cmd_ls", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert "error: frida and frida2 both resolved to 27043" in str(exc.value)
