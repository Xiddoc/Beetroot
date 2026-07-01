"""Tests for registry.py — instances.json CRUD with flock."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from beetroot import paths, registry
from beetroot import ports as ports_mod
from beetroot.config import InstanceConfig, PortMapping, _default_port_mappings, write_yaml
from beetroot.ports import ResolvedPort
from beetroot.registry import RedroidBackendConfig


def _pinned(**hosts: int) -> list[PortMapping]:
    """Return the seeded well-known mappings with explicit host overrides applied."""
    return [
        PortMapping(service=m.service, guest=m.guest, host=hosts.get(m.service or ""))
        for m in _default_port_mappings()
    ]


def _make_instance(base: Path, name: str, ports: list[PortMapping] | None = None) -> Path:
    """Create an instance dir at ``base/name`` with a minimal beetroot.yaml."""
    root = base / name
    root.mkdir(parents=True)
    cfg = InstanceConfig() if ports is None else InstanceConfig(ports=ports)
    write_yaml(root / "beetroot.yaml", cfg)
    return root


def _seed(base: Path, name: str, ports: list[PortMapping] | None = None) -> Path:
    """Create an instance dir and register it (auto-allocating index). Returns the path."""
    root = _make_instance(base, name, ports)
    registry.add_allocating(name, root)
    return root


def _resolved(*ports: ResolvedPort) -> list[ResolvedPort]:
    """Build a resolved-port list for the find_port_collision tests."""
    return list(ports)


class TestRegistryAdd:
    def test_add_creates_registry(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        assert paths.user_registry_file().exists()

    def test_add_and_get(self, isolated_registry: Path, tmp_path: Path) -> None:
        root = _seed(tmp_path, "alpha")
        entry = registry.get("alpha")
        assert entry is not None
        assert entry.index == 0
        assert isinstance(entry.backend, RedroidBackendConfig)
        assert entry.backend.absolute_path == str(root)

    def test_add_stores_created_at(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        entry = registry.get("alpha")
        assert entry is not None
        assert entry.created_at is not None

    def test_add_duplicate_raises(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        with pytest.raises(ValueError, match="already in registry"):
            registry.add_allocating("alpha", tmp_path / "alpha")

    def test_add_multiple_instances(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        alpha = registry.get("alpha")
        bravo = registry.get("bravo")
        assert alpha is not None
        assert bravo is not None
        assert alpha.index == 0
        assert bravo.index == 1

    def test_add_over_cap_index_raises_at_registration(
        self, isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A registry already full to the stride cap must reject the next
        # allocation AT registration — not later, when every port resolution
        # crashes on the over-cap index (#267). Shrink the cap so filling it
        # is cheap: indices 0 and 1 fill it, so allocating a third fails.
        monkeypatch.setattr(ports_mod, "_MAX_PORT_INDEX", 1)
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        with pytest.raises(ValueError, match="exceeds the maximum supported index"):
            registry.add_allocating("charlie", tmp_path / "charlie")
        # The rejected instance was NOT written — the registry is unmutated.
        assert registry.get("charlie") is None
        assert set(registry.list_instances()) == {"alpha", "bravo"}


class TestRegistryGet:
    def test_get_missing_returns_none(self, isolated_registry: Path) -> None:
        assert registry.get("ghost") is None

    def test_get_missing_with_no_registry_returns_none(self, isolated_registry: Path) -> None:
        assert not paths.user_registry_file().exists()
        assert registry.get("ghost") is None


class TestInstancePath:
    def test_lookup_returns_path(self, isolated_registry: Path, tmp_path: Path) -> None:
        root = _seed(tmp_path, "alpha")
        assert registry.instance_path("alpha") == root

    def test_unknown_name_raises(self, isolated_registry: Path) -> None:
        with pytest.raises(registry.RegistryError, match="ghost"):
            registry.instance_path("ghost")


class TestRegistryRemove:
    def test_remove_deletes_entry(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        registry.remove("alpha")
        assert registry.get("alpha") is None

    def test_remove_nonexistent_is_noop(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        registry.remove("ghost")
        assert registry.get("alpha") is not None

    def test_remove_without_registry_is_noop(self, isolated_registry: Path) -> None:
        registry.remove("ghost")


class TestListInstances:
    def test_empty_when_no_registry(self, isolated_registry: Path) -> None:
        assert registry.list_instances() == {}

    def test_returns_all_added(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        instances = registry.list_instances()
        assert set(instances.keys()) == {"alpha", "bravo"}

    def test_empty_after_remove_all(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        registry.remove("alpha")
        assert registry.list_instances() == {}


class TestUsedIndices:
    def test_empty_when_no_registry(self, isolated_registry: Path) -> None:
        assert registry.used_indices() == set()

    def test_returns_all_indices(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        assert registry.used_indices() == {0, 1}

    def test_freed_index_is_removed(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        registry.remove("alpha")
        assert registry.used_indices() == {1}


class TestAllResolvedHostPorts:
    def test_empty_registry_returns_empty(self, isolated_registry: Path) -> None:
        assert registry.all_resolved_host_ports() == {}

    def test_single_instance_uses_stride_defaults(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha")
        assert registry.all_resolved_host_ports() == {
            "alpha": {5555, 27042, 27043},
        }

    def test_multiple_instances_each_their_own_ports(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        _seed(tmp_path, "charlie")
        assert registry.all_resolved_host_ports() == {
            "alpha": {5555, 27042, 27043},
            "bravo": {5565, 27052, 27053},
            "charlie": {5575, 27062, 27063},
        }

    def test_override_wins_over_stride(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha", _pinned(adb=9000))
        result = registry.all_resolved_host_ports()
        assert result["alpha"] == {9000, 27042, 27043}

    def test_includes_arbitrary_mappings(self, isolated_registry: Path, tmp_path: Path) -> None:
        # Arbitrary host ports must be enumerated too, so a cross-instance
        # clash over an arbitrary port is caught (issue #108).
        ports = [*_default_port_mappings(), PortMapping(guest=8080, host=9090)]
        _seed(tmp_path, "alpha", ports)
        result = registry.all_resolved_host_ports()
        assert 9090 in result["alpha"]

    def test_poisoned_row_fallback_keeps_explicit_pins(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # A config that validates but fails port resolution must not have its
        # real pinned host ports discarded by the fallback. Here ``x`` is
        # pinned to 5555 — exactly adb's index-0 stride default — so resolution
        # self-collides and raises; ``y`` is pinned to a distinct 6000.
        poisoned = [
            PortMapping(service="adb", guest=5555),
            PortMapping(service="x", guest=8080, host=5555),
            PortMapping(service="y", guest=9090, host=6000),
        ]
        _seed(tmp_path, "alpha", poisoned)

        # The scan must not crash on the poisoned row (orphan-behavior preserved).
        result = registry.all_resolved_host_ports()

        # The real explicit pin survives the fallback (#216) ...
        assert 6000 in result["alpha"]
        # ... and so do the stride defaults the fallback conservatively unions in.
        assert set(ports_mod.ports_for_index(0).values()) <= result["alpha"]


class TestFindPortCollision:
    def test_empty_others_returns_none(self) -> None:
        new_ports = _resolved(ResolvedPort("adb", 5555, 5555))
        assert registry.find_port_collision(new_ports, {}) is None

    def test_no_collision_returns_none(self) -> None:
        new_ports = _resolved(ResolvedPort("adb", 5555, 5555))
        others = {"bravo": {5565, 27052, 27053}}
        assert registry.find_port_collision(new_ports, others) is None

    def test_adb_collision_returns_tuple(self) -> None:
        new_ports = _resolved(
            ResolvedPort("adb", 5555, 5555),
            ResolvedPort("frida", 27042, 9000),
        )
        others = {"bravo": {5555, 27052, 27053}}
        result = registry.find_port_collision(new_ports, others)
        assert result == (5555, "bravo", "adb")

    def test_frida_collision_returns_tuple(self) -> None:
        new_ports = _resolved(
            ResolvedPort("adb", 5555, 8000),
            ResolvedPort("frida", 27042, 27042),
        )
        others = {"bravo": {5565, 27042, 27053}}
        result = registry.find_port_collision(new_ports, others)
        assert result == (27042, "bravo", "frida")

    def test_arbitrary_unlabelled_collision_reports_none_service(self) -> None:
        new_ports = _resolved(ResolvedPort(None, 8080, 9090))
        others = {"bravo": {9090}}
        result = registry.find_port_collision(new_ports, others)
        assert result == (9090, "bravo", "None")

    def test_first_collision_among_many_is_returned(self) -> None:
        new_ports = _resolved(
            ResolvedPort("adb", 5555, 5555),
            ResolvedPort("frida", 27042, 27042),
        )
        others = {
            "bravo": {5555, 27052, 27053},
            "charlie": {5575, 27042, 27063},
        }
        result = registry.find_port_collision(new_ports, others)
        assert result is not None
        _port, _name, kind = result
        assert kind in {"adb", "frida"}


class TestRoundtrip:
    def test_add_remove_readd_same_index(self, isolated_registry: Path, tmp_path: Path) -> None:
        root = _make_instance(tmp_path, "alpha")
        # First allocation picks index 0 (lowest free).
        registry.add_allocating("alpha", root)
        registry.remove("alpha")
        # After removal, index 0 is free again — re-allocation picks it.
        registry.add_allocating("alpha", root)
        entry = registry.get("alpha")
        assert entry is not None
        assert entry.index == 0

    def test_sequential_add_remove_add(self, isolated_registry: Path, tmp_path: Path) -> None:
        # alpha→0, bravo→1; remove alpha frees 0; charlie picks 0 (lowest free).
        _seed(tmp_path, "alpha")
        _seed(tmp_path, "bravo")
        registry.remove("alpha")
        _seed(tmp_path, "charlie")
        assert registry.used_indices() == {0, 1}


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
        # The legacy-registry hint is emitted on stderr (not stdout) so
        # it doesn't mangle JSON output streams. Mirrors the
        # v0.2-registry-at-cwd hint's channel.
        err = capsys.readouterr().err
        assert "register" in err
        assert ".bak" in err

    def test_v2_registry_renamed_to_bak(
        self, isolated_registry: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # T1's 2 → 3 migration. v0.3 registries had no
        # ``backend.kind`` discriminator and a flat
        # ``absolute_path`` field; the new strict validator rejects
        # them, so they get backed up and a fresh v3 file is emitted
        # — exactly the v1-readers fall-through pattern v0.3 used.
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        v2 = {
            "version": 2,
            "instances": {
                "alpha": {
                    "absolute_path": "/tmp/alpha",
                    "index": 0,
                    "created_at": "2025-01-01T00:00:00",
                }
            },
        }
        path.write_text(json.dumps(v2))
        registry._LEGACY_HINT_PRINTED = False

        # Read AND then write: list_instances() takes a shared lock
        # (doesn't bootstrap a fresh file); a subsequent ``add`` writes
        # under the exclusive lock and produces the v3-shaped doc on
        # disk, matching the v0.2 → v0.3 path.
        result = registry.list_instances()
        assert result == {}
        registry.add_allocating("placeholder", Path("/tmp/placeholder"))
        registry.remove("placeholder")

        bak = path.with_suffix(path.suffix + ".bak")
        assert bak.exists()
        # The fresh v3 doc is emitted on the first exclusive-lock
        # write after the migration.
        assert path.exists()
        new = json.loads(path.read_text())
        assert new["version"] == 3
        assert new["instances"] == {}
        err = capsys.readouterr().err
        assert "register" in err
        assert ".bak" in err

    def test_legacy_hint_fires_once_per_process(
        self, isolated_registry: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The migration hint should dedup the same way the v0.2 cwd
        # hint does (cascading reads in a single ``beetroot ls`` would
        # otherwise blast stderr). Each ``_read`` call has to hit the
        # legacy-fall-through path, so we re-write the broken file
        # before the second call — without that, the file is renamed
        # to .bak after the first call and subsequent reads hit the
        # ``not path.exists()`` fast-path without re-running the
        # validation.
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        registry._LEGACY_HINT_PRINTED = False

        path.write_text(json.dumps({"version": 2, "instances": {}}))
        registry._read(path)
        # The first _read renamed the file to .bak; re-stage a broken
        # file so the second _read takes the same fall-through and
        # hits the dedup branch.
        path.with_suffix(path.suffix + ".bak").unlink()
        path.write_text(json.dumps({"version": 2, "instances": {}}))
        registry._read(path)

        err = capsys.readouterr().err
        assert err.count("renamed to") == 1

    def test_v3_registry_loads_directly(self, isolated_registry: Path, tmp_path: Path) -> None:
        _seed(tmp_path, "alpha")
        # Subsequent reads must succeed without "migrating".
        assert "alpha" in registry.list_instances()

    def test_unparseable_registry_falls_through_to_empty(self, isolated_registry: Path) -> None:
        # A corrupt / non-JSON registry file fails strict validation
        # AND fails the version-extraction probe; both branches should
        # produce a backup + empty registry rather than crashing.
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not json at all")
        registry._LEGACY_HINT_PRINTED = False

        result = registry.list_instances()
        assert result == {}
        bak = path.with_suffix(path.suffix + ".bak")
        assert bak.exists()


class TestAddAllocatingBackendForms:
    """``add_allocating`` accepts a ``backend=<BackendConfig>`` keyword arg."""

    def test_add_allocating_with_adb_backend_kwarg(
        self,
        isolated_registry: Path,
    ) -> None:
        cfg = registry.AdbBackendConfig(serial="emulator-5554")
        idx = registry.add_allocating("phone", backend=cfg)
        assert idx == 0
        meta = registry.get("phone")
        assert meta is not None
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == "emulator-5554"

    def test_add_allocating_without_any_args_raises(
        self,
        isolated_registry: Path,
    ) -> None:
        with pytest.raises(ValueError, match="absolute_path"):
            registry.add_allocating("orphan")

    def test_adb_entry_round_trips_through_json(
        self,
        isolated_registry: Path,
    ) -> None:
        # Discriminated-union persistence — write an adb row, re-read,
        # confirm the union picks the right arm.
        cfg = registry.AdbBackendConfig(serial="emulator-5554")
        registry.add_allocating("phone", backend=cfg)
        meta = registry.get("phone")
        assert meta is not None
        assert meta.backend.kind == "adb"
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == "emulator-5554"

    def test_instance_path_rejects_adb_backend(
        self,
        isolated_registry: Path,
    ) -> None:
        cfg = registry.AdbBackendConfig(serial="emulator-5554")
        registry.add_allocating("phone", backend=cfg)
        with pytest.raises(registry.RegistryError, match="adb"):
            registry.instance_path("phone")


class TestReconcileBackendKind:
    def test_redroid_to_vm(self, isolated_registry: Path) -> None:
        _make_instance(isolated_registry, "alpha")
        registry.add_allocating(
            "alpha",
            backend=RedroidBackendConfig(absolute_path=str(isolated_registry / "alpha")),
        )
        changed = registry.reconcile_backend_kind("alpha", "vm")
        assert changed is True
        meta = registry.get("alpha")
        assert meta is not None
        assert meta.backend.kind == "vm"
        assert isinstance(meta.backend, registry.VmBackendConfig)
        assert meta.backend.absolute_path == str(isolated_registry / "alpha")

    def test_vm_to_redroid(self, isolated_registry: Path) -> None:
        _make_instance(isolated_registry, "alpha")
        registry.add_allocating(
            "alpha",
            backend=registry.VmBackendConfig(absolute_path=str(isolated_registry / "alpha")),
        )
        changed = registry.reconcile_backend_kind("alpha", "auto")
        assert changed is True
        meta = registry.get("alpha")
        assert meta is not None
        assert meta.backend.kind == "redroid"

    def test_no_change_when_already_matching(self, isolated_registry: Path) -> None:
        _make_instance(isolated_registry, "alpha")
        registry.add_allocating(
            "alpha",
            backend=RedroidBackendConfig(absolute_path=str(isolated_registry / "alpha")),
        )
        assert registry.reconcile_backend_kind("alpha", "host") is False

    def test_missing_row_returns_false(self, isolated_registry: Path) -> None:
        assert registry.reconcile_backend_kind("ghost", "vm") is False

    def test_non_directory_backed_left_untouched(self, isolated_registry: Path) -> None:
        registry.add_allocating("phone", backend=registry.AdbBackendConfig(serial="x"))
        assert registry.reconcile_backend_kind("phone", "vm") is False
        meta = registry.get("phone")
        assert meta is not None
        assert meta.backend.kind == "adb"


class TestVmBackendDirectoryBacked:
    def test_instance_path_resolves_vm(self, isolated_registry: Path) -> None:
        _make_instance(isolated_registry, "alpha")
        registry.add_allocating(
            "alpha",
            backend=registry.VmBackendConfig(absolute_path=str(isolated_registry / "alpha")),
        )
        assert registry.instance_path("alpha") == isolated_registry / "alpha"

    def test_all_resolved_host_ports_includes_vm(self, isolated_registry: Path) -> None:
        _make_instance(isolated_registry, "alpha")
        registry.add_allocating(
            "alpha",
            backend=registry.VmBackendConfig(absolute_path=str(isolated_registry / "alpha")),
        )
        all_ports = registry.all_resolved_host_ports()
        assert "alpha" in all_ports
        assert 5555 in all_ports["alpha"]


class TestWriteDurability:
    """A crash racing a registry write must not reset instances.json (#203).

    ``_write`` published the new payload with no fsync, so a power loss racing
    the rename could surface a zero-length ``instances.json`` — which ``_read``
    amplifies into a silent backup-and-return-empty, dropping every instance's
    port index + path mapping. The fix fsyncs the temp file *before* the rename
    and the parent directory *after* it.
    """

    def test_write_fsyncs_tmp_then_replaces_then_fsyncs_parent_dir(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drive a real registry mutation and record the ordering of os.fsync
        # and Path.replace. The temp-file fsync must precede the replace, and
        # the parent-dir fsync must follow it.
        recorder = Mock()

        real_fsync = os.fsync
        real_replace = Path.replace

        def traced_fsync(fd: int) -> None:
            recorder("fsync")
            real_fsync(fd)

        def traced_replace(self: Path, target: Path) -> Path:
            recorder("replace")
            return real_replace(self, target)

        monkeypatch.setattr("beetroot.registry.os.fsync", traced_fsync)
        monkeypatch.setattr(Path, "replace", traced_replace)

        root = isolated_registry / "alpha"
        root.mkdir()
        registry.add_allocating("alpha", root)

        names = [c.args[0] for c in recorder.call_args_list]
        # Exactly two fsyncs per write: the temp file fd, then the parent dir.
        assert names.count("fsync") == 2
        assert names.count("replace") == 1
        # tmp-file fsync BEFORE replace; parent-dir fsync AFTER replace.
        assert names == ["fsync", "replace", "fsync"]
        assert recorder.call_args_list[0] == call("fsync")

    def test_write_round_trips_unchanged_through_durable_path(
        self, isolated_registry: Path
    ) -> None:
        # Behavior is unchanged: what _write persists, _read reads back.
        root = isolated_registry / "bravo"
        root.mkdir()
        registry.add_allocating("bravo", root)

        meta = registry.get("bravo")
        assert meta is not None
        assert isinstance(meta.backend, RedroidBackendConfig)
        assert Path(meta.backend.absolute_path) == root

    def test_orphan_tmp_cleaned_up_when_replace_raises(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the atomic replace raises after the tmp file is written (and
        # fsynced), the finally block must unlink the orphan tmp file so no
        # debris is left — and the parent-dir fsync (post-replace) is skipped.
        real_replace = Path.replace

        def exploding_replace(self: Path, target: Path) -> Path:
            if self.suffix == ".tmp":
                raise RuntimeError("rename failed")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", exploding_replace)

        root = isolated_registry / "charlie"
        root.mkdir()
        with pytest.raises(RuntimeError, match="rename failed"):
            registry.add_allocating("charlie", root)

        config_dir = paths.user_registry_file().parent
        assert list(config_dir.glob("*.tmp")) == []
