"""Backend-registry dispatch + DeviceBackend Protocol conformance (T1).

T1 introduces the backend registry (:mod:`beetroot.backends`) that maps
a ``kind`` discriminator to a concrete class. ``Manager.resolve``
dispatches via this registry. Third-party backends register
programmatically (in-process) or via the
``[project.entry-points."beetroot.backends"]`` group; both paths are
exercised here.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from beetroot import api, backends, paths, registry
from beetroot.registry import (
    AdbBackendConfig,
    BackendConfig,
    InstanceMeta,
    RegistryFile,
)


class _StubBackend:
    """Minimal class satisfying :class:`api.DeviceBackend` for dispatch tests."""

    def __init__(self, name: str, cfg: AdbBackendConfig) -> None:
        self._name = name
        self._cfg = cfg

    @classmethod
    def from_meta(cls, name: str, backend_config: BackendConfig) -> _StubBackend:
        assert isinstance(backend_config, AdbBackendConfig)
        return cls(name, backend_config)

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "test-stub"

    @property
    def adb_address(self) -> str:
        return self._cfg.serial

    @property
    def frida_address(self) -> str:
        return "stub:0"

    @property
    def is_available(self) -> bool:
        return True

    def install_frida(self, version: str | None = None) -> None:
        del version

    def shell(self) -> int:
        return 0

    def frida_cli(self, args: Sequence[str]) -> int:
        del args
        return 0


@pytest.fixture
def _stub_registered() -> Iterator[None]:
    """Register the stub backend for one test, then remove it."""
    backends.register_backend("test-stub", _StubBackend)
    try:
        yield
    finally:
        backends._BACKEND_REGISTRY.pop("test-stub", None)


class TestProtocolConformance:
    """Both :class:`Instance` and an adb-shaped stub satisfy the Protocol."""

    def test_instance_satisfies_protocol(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # ``Instance`` is the v0.3 backend; it has to keep satisfying
        # the expanded :class:`DeviceBackend` Protocol after T1's
        # additions (``name``, ``kind``, ``shell``, ``frida_cli``).
        root = tmp_path / "alpha"
        root.mkdir()
        (root / "beetroot.yaml").write_text(
            "api_version: 3\nandroid:\n  version: 14\n"
        )
        registry.add_allocating("alpha", root)
        inst = api.Instance.load("alpha")
        assert isinstance(inst, api.DeviceBackend)

    def test_stub_satisfies_protocol(self) -> None:
        stub = _StubBackend("phone", AdbBackendConfig(serial="emulator-5554"))
        assert isinstance(stub, api.DeviceBackend)

    def test_instance_kind_is_redroid(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # The new ``kind`` property defaults to the redroid
        # discriminator; this is the runtime-readable echo of the
        # registry's ``backend.kind``.
        root = tmp_path / "alpha"
        root.mkdir()
        (root / "beetroot.yaml").write_text(
            "api_version: 3\nandroid:\n  version: 14\n"
        )
        registry.add_allocating("alpha", root)
        assert api.Instance.load("alpha").kind == "redroid"


class TestRegisterBackend:
    def test_register_and_get(self) -> None:
        backends.register_backend("test-stub-rg", _StubBackend)
        try:
            cls = backends.get_backend("test-stub-rg")
            assert cls is _StubBackend
        finally:
            backends._BACKEND_REGISTRY.pop("test-stub-rg", None)

    def test_duplicate_registration_raises(self) -> None:
        backends.register_backend("dup-stub", _StubBackend)
        try:
            with pytest.raises(
                backends.BackendRegistrationError, match="already registered"
            ):
                backends.register_backend("dup-stub", _StubBackend)
        finally:
            backends._BACKEND_REGISTRY.pop("dup-stub", None)

    def test_register_without_from_meta_raises(self) -> None:
        class _NotABackend:
            pass

        with pytest.raises(
            backends.BackendRegistrationError, match="from_meta"
        ):
            backends.register_backend(
                "broken", _NotABackend,  # type: ignore[arg-type]
            )

    def test_get_unknown_kind_raises(self) -> None:
        with pytest.raises(KeyError):
            backends.get_backend("never-registered-anywhere")

    def test_registered_kinds_includes_builtin(self) -> None:
        # The redroid backend is registered at import time. Whatever
        # else has been added in this session is sorted in.
        kinds = backends.registered_kinds()
        assert "redroid" in kinds


class TestManagerResolve:
    """``Manager.resolve(name)`` dispatches via the backend registry."""

    def test_resolve_redroid_returns_instance(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "alpha"
        root.mkdir()
        (root / "beetroot.yaml").write_text(
            "api_version: 3\nandroid:\n  version: 14\n"
        )
        registry.add_allocating("alpha", root)
        resolved = api.Manager.resolve("alpha")
        assert isinstance(resolved, api.Instance)
        assert resolved.name == "alpha"

    @pytest.mark.usefixtures("_stub_registered")
    def test_resolve_stub_dispatches(
        self, isolated_registry: Path,
    ) -> None:
        # Wire a fake registry row for kind ``"test-stub"`` and assert
        # ``Manager.resolve`` constructs the stub class via its
        # ``from_meta`` classmethod.
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
        # Hand-write the file; the ``adb`` kind in the BackendConfig
        # union is the closest match for our stub-construction path.
        registry._write(path, doc)
        # Re-register the stub under "adb" so resolve finds it.
        backends._BACKEND_REGISTRY.pop("adb", None)
        backends.register_backend("adb", _StubBackend)
        try:
            resolved = api.Manager.resolve("phone")
        finally:
            backends._BACKEND_REGISTRY.pop("adb", None)
        assert isinstance(resolved, _StubBackend)
        assert resolved.name == "phone"

    def test_resolve_unknown_name_raises(
        self, isolated_registry: Path
    ) -> None:
        with pytest.raises(api.InstanceNotFoundError, match="ghost"):
            api.Manager.resolve("ghost")

    def test_resolve_missing_backend_class_raises(
        self, isolated_registry: Path
    ) -> None:
        # An adb-kind row whose backend class hasn't been registered
        # surfaces a friendly InstanceNotFoundError, not a bare KeyError.
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
        # Snapshot the registry, remove "adb", and restore after.
        original = backends._BACKEND_REGISTRY.pop("adb", None)
        try:
            with pytest.raises(api.InstanceNotFoundError, match="adb"):
                api.Manager.resolve("phone")
        finally:
            if original is not None:
                backends._BACKEND_REGISTRY["adb"] = original


class TestInstanceFromMeta:
    """Construction via the dispatcher-friendly ``from_meta`` classmethod."""

    def test_from_meta_redroid(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "alpha"
        root.mkdir()
        (root / "beetroot.yaml").write_text(
            "api_version: 3\nandroid:\n  version: 14\n"
        )
        registry.add_allocating("alpha", root)
        backend = registry.RedroidBackendConfig(absolute_path=str(root))
        inst = api.Instance.from_meta("alpha", backend)
        assert inst.root == root

    def test_from_meta_adb_raises(self) -> None:
        backend = AdbBackendConfig(serial="emulator-5554")
        with pytest.raises(
            api.InstanceNotFoundError, match="adb"
        ):
            api.Instance.from_meta("phone", backend)


class TestEntryPointDiscovery:
    def test_entry_point_load_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reset the loaded flag and stub ``entry_points`` to count calls.
        monkeypatch.setattr(backends, "_ENTRY_POINTS_LOADED", False)
        fake = MagicMock(return_value=[])
        monkeypatch.setattr(backends, "entry_points", fake)
        backends._load_entry_point_backends()
        backends._load_entry_point_backends()
        backends._load_entry_point_backends()
        assert fake.call_count == 1

    def test_entry_point_registers_third_party(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Synthesise a third-party entry point pointing at the stub.
        fake_ep = MagicMock()
        fake_ep.name = "ep-stub"
        fake_ep.load.return_value = _StubBackend
        monkeypatch.setattr(backends, "_ENTRY_POINTS_LOADED", False)
        monkeypatch.setattr(
            backends, "entry_points", lambda group: [fake_ep],
        )
        backends._BACKEND_REGISTRY.pop("ep-stub", None)
        try:
            cls = backends.get_backend("ep-stub")
            assert cls is _StubBackend
        finally:
            backends._BACKEND_REGISTRY.pop("ep-stub", None)

    def test_entry_point_skips_already_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-register the same kind, then run the entry-point loader.
        # The loader must NOT raise BackendRegistrationError just
        # because the in-tree path beat it to the punch.
        backends.register_backend("dup-ep", _StubBackend)
        try:
            fake_ep = MagicMock()
            fake_ep.name = "dup-ep"
            fake_ep.load.return_value = _StubBackend
            monkeypatch.setattr(backends, "_ENTRY_POINTS_LOADED", False)
            monkeypatch.setattr(
                backends, "entry_points", lambda group: [fake_ep],
            )
            backends._load_entry_point_backends()
        finally:
            backends._BACKEND_REGISTRY.pop("dup-ep", None)


class TestBuiltinRegistration:
    def test_register_builtin_is_idempotent(self) -> None:
        # ``_register_builtin_backends`` is called at module import; a
        # second call must NOT raise BackendRegistrationError because
        # ``redroid`` is already there. The ``in`` guard is the
        # branch-coverage target.
        backends._register_builtin_backends()


class TestBackendCapabilityError:
    def test_is_runtime_error(self) -> None:
        # Subclass check so callers can catch the broader stdlib type.
        assert issubclass(api.BackendCapabilityError, RuntimeError)

    def test_exposed_via_top_level_import(self) -> None:
        import beetroot

        assert beetroot.BackendCapabilityError is api.BackendCapabilityError
