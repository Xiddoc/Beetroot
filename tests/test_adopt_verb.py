"""End-to-end CLI tests for the ``beetroot adopt`` verb (T5).

``adopt`` registers a real (or emulator) Android device under an ``adb``-
kind registry row. Unlike ``create`` / ``register``, no on-disk instance
directory is made; the device is managed outside Beetroot. These tests
drive the full Typer entry point with a stubbed adb so the test is
hermetic.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, registry
from beetroot.backends import adb as adb_backend

runner = CliRunner()


def _run_main_with_argv(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    """Drive cli.main() under a faked argv. Returns (exit_code, stderr)."""
    monkeypatch.setattr(sys, "argv", argv)
    stderr_capture: list[str] = []
    original_echo = __import__("typer").echo

    def _spy(msg: str, *, err: bool = False, **kw: object) -> None:
        if err:
            stderr_capture.append(msg)
        original_echo(msg, err=err, **kw)

    monkeypatch.setattr("beetroot.cli.typer.echo", _spy)
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0), "\n".join(stderr_capture)
    return 0, "\n".join(stderr_capture)


@pytest.fixture
def stub_adb(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture every subprocess.run call inside backends.adb."""
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
            stdout="List of devices attached\nemulator-5554\tdevice\n",
            stderr="",
        )

    monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
    return captured


class TestAdoptVerb:
    def test_adopt_with_default_name(
        self, isolated_registry: Path,
    ) -> None:
        result = runner.invoke(cli.app, ["adopt", "emulator-5554"])
        assert result.exit_code == 0
        # Default name: adb-emulator-5554 (lowercased, colon→hyphen, ≤24 chars)
        meta = registry.get("adb-emulator-5554")
        assert meta is not None
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == "emulator-5554"
        assert meta.backend.kind == "adb"

    def test_adopt_with_explicit_name(
        self, isolated_registry: Path,
    ) -> None:
        result = runner.invoke(
            cli.app, ["adopt", "emulator-5554", "--name", "my-phone"],
        )
        assert result.exit_code == 0
        meta = registry.get("my-phone")
        assert meta is not None
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == "emulator-5554"

    def test_adopt_with_network_serial_requires_explicit_name(
        self, isolated_registry: Path,
    ) -> None:
        # IPv4-shaped serials like ``192.168.1.10:5555`` contain dots,
        # which the [a-z0-9_-]+ grammar rejects. The CLI surfaces a
        # friendly "pass --name" error rather than half-registering
        # the row.
        result = runner.invoke(cli.app, ["adopt", "192.168.1.10:5555"])
        assert result.exit_code != 0
        assert "invalid" in result.stderr.lower()
        assert "--name" in result.stderr

    def test_adopt_with_network_serial_and_explicit_name(
        self, isolated_registry: Path,
    ) -> None:
        result = runner.invoke(
            cli.app,
            ["adopt", "192.168.1.10:5555", "--name", "lan-phone"],
        )
        assert result.exit_code == 0
        meta = registry.get("lan-phone")
        assert meta is not None
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == "192.168.1.10:5555"

    def test_adopt_collision_errors(
        self, isolated_registry: Path,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554"])
        result = runner.invoke(cli.app, ["adopt", "emulator-5554"])
        assert result.exit_code != 0
        assert "already registered" in result.stderr

    def test_adopt_invalid_explicit_name(
        self, isolated_registry: Path,
    ) -> None:
        result = runner.invoke(
            cli.app, ["adopt", "emulator-5554", "--name", "Bad Name!"],
        )
        assert result.exit_code != 0


class TestAdoptedInstanceDispatch:
    """Verbs that go through ``Manager.resolve`` work for adb-kind rows."""

    def test_manager_resolve_returns_adb_device(
        self, isolated_registry: Path,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        backend = api.Manager.resolve("phone")
        assert isinstance(backend, adb_backend.AdbDevice)
        assert backend.name == "phone"
        assert backend.kind == "adb"
        assert backend.adb_address == "emulator-5554"

    def test_shell_dispatches_to_adb_device(
        self,
        isolated_registry: Path,
        stub_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import shutil

        monkeypatch.setattr(
            shutil, "which", lambda name: f"/usr/bin/{name}",
        )
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        result = runner.invoke(cli.app, ["shell", "phone"])
        assert result.exit_code == 0
        # The shell verb dispatched through Manager.resolve → AdbDevice
        # → ``adb -s emulator-5554 shell``.
        assert ["adb", "-s", "emulator-5554", "shell"] in stub_adb

    def test_env_dispatches_to_adb_device(
        self, isolated_registry: Path,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        result = runner.invoke(cli.app, ["env", "phone"])
        assert result.exit_code == 0
        assert "ANDROID_DEVICE=emulator-5554" in result.stdout
        assert "FRIDA_DEVICE=localhost:27042" in result.stdout

    def test_up_raises_backend_capability_error_with_exit_code_2(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        code, err = _run_main_with_argv(
            ["beetroot", "up", "phone"], monkeypatch,
        )
        # ``up`` against an adb-backed instance → BackendCapabilityError
        # → exit 2 (distinct from "instance not found" → 1).
        assert code == 2
        assert "not supported" in err
        assert "adb" in err

    def test_destroy_raises_backend_capability_error(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        code, err = _run_main_with_argv(
            ["beetroot", "destroy", "phone", "-y"], monkeypatch,
        )
        assert code == 2
        assert "not supported" in err
        assert "adb" in err

    def test_snapshot_raises_backend_capability_error(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        code, err = _run_main_with_argv(
            ["beetroot", "snapshot", "phone"], monkeypatch,
        )
        assert code == 2
        assert "not supported" in err


class TestModuleVerbAdbDispatch:
    def test_module_verb_dispatches_to_adb_device(
        self,
        isolated_registry: Path,
        stub_adb: list[list[str]],
        tmp_path: Path,
    ) -> None:
        zip_path = tmp_path / "ModX.zip"
        zip_path.write_bytes(b"PK\x03\x04")
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        result = runner.invoke(cli.app, ["module", "phone", str(zip_path)])
        assert result.exit_code == 0
        # The module verb should have invoked ``adb -s emulator-5554
        # push <zip> /sdcard/Download/ModX.zip`` exactly once.
        assert [
            "adb", "-s", "emulator-5554", "push",
            str(zip_path), "/sdcard/Download/ModX.zip",
        ] in stub_adb


class TestModuleVerbThirdParty:
    """Third-party backends without ``add_module`` get a friendly error."""

    def test_third_party_without_add_module_errors(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Synthesise a backend that lacks ``add_module``; assert the
        # CLI surfaces the friendly error rather than an AttributeError.

        class _NoModuleBackend:
            def __init__(self, name: str) -> None:
                self._name = name

            @classmethod
            def from_meta(
                cls, name: str, backend: registry.BackendConfig,
            ) -> _NoModuleBackend:
                del backend
                return cls(name)

            @property
            def name(self) -> str:
                return self._name

            @property
            def kind(self) -> str:
                return "fake"

            @property
            def adb_address(self) -> str:
                return ""

            @property
            def frida_address(self) -> str:
                return ""

            @property
            def is_available(self) -> bool:
                return True

            def install_frida(self, version: str) -> None:
                del version

            def shell(self) -> int:
                return 0

            def frida_cli(self, args: list[str]) -> int:
                del args
                return 0

        # Direct call to the CLI helper paths is the cleanest way to
        # exercise the "no add_module" branch; the in-tree registry
        # discriminated-union can't accept a foreign ``kind``.
        backend = _NoModuleBackend("fake-1")
        monkeypatch.setattr(
            api.Manager, "resolve", lambda name: backend,
        )
        # ``_ensure_exists`` checks registry.get; stub it for the test.
        monkeypatch.setattr(
            "beetroot.cli.registry.get",
            lambda name: registry.InstanceMeta(
                backend=registry.AdbBackendConfig(serial="x"),
                index=0,
                created_at=datetime(2026, 5, 19, tzinfo=UTC),
            ),
        )
        result = runner.invoke(cli.app, ["module", "fake-1", "/tmp/x.zip"])
        assert result.exit_code != 0
        assert "does not" in result.stderr
        assert "module" in result.stderr


class TestDefaultNameBuilder:
    def test_emulator_serial(self) -> None:
        assert cli._adopt_default_name("emulator-5554") == "adb-emulator-5554"

    def test_uppercase_lowered(self) -> None:
        assert cli._adopt_default_name("ABCD1234") == "adb-abcd1234"

    def test_colons_become_hyphens(self) -> None:
        # ``192.168.1.10:5555`` → colons → hyphens; dots remain (the
        # caller must pass --name for IPv4-shaped serials).
        out = cli._adopt_default_name("ip:5555")
        assert out == "adb-ip-5555"

    def test_truncated_to_24_chars(self) -> None:
        long_serial = "x" * 64
        out = cli._adopt_default_name(long_serial)
        assert len(out) == 24
        assert out.startswith("adb-")

    def test_trailing_hyphens_stripped(self) -> None:
        # Truncation can leave a trailing hyphen that fails the
        # ``[a-z0-9_-]+`` grammar in unusual cases.
        out = cli._adopt_default_name("a-" + "x" * 64)
        assert not out.endswith("--")
