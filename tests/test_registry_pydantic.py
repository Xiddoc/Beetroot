"""Pydantic-model tests for the v3 registry schema (T1).

T1's central refactor: ``registry`` migrated from ``dict[str, Any]``
end-to-end to a strict pydantic model (:class:`RegistryFile` →
:class:`InstanceMeta` → discriminated union :data:`BackendConfig`). These
tests pin the round-trip, the discriminator validation, and the
adb-shaped second variant of the union — none of which existed before
T1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from beetroot import paths, registry
from beetroot.registry import (
    AdbBackendConfig,
    InstanceMeta,
    RedroidBackendConfig,
    RegistryFile,
)


class TestRegistryFileRoundTrip:
    """Both backend-config variants round-trip through _write/_read identically."""

    def test_redroid_variant_round_trip(self, tmp_path: Path) -> None:
        original = RegistryFile(
            instances={
                "alpha": InstanceMeta(
                    backend=RedroidBackendConfig(
                        absolute_path="/var/lib/beetroot/alpha",
                        stealth_paths={"frida": "/data/local/tmp/x"},
                    ),
                    index=0,
                    created_at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
                ),
            },
        )
        path = tmp_path / "instances.json"
        # _write/_read is the canonical round-trip; pydantic's
        # model_validate_json can't dispatch the open-union backend field.
        registry._write(path, original)
        rebuilt = registry._read(path)
        alpha = rebuilt.instances["alpha"]
        assert isinstance(alpha.backend, RedroidBackendConfig)
        assert alpha.backend.absolute_path == "/var/lib/beetroot/alpha"
        assert alpha.backend.stealth_paths == {"frida": "/data/local/tmp/x"}

    def test_adb_variant_round_trip(self, tmp_path: Path) -> None:
        original = RegistryFile(
            instances={
                "phone": InstanceMeta(
                    backend=AdbBackendConfig(serial="emulator-5554"),
                    index=1,
                    created_at=datetime(2026, 5, 19, 13, 0, tzinfo=UTC),
                ),
            },
        )
        path = tmp_path / "instances.json"
        registry._write(path, original)
        rebuilt = registry._read(path)
        phone = rebuilt.instances["phone"]
        assert isinstance(phone.backend, AdbBackendConfig)
        assert phone.backend.serial == "emulator-5554"

    def test_mixed_variants_round_trip(self, tmp_path: Path) -> None:
        # The open registry holds heterogeneous values inside a single file —
        # exactly the v0.4 multi-backend story.
        original = RegistryFile(
            instances={
                "alpha": InstanceMeta(
                    backend=RedroidBackendConfig(absolute_path="/p/alpha"),
                    index=0,
                    created_at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
                ),
                "phone": InstanceMeta(
                    backend=AdbBackendConfig(serial="emulator-5554"),
                    index=1,
                    created_at=datetime(2026, 5, 19, 13, 0, tzinfo=UTC),
                ),
            },
        )
        path = tmp_path / "instances.json"
        registry._write(path, original)
        rebuilt = registry._read(path)
        assert isinstance(rebuilt.instances["alpha"].backend, RedroidBackendConfig)
        assert isinstance(rebuilt.instances["phone"].backend, AdbBackendConfig)

    def test_empty_round_trip(self, tmp_path: Path) -> None:
        original = RegistryFile()
        path = tmp_path / "instances.json"
        registry._write(path, original)
        rebuilt = registry._read(path)
        assert rebuilt.instances == {}
        assert rebuilt.version == 3


class TestDiscriminatedUnionValidation:
    """Pydantic refuses mixed-kind backend payloads via the discriminator."""

    def test_adb_kind_with_absolute_path_raises(self) -> None:
        # Pre-write a registry blob where ``kind: "adb"`` is paired
        # with a redroid-only field (``absolute_path``). The
        # discriminated union must reject it — Agent 2 B-7's lever
        # for "the registry payload knows which fields belong to
        # which backend".
        raw = json.dumps(
            {
                "version": 3,
                "instances": {
                    "phone": {
                        "backend": {
                            "kind": "adb",
                            "absolute_path": "/x",
                        },
                        "index": 0,
                        "created_at": "2026-05-19T12:00:00+00:00",
                    }
                },
            }
        )
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)

    def test_redroid_kind_with_serial_raises(self) -> None:
        raw = json.dumps(
            {
                "version": 3,
                "instances": {
                    "alpha": {
                        "backend": {
                            "kind": "redroid",
                            "serial": "emulator-5554",
                        },
                        "index": 0,
                        "created_at": "2026-05-19T12:00:00+00:00",
                    }
                },
            }
        )
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)

    def test_unknown_kind_raises(self) -> None:
        raw = json.dumps(
            {
                "version": 3,
                "instances": {
                    "alpha": {
                        "backend": {"kind": "spaceship", "serial": "x"},
                        "index": 0,
                        "created_at": "2026-05-19T12:00:00+00:00",
                    }
                },
            }
        )
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)

    def test_missing_kind_raises(self) -> None:
        raw = json.dumps(
            {
                "version": 3,
                "instances": {
                    "alpha": {
                        "backend": {"absolute_path": "/x"},
                        "index": 0,
                        "created_at": "2026-05-19T12:00:00+00:00",
                    }
                },
            }
        )
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)

    def test_extra_top_level_field_raises(self) -> None:
        raw = json.dumps(
            {
                "version": 3,
                "instances": {},
                "rogue": "unknown",
            }
        )
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)

    def test_extra_meta_field_raises(self) -> None:
        raw = json.dumps(
            {
                "version": 3,
                "instances": {
                    "alpha": {
                        "backend": {"kind": "redroid", "absolute_path": "/x"},
                        "index": 0,
                        "created_at": "2026-05-19T12:00:00+00:00",
                        "rogue": True,
                    }
                },
            }
        )
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)

    def test_wrong_version_raises(self) -> None:
        raw = json.dumps({"version": 99, "instances": {}})
        with pytest.raises(ValidationError):
            RegistryFile.model_validate_json(raw)


class TestInstancePathBackendDispatch:
    """``registry.instance_path`` only resolves redroid-kind entries."""

    def test_adb_kind_raises(self, isolated_registry: Path) -> None:
        # Hand-craft an adb-backed registry row so we can exercise the
        # error branch without waiting for T5's AdbDevice plumbing.
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = RegistryFile(
            instances={
                "phone": InstanceMeta(
                    backend=AdbBackendConfig(serial="emulator-5554"),
                    index=0,
                    created_at=datetime(2026, 5, 19, tzinfo=UTC),
                ),
            },
        )
        registry._write(path, doc)
        with pytest.raises(registry.RegistryError, match="adb"):
            registry.instance_path("phone")


class TestAllResolvedPortsIncludesAdb:
    """Adb-kind rows are included in port resolution (B4 fix: prevents Frida collision)."""

    def test_adb_row_is_included(self, isolated_registry: Path) -> None:
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = RegistryFile(
            instances={
                "phone": InstanceMeta(
                    backend=AdbBackendConfig(serial="emulator-5554"),
                    index=0,
                    created_at=datetime(2026, 5, 19, tzinfo=UTC),
                ),
            },
        )
        registry._write(path, doc)
        # B4 fix: adb-kind rows use stride-of-10 defaults (no yaml to
        # consult) and ARE included so a redroid instance can't silently
        # collide with an adopted device's Frida port.
        assert registry.all_resolved_host_ports() == {
            "phone": {5555, 27042, 27043},
        }


def _write_mixed_registry(tmp_path: Path) -> Path:
    """Write a registry containing both a redroid and an adb entry, return the redroid root.

    Ordering matters for branch-coverage: putting the adb row first
    forces every iterator over ``registry.list_instances()`` to actually
    take the ``isinstance(..., RedroidBackendConfig)`` skip branch
    before reaching the redroid row.
    """
    redroid_root = tmp_path / "alpha"
    redroid_root.mkdir()
    (redroid_root / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
    path = paths.user_registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = RegistryFile(
        instances={
            "phone": InstanceMeta(
                backend=AdbBackendConfig(serial="emulator-5554"),
                index=1,
                created_at=datetime(2026, 5, 19, tzinfo=UTC),
            ),
            "alpha": InstanceMeta(
                backend=RedroidBackendConfig(absolute_path=str(redroid_root)),
                index=0,
                created_at=datetime(2026, 5, 19, tzinfo=UTC),
            ),
        },
    )
    registry._write(path, doc)
    return redroid_root


class TestAdbRowsSkippedByConsumers:
    """Every consumer that iterates the registry skips non-redroid rows."""

    def test_snapshot_lookup_skips_adb(self, isolated_registry: Path, tmp_path: Path) -> None:
        from beetroot import snapshot

        redroid_root = _write_mixed_registry(tmp_path)
        # ``snapshot`` operates on the redroid row; the adb-kind row
        # must be skipped silently rather than raising. T4 widened the
        # return tuple to ``(name, meta, backend)`` — the third element
        # is the narrowed ``RedroidBackendConfig`` so callers can reach
        # ``stealth_paths`` without re-isinstance-ing.
        name, meta, backend = snapshot._find_registry_entry(redroid_root)
        assert name == "alpha"
        assert isinstance(meta.backend, RedroidBackendConfig)
        assert isinstance(backend, RedroidBackendConfig)

    def test_manager_list_orphans_skips_adb(self, isolated_registry: Path, tmp_path: Path) -> None:
        from beetroot import api

        # Set up a registry where the redroid row is valid (has YAML
        # on disk) and the adb row is intrinsically non-orphan-able.
        _write_mixed_registry(tmp_path)
        # No orphans expected: the redroid row's YAML is on disk and
        # the adb row is skipped from orphan-checking entirely.
        assert api.Manager.list_orphans() == []

    def test_manager_list_skips_adb(self, isolated_registry: Path, tmp_path: Path) -> None:
        from beetroot import api

        _write_mixed_registry(tmp_path)
        instances = api.Manager.list_instances()
        assert {i.name for i in instances} == {"alpha"}

    def test_instance_from_path_skips_adb(self, isolated_registry: Path, tmp_path: Path) -> None:
        from beetroot import api

        redroid_root = _write_mixed_registry(tmp_path)
        # ``Instance.from_path`` walks the registry until it finds a
        # matching redroid-backed row. Adb rows are skipped so a
        # serial-shaped "absolute_path" stand-in can never match a
        # filesystem path.
        inst = api.Instance.from_path(redroid_root)
        assert inst.name == "alpha"

    def test_instance_load_refuses_adb_kind(self, isolated_registry: Path, tmp_path: Path) -> None:
        from beetroot import api

        _write_mixed_registry(tmp_path)
        with pytest.raises(api.InstanceNotFoundError, match="adb"):
            api.Instance.load("phone")

    def test_snapshot_restore_force_path_skips_adb(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Exercise the adb-skip branch in ``snapshot.restore``'s
        # foreign-instance refusal. The branch is taken when the
        # destination dir is non-empty AND a registered adb row
        # exists; the adb row must be ignored so we fall through to
        # the ``--force without an offending peer`` path.
        from beetroot import snapshot

        redroid_root = _write_mixed_registry(tmp_path)
        archive = snapshot.snapshot(redroid_root, tmp_path / "out")
        # Create the destination dir non-empty and rerun restore with --force.
        dest = tmp_path / "destination"
        dest.mkdir()
        (dest / "something").write_text("not empty")
        snapshot.restore(
            archive,
            dest_name="restored",
            dest_path=dest,
            force=True,
        )
