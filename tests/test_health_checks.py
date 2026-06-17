"""Unit tests for the private health-check helpers in api.py.

These cover the skip / error / value=0 / unknown-output branches that
the higher-level doctor-verb tests don't easily reach.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import api, hostcheck


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestCheckHostBinder:
    def _pin(self, monkeypatch: pytest.MonkeyPatch, status: hostcheck.BinderStatus) -> None:
        monkeypatch.setattr(hostcheck, "binder_status", lambda: status)

    def test_ready_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._pin(monkeypatch, hostcheck.BinderStatus(state="ready", reason="ok", remedy=""))
        result = api._check_host_binder()
        assert result.status == "pass"
        assert result.reason is None

    def test_unknown_is_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._pin(
            monkeypatch,
            hostcheck.BinderStatus(state="unknown", reason="cannot tell", remedy="try x"),
        )
        result = api._check_host_binder()
        assert result.status == "skip"
        assert result.reason == "cannot tell"

    def test_unsupported_is_fail_with_remedy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._pin(
            monkeypatch,
            hostcheck.BinderStatus(
                state="unsupported", reason="compiled out", remedy="use adb"
            ),
        )
        result = api._check_host_binder()
        assert result.status == "fail"
        assert "compiled out" in (result.reason or "")
        assert "use adb" in (result.reason or "")
        assert "beetroot modes" in (result.reason or "")

    def test_vm_mode_is_skip_without_probing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # vm mode must not depend on host binder — binder_status must not even be called.
        def _boom() -> hostcheck.BinderStatus:
            raise AssertionError("binder_status must not be probed for binder: vm")

        monkeypatch.setattr(hostcheck, "binder_status", _boom)
        result = api._check_host_binder("vm")
        assert result.status == "skip"
        assert "micro-VM" in (result.reason or "")

    def test_host_mode_unknown_is_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._pin(
            monkeypatch,
            hostcheck.BinderStatus(state="unknown", reason="cannot tell", remedy="try x"),
        )
        result = api._check_host_binder("host")
        assert result.status == "fail"
        assert "cannot tell" in (result.reason or "")

    def test_host_mode_unsupported_is_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._pin(
            monkeypatch,
            hostcheck.BinderStatus(state="unsupported", reason="compiled out", remedy="adb"),
        )
        assert api._check_host_binder("host").status == "fail"

    def test_ready_is_pass_under_host_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._pin(monkeypatch, hostcheck.BinderStatus(state="ready", reason="ok", remedy=""))
        assert api._check_host_binder("host").status == "pass"


class TestCheckAdbConnect:
    def test_skips_when_adb_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        result = api._check_adb_connect("localhost:5555")
        assert result.status == "skip"
        assert result.reason == "adb not on PATH"

    def test_fails_on_subprocess_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")

        def _raise(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise OSError("boom")

        with patch("subprocess.run", side_effect=_raise):
            result = api._check_adb_connect("localhost:5555")
        assert result.status == "fail"
        assert "boom" in (result.reason or "")

    def test_fails_when_stderr_says_cannot_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(0, "", "cannot connect to localhost")):
            result = api._check_adb_connect("localhost:5555")
        assert result.status == "fail"

    def test_passes_on_clean_zero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(0, "connected to localhost:5555", "")):
            result = api._check_adb_connect("localhost:5555")
        assert result.status == "pass"


class TestCheckFridaSocket:
    def test_skip_when_disabled(self) -> None:
        result = api._check_frida_socket("localhost", 27042, enabled=False)
        assert result.status == "skip"
        assert result.reason == "frida not configured"

    def test_passes_when_connect_succeeds(self) -> None:
        with patch("socket.create_connection") as mock_conn:
            result = api._check_frida_socket("localhost", 27042, enabled=True)
        mock_conn.assert_called_once_with(("localhost", 27042), timeout=1)
        assert result.status == "pass"

    def test_fails_when_connect_refused(self) -> None:
        with patch("socket.create_connection", side_effect=OSError("connection refused")):
            result = api._check_frida_socket("localhost", 27042, enabled=True)
        assert result.status == "fail"
        assert "no listener at localhost:27042" in (result.reason or "")


class TestCheckMagiskZygisk:
    def test_skip_when_adb_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        result = api._check_magisk_zygisk_over_adb("localhost:5555")
        assert result.status == "skip"

    def test_fails_on_subprocess_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")

        def _raise(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise OSError("dead")

        with patch("subprocess.run", side_effect=_raise):
            result = api._check_magisk_zygisk_over_adb("localhost:5555")
        assert result.status == "fail"

    def test_fails_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(1, "", "magisk not installed")):
            result = api._check_magisk_zygisk_over_adb("localhost:5555")
        assert result.status == "fail"
        assert "magisk not installed" in (result.reason or "")

    def test_fail_value_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(0, "value=0\n", "")):
            result = api._check_magisk_zygisk_over_adb("localhost:5555")
        assert result.status == "fail"
        assert result.reason == "expected 1, got 0"

    def test_fail_unexpected_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(0, "junk\n", "")):
            result = api._check_magisk_zygisk_over_adb("localhost:5555")
        assert result.status == "fail"
        assert "unexpected output" in (result.reason or "")


class TestCheckMagiskDenylist:
    def test_skip_when_not_enrolled(self) -> None:
        result = api._check_magisk_denylist_over_adb(
            "localhost:5555", "com.example", enrolled=False,
        )
        assert result.status == "skip"
        assert "not in magisk.denylist" in (result.reason or "")

    def test_skip_when_adb_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        result = api._check_magisk_denylist_over_adb(
            "localhost:5555", "com.example", enrolled=True,
        )
        assert result.status == "skip"

    def test_fails_on_subprocess_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")

        def _raise(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise OSError("dead")

        with patch("subprocess.run", side_effect=_raise):
            result = api._check_magisk_denylist_over_adb(
                "localhost:5555", "com.example", enrolled=True,
            )
        assert result.status == "fail"

    def test_fails_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(1, "", "error")):
            result = api._check_magisk_denylist_over_adb(
                "localhost:5555", "com.example", enrolled=True,
            )
        assert result.status == "fail"

    def test_fails_when_pkg_not_in_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(0, "", "")):
            result = api._check_magisk_denylist_over_adb(
                "localhost:5555", "com.example", enrolled=True,
            )
        assert result.status == "fail"
        assert "com.example not enrolled" in (result.reason or "")


class TestCheckAdbSerialListed:
    def test_skip_when_adb_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        result = api._check_adb_serial_listed("emulator-5554")
        assert result.status == "skip"

    def test_fails_on_subprocess_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")

        def _raise(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise OSError("dead")

        with patch("subprocess.run", side_effect=_raise):
            result = api._check_adb_serial_listed("emulator-5554")
        assert result.status == "fail"

    def test_fails_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(1)):
            result = api._check_adb_serial_listed("emulator-5554")
        assert result.status == "fail"
        assert "adb devices exit" in (result.reason or "")

    def test_fails_when_serial_offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch(
            "subprocess.run",
            return_value=_proc(0, "List of devices attached\nemulator-5554\toffline\n"),
        ):
            result = api._check_adb_serial_listed("emulator-5554")
        assert result.status == "fail"
        assert result.reason == "state=offline"

    def test_fails_when_serial_not_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch(
            "subprocess.run",
            return_value=_proc(0, "List of devices attached\n"),
        ):
            result = api._check_adb_serial_listed("emulator-5554")
        assert result.status == "fail"
        assert "not listed" in (result.reason or "")

    def test_passes_when_serial_in_device_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch(
            "subprocess.run",
            return_value=_proc(0, "List of devices attached\nemulator-5554\tdevice\n"),
        ):
            result = api._check_adb_serial_listed("emulator-5554")
        assert result.status == "pass"


class TestAdbDeviceHealth:
    def _stub(self, frida_address: str = "localhost:27042") -> api.DeviceBackend:
        class _Stub:
            adb_address = "emulator-5554"

            def __init__(self, fa: str) -> None:
                self._fa = fa

            @property
            def frida_address(self) -> str:
                return self._fa

        # The stub only needs the fields adb_device_health reads
        # (``adb_address`` and ``frida_address``); cast to the
        # Protocol for the type-checker without standing up the full
        # surface.
        return _Stub(frida_address)  # type: ignore[return-value]  # partial structural match — adb_device_health only reads two fields

    def test_handles_invalid_frida_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unparseable port → frida_port=0 → handshake skips.
        device = self._stub("localhost:not-a-port")
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch(
            "subprocess.run",
            return_value=_proc(0, "emulator-5554\tdevice\nvalue=1\npackage_name=com.google.android.gms\n"),
        ):
            results = api.adb_device_health(device)
        assert "compose.status" not in results
        assert results["frida.handshake"].status == "skip"


class TestInstanceHealthExitCodeCap:
    def test_doctor_exit_code_capped_at_255(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del cli_root  # fixture present for XDG isolation only
        # Synthesize an Instance.health() that returns 300 fails to
        # exercise the min(fail_count, 255) clamp without actually
        # standing up 300 instances.
        from typer.testing import CliRunner

        from beetroot import api as _api
        from beetroot import cli as _cli

        runner = CliRunner()
        runner.invoke(_cli.app, ["create", "alpha"])

        def _fake_health(_self: _api.Instance) -> dict[str, _api.CheckResult]:
            return {f"check.{i}": _api.CheckResult(status="fail", reason="x") for i in range(300)}

        monkeypatch.setattr(_api.Instance, "health", _fake_health)
        result = runner.invoke(_cli.app, ["doctor", "alpha"])
        assert result.exit_code == 255
