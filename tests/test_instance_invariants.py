"""H1+H2 guardrail — every instance-producing operation leaves a usable instance.

The post-CR fixes shipped a uniform contract: after ``Instance.create``,
``Instance.register``, ``snapshot.restore``, and ``Instance.apply``, the
instance is ready for a follow-up ``beetroot up`` — no intermediate
``beetroot apply`` required. That means three artifacts must exist on
disk and be coherent:

  (a) ``paths.instance_env(root)`` exists and parses
  (b) ``paths.instance_frida(root)`` exists (placeholder or executable
      depending on whether ``frida:`` is in beetroot.yaml)
  (c) ``Instance.load(name)`` round-trips and the resolved ports are
      collision-free against the rest of the registry

The bug class this catches: any operation that registers an instance
but forgets to stage its derived files. ``Instance.register`` and
``snapshot.restore`` both hit this before the post-CR fix.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from beetroot import api, config, paths, registry, snapshot


def _parse_env(text: str) -> dict[str, str]:
    """Tiny parser for the .env format render_env produces."""
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _assert_invariants(name: str, *, expect_frida: bool) -> None:
    inst = api.Instance.load(name)
    # (a) .env exists and parses.
    env_path = paths.instance_env(inst.root)
    assert env_path.is_file(), f"no .env at {env_path}"
    env = _parse_env(env_path.read_text())
    assert env.get("INSTANCE_NAME") == name, env
    assert "ADB_PORT" in env
    assert "FRIDA_PORT" in env
    # (b) frida-server exists. Placeholder when no frida block;
    # executable when there is one.
    frida_path = paths.instance_frida(inst.root)
    assert frida_path.exists(), f"no frida-server at {frida_path}"
    if expect_frida:
        assert frida_path.stat().st_size > 0
        assert frida_path.stat().st_mode & 0o111
    else:
        assert frida_path.stat().st_size == 0
        assert frida_path.stat().st_mode & 0o111 == 0
    # (c) load round-trips and ports don't collide against the rest of
    # the registry.
    others = {
        n: p for n, p in registry.all_resolved_ports().items() if n != name
    }
    assert registry.find_port_collision(inst.ports, others) is None


# ---------------------------------------------------------------------------
# Each operation gets its own test; parametrizing through a single function
# would obscure which operation failed in the assertion output.
# ---------------------------------------------------------------------------


def test_create_leaves_instance_ready_to_up(cli_root: Path) -> None:
    api.Instance.create("alpha")
    _assert_invariants("alpha", expect_frida=False)


def test_create_with_frida_leaves_executable_frida(cli_root: Path) -> None:
    cfg = config.InstanceConfig(frida=config.Frida(version="16.4.10"))
    api.Instance.create("alpha", cfg=cfg)
    _assert_invariants("alpha", expect_frida=True)


def test_register_leaves_instance_ready_to_up(cli_root: Path) -> None:
    target = cli_root / "external"
    target.mkdir()
    config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
    api.Instance.register(target)
    _assert_invariants("external", expect_frida=False)


def test_register_with_frida_stages_executable(cli_root: Path) -> None:
    target = cli_root / "external"
    target.mkdir()
    config.write_yaml(
        target / "beetroot.yaml",
        config.InstanceConfig(frida=config.Frida(version="16.4.10")),
    )
    api.Instance.register(target)
    _assert_invariants("external", expect_frida=True)


def test_restore_leaves_instance_ready_to_up(cli_root: Path) -> None:
    src = cli_root / "alpha"
    api.Instance.create("alpha", path=src)
    archive = snapshot.snapshot(src, cli_root / "alpha-archive")
    api.Instance.load("alpha").destroy(yes=True)

    snapshot.restore(
        archive, dest_name="alpha-restored", dest_path=cli_root / "alpha-restored"
    )
    _assert_invariants("alpha-restored", expect_frida=False)


def test_restore_with_frida_stages_executable(cli_root: Path) -> None:
    src = cli_root / "alpha"
    cfg = config.InstanceConfig(frida=config.Frida(version="16.4.10"))
    api.Instance.create("alpha", path=src, cfg=cfg)
    archive = snapshot.snapshot(src, cli_root / "alpha-archive")
    api.Instance.load("alpha").destroy(yes=True)

    snapshot.restore(
        archive, dest_name="alpha-restored", dest_path=cli_root / "alpha-restored"
    )
    _assert_invariants("alpha-restored", expect_frida=True)


def test_apply_leaves_instance_ready_to_up(cli_root: Path) -> None:
    api.Instance.create("alpha")
    # Nuke the staged files so apply has to regenerate them.
    paths.instance_env(registry.instance_path("alpha")).unlink()
    paths.instance_frida(registry.instance_path("alpha")).unlink()
    api.Instance.load("alpha").apply()
    _assert_invariants("alpha", expect_frida=False)


# ---------------------------------------------------------------------------
# Negative case: prove the invariants helper detects a port-collision
# regression. If the operation forgets to refuse a collision, the
# helper's _check_port_collisions assertion fires.
# ---------------------------------------------------------------------------


def test_invariants_helper_detects_collision_regression(cli_root: Path) -> None:
    # Two instances both pinned to ADB 5555 — simulate a regression
    # where some operation skipped the collision check. The helper
    # must surface it via find_port_collision.
    api.Instance.create("alpha")
    # Forcibly inject a colliding peer directly through the registry
    # (bypassing api.Instance.create's collision check).
    target = cli_root / "bravo"
    target.mkdir()
    config.write_yaml(
        target / "beetroot.yaml",
        config.InstanceConfig(ports=config.Ports(adb=5555)),
    )
    registry.add("bravo", target, 1)
    with pytest.raises(AssertionError):
        _assert_invariants("bravo", expect_frida=False)


# Tag this file with the operation names so a grep across the test
# tree turns up every operation that must satisfy the contract.
_OPERATIONS: dict[str, Callable[..., Any]] = {
    "create": api.Instance.create,
    "register": api.Instance.register,
    "restore": snapshot.restore,
    "apply": api.Instance.apply,
}
