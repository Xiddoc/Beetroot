"""Integration tests for port-collision pre-validation in the CLI.

These also exercise the full cmd_create → port allocator → registry write
→ .env render path; they are the load-bearing user-input → final-artifact
behavior tests for T1's path refactor (the ``fix/ports-resolver-self-collision``
pattern, generalised to also assert on the resulting ``.env`` content).
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import cli, paths, registry
from beetroot.config import InstanceConfig, Ports, write_yaml


def _create_ns(name: str, **overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "name": name,
        "preset": "default",
        "from_data": None,
        "path": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _bundle_preset(
    cli_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    ports: Ports | None = None,
) -> None:
    """Write a preset under a tmp dir and route load_preset there.

    Each test that needs a preset with a specific override uses this to
    avoid touching the real bundled presets. ``frida=None`` keeps
    ``cmd_create`` off the network.
    """
    from beetroot import config

    bundle = cli_root / "_test_presets"
    bundle.mkdir(exist_ok=True)
    cfg = InstanceConfig(ports=ports or Ports(), frida=None)
    write_yaml(bundle / f"{name}.yaml", cfg)

    real_load = config.load_preset

    def _patched(preset_name: str) -> InstanceConfig:
        candidate = bundle / f"{preset_name}.yaml"
        if candidate.exists():
            return config.load_yaml(candidate)
        return real_load(preset_name)

    monkeypatch.setattr(config, "load_preset", _patched)


class TestCmdCreateCollision:
    def test_create_succeeds_with_no_collision(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        cli.cmd_create(_create_ns("bravo"))
        assert registry.get("alpha") is not None
        assert registry.get("bravo") is not None

    def test_create_with_pinned_adb_collides(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        _bundle_preset(cli_root, monkeypatch, "pinned", ports=Ports(adb=5555))
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_create(_create_ns("bravo", preset="pinned"))
        msg = str(exc_info.value)
        assert "5555" in msg
        assert "alpha" in msg
        assert "adb" in msg
        assert registry.get("bravo") is None

    def test_collision_message_format(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        _bundle_preset(cli_root, monkeypatch, "pinned", ports=Ports(adb=5555))
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_create(_create_ns("bravo", preset="pinned"))
        msg = str(exc_info.value)
        assert msg == (
            "error: port 5555 (adb) collides with instance 'alpha' "
            "(which also uses 5555). Pin or remove one."
        )

    def test_frida_collision_detected(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        _bundle_preset(cli_root, monkeypatch, "pinned", ports=Ports(frida=27042))
        with pytest.raises(SystemExit, match="27042"):
            cli.cmd_create(_create_ns("bravo", preset="pinned"))

    def test_create_collision_exits_before_registry_add(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        _bundle_preset(cli_root, monkeypatch, "pinned", ports=Ports(adb=5555))

        with patch.object(registry, "add") as fake_add:
            with pytest.raises(SystemExit, match="5555"):
                cli.cmd_create(_create_ns("bravo", preset="pinned"))
            fake_add.assert_not_called()


class TestCmdApplyCollision:
    def test_apply_excludes_self_from_collision_check(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        cli.cmd_apply(argparse.Namespace(name="alpha"))
        meta = registry.get("alpha")
        assert meta is not None
        assert meta["index"] == 0

    def test_apply_detects_new_collision_against_other_instance(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        cli.cmd_create(_create_ns("bravo"))

        cfg = InstanceConfig(ports=Ports(adb=5555), frida=None)
        bravo_root = registry.instance_path("bravo")
        write_yaml(paths.instance_yaml(bravo_root), cfg)

        with pytest.raises(SystemExit, match="5555"):
            cli.cmd_apply(argparse.Namespace(name="bravo"))

    def test_apply_collision_exits_before_stage_instance(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")
        cli.cmd_create(_create_ns("alpha"))
        cli.cmd_create(_create_ns("bravo"))
        cfg = InstanceConfig(ports=Ports(adb=5555), frida=None)
        bravo_root = registry.instance_path("bravo")
        write_yaml(paths.instance_yaml(bravo_root), cfg)
        with patch.object(cli, "_stage_instance") as fake_stage:
            with pytest.raises(SystemExit, match="5555"):
                cli.cmd_apply(argparse.Namespace(name="bravo"))
            fake_stage.assert_not_called()


class TestCmdCreateEndToEndEnvBytes:
    """Behavior tests covering the full user-input → ``.env`` artifact path.

    The ``fix/ports-resolver-self-collision`` retro showed that line + branch
    coverage on the resolver was insufficient: silent self-collisions slipped
    through because no test asserted on the final ``.env`` bytes given a
    realistic model input. T1 changes the entire path-resolution layer the
    resolver feeds into, so we re-pin the .env byte string here for two
    instances at unrelated tmp-paths.
    """

    def test_two_instances_at_unrelated_paths_each_get_distinct_env(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bundle_preset(cli_root, monkeypatch, "default")

        alpha_root = cli_root / "alpha-elsewhere"
        bravo_root = cli_root / "deep" / "nested" / "bravo-elsewhere"

        cli.cmd_create(_create_ns("alpha", path=str(alpha_root)))
        # chdir into a subdir of alpha to confirm cwd doesn't leak into bravo.
        sub = alpha_root / "data"
        sub.mkdir(exist_ok=True)
        monkeypatch.chdir(sub)

        cli.cmd_create(_create_ns("bravo", path=str(bravo_root)))

        alpha_env_bytes = paths.instance_env(alpha_root).read_bytes()
        bravo_env_bytes = paths.instance_env(bravo_root).read_bytes()

        expected_alpha = (
            b"INSTANCE_NAME=alpha\n"
            b"BASE_IMAGE=redroid/redroid:14.0.0_litegapps_houdini_magisk\n"
            b"ADB_PORT=5555\n"
            b"FRIDA_PORT=27042\n"
            b"FRIDA_PORT2=27043\n"
            b"MEM_LIMIT=3g\n"
            b"CPUS=2.0\n"
            b"SHM_SIZE=256m\n"
            b"PIDS_LIMIT=4096\n"
            b"DISPLAY_WIDTH=540\n"
            b"DISPLAY_HEIGHT=960\n"
            b"DISPLAY_FPS=3\n"
            b"DISPLAY_GPU=host\n"
        )
        expected_bravo = (
            b"INSTANCE_NAME=bravo\n"
            b"BASE_IMAGE=redroid/redroid:14.0.0_litegapps_houdini_magisk\n"
            b"ADB_PORT=5565\n"
            b"FRIDA_PORT=27052\n"
            b"FRIDA_PORT2=27053\n"
            b"MEM_LIMIT=3g\n"
            b"CPUS=2.0\n"
            b"SHM_SIZE=256m\n"
            b"PIDS_LIMIT=4096\n"
            b"DISPLAY_WIDTH=540\n"
            b"DISPLAY_HEIGHT=960\n"
            b"DISPLAY_FPS=3\n"
            b"DISPLAY_GPU=host\n"
        )
        assert alpha_env_bytes == expected_alpha
        assert bravo_env_bytes == expected_bravo

    def test_apply_from_subdir_of_instance_finds_root_via_cwd(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even though apply takes a name (not a path), this exercises the
        # invariant that the registry's recorded ``absolute_path`` survives
        # a chdir into an arbitrary subdir of the instance.
        _bundle_preset(cli_root, monkeypatch, "default")
        alpha_root = cli_root / "alpha-foo"
        cli.cmd_create(_create_ns("alpha", path=str(alpha_root)))

        subdir = alpha_root / "data" / "deep"
        subdir.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(subdir)

        paths.instance_env(alpha_root).unlink()
        cli.cmd_apply(argparse.Namespace(name="alpha"))
        assert paths.instance_env(alpha_root).exists()
