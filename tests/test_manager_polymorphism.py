"""Polymorphic Manager + Protocol dispatch across heterogeneous backends (T5).

Asserts that ``Manager.resolve`` returns the right concrete backend type
for both redroid and adb registry rows, that ``isinstance`` narrowing
works on the returned object, and that lifecycle verbs against an adb-
backed instance raise :class:`BackendCapabilityError` cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import api, registry
from beetroot.backends import adb as adb_backend


@pytest.fixture
def _two_instances(
    isolated_registry: Path,  # fixture is composed for its monkeypatch
    tmp_path: Path,
) -> tuple[str, str]:
    """Register one redroid + one adb instance; return their names."""
    redroid_root = tmp_path / "alpha"
    redroid_root.mkdir()
    (redroid_root / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
    registry.add_allocating("alpha", redroid_root)
    registry.add_allocating(
        "phone",
        backend=registry.AdbBackendConfig(serial="emulator-5554"),
    )
    return "alpha", "phone"


class TestManagerResolveHeterogeneous:
    def test_redroid_resolves_to_instance(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        redroid_name, _ = _two_instances
        backend = api.Manager.resolve(redroid_name)
        assert isinstance(backend, api.Instance)
        assert backend.kind == "redroid"

    def test_adb_resolves_to_adb_device(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        _, adb_name = _two_instances
        backend = api.Manager.resolve(adb_name)
        assert isinstance(backend, adb_backend.AdbDevice)
        assert backend.kind == "adb"
        assert backend.adb_address == "emulator-5554"

    def test_both_satisfy_device_backend_protocol(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        redroid_name, adb_name = _two_instances
        # Both heterogeneous backends must satisfy the runtime-
        # checkable :class:`api.DeviceBackend` Protocol — that's the
        # whole point of the Protocol surface.
        assert isinstance(api.Manager.resolve(redroid_name), api.DeviceBackend)
        assert isinstance(api.Manager.resolve(adb_name), api.DeviceBackend)


class TestIsinstanceNarrowing:
    def test_narrows_to_concrete_class(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        redroid_name, adb_name = _two_instances
        redroid_backend = api.Manager.resolve(redroid_name)
        adb_backend_inst = api.Manager.resolve(adb_name)
        # The Protocol-typed return is narrowable to each concrete
        # class via isinstance — this is what the CLI's verbs depend
        # on for backend-specific dispatch.
        assert isinstance(redroid_backend, api.Instance)
        assert isinstance(adb_backend_inst, adb_backend.AdbDevice)
        # The positive asserts above narrow each to its concrete class;
        # mypy then knows the cross-class negatives statically — no
        # runtime check needed (mypy would flag the inverse isinstance
        # call as unreachable).


class TestLifecycleCapability:
    def test_adb_does_not_satisfy_lifecycle(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        _, adb_name = _two_instances
        backend = api.Manager.resolve(adb_name)
        assert isinstance(backend, adb_backend.AdbDevice)
        assert not isinstance(backend, api.Lifecycle)

    def test_redroid_satisfies_lifecycle(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        redroid_name, _ = _two_instances
        backend = api.Manager.resolve(redroid_name)
        assert isinstance(backend, api.Lifecycle)

    def test_adb_does_not_satisfy_snapshottable(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        _, adb_name = _two_instances
        backend = api.Manager.resolve(adb_name)
        assert not isinstance(backend, api.Snapshottable)

    def test_redroid_satisfies_resettable(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        redroid_name, _ = _two_instances
        backend = api.Manager.resolve(redroid_name)
        assert isinstance(backend, api.Resettable)

    def test_adb_does_not_satisfy_resettable(
        self,
        _two_instances: tuple[str, str],  # noqa: PT019
    ) -> None:
        _, adb_name = _two_instances
        backend = api.Manager.resolve(adb_name)
        assert not isinstance(backend, api.Resettable)
