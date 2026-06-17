"""Behavior tests for the v0.6 open-registry backend-contract spec.

Covers the surfaces that were added or changed in this branch:

* :func:`registry.register_backend_config` — open-union config registration
* :class:`registry.UnresolvedBackendConfig` — opaque row preservation
* :func:`registry._parse_backend_config` — dispatch + unknown-kind fallback
* :func:`registry._registry_to_json` — opaque row byte-for-byte re-emission
* :func:`backends.reset_for_testing` — test seam
* Entry-point collision (different class for same kind)
* :meth:`api.Manager.all` — walks every resolvable backend
* :meth:`api.Manager.resolve` with :class:`registry.UnresolvedBackendConfig`
* :meth:`api.Instance.install_frida` with ``version=None`` and no frida block
* :meth:`backends.adb.AdbDevice.install_frida` with ``version=None``
* ``cli.main()`` catching :class:`api.InstanceNotFoundError` → exit 1
* ``beetroot up`` non-Instance backend echo path
* ``beetroot destroy`` orphan-path branches:
  * non-redroid orphan raises :class:`api.BackendCapabilityError`
  * prompt "n" aborts with exit 0
  * redroid orphan with existing root (compose.down + rmtree)
* Corrupted / missing-key registry JSON rows are skipped silently
* Duplicate legacy-hint suppression
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from beetroot import api, backends, cli, paths, registry
from beetroot.backends import adb as adb_backend
from beetroot.registry import (
    AdbBackendConfig,
    BackendConfigBase,
    InstanceMeta,
    RedroidBackendConfig,
    RegistryFile,
    UnresolvedBackendConfig,
)

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeBackendConfig(BackendConfigBase):
    """Synthetic third-party backend config for open-union tests."""

    kind: Literal["fake-v6"] = "fake-v6"  # type: ignore[mutable-override]
    host: str


@pytest.fixture
def _fake_config_registered() -> Iterator[None]:
    """Register _FakeBackendConfig for one test, then remove it."""
    registry._BACKEND_CONFIG_REGISTRY["fake-v6"] = _FakeBackendConfig
    try:
        yield
    finally:
        registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)


class _FakeBackend:
    """Third-party backend satisfying :class:`api.DeviceBackend`."""

    def __init__(self, name: str, cfg: _FakeBackendConfig) -> None:
        self._name = name
        self._cfg = cfg

    @classmethod
    def from_meta(cls, name: str, backend_config: BackendConfigBase) -> _FakeBackend:
        assert isinstance(backend_config, _FakeBackendConfig)
        return cls(name, backend_config)

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "fake-v6"

    @property
    def adb_address(self) -> str:
        return self._cfg.host

    @property
    def frida_address(self) -> str:
        return f"{self._cfg.host}:27042"

    @property
    def is_available(self) -> bool:
        return True

    def install_frida(self, version: str | None = None) -> None:
        del version

    def shell(self, args: Sequence[str] | None = None) -> int:
        del args
        return 0

    def frida_cli(self, args: Sequence[str]) -> int:
        del args
        return 0


@pytest.fixture
def _fake_backend_registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Register _FakeBackend + _FakeBackendConfig for one test."""
    registry._BACKEND_CONFIG_REGISTRY["fake-v6"] = _FakeBackendConfig
    monkeypatch.setitem(
        backends._BACKEND_REGISTRY,
        "fake-v6",
        _FakeBackend,
    )
    try:
        yield
    finally:
        registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)
        backends._BACKEND_REGISTRY.pop("fake-v6", None)


# ---------------------------------------------------------------------------
# register_backend_config
# ---------------------------------------------------------------------------


class TestRegisterBackendConfig:
    def test_registers_new_kind(self) -> None:
        registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)
        try:
            registry.register_backend_config(_FakeBackendConfig)
            assert registry._BACKEND_CONFIG_REGISTRY["fake-v6"] is _FakeBackendConfig
        finally:
            registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)

    def test_same_class_is_idempotent(self) -> None:
        registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)
        try:
            registry.register_backend_config(_FakeBackendConfig)
            registry.register_backend_config(_FakeBackendConfig)  # second call — no error
            assert registry._BACKEND_CONFIG_REGISTRY["fake-v6"] is _FakeBackendConfig
        finally:
            registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)

    def test_different_class_same_kind_raises(self) -> None:
        class _AnotherFakeConfig(BackendConfigBase):
            kind: Literal["fake-v6"] = "fake-v6"  # type: ignore[mutable-override]

        registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)
        try:
            registry.register_backend_config(_FakeBackendConfig)
            with pytest.raises(ValueError, match="fake-v6"):
                registry.register_backend_config(_AnotherFakeConfig)
        finally:
            registry._BACKEND_CONFIG_REGISTRY.pop("fake-v6", None)

    def test_class_with_none_kind_default_raises(self) -> None:
        # A config class whose ``kind`` field has an explicit ``None``
        # default (instead of a Literal string) triggers the guard that
        # ensures every registered config has a pinned discriminator.
        from pydantic import Field

        class _NoneDefault(BackendConfigBase):
            kind: str = Field(default=None)  # type: ignore[assignment]

        with pytest.raises(ValueError, match="kind"):
            registry.register_backend_config(_NoneDefault)


# ---------------------------------------------------------------------------
# UnresolvedBackendConfig — opaque row preservation
# ---------------------------------------------------------------------------


class TestUnresolvedBackendConfig:
    def test_unknown_kind_produces_unresolved(self) -> None:
        raw: dict[str, object] = {"kind": "future-cloud", "endpoint": "https://example.com"}
        result = registry._parse_backend_config(raw)
        assert isinstance(result, UnresolvedBackendConfig)
        assert result.kind == "future-cloud"

    def test_unresolved_raw_preserved(self) -> None:
        raw: dict[str, object] = {"kind": "future-cloud", "endpoint": "https://example.com"}
        result = registry._parse_backend_config(raw)
        assert isinstance(result, UnresolvedBackendConfig)
        assert result._raw == raw

    def test_opaque_row_round_trips_byte_for_byte(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "reg.json"
        raw: dict[str, object] = {
            "kind": "future-cloud",
            "endpoint": "https://example.com",
            "token": "secret",
        }
        doc = RegistryFile(
            instances={
                "remote": InstanceMeta(
                    backend=UnresolvedBackendConfig(kind="future-cloud", raw=raw),
                    index=5,
                    created_at=datetime(2026, 5, 19, tzinfo=UTC),
                ),
            },
        )
        registry._write(path, doc)
        rebuilt = registry._read(path)
        remote_meta = rebuilt.instances["remote"]
        assert isinstance(remote_meta.backend, UnresolvedBackendConfig)
        assert remote_meta.backend._raw == raw

    def test_opaque_row_serialized_backend_equals_raw(self, tmp_path: Path) -> None:
        raw: dict[str, object] = {"kind": "x", "data": "preserved"}
        meta = InstanceMeta(
            backend=UnresolvedBackendConfig(kind="x", raw=raw),
            index=0,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = meta._serialize_backend(meta.backend)
        assert result == raw

    def test_unknown_kind_sibling_preserves_known_rows_and_round_trips(
        self, tmp_path: Path
    ) -> None:
        # Data-loss regression: a registry containing an unknown-kind row
        # alongside valid redroid and adb rows must NEVER wipe the file.
        # The known rows must load intact; the unknown row must survive as
        # UnresolvedBackendConfig; a subsequent write must re-emit the
        # unknown row's raw dict byte-for-byte.
        path = tmp_path / "reg.json"
        unknown_raw: dict[str, object] = {
            "kind": "future-cloud",
            "endpoint": "https://cloud.example",
            "token": "tok-abc",
        }
        doc = RegistryFile(
            instances={
                "alpha": InstanceMeta(
                    backend=RedroidBackendConfig(absolute_path="/tmp/alpha"),
                    index=0,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                "phone": InstanceMeta(
                    backend=AdbBackendConfig(serial="emulator-5554"),
                    index=1,
                    created_at=datetime(2026, 2, 1, tzinfo=UTC),
                ),
                "cloud": InstanceMeta(
                    backend=UnresolvedBackendConfig(kind="future-cloud", raw=unknown_raw),
                    index=2,
                    created_at=datetime(2026, 3, 1, tzinfo=UTC),
                ),
            },
        )
        registry._write(path, doc)

        # --- (a) + (b): read back; known rows intact, unknown is opaque ---
        rebuilt = registry._read(path)
        assert "alpha" in rebuilt.instances
        assert isinstance(rebuilt.instances["alpha"].backend, RedroidBackendConfig)
        assert rebuilt.instances["alpha"].backend.absolute_path == "/tmp/alpha"
        assert "phone" in rebuilt.instances
        assert isinstance(rebuilt.instances["phone"].backend, AdbBackendConfig)
        assert rebuilt.instances["phone"].backend.serial == "emulator-5554"
        assert "cloud" in rebuilt.instances
        cloud_meta = rebuilt.instances["cloud"]
        assert isinstance(cloud_meta.backend, UnresolvedBackendConfig)
        assert cloud_meta.backend.kind == "future-cloud"

        # --- (c): no backup-and-empty (bak file must NOT exist) -----------
        assert not path.with_suffix(".json.bak").exists()

        # --- (d): re-write and confirm unknown row is byte-for-byte equal --
        registry._write(path, rebuilt)
        second_read = registry._read(path)
        assert isinstance(second_read.instances["cloud"].backend, UnresolvedBackendConfig)
        assert second_read.instances["cloud"].backend._raw == unknown_raw


# ---------------------------------------------------------------------------
# Registry JSON edge cases: corrupt rows, non-dict values
# ---------------------------------------------------------------------------


class TestRegistryJsonEdgeCases:
    def test_non_dict_instances_value_is_skipped(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "reg.json"
        raw = {"version": registry.SCHEMA_VERSION, "instances": {"bad": "not-a-dict"}}
        path.write_text(json.dumps(raw))
        result = registry._read(path)
        assert "bad" not in result.instances

    def test_non_dict_instances_field_becomes_empty(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "reg.json"
        raw = {"version": registry.SCHEMA_VERSION, "instances": "not-a-dict"}
        path.write_text(json.dumps(raw))
        result = registry._read(path)
        assert result.instances == {}

    def test_meta_with_non_dict_backend_skipped(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "reg.json"
        raw = {
            "version": registry.SCHEMA_VERSION,
            "instances": {
                "bad": {
                    "backend": "not-a-dict",
                    "index": 0,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            },
        }
        path.write_text(json.dumps(raw))
        result = registry._read(path)
        assert "bad" not in result.instances

    def test_corrupt_meta_row_is_skipped(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "reg.json"
        raw = {
            "version": registry.SCHEMA_VERSION,
            "instances": {
                "corrupt": {
                    "backend": {"kind": "redroid", "absolute_path": "/some/path"},
                    "index": "not-an-int",  # pydantic will reject this
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            },
        }
        path.write_text(json.dumps(raw))
        result = registry._read(path)
        assert "corrupt" not in result.instances

    def test_legacy_hint_printed_only_once(
        self, isolated_registry: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The _LEGACY_HINT_PRINTED dedup flag prevents spamming the hint
        # when multiple parse failures hit in rapid succession.  Simulate
        # two bad-version registry files and assert the hint appears once.
        original_flag = registry._LEGACY_HINT_PRINTED
        registry._LEGACY_HINT_PRINTED = False
        try:
            p1 = tmp_path / "r1.json"
            p2 = tmp_path / "r2.json"
            p1.write_text(json.dumps({"version": 999}))
            p2.write_text(json.dumps({"version": 999}))
            registry._read(p1)
            registry._read(p2)
            err = capsys.readouterr().err
            assert err.count("[beetroot] registry") == 1
        finally:
            registry._LEGACY_HINT_PRINTED = original_flag

    def test_json_decode_error_hint_printed_only_once(
        self, isolated_registry: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same dedup for the JSON-parse-error path (invalid JSON, not just
        # wrong schema version). Two invalid-JSON files must print the
        # hint exactly once, not twice.
        original_flag = registry._LEGACY_HINT_PRINTED
        registry._LEGACY_HINT_PRINTED = False
        try:
            p1 = tmp_path / "bad1.json"
            p2 = tmp_path / "bad2.json"
            p1.write_text("this is not json {{{")
            p2.write_text("also not json {{{")
            registry._read(p1)
            registry._read(p2)
            err = capsys.readouterr().err
            assert err.count("[beetroot] registry") == 1
        finally:
            registry._LEGACY_HINT_PRINTED = original_flag


# ---------------------------------------------------------------------------
# Manager.all
# ---------------------------------------------------------------------------


class TestManagerAll:
    def test_all_returns_all_resolvable(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "alpha"
        root.mkdir()
        (root / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
        registry.add_allocating("alpha", root)
        result = api.Manager.all()
        assert any(b.name == "alpha" for b in result)

    def test_all_skips_unresolvable(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # An UnresolvedBackendConfig row that has no registered class must
        # be silently skipped by Manager.all.
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = RegistryFile(
            instances={
                "ghost-kind": InstanceMeta(
                    backend=UnresolvedBackendConfig(
                        kind="unregistered-xyz",
                        raw={"kind": "unregistered-xyz"},
                    ),
                    index=99,
                    created_at=datetime(2026, 5, 19, tzinfo=UTC),
                ),
            },
        )
        registry._write(path, doc)
        result = api.Manager.all()
        assert not any(b.name == "ghost-kind" for b in result)


# ---------------------------------------------------------------------------
# Manager.resolve with UnresolvedBackendConfig
# ---------------------------------------------------------------------------


class TestManagerResolveUnresolved:
    def test_resolve_opaque_row_raises_instance_not_found(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = RegistryFile(
            instances={
                "cloud": InstanceMeta(
                    backend=UnresolvedBackendConfig(
                        kind="future-cloud",
                        raw={"kind": "future-cloud"},
                    ),
                    index=7,
                    created_at=datetime(2026, 5, 19, tzinfo=UTC),
                ),
            },
        )
        registry._write(path, doc)
        with pytest.raises(api.InstanceNotFoundError, match="future-cloud"):
            api.Manager.resolve("cloud")


# ---------------------------------------------------------------------------
# backends.reset_for_testing
# ---------------------------------------------------------------------------


class TestResetForTesting:
    def test_reset_clears_registry(self) -> None:
        saved = dict(backends._BACKEND_REGISTRY)
        saved_flag = backends._ENTRY_POINTS_LOADED
        try:
            backends.reset_for_testing()
            assert backends._BACKEND_REGISTRY == {}
            assert backends._ENTRY_POINTS_LOADED is False
        finally:
            backends._BACKEND_REGISTRY.update(saved)
            backends._ENTRY_POINTS_LOADED = saved_flag


# ---------------------------------------------------------------------------
# Entry-point collision: different class for same kind
# ---------------------------------------------------------------------------


class TestEntryPointCollision:
    def test_different_class_same_kind_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _AltBackend:
            @classmethod
            def from_meta(cls, name: str, backend_config: BackendConfigBase) -> _AltBackend:
                del name, backend_config
                return cls()

        backends._BACKEND_REGISTRY["ep-collision-kind"] = _FakeBackend
        try:
            fake_ep = MagicMock()
            fake_ep.name = "ep-collision-kind"
            fake_ep.load.return_value = _AltBackend
            monkeypatch.setattr(backends, "_ENTRY_POINTS_LOADED", False)
            monkeypatch.setattr(
                backends, "entry_points", lambda group: [fake_ep],
            )
            with pytest.raises(
                backends.BackendRegistrationError, match="ep-collision-kind"
            ):
                backends._load_entry_point_backends()
        finally:
            backends._BACKEND_REGISTRY.pop("ep-collision-kind", None)


# ---------------------------------------------------------------------------
# Instance.install_frida(None) raises when no frida block
# ---------------------------------------------------------------------------


class TestInstanceInstallFridaNone:
    def test_install_frida_none_raises_when_no_frida_block(
        self, cli_root: Path
    ) -> None:
        inst = api.Instance.create("alpha")
        with pytest.raises(ValueError, match="no frida"):
            inst.install_frida(None)

    def test_install_frida_none_uses_configured_version(
        self, cli_root: Path
    ) -> None:
        # When version=None but cfg.frida IS set, use the pinned version.
        from beetroot.config import Frida, InstanceConfig
        cfg = InstanceConfig(frida=Frida(version="16.4.10"))
        inst = api.Instance.create("alpha", cfg=cfg)
        # The frida_download.stage_for_instance call is stubbed out
        # by the cli_root fixture; just assert no ValueError is raised.
        inst.install_frida(None)


# ---------------------------------------------------------------------------
# AdbDevice.install_frida(None) raises ValueError
# ---------------------------------------------------------------------------


class TestAdbDeviceInstallFridaNone:
    def test_install_frida_none_raises(self, isolated_registry: Path) -> None:
        registry.add_allocating("phone", backend=AdbBackendConfig(serial="emulator-5554"))
        device = adb_backend.AdbDevice.from_meta("phone", AdbBackendConfig(serial="emulator-5554"))
        with pytest.raises(ValueError, match="explicit version"):
            device.install_frida(None)


# ---------------------------------------------------------------------------
# cli.main() catches InstanceNotFoundError → exit 1
# ---------------------------------------------------------------------------


class TestCliMainCatchesInstanceNotFound:
    def test_main_exits_1_on_instance_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _raise() -> None:
            raise api.InstanceNotFoundError("no instance named 'ghost'")

        monkeypatch.setattr(cli, "app", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "ghost" in err


# ---------------------------------------------------------------------------
# cli up — non-Instance Lifecycle backend echo path
# ---------------------------------------------------------------------------


class TestUpVerbNonInstanceBackend:
    def test_up_non_instance_backend_echoes_name(
        self, isolated_registry: Path
    ) -> None:
        # Synthesise a third-party Lifecycle backend.  When ``up`` is
        # called it must echo the plain "[beetroot] <name> up" line
        # (no ADB/Frida ports — those belong to Instance only).
        class _LifecycleBackend:
            def __init__(self, name: str) -> None:
                self._name = name

            @classmethod
            def from_meta(
                cls, name: str, backend_config: BackendConfigBase,
            ) -> _LifecycleBackend:
                del backend_config
                return cls(name)

            @property
            def name(self) -> str:
                return self._name

            @property
            def kind(self) -> str:
                return "fake-lifecycle"

            @property
            def adb_address(self) -> str:
                return ""

            @property
            def frida_address(self) -> str:
                return ""

            @property
            def is_available(self) -> bool:
                return True

            def install_frida(self, version: str | None = None) -> None:
                del version

            def shell(self, args: Sequence[str] | None = None) -> int:
                del args
                return 0

            def frida_cli(self, args: Sequence[str]) -> int:
                del args
                return 0

            def up(self) -> None:
                pass

            def down(self) -> None:
                pass

            def restart(self) -> None:
                pass

            def apply(self) -> None:
                pass

            def destroy(self, *, yes: bool = False) -> None:
                del yes

        registry.add_allocating(
            "fake-1", backend=AdbBackendConfig(serial="fake-serial"),
        )

        with patch.object(
            api.Manager, "resolve",
            return_value=_LifecycleBackend("fake-1"),
        ):
            result = runner.invoke(cli.app, ["up", "fake-1"])
        assert result.exit_code == 0, result.stderr
        assert "fake-1 up" in result.stdout


# ---------------------------------------------------------------------------
# cli destroy — orphan-path branches
# ---------------------------------------------------------------------------


class TestDestroyOrphanBranches:
    def test_non_redroid_orphan_raises_backend_capability_error(
        self, cli_root: Path
    ) -> None:
        # An adb-kind row that Manager.resolve can't build (the yaml-path
        # check fails for it too if we break resolution) triggers the
        # non-redroid orphan branch — which raises BackendCapabilityError.
        # Simulate by patching Manager.resolve to raise InstanceNotFoundError
        # while the registry row is adb-kind.
        registry.add_allocating("phone", backend=AdbBackendConfig(serial="x"))

        with patch.object(
            api.Manager, "resolve",
            side_effect=api.InstanceNotFoundError("phone orphan"),
        ):
            result = runner.invoke(cli.app, ["destroy", "phone", "-y"])
        # BackendCapabilityError propagates from the destroy verb to the
        # CliRunner (exit 1 in the runner; exit 2 when routed through main).
        assert result.exit_code != 0
        assert isinstance(result.exception, api.BackendCapabilityError)
        assert "destroy" in str(result.exception)

    def test_orphan_prompt_no_aborts_with_exit_zero(
        self, cli_root: Path
    ) -> None:
        # A redroid orphan (yaml gone) that the user declines to destroy.
        api.Instance.create("alpha")
        import shutil as _shutil
        _shutil.rmtree(registry.instance_path("alpha"))

        result = runner.invoke(cli.app, ["destroy", "alpha"], input="n\n")
        assert result.exit_code == 0, result.stderr
        assert "aborted" in result.output
        assert registry.get("alpha") is not None  # not cleaned up

    def test_orphan_prompt_yes_proceeds_to_cleanup(
        self, cli_root: Path
    ) -> None:
        # Redroid orphan (yaml gone), user types "y" at the prompt → cleanup runs.
        api.Instance.create("alpha")
        import shutil as _shutil
        _shutil.rmtree(registry.instance_path("alpha"))

        result = runner.invoke(cli.app, ["destroy", "alpha"], input="y\n")
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is None

    def test_orphan_with_existing_dir_runs_compose_and_rmtree(
        self, cli_root: Path
    ) -> None:
        # A redroid orphan whose DIRECTORY still exists (only the yaml is
        # gone, not the whole dir). The orphan-path must still try
        # compose.down and rmtree the directory.
        api.Instance.create("alpha")
        root = registry.instance_path("alpha")
        # Remove just the yaml, leave the directory.
        (root / "beetroot.yaml").unlink()

        from beetroot import compose as _compose
        down_calls: list[tuple[str, Path]] = []

        def _fake_down(
            name: str, path: Path, *, volumes: bool = False
        ) -> None:
            down_calls.append((name, path))

        with patch.object(_compose, "down", side_effect=_fake_down):
            result = runner.invoke(cli.app, ["destroy", "alpha", "-y"])
        assert result.exit_code == 0, result.stderr
        assert any(c[0] == "alpha" for c in down_calls)
        assert registry.get("alpha") is None
        assert not root.exists()

    def test_orphan_compose_error_continues_cleanup(
        self, cli_root: Path
    ) -> None:
        # Redroid orphan with existing directory: compose.down raises
        # ComposeError. Cleanup (rmtree + registry.remove) must still run
        # and exit 0 with a "continuing" advisory.
        api.Instance.create("alpha")
        root = registry.instance_path("alpha")
        (root / "beetroot.yaml").unlink()

        from beetroot import compose as _compose

        def _boom(name: str, path: Path, *, volumes: bool = False) -> None:
            raise _compose.ComposeError("simulated failure")

        with patch.object(_compose, "down", side_effect=_boom):
            result = runner.invoke(cli.app, ["destroy", "alpha", "-y"])
        assert result.exit_code == 0, result.stderr
        # The "continuing" advisory is an out-of-band note → stderr.
        assert "continuing" in result.stderr
        assert registry.get("alpha") is None
        assert not root.exists()
