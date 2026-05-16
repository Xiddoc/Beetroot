"""Tests for registry.py — instances.json CRUD with flock."""
from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import paths, registry
from beetroot.config import InstanceConfig, Ports, write_yaml


def _seed_instance(name: str, index: int, override: Ports | None = None) -> None:
    """Write a minimal beetroot.yaml and register the instance.

    Used by the resolved-ports + collision tests, which call ``load_instance``
    under the hood via ``registry.all_resolved_ports``.
    """
    write_yaml(paths.instance_yaml(name), InstanceConfig(ports=override or Ports()))
    registry.add(name, index)


class TestRegistryAdd:
    def test_add_creates_registry(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        assert paths.registry_file().exists()

    def test_add_and_get(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        entry = registry.get("alpha")
        assert entry is not None
        assert entry["index"] == 0

    def test_add_stores_created_at(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        entry = registry.get("alpha")
        assert entry is not None
        assert "created_at" in entry

    def test_add_duplicate_raises(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        with pytest.raises(ValueError, match="already in registry"):
            registry.add("alpha", 1)

    def test_add_multiple_instances(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.add("bravo", 1)
        alpha = registry.get("alpha")
        bravo = registry.get("bravo")
        assert alpha is not None
        assert bravo is not None
        assert alpha["index"] == 0
        assert bravo["index"] == 1


class TestRegistryGet:
    def test_get_missing_returns_none(self, isolated_root: Path) -> None:
        assert registry.get("ghost") is None

    def test_get_missing_with_no_registry_returns_none(self, isolated_root: Path) -> None:
        assert not paths.registry_file().exists()
        assert registry.get("ghost") is None


class TestRegistryRemove:
    def test_remove_deletes_entry(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.remove("alpha")
        assert registry.get("alpha") is None

    def test_remove_nonexistent_is_noop(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.remove("ghost")
        assert registry.get("alpha") is not None

    def test_remove_without_registry_is_noop(self, isolated_root: Path) -> None:
        registry.remove("ghost")


class TestListInstances:
    def test_empty_when_no_registry(self, isolated_root: Path) -> None:
        assert registry.list_instances() == {}

    def test_returns_all_added(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.add("bravo", 1)
        instances = registry.list_instances()
        assert set(instances.keys()) == {"alpha", "bravo"}

    def test_empty_after_remove_all(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.remove("alpha")
        assert registry.list_instances() == {}


class TestUsedIndices:
    def test_empty_when_no_registry(self, isolated_root: Path) -> None:
        assert registry.used_indices() == set()

    def test_returns_all_indices(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.add("bravo", 3)
        assert registry.used_indices() == {0, 3}

    def test_freed_index_is_removed(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.add("bravo", 1)
        registry.remove("alpha")
        assert registry.used_indices() == {1}


class TestAllResolvedPorts:
    def test_empty_registry_returns_empty(self, isolated_root: Path) -> None:
        assert registry.all_resolved_ports() == {}

    def test_single_instance_uses_stride_defaults(self, isolated_root: Path) -> None:
        _seed_instance("alpha", 0)
        assert registry.all_resolved_ports() == {
            "alpha": {"adb": 5555, "frida": 27042, "frida2": 27043},
        }

    def test_multiple_instances_each_their_own_ports(self, isolated_root: Path) -> None:
        _seed_instance("alpha", 0)
        _seed_instance("bravo", 1)
        _seed_instance("charlie", 2)
        assert registry.all_resolved_ports() == {
            "alpha": {"adb": 5555, "frida": 27042, "frida2": 27043},
            "bravo": {"adb": 5565, "frida": 27052, "frida2": 27053},
            "charlie": {"adb": 5575, "frida": 27062, "frida2": 27063},
        }

    def test_override_wins_over_stride(self, isolated_root: Path) -> None:
        _seed_instance("alpha", 0, Ports(adb=9000))
        result = registry.all_resolved_ports()
        assert result["alpha"]["adb"] == 9000
        # The non-overridden frida ports still come from the stride defaults.
        assert result["alpha"]["frida"] == 27042
        assert result["alpha"]["frida2"] == 27043

    def test_partial_override_only_replaces_named_field(self, isolated_root: Path) -> None:
        _seed_instance("alpha", 2, Ports(frida_control=12345))
        result = registry.all_resolved_ports()
        assert result["alpha"] == {"adb": 5575, "frida": 27062, "frida2": 12345}


class TestFindPortCollision:
    def test_empty_others_returns_none(self) -> None:
        new_ports = {"adb": 5555, "frida": 27042, "frida2": 27043}
        assert registry.find_port_collision(new_ports, {}) is None

    def test_no_collision_returns_none(self) -> None:
        new_ports = {"adb": 5555, "frida": 27042, "frida2": 27043}
        others = {"bravo": {"adb": 5565, "frida": 27052, "frida2": 27053}}
        assert registry.find_port_collision(new_ports, others) is None

    def test_adb_collision_returns_tuple(self) -> None:
        new_ports = {"adb": 5555, "frida": 9000, "frida2": 9001}
        others = {"bravo": {"adb": 5555, "frida": 27052, "frida2": 27053}}
        result = registry.find_port_collision(new_ports, others)
        assert result == (5555, "bravo", "adb")

    def test_frida_collision_returns_tuple(self) -> None:
        new_ports = {"adb": 8000, "frida": 27042, "frida2": 8001}
        others = {"bravo": {"adb": 5565, "frida": 27042, "frida2": 27053}}
        result = registry.find_port_collision(new_ports, others)
        assert result == (27042, "bravo", "frida")

    def test_frida2_collision_returns_tuple(self) -> None:
        new_ports = {"adb": 8000, "frida": 9000, "frida2": 27043}
        others = {"bravo": {"adb": 5565, "frida": 27052, "frida2": 27043}}
        result = registry.find_port_collision(new_ports, others)
        assert result == (27043, "bravo", "frida2")

    def test_cross_kind_collision_detected(self) -> None:
        # New instance pins adb to 27042, which lives in another instance's frida slot.
        new_ports = {"adb": 27042, "frida": 9000, "frida2": 9001}
        others = {"bravo": {"adb": 5565, "frida": 27042, "frida2": 27053}}
        result = registry.find_port_collision(new_ports, others)
        assert result is not None
        port, name, kind = result
        assert port == 27042
        assert name == "bravo"
        assert kind == "adb"

    def test_first_collision_among_many_is_returned(self) -> None:
        new_ports = {"adb": 5555, "frida": 27042, "frida2": 27043}
        others = {
            "bravo": {"adb": 5555, "frida": 27052, "frida2": 27053},
            "charlie": {"adb": 5575, "frida": 27042, "frida2": 27063},
        }
        result = registry.find_port_collision(new_ports, others)
        assert result is not None
        # Both bravo and charlie collide; either is acceptable, but the port + kind
        # must align with one of them.
        _port, _name, kind = result
        assert kind in {"adb", "frida"}


class TestRoundtrip:
    def test_add_remove_readd_same_index(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.remove("alpha")
        registry.add("alpha", 0)
        entry = registry.get("alpha")
        assert entry is not None
        assert entry["index"] == 0

    def test_sequential_add_remove_add(self, isolated_root: Path) -> None:
        registry.add("alpha", 0)
        registry.add("bravo", 1)
        registry.remove("alpha")
        registry.add("charlie", 2)
        assert registry.used_indices() == {1, 2}
        bravo = registry.get("bravo")
        charlie = registry.get("charlie")
        assert bravo is not None
        assert charlie is not None
        assert bravo["index"] == 1
        assert charlie["index"] == 2
