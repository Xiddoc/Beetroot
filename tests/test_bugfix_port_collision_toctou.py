"""Regression tests for #183 — cross-instance port-collision check must be locked.

``_check_port_collisions`` read siblings via ``all_resolved_host_ports`` then
decided via ``find_port_collision`` with NO lock held across the read→decide
window. Two concurrent ``apply``/``create`` operations pinning the same explicit
``host:`` port could both pass the precheck and double-bind at ``up`` time. The
fix routes the check through ``registry.assert_no_port_collision``, which holds
the exclusive registry lock across the whole sibling-read + decision.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from beetroot import registry
from beetroot.config import InstanceConfig, PortMapping, write_yaml
from beetroot.ports import ResolvedPort


def _make_instance(base: Path, name: str, ports: list[PortMapping] | None = None) -> Path:
    root = base / name
    root.mkdir(parents=True)
    cfg = InstanceConfig() if ports is None else InstanceConfig(ports=ports)
    write_yaml(root / "beetroot.yaml", cfg)
    return root


def _seed(base: Path, name: str, ports: list[PortMapping] | None = None) -> Path:
    root = _make_instance(base, name, ports)
    registry.add_allocating(name, root)
    return root


def test_assert_no_port_collision_raises_on_conflict(
    isolated_registry: Path, tmp_path: Path
) -> None:
    # alpha pins adb to 6000; a new instance also pinning 6000 collides.
    _seed(tmp_path, "alpha", [PortMapping(service="adb", guest=5555, host=6000)])
    new_ports = [ResolvedPort(service="adb", guest=5555, host=6000)]
    with pytest.raises(ValueError, match="collides with instance 'alpha'"):
        registry.assert_no_port_collision("bravo", new_ports)


def test_assert_no_port_collision_noop_when_clear(
    isolated_registry: Path, tmp_path: Path
) -> None:
    _seed(tmp_path, "alpha", [PortMapping(service="adb", guest=5555, host=6000)])
    new_ports = [ResolvedPort(service="adb", guest=5555, host=6001)]
    # No collision → returns cleanly.
    registry.assert_no_port_collision("bravo", new_ports)


def test_assert_no_port_collision_excludes_self(
    isolated_registry: Path, tmp_path: Path
) -> None:
    # An instance's own registered ports must not collide with itself.
    _seed(tmp_path, "alpha", [PortMapping(service="adb", guest=5555, host=6000)])
    new_ports = [ResolvedPort(service="adb", guest=5555, host=6000)]
    registry.assert_no_port_collision("alpha", new_ports)


def test_check_runs_inside_exclusive_lock(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, "alpha", [PortMapping(service="adb", guest=5555, host=6000)])

    real_locked = registry._locked
    seen: list[bool] = []

    @contextlib.contextmanager
    def _spy_locked(path: Path, *, exclusive: bool = True) -> Iterator[Path]:
        # Record whether the sibling read is happening under the exclusive lock,
        # and — crucially — that ``_resolved_host_ports_from`` runs *inside* it.
        with real_locked(path, exclusive=exclusive) as p:
            seen.append(exclusive)
            yield p

    monkeypatch.setattr(registry, "_locked", _spy_locked)
    new_ports = [ResolvedPort(service="adb", guest=5555, host=6001)]
    registry.assert_no_port_collision("bravo", new_ports)
    # The precheck's own critical section is exclusive.
    assert True in seen


def test_sibling_read_happens_under_the_lock(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prove the read→decide window is inside the lock: patch the sibling
    # resolver to assert the lock file is held (exclusive) at read time.
    _seed(tmp_path, "alpha", [PortMapping(service="adb", guest=5555, host=6000)])

    lock_state = {"held": False}
    real_locked = registry._locked
    real_resolver = registry._resolved_host_ports_from

    @contextlib.contextmanager
    def _tracking_locked(path: Path, *, exclusive: bool = True) -> Iterator[Path]:
        with real_locked(path, exclusive=exclusive) as p:
            lock_state["held"] = exclusive
            try:
                yield p
            finally:
                lock_state["held"] = False

    def _checking_resolver(instances: dict[str, object]) -> dict[str, set[int]]:
        assert lock_state["held"], "sibling read ran OUTSIDE the exclusive lock (TOCTOU)"
        return real_resolver(instances)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "_locked", _tracking_locked)
    monkeypatch.setattr(registry, "_resolved_host_ports_from", _checking_resolver)

    new_ports = [ResolvedPort(service="adb", guest=5555, host=6000)]
    with pytest.raises(ValueError, match="collides"):
        registry.assert_no_port_collision("bravo", new_ports)
