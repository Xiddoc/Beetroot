"""Integration tests for port-collision pre-validation in the CLI.

These also exercise the full ``beetroot create`` → port allocator →
registry write → ``.env`` render path; they are the load-bearing
user-input → final-artifact behavior tests for the path refactor
(the ``fix/ports-resolver-self-collision`` pattern, generalised to also
assert on the resulting ``.env`` content).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from beetroot import cli, paths, registry
from beetroot.config import InstanceConfig, Ports, write_yaml

runner = CliRunner()


def _write_pinned_yaml(root: Path, ports: Ports) -> None:
    """Write a beetroot.yaml that pins specific port overrides.

    ``frida=None`` keeps ``_stage_instance`` off the network — without
    this the cli_root fixture's stub is enough, but explicit ``frida:
    null`` matches the v0.2 semantics for the collision tests.
    """
    cfg = InstanceConfig(ports=ports, frida=None)
    write_yaml(paths.instance_yaml(root), cfg)


class TestCmdCreateCollision:
    def test_create_succeeds_with_no_collision(self, cli_root: Path) -> None:
        assert runner.invoke(cli.app, ["create", "alpha"]).exit_code == 0
        assert runner.invoke(cli.app, ["create", "bravo"]).exit_code == 0
        alpha_meta = registry.get("alpha")
        bravo_meta = registry.get("bravo")
        assert alpha_meta is not None
        assert bravo_meta is not None
        # Distinct stride indices → distinct port slots.
        assert alpha_meta["index"] != bravo_meta["index"]

    def test_create_collides_with_neighbour_pinned_to_next_stride_slot(
        self, cli_root: Path
    ) -> None:
        """When a pre-existing instance pins a port that lands on the next free index's stride slot, ``create`` must refuse before mutating the registry.

        Stride is 10 starting at 5555, so the second free index (1) lands
        on ADB 5565. Pinning ``alpha`` at ``adb=5565`` forces
        ``beetroot create bravo`` to collide on the very port the stride
        allocator would otherwise hand bravo.
        """
        assert runner.invoke(cli.app, ["create", "alpha"]).exit_code == 0
        _write_pinned_yaml(registry.instance_path("alpha"), Ports(adb=5565))
        assert runner.invoke(cli.app, ["apply", "alpha"]).exit_code == 0

        with patch.object(registry, "add") as fake_add:
            result = runner.invoke(cli.app, ["create", "bravo"])
            assert result.exit_code == 1
            assert "5565" in result.stderr
            fake_add.assert_not_called()


class TestCmdApplyCollision:
    def test_apply_excludes_self_from_collision_check(self, cli_root: Path) -> None:
        assert runner.invoke(cli.app, ["create", "alpha"]).exit_code == 0
        assert runner.invoke(cli.app, ["apply", "alpha"]).exit_code == 0
        meta = registry.get("alpha")
        assert meta is not None
        assert meta["index"] == 0

    def test_apply_with_pinned_adb_collides(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        bravo_root = registry.instance_path("bravo")
        _write_pinned_yaml(bravo_root, Ports(adb=5555))
        result = runner.invoke(cli.app, ["apply", "bravo"])
        assert result.exit_code == 1
        assert "5555" in result.stderr
        assert "alpha" in result.stderr
        assert "adb" in result.stderr

    def test_apply_collision_message_format(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        bravo_root = registry.instance_path("bravo")
        _write_pinned_yaml(bravo_root, Ports(adb=5555))
        result = runner.invoke(cli.app, ["apply", "bravo"])
        assert result.exit_code == 1
        assert result.stderr.rstrip("\n") == (
            "error: port 5555 (adb) collides with instance 'alpha' "
            "(which also uses 5555). Pin or remove one."
        )

    def test_apply_frida_collision_detected(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        bravo_root = registry.instance_path("bravo")
        _write_pinned_yaml(bravo_root, Ports(frida=27042))
        result = runner.invoke(cli.app, ["apply", "bravo"])
        assert result.exit_code == 1
        assert "27042" in result.stderr

    def test_apply_detects_new_collision_against_other_instance(
        self, cli_root: Path
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        _write_pinned_yaml(registry.instance_path("bravo"), Ports(adb=5555))
        result = runner.invoke(cli.app, ["apply", "bravo"])
        assert result.exit_code == 1
        assert "5555" in result.stderr

    def test_apply_collision_exits_before_stage_instance(self, cli_root: Path) -> None:
        from beetroot import api

        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        _write_pinned_yaml(registry.instance_path("bravo"), Ports(adb=5555))
        # T8 moved staging onto Instance._stage; the collision precheck
        # in Instance.apply must still bail out before it runs.
        with patch.object(api.Instance, "_stage") as fake_stage:
            result = runner.invoke(cli.app, ["apply", "bravo"])
            assert result.exit_code == 1
            assert "5555" in result.stderr
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
        alpha_root = cli_root / "alpha-elsewhere"
        bravo_root = cli_root / "deep" / "nested" / "bravo-elsewhere"

        result = runner.invoke(cli.app, ["create", "alpha", "--path", str(alpha_root)])
        assert result.exit_code == 0, result.stderr
        # chdir into a subdir of alpha to confirm cwd doesn't leak into bravo.
        sub = alpha_root / "data"
        sub.mkdir(exist_ok=True)
        monkeypatch.chdir(sub)

        result = runner.invoke(cli.app, ["create", "bravo", "--path", str(bravo_root)])
        assert result.exit_code == 0, result.stderr

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
        alpha_root = cli_root / "alpha-foo"
        result = runner.invoke(cli.app, ["create", "alpha", "--path", str(alpha_root)])
        assert result.exit_code == 0, result.stderr

        subdir = alpha_root / "data" / "deep"
        subdir.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(subdir)

        paths.instance_env(alpha_root).unlink()
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert paths.instance_env(alpha_root).exists()
