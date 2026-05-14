"""Tests for registry.py — instances.json CRUD with flock."""
from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import paths, registry


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
