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

from pathlib import Path

import pytest
from docker_daemon import daemon_available

from beetroot import api, compose, config, paths, registry, snapshot

# These restore flows free the source slot via ``destroy`` → ``compose down``,
# which needs a live Docker daemon; skip (don't fail) when there isn't one.
_needs_daemon = pytest.mark.skipif(not daemon_available(), reason="docker daemon not available")


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
    # (a2) the per-instance compose ports override exists and carries the
    # well-known guest mappings (issue #108 moved ports out of .env).
    override_path = paths.instance_compose_override(inst.root)
    assert override_path.is_file(), f"no compose.override.yaml at {override_path}"
    override_text = override_path.read_text()
    assert ":5555" in override_text
    assert ":27042" in override_text
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
    others = {n: p for n, p in registry.all_resolved_host_ports().items() if n != name}
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


@_needs_daemon
def test_restore_leaves_instance_ready_to_up(cli_root: Path) -> None:
    src = cli_root / "alpha"
    api.Instance.create("alpha", path=src)
    archive = snapshot.snapshot(src, cli_root / "alpha-archive")
    api.Instance.load("alpha").destroy(yes=True)

    snapshot.restore(archive, dest_name="alpha-restored", dest_path=cli_root / "alpha-restored")
    _assert_invariants("alpha-restored", expect_frida=False)


@_needs_daemon
def test_restore_with_frida_stages_executable(cli_root: Path) -> None:
    src = cli_root / "alpha"
    cfg = config.InstanceConfig(frida=config.Frida(version="16.4.10"))
    api.Instance.create("alpha", path=src, cfg=cfg)
    archive = snapshot.snapshot(src, cli_root / "alpha-archive")
    api.Instance.load("alpha").destroy(yes=True)

    snapshot.restore(archive, dest_name="alpha-restored", dest_path=cli_root / "alpha-restored")
    _assert_invariants("alpha-restored", expect_frida=True)


def test_apply_leaves_instance_ready_to_up(cli_root: Path) -> None:
    api.Instance.create("alpha")
    # Nuke the staged files so apply has to regenerate them.
    paths.instance_env(registry.instance_path("alpha")).unlink()
    paths.instance_frida(registry.instance_path("alpha")).unlink()
    api.Instance.load("alpha").apply()
    _assert_invariants("alpha", expect_frida=False)


def test_up_regenerates_missing_compose_override(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BLOCKER guard (issue #108): an instance whose compose.override.yaml was
    # deleted (pre-v8, or a manual rm) would boot with ZERO published ports
    # because the bundled template publishes none. ``up`` must self-heal the
    # override before starting compose.
    api.Instance.create("alpha")
    inst = api.Instance.load("alpha")
    override = paths.instance_compose_override(inst.root)
    override.unlink()
    assert not override.is_file()

    called: list[tuple[str, Path]] = []
    monkeypatch.setattr(compose, "up", lambda name, root: called.append((name, root)))

    inst.up()

    assert override.is_file(), "up() did not regenerate the missing override"
    assert ":5555" in override.read_text()
    assert called == [("alpha", inst.root)]


def test_up_regenerates_missing_env(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # issue #219: ``_base_cmd`` appends ``--env-file <root>/.env``
    # unconditionally, so a hand-deleted .env hard-fails ``compose up`` and
    # makes ``ps_status`` misreport a running container as not-created. ``up``
    # must self-heal a missing .env (not just the override) before starting.
    api.Instance.create("alpha")
    inst = api.Instance.load("alpha")
    env = paths.instance_env(inst.root)
    env.unlink()
    assert not env.is_file()

    called: list[tuple[str, Path]] = []
    monkeypatch.setattr(compose, "up", lambda name, root: called.append((name, root)))

    inst.up()

    assert env.is_file(), "up() did not regenerate the missing .env"
    assert "INSTANCE_NAME=alpha" in env.read_text()
    assert called == [("alpha", inst.root)]


def test_up_self_heal_rechecks_port_collision(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # issue #166: when ``up`` self-heals a missing override, it must re-run
    # the cross-instance port-collision precheck that ``apply`` enforces —
    # otherwise it could re-publish a host port another registered instance
    # already owns. The check must bail BEFORE staging / compose.up.
    api.Instance.create("alpha")
    inst = api.Instance.load("alpha")
    paths.instance_compose_override(inst.root).unlink()

    # Inject a colliding peer pinned to alpha's ADB host port (5555).
    peer = cli_root / "bravo"
    peer.mkdir()
    pinned = [
        config.PortMapping(
            service=m.service, guest=m.guest, host=5555 if m.service == "adb" else None
        )
        for m in config._default_port_mappings()
    ]
    config.write_yaml(peer / "beetroot.yaml", config.InstanceConfig(ports=pinned))
    registry.add_allocating("bravo", peer)

    up_called: list[str] = []
    staged: list[str] = []
    monkeypatch.setattr(compose, "up", lambda name, root: up_called.append(name))
    monkeypatch.setattr(inst, "_stage_local", lambda: staged.append("staged"))

    with pytest.raises(ValueError, match="5555"):
        inst.up()

    assert staged == [], "collision check must bail BEFORE staging"
    assert up_called == [], "collision check must bail BEFORE compose.up"


def test_restart_self_heals_missing_override(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # issue #166: ``restart`` previously called compose.down/up directly with
    # no self-heal, so a missing override restarted with ZERO published ports.
    # Routing the start through ``up`` re-stages the override (with its
    # well-known mappings) before booting.
    api.Instance.create("alpha")
    inst = api.Instance.load("alpha")
    override = paths.instance_compose_override(inst.root)
    override.unlink()

    order: list[str] = []
    monkeypatch.setattr(compose, "down", lambda name, root: order.append("down"))
    monkeypatch.setattr(compose, "up", lambda name, root: order.append("up"))

    inst.restart()

    assert override.is_file(), "restart() did not regenerate the missing override"
    assert ":5555" in override.read_text()
    assert order == ["down", "up"]


def test_restart_self_heals_missing_env(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # issue #219: a hand-deleted .env must be re-staged by ``restart`` (which
    # now routes through ``up``) before compose up, mirroring the override case.
    api.Instance.create("alpha")
    inst = api.Instance.load("alpha")
    env = paths.instance_env(inst.root)
    env.unlink()

    order: list[str] = []
    monkeypatch.setattr(compose, "down", lambda name, root: order.append("down"))
    monkeypatch.setattr(compose, "up", lambda name, root: order.append("up"))

    inst.restart()

    assert env.is_file(), "restart() did not regenerate the missing .env"
    assert "INSTANCE_NAME=alpha" in env.read_text()
    assert order == ["down", "up"]


def test_up_does_not_restage_when_override_present(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the override already exists, ``up`` must NOT re-stage (no needless
    # work, and no clobbering an override the user may have tweaked between
    # apply and up): it just starts compose.
    api.Instance.create("alpha")
    inst = api.Instance.load("alpha")
    staged: list[str] = []
    monkeypatch.setattr(compose, "up", lambda name, root: None)
    monkeypatch.setattr(inst, "_stage_local", lambda: staged.append("staged"))

    inst.up()

    assert staged == []


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
    pinned = [
        config.PortMapping(
            service=m.service, guest=m.guest, host=5555 if m.service == "adb" else None
        )
        for m in config._default_port_mappings()
    ]
    config.write_yaml(
        target / "beetroot.yaml",
        config.InstanceConfig(ports=pinned),
    )
    registry.add_allocating("bravo", target)
    with pytest.raises(AssertionError):
        _assert_invariants("bravo", expect_frida=False)


# Tag this file with the operation names so a grep across the test
# tree turns up every operation that must satisfy the contract. The
# mapping value type is left as a plain object reference (not a
# ``Callable[..., Any]``) because we never invoke through this dict —
# it's an audit-only marker.
_OPERATIONS = {
    "create": api.Instance.create,
    "register": api.Instance.register,
    "restore": snapshot.restore,
    "apply": api.Instance.apply,
}
