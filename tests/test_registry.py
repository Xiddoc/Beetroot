"""Tests for registry.py — instances.json CRUD with flock."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from beetroot import paths, registry
from beetroot.config import InstanceConfig, Ports, write_yaml


def _make_instance(
    base: Path, name: str, ports: Ports | None = None
) -> Path:
    """Create an instance dir at ``base/name`` with a minimal beetroot.yaml."""
    root = base / name
    root.mkdir(parents=True)
    write_yaml(root / "beetroot.yaml", InstanceConfig(ports=ports or Ports()))
    return root


def _seed(base: Path, name: str, index: int, ports: Ports | None = None) -> Path:
    """Create an instance dir and register it. Returns the path."""
    root = _make_instance(base, name, ports)
    registry.add(name, root, index)
    return root


class TestRegistryAdd:
    def test_add_creates_registry(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        assert paths.user_registry_file().exists()

    def test_add_and_get(self, isolated_registry: Path, tmp_path: Path) -> None:
        root = _seed(tmp_path, "alpha", 0)
        entry = registry.get("alpha")
        assert entry is not None
        assert entry["index"] == 0
        assert entry["absolute_path"] == str(root)

    def test_add_stores_created_at(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        entry = registry.get("alpha")
        assert entry is not None
        assert "created_at" in entry

    def test_add_duplicate_raises(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        root = _seed(tmp_path, "alpha", 0)
        with pytest.raises(ValueError, match="already in registry"):
            registry.add("alpha", root, 1)

    def test_add_multiple_instances(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        _seed(tmp_path, "bravo", 1)
        alpha = registry.get("alpha")
        bravo = registry.get("bravo")
        assert alpha is not None
        assert bravo is not None
        assert alpha["index"] == 0
        assert bravo["index"] == 1


class TestRegistryGet:
    def test_get_missing_returns_none(self, isolated_registry: Path) -> None:
        assert registry.get("ghost") is None

    def test_get_missing_with_no_registry_returns_none(
        self, isolated_registry: Path
    ) -> None:
        assert not paths.user_registry_file().exists()
        assert registry.get("ghost") is None


class TestInstancePath:
    def test_lookup_returns_path(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        root = _seed(tmp_path, "alpha", 0)
        assert registry.instance_path("alpha") == root

    def test_unknown_name_raises(self, isolated_registry: Path) -> None:
        with pytest.raises(registry.RegistryError, match="ghost"):
            registry.instance_path("ghost")


class TestRegistryRemove:
    def test_remove_deletes_entry(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        registry.remove("alpha")
        assert registry.get("alpha") is None

    def test_remove_nonexistent_is_noop(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        registry.remove("ghost")
        assert registry.get("alpha") is not None

    def test_remove_without_registry_is_noop(
        self, isolated_registry: Path
    ) -> None:
        registry.remove("ghost")


class TestListInstances:
    def test_empty_when_no_registry(self, isolated_registry: Path) -> None:
        assert registry.list_instances() == {}

    def test_returns_all_added(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        _seed(tmp_path, "bravo", 1)
        instances = registry.list_instances()
        assert set(instances.keys()) == {"alpha", "bravo"}

    def test_empty_after_remove_all(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        registry.remove("alpha")
        assert registry.list_instances() == {}


class TestUsedIndices:
    def test_empty_when_no_registry(self, isolated_registry: Path) -> None:
        assert registry.used_indices() == set()

    def test_returns_all_indices(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        _seed(tmp_path, "bravo", 3)
        assert registry.used_indices() == {0, 3}

    def test_freed_index_is_removed(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        _seed(tmp_path, "bravo", 1)
        registry.remove("alpha")
        assert registry.used_indices() == {1}


class TestAllResolvedPorts:
    def test_empty_registry_returns_empty(
        self, isolated_registry: Path
    ) -> None:
        assert registry.all_resolved_ports() == {}

    def test_single_instance_uses_stride_defaults(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        assert registry.all_resolved_ports() == {
            "alpha": {"adb": 5555, "frida": 27042, "frida2": 27043},
        }

    def test_multiple_instances_each_their_own_ports(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        _seed(tmp_path, "bravo", 1)
        _seed(tmp_path, "charlie", 2)
        assert registry.all_resolved_ports() == {
            "alpha": {"adb": 5555, "frida": 27042, "frida2": 27043},
            "bravo": {"adb": 5565, "frida": 27052, "frida2": 27053},
            "charlie": {"adb": 5575, "frida": 27062, "frida2": 27063},
        }

    def test_override_wins_over_stride(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0, Ports(adb=9000))
        result = registry.all_resolved_ports()
        assert result["alpha"]["adb"] == 9000
        assert result["alpha"]["frida"] == 27042
        assert result["alpha"]["frida2"] == 27043

    def test_partial_override_only_replaces_named_field(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 2, Ports(frida_control=12345))
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
        _port, _name, kind = result
        assert kind in {"adb", "frida"}


class TestRoundtrip:
    def test_add_remove_readd_same_index(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        root = _make_instance(tmp_path, "alpha")
        registry.add("alpha", root, 0)
        registry.remove("alpha")
        registry.add("alpha", root, 0)
        entry = registry.get("alpha")
        assert entry is not None
        assert entry["index"] == 0

    def test_sequential_add_remove_add(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        _seed(tmp_path, "bravo", 1)
        registry.remove("alpha")
        _seed(tmp_path, "charlie", 2)
        assert registry.used_indices() == {1, 2}


class TestSchemaMigration:
    def test_v1_registry_renamed_to_bak(
        self, isolated_registry: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Hand-write a v1-shaped registry into the XDG location.
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        v1 = {
            "version": 1,
            "instances": {"alpha": {"index": 0, "created_at": "2025-01-01T00:00:00"}},
        }
        path.write_text(json.dumps(v1))

        # Force a read — registry.list_instances() triggers the migration.
        result = registry.list_instances()

        assert result == {}
        bak = path.with_suffix(path.suffix + ".bak")
        assert bak.exists()
        assert not path.exists() or json.loads(path.read_text())["instances"] == {}
        out = capsys.readouterr().out
        assert "register" in out
        assert ".bak" in out

    def test_v2_registry_loads_directly(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha", 0)
        # Subsequent reads must succeed without "migrating".
        assert "alpha" in registry.list_instances()
