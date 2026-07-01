"""End-to-end CLI tests for the ``beetroot adopt`` verb (T5).

``adopt`` registers a real (or emulator) Android device under an ``adb``-
kind registry row. Unlike ``create`` / ``register``, no on-disk instance
directory is made; the device is managed outside Beetroot. These tests
drive the full Typer entry point with a stubbed adb so the test is
hermetic.
"""

from __future__ import annotations

import io
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from beetroot import api, cli, console, registry
from beetroot.backends import adb as adb_backend

runner = CliRunner()


def _run_main_with_argv(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """Drive cli.main() under a faked argv. Returns (exit_code, stderr)."""
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    console.set_consoles(stderr=Console(file=buf, force_terminal=False))
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0), buf.getvalue()
    return 0, buf.getvalue()


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
        self,
        isolated_registry: Path,
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
        self,
        isolated_registry: Path,
    ) -> None:
        result = runner.invoke(
            cli.app,
            ["adopt", "emulator-5554", "--name", "my-phone"],
        )
        assert result.exit_code == 0
        meta = registry.get("my-phone")
        assert meta is not None
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == "emulator-5554"

    def test_adopt_with_network_serial_auto_derives_valid_name(
        self,
        isolated_registry: Path,
    ) -> None:
        # #257: IPv4-shaped serials like ``192.168.1.10:5555`` (the
        # help's own example) contain dots and a colon. The default
        # name builder collapses every non-alnum run to a hyphen, so
        # adopt succeeds with no --name and the derived name satisfies
        # the [a-z0-9_-]+ grammar rather than half-registering the row.
        serial = "192.168.1.10:5555"
        derived = cli._adopt_default_name(serial)
        assert cli._INSTANCE_NAME_RE.fullmatch(derived)

        result = runner.invoke(cli.app, ["adopt", serial])
        assert result.exit_code == 0, result.stderr
        meta = registry.get(derived)
        assert meta is not None
        assert isinstance(meta.backend, registry.AdbBackendConfig)
        assert meta.backend.serial == serial

    def test_adopt_with_network_serial_and_explicit_name(
        self,
        isolated_registry: Path,
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
        self,
        isolated_registry: Path,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554"])
        result = runner.invoke(cli.app, ["adopt", "emulator-5554"])
        assert result.exit_code != 0
        assert "already registered" in result.stderr

    def test_adopt_invalid_explicit_name(
        self,
        isolated_registry: Path,
    ) -> None:
        result = runner.invoke(
            cli.app,
            ["adopt", "emulator-5554", "--name", "Bad Name!"],
        )
        assert result.exit_code != 0


class TestAdoptedInstanceDispatch:
    """Verbs that go through ``Manager.resolve`` work for adb-kind rows."""

    def test_manager_resolve_returns_adb_device(
        self,
        isolated_registry: Path,
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
            shutil,
            "which",
            lambda name: f"/usr/bin/{name}",
        )
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        result = runner.invoke(cli.app, ["shell", "phone"])
        assert result.exit_code == 0
        # The shell verb dispatched through Manager.resolve → AdbDevice
        # → ``adb -s emulator-5554 shell``.
        assert ["adb", "-s", "emulator-5554", "shell"] in stub_adb

    def test_shell_forwards_extra_args_to_adb(
        self,
        isolated_registry: Path,
        stub_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        result = runner.invoke(cli.app, ["shell", "phone", "-c", "id"])
        assert result.exit_code == 0
        # Extra tokens must appear in the adb argv after "shell".
        adb_calls = [c for c in stub_adb if "shell" in c]
        assert adb_calls, "no adb shell call recorded"
        last_shell = adb_calls[-1]
        assert "-c" in last_shell
        assert "id" in last_shell

    def test_up_raises_backend_capability_error_with_exit_code_2(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        code, err = _run_main_with_argv(
            ["beetroot", "up", "phone"],
            monkeypatch,
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
            ["beetroot", "destroy", "phone", "-y"],
            monkeypatch,
        )
        assert code == 2
        assert "not supported" in err
        assert "adb" in err

    def test_destroy_adb_gates_before_confirm(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # #206: the capability gate must run BEFORE the destructive-wipe
        # confirmation so an adb backend (no Lifecycle) fails fast (exit 2)
        # without ever prompting the user to authorize an impossible wipe.
        from unittest.mock import MagicMock

        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        confirm = MagicMock()
        monkeypatch.setattr("beetroot.cli.typer.confirm", confirm)
        code, _ = _run_main_with_argv(["beetroot", "destroy", "phone"], monkeypatch)
        assert code == 2
        confirm.assert_not_called()

    def test_reset_adb_gates_before_confirm(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # #206: same ordering for reset — adb backends aren't Resettable.
        from unittest.mock import MagicMock

        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        confirm = MagicMock()
        monkeypatch.setattr("beetroot.cli.typer.confirm", confirm)
        code, _ = _run_main_with_argv(["beetroot", "reset", "phone"], monkeypatch)
        assert code == 2
        confirm.assert_not_called()

    def test_snapshot_raises_backend_capability_error(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        code, err = _run_main_with_argv(
            ["beetroot", "snapshot", "phone"],
            monkeypatch,
        )
        assert code == 2
        # #128: the generic "not supported by the adb backend" message is
        # replaced by the redroid-only one, still exiting 2.
        assert "only supported for the redroid backend" in err
        assert "adb" in err
        assert "#128" in err


class TestModuleVerbAdbDispatch:
    def test_module_verb_dispatches_to_adb_device(
        self,
        isolated_registry: Path,
        stub_adb: list[list[str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import shutil

        # ``AdbDevice.add_module`` now guards on ``shutil.which("adb")`` (#275),
        # so stub it present — otherwise this dispatch test depends on whether
        # ``adb`` happens to be on the host PATH (present in the sandbox, absent
        # on CI runners), which is exactly the ambient dependency the guard adds.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "ModX.zip"
        zip_path.write_bytes(b"PK\x03\x04")
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        result = runner.invoke(cli.app, ["module", "phone", str(zip_path)])
        assert result.exit_code == 0
        # The module verb should have invoked ``adb -s emulator-5554
        # push <zip> /sdcard/Download/ModX.zip`` exactly once.
        assert [
            "adb",
            "-s",
            "emulator-5554",
            "push",
            str(zip_path),
            "/sdcard/Download/ModX.zip",
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
                cls,
                name: str,
                backend: registry.BackendConfig,
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

            def install_frida(self, version: str | None = None) -> None:
                del version

            def shell(self, args: Sequence[str] | None = None) -> int:
                del args
                return 0

            def frida_cli(self, args: Sequence[str]) -> int:
                del args
                return 0

        # Direct call to the CLI helper paths is the cleanest way to
        # exercise the "no add_module" branch; the in-tree registry
        # discriminated-union can't accept a foreign ``kind``.
        backend = _NoModuleBackend("fake-1")
        monkeypatch.setattr(
            api.Manager,
            "resolve",
            lambda name: backend,
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
        assert isinstance(result.exception, api.BackendCapabilityError)
        assert "module" in str(result.exception)


class TestAdoptVerify:
    """Tests for the ``--verify`` / ``-V`` flag on ``beetroot adopt``."""

    def test_verify_accepts_when_serial_is_device(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_adb: list[list[str]],
    ) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        result = runner.invoke(
            cli.app,
            ["adopt", "emulator-5554", "--name", "phone", "--verify"],
        )
        assert result.exit_code == 0
        assert registry.get("phone") is not None

    def test_verify_refuses_when_serial_not_listed(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

        def _no_devices(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="List of devices attached\n",
                stderr="",
            )

        monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _no_devices)
        result = runner.invoke(
            cli.app,
            ["adopt", "ghost-9999", "--name", "phone", "--verify"],
        )
        assert result.exit_code != 0
        assert "not listed" in result.stderr or "device" in result.stderr
        assert registry.get("phone") is None

    def test_verify_errors_when_adb_not_on_path(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = runner.invoke(
            cli.app,
            ["adopt", "emulator-5554", "--name", "phone", "--verify"],
        )
        assert result.exit_code != 0
        assert "adb not found" in result.stderr
        assert registry.get("phone") is None


class TestForgetVerb:
    """Tests for the ``beetroot forget`` verb."""

    def test_forget_removes_registry_row_for_adb_instance(
        self,
        isolated_registry: Path,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        assert registry.get("phone") is not None
        result = runner.invoke(cli.app, ["forget", "phone"])
        assert result.exit_code == 0
        assert registry.get("phone") is None
        assert "forgot phone" in result.stdout

    def test_forget_removes_registry_row_for_redroid_instance(
        self,
        cli_root: Path,
    ) -> None:
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0
        assert registry.get("alpha") is not None
        instance_dir = cli_root / "alpha"
        result = runner.invoke(cli.app, ["forget", "alpha"])
        assert result.exit_code == 0
        assert registry.get("alpha") is None
        assert instance_dir.exists()

    def test_forget_frees_port_index(
        self,
        isolated_registry: Path,
    ) -> None:
        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        meta_before = registry.get("phone")
        assert meta_before is not None
        idx = meta_before.index
        runner.invoke(cli.app, ["forget", "phone"])
        assert idx not in registry.used_indices()

    def test_forget_does_not_delete_host_dir(
        self,
        cli_root: Path,
    ) -> None:
        runner.invoke(cli.app, ["create", "beta"])
        host_dir = cli_root / "beta"
        assert host_dir.exists()
        runner.invoke(cli.app, ["forget", "beta"])
        assert host_dir.exists()

    def test_forget_errors_when_instance_not_found(
        self,
        isolated_registry: Path,
    ) -> None:
        result = runner.invoke(cli.app, ["forget", "nonexistent"])
        assert result.exit_code != 0
        assert "nonexistent" in result.stderr


class TestDefaultNameBuilder:
    def test_emulator_serial(self) -> None:
        assert cli._adopt_default_name("emulator-5554") == "adb-emulator-5554"

    def test_uppercase_lowered(self) -> None:
        assert cli._adopt_default_name("ABCD1234") == "adb-abcd1234"

    def test_colons_become_hyphens(self) -> None:
        out = cli._adopt_default_name("ip:5555")
        assert out == "adb-ip-5555"

    def test_dots_become_hyphens(self) -> None:
        # #257: dots (IPv4-shaped serials) collapse to hyphens too, so
        # the derived name matches the [a-z0-9_-]+ grammar with no
        # --name required.
        out = cli._adopt_default_name("192.168.1.10:5555")
        assert out == "adb-192-168-1-10-5555"
        assert cli._INSTANCE_NAME_RE.fullmatch(out)

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


class TestInstallFridaVerb:
    """#205: the `install-frida` verb the adopt hint advertises.

    It resolves the backend and calls ``backend.install_frida(version)``,
    mapping ``ValueError`` / ``AdbNotInstalledError`` to the friendly
    ``error: ...`` contract. ``--version`` is required.
    """

    def test_install_frida_calls_backend(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        backend = MagicMock()
        monkeypatch.setattr(api.Manager, "resolve", lambda name: backend)
        result = runner.invoke(cli.app, ["install-frida", "phone", "--version", "16.4.10"])
        assert result.exit_code == 0, result.stderr
        backend.install_frida.assert_called_once_with("16.4.10")

    def test_install_frida_value_error_is_friendly(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        backend = MagicMock()
        backend.install_frida.side_effect = ValueError("bad version")
        monkeypatch.setattr(api.Manager, "resolve", lambda name: backend)
        code, err = _run_main_with_argv(
            ["beetroot", "install-frida", "phone", "--version", "nope"],
            monkeypatch,
        )
        assert code == 1
        assert "error:" in err
        assert "bad version" in err
        assert "Traceback" not in err

    def test_install_frida_adb_missing_is_friendly(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
        backend = MagicMock()
        backend.install_frida.side_effect = api.AdbNotInstalledError(
            "adb not found on PATH (install android-tools)"
        )
        monkeypatch.setattr(api.Manager, "resolve", lambda name: backend)
        code, err = _run_main_with_argv(
            ["beetroot", "install-frida", "phone", "--version", "16.4.10"],
            monkeypatch,
        )
        assert code == 1
        assert "error:" in err
        assert "adb not found" in err
        assert "Traceback" not in err

    def test_install_frida_missing_instance_errors(
        self,
        isolated_registry: Path,
    ) -> None:
        result = runner.invoke(cli.app, ["install-frida", "ghost", "--version", "16.4.10"])
        assert result.exit_code == 1
        assert "no instance named" in result.stderr
