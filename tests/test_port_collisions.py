"""Integration tests for port-collision pre-validation in the CLI."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import cli, paths, registry
from beetroot.config import InstanceConfig, Ports, write_yaml


def _seed_preset(root: Path, name: str = "default", ports: Ports | None = None) -> None:
    """Write a preset with ``frida: null`` to avoid hitting the network in tests."""
    presets = root / "presets"
    presets.mkdir(exist_ok=True)
    cfg = InstanceConfig(ports=ports or Ports(), frida=None)
    write_yaml(presets / f"{name}.yaml", cfg)


class TestCmdCreateCollision:
    def test_create_succeeds_with_no_collision(self, isolated_root: Path) -> None:
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        cli.cmd_create(argparse.Namespace(name="bravo", preset="default", from_data=None))
        # bravo lands at index 1 (5565) — no collision.
        assert registry.get("alpha") is not None
        assert registry.get("bravo") is not None

    def test_create_with_pinned_adb_collides(self, isolated_root: Path) -> None:
        # Seed an instance at index 0 (ADB 5555) using stride defaults.
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))

        # Second preset pins ADB to 5555 — the same as alpha's stride-allocated port.
        _seed_preset(isolated_root, name="pinned", ports=Ports(adb=5555))
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_create(argparse.Namespace(name="bravo", preset="pinned", from_data=None))
        msg = str(exc_info.value)
        assert "5555" in msg
        assert "alpha" in msg
        assert "adb" in msg
        # bravo must not have been registered.
        assert registry.get("bravo") is None

    def test_collision_message_format(self, isolated_root: Path) -> None:
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        _seed_preset(isolated_root, name="pinned", ports=Ports(adb=5555))
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_create(argparse.Namespace(name="bravo", preset="pinned", from_data=None))
        msg = str(exc_info.value)
        assert msg == (
            "error: port 5555 (adb) collides with instance 'alpha' "
            "(which also uses 5555). Pin or remove one."
        )

    def test_frida_collision_detected(self, isolated_root: Path) -> None:
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        _seed_preset(isolated_root, name="pinned", ports=Ports(frida=27042))
        with pytest.raises(SystemExit, match="27042"):
            cli.cmd_create(argparse.Namespace(name="bravo", preset="pinned", from_data=None))

    def test_create_collision_exits_before_registry_add(self, isolated_root: Path) -> None:
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        _seed_preset(isolated_root, name="pinned", ports=Ports(adb=5555))

        with patch.object(registry, "add") as fake_add:
            with pytest.raises(SystemExit, match="5555"):
                cli.cmd_create(
                    argparse.Namespace(name="bravo", preset="pinned", from_data=None)
                )
            fake_add.assert_not_called()


class TestCmdApplyCollision:
    def test_apply_excludes_self_from_collision_check(self, isolated_root: Path) -> None:
        # An instance applying its own current ports must not "collide with itself".
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        # No edit — apply should succeed.
        cli.cmd_apply(argparse.Namespace(name="alpha"))
        # And alpha's index is still 0.
        meta = registry.get("alpha")
        assert meta is not None
        assert meta["index"] == 0

    def test_apply_detects_new_collision_against_other_instance(
        self, isolated_root: Path,
    ) -> None:
        # alpha at index 0 (ADB 5555), bravo at index 1 (ADB 5565).
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        cli.cmd_create(argparse.Namespace(name="bravo", preset="default", from_data=None))

        # Edit bravo's yaml to pin adb=5555 — now collides with alpha.
        cfg = InstanceConfig(ports=Ports(adb=5555), frida=None)
        write_yaml(paths.instance_yaml("bravo"), cfg)

        with pytest.raises(SystemExit, match="5555"):
            cli.cmd_apply(argparse.Namespace(name="bravo"))

    def test_apply_collision_exits_before_stage_instance(
        self, isolated_root: Path,
    ) -> None:
        _seed_preset(isolated_root)
        cli.cmd_create(argparse.Namespace(name="alpha", preset="default", from_data=None))
        cli.cmd_create(argparse.Namespace(name="bravo", preset="default", from_data=None))

        cfg = InstanceConfig(ports=Ports(adb=5555), frida=None)
        write_yaml(paths.instance_yaml("bravo"), cfg)

        with patch.object(cli, "_stage_instance") as fake_stage:
            with pytest.raises(SystemExit, match="5555"):
                cli.cmd_apply(argparse.Namespace(name="bravo"))
            fake_stage.assert_not_called()
