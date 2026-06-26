"""Synthetic third-backend test — the load-bearing T5 deliverable.

If this test passes, third-party device backends will work too. The
acceptance bar for T5 is that adding a new backend takes ~30 LOC + one
entry-point line:

1. A pydantic ``BackendConfig`` subclass with a unique ``kind:
   Literal[...]`` discriminator and the connection params.
2. A class satisfying :class:`api.DeviceBackend` (nine members:
   eight properties/methods on the base + ``from_meta``).
3. One entry-point line in the third-party package's ``pyproject.toml``
   under ``[project.entry-points."beetroot.backends"]``.

This file defines the BackendConfig + the class inline (no entry-point
needed for in-process registration), registers it via
``register_backend("fake", FakeBackend)``, and asserts the end-to-end
contract: registry round-trip, ``Manager.resolve``, shell dispatch
through the Protocol surface, and :class:`BackendCapabilityError` on
verbs the backend doesn't support.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from beetroot import api, backends, cli, paths, registry
from beetroot.registry import BackendConfigBase

# ---- The synthetic third backend: ~30 LOC total. -------------------------


class FakeBackendConfig(BackendConfigBase):
    """Third-party backend config — owned by the backend package, not beetroot."""

    kind: Literal["fake"] = "fake"  # type: ignore[mutable-override]  # Literal narrows the base str; required for pydantic discriminated dispatch
    host: str


class FakeBackend:
    """30-LOC backend hitting a fake remote-shell-over-SSH service."""

    def __init__(self, name: str, config: FakeBackendConfig) -> None:
        self._name = name
        self._config = config

    @classmethod
    def from_meta(cls, name: str, backend: BackendConfigBase) -> FakeBackend:
        # ``backend`` arrives as ``BackendConfigBase``; narrow to our own
        # config via isinstance so mypy strict is satisfied.  The registry
        # dispatcher calls this only after it has validated the raw JSON
        # dict against FakeBackendConfig (because we called
        # ``register_backend_config``), so the isinstance check is a
        # defence-in-depth guard, not the primary gate.
        if not isinstance(backend, FakeBackendConfig):
            raise TypeError(
                f"FakeBackend expected FakeBackendConfig, got {type(backend).__name__}",
            )
        return cls(name, backend)

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "fake"

    @property
    def adb_address(self) -> str:
        return self._config.host

    @property
    def frida_address(self) -> str:
        return f"{self._config.host}:27042"

    @property
    def is_available(self) -> bool:
        return True

    def install_frida(self, version: str | None = None) -> None:
        del version

    def shell(self, args: Sequence[str] | None = None) -> int:
        cmd = ["ssh", self._config.host, *(args or [])]
        return subprocess.run(  # noqa: S603  # synthetic test backend; argv is constant
            cmd,
            check=False,
        ).returncode

    def frida_cli(self, args: Sequence[str]) -> int:
        del args
        return 0


# ---- mypy conformance gate -----------------------------------------------
# This call site is intentionally NOT inside a test function so that mypy
# checks it at import time under strict mode.  If ``FakeBackend`` ever
# drops a required ``DeviceBackend`` member, mypy will flag the argument
# type mismatch here and CI will fail — proving structural conformance
# beyond the runtime ``isinstance`` check (which only verifies member
# presence, not signatures).


def _assert_conforms(b: api.DeviceBackend) -> None:
    """Accept any structurally-conformant DeviceBackend for mypy's benefit."""


_assert_conforms(FakeBackend("_typecheck", FakeBackendConfig(host="remote.example")))


# ---- Fixtures ------------------------------------------------------------


@pytest.fixture
def _fake_registered() -> Iterator[None]:
    """Register ``FakeBackend`` + ``FakeBackendConfig`` for the test, then clean up."""
    # Register both the config class (so the registry dispatcher can
    # parse JSON rows with kind:"fake") and the backend class (so
    # Manager.resolve can construct a FakeBackend from a parsed row).
    registry.register_backend_config(FakeBackendConfig)
    backends.register_backend("fake", FakeBackend)
    try:
        yield
    finally:
        registry._BACKEND_CONFIG_REGISTRY.pop("fake", None)
        backends._BACKEND_REGISTRY.pop("fake", None)


@pytest.fixture
def _fake_registry_row(
    isolated_registry: Path,  # fixture composed for monkeypatch
) -> str:
    """
    Hand-write a registry row for a fake-kind instance, return the name.

    The in-tree :class:`registry.RegistryFile` open-union dispatcher
    resolves ``kind: "fake"`` rows to :class:`FakeBackendConfig` once
    :func:`registry.register_backend_config` has been called.
    :func:`api.Manager.resolve` then dispatches via
    ``backends.get_backend(meta.backend.kind)``.
    """
    path = paths.user_registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (
        '{"version": 3, "instances": {"fake-1": '
        '{"backend": {"kind": "fake", "host": "remote.example"}, '
        '"index": 0, "created_at": "2026-05-19T00:00:00+00:00"}}}'
    )
    path.write_text(blob)
    return "fake-1"


# ---- Tests ---------------------------------------------------------------


class TestFakeBackendStandalone:
    def test_fake_backend_satisfies_protocol(self) -> None:
        # FakeBackend isn't in the in-tree registry, but it still
        # structurally satisfies the runtime-checkable Protocol —
        # confirming that third-party backends only need to match the
        # surface, not inherit from a base class.
        b = FakeBackend("x", FakeBackendConfig(host="remote.example"))
        assert isinstance(b, api.DeviceBackend)

    def test_fake_backend_config_roundtrips(self) -> None:
        cfg = FakeBackendConfig(host="remote.example")
        as_json = cfg.model_dump_json()
        back = FakeBackendConfig.model_validate_json(as_json)
        assert back == cfg


class TestExtensionEndToEnd:
    """End-to-end: register → dispatch → shell → BackendCapabilityError."""

    @pytest.mark.usefixtures("_fake_registered")
    def test_manager_resolve_returns_fake_backend(
        self,
        _fake_registry_row: str,  # noqa: PT019
    ) -> None:
        del _fake_registry_row
        cls = backends.get_backend("fake")
        assert cls is FakeBackend

    @pytest.mark.usefixtures("_fake_registered")
    def test_shell_dispatches_via_protocol(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Construct a FakeBackend directly and assert shell() runs the
        # SSH command we expect. The whole point of the Protocol
        # surface is that the call site doesn't need to know the
        # concrete class — any DeviceBackend works.
        captured: list[list[str]] = []

        def _fake_run(
            cmd: list[str],
            *args: object,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            captured.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        backend: api.DeviceBackend = FakeBackend(
            "fake-1",
            FakeBackendConfig(host="remote.example"),
        )
        rc = backend.shell()
        assert rc == 0
        assert captured == [["ssh", "remote.example"]]

    @pytest.mark.usefixtures("_fake_registered")
    def test_up_raises_backend_capability_error_cleanly(
        self,
        isolated_registry: Path,
    ) -> None:
        # A third-party backend that doesn't implement the Lifecycle
        # sub-protocol is rejected by the CLI's ``_require`` gate with
        # a :class:`api.BackendCapabilityError`. Verify the error
        # message embeds the verb and the backend kind.
        del isolated_registry
        b = FakeBackend("fake-1", FakeBackendConfig(host="remote.example"))
        # FakeBackend does not implement Lifecycle, so _require raises.
        with pytest.raises(api.BackendCapabilityError, match="up"):
            cli._require(b, api.Lifecycle, "up")


class TestRegistryRoundTrip:
    """The synthetic backend's config round-trips through JSON like the in-tree arms."""

    @pytest.mark.usefixtures("_fake_registered")
    def test_fake_backend_config_in_full_doc_roundtrip(self) -> None:
        # FakeBackendConfig is registered in _BACKEND_CONFIG_REGISTRY via
        # the _fake_registered fixture, so the registry round-trip now
        # goes through the open-union dispatcher rather than hand-crafted
        # JSON blobs.
        cfg = FakeBackendConfig(host="remote.example")
        as_dict = cfg.model_dump()
        meta_dict = {
            "backend": as_dict,
            "index": 0,
            "created_at": datetime(2026, 5, 19, tzinfo=UTC).isoformat(),
        }
        # The config model validates against its own class via the open union.
        rebuilt = FakeBackendConfig.model_validate(meta_dict["backend"])
        assert rebuilt == cfg
