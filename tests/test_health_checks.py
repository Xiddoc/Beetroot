"""Unit tests for the private health-check helpers in api.py.

These cover the skip / error / value=0 / unknown-output branches that
the higher-level doctor-verb tests don't easily reach.
"""

from __future__ import annotations

import contextlib
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import api, hostcheck


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
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
            hostcheck.BinderStatus(state="unsupported", reason="compiled out", remedy="use adb"),
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
            "localhost:5555",
            "com.example",
            enrolled=False,
        )
        assert result.status == "skip"
        assert "not in magisk.denylist" in (result.reason or "")

    def test_skip_when_adb_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        result = api._check_magisk_denylist_over_adb(
            "localhost:5555",
            "com.example",
            enrolled=True,
        )
        assert result.status == "skip"

    def test_fails_on_subprocess_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")

        def _raise(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise OSError("dead")

        with patch("subprocess.run", side_effect=_raise):
            result = api._check_magisk_denylist_over_adb(
                "localhost:5555",
                "com.example",
                enrolled=True,
            )
        assert result.status == "fail"

    def test_fails_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(1, "", "error")):
            result = api._check_magisk_denylist_over_adb(
                "localhost:5555",
                "com.example",
                enrolled=True,
            )
        assert result.status == "fail"

    def test_fails_when_pkg_not_in_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        with patch("subprocess.run", return_value=_proc(0, "", "")):
            result = api._check_magisk_denylist_over_adb(
                "localhost:5555",
                "com.example",
                enrolled=True,
            )
        assert result.status == "fail"
        assert "com.example not enrolled" in (result.reason or "")


class TestMagiskSqliteSuQuoting:
    """Issue #159: the adb backend must hop the root-only DB read via ``su -c``.

    The redroid container's adbd is uid 0 so the bare ``magisk --sqlite``
    works there; a genuine adopted phone's adbd is the unprivileged
    ``shell`` user (uid 2000), so the read is permission-denied unless it
    goes through MagiskSU. These tests pin the EMITTED argv (not just the
    stubbed return value) so a regression that drops ``su -c`` is caught.
    """

    @staticmethod
    def _captured_argv(use_su: bool, sql: str = "SELECT value FROM settings") -> list[str]:
        with patch("subprocess.run") as run:
            run.return_value = _proc(0, "value=1\n", "")
            api._magisk_sqlite_value_over_adb("emulator-5554", sql, use_su=use_su)
        argv = run.call_args.args[0]
        assert isinstance(argv, list)
        return argv

    def test_bare_form_emits_unwrapped_argv(self) -> None:
        # The redroid path (use_su defaults to False) must keep the
        # historical bare argv — no ``su``/``-c`` anywhere.
        argv = self._captured_argv(use_su=False)
        assert argv == [
            "adb",
            "-s",
            "emulator-5554",
            "shell",
            "magisk",
            "--sqlite",
            "SELECT value FROM settings",
        ]
        assert "su" not in argv

    def test_su_form_wraps_payload_in_su_c(self) -> None:
        # The adb path must emit ``adb -s <s> shell su -c <quoted>`` with
        # ``su`` and ``-c`` as consecutive standalone argv elements.
        argv = self._captured_argv(use_su=True)
        assert argv[:5] == ["adb", "-s", "emulator-5554", "shell", "su"]
        assert argv[5] == "-c"
        assert len(argv) == 7

    def test_su_payload_survives_inner_shell_parse(self) -> None:
        # The element after ``-c`` is shell-parsed TWICE on-device: the
        # device shell flattens argv (first split → the single
        # ``magisk --sqlite <sql>`` word), then MagiskSU re-joins its
        # post-``-c`` args into a second ``sh -c`` (second split → the
        # argv tokens). Re-running both splits must recover the exact
        # ``magisk --sqlite <sql>`` invocation with the SQL string — and
        # its embedded single quotes — intact, proving the dual-parse
        # quoting holds.
        sql = "SELECT value FROM settings WHERE key='zygisk'"
        argv = self._captured_argv(use_su=True, sql=sql)
        (outer,) = shlex.split(argv[6])
        inner = shlex.split(outer)
        assert inner == ["magisk", "--sqlite", sql]

    def test_zygisk_check_routes_through_su_on_adb_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end through the check function: an unprivileged adbd
        # rejects the bare read (rc!=0) but the su-wrapped read returns
        # value=1, so the check must emit su -c and report pass.
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")

        def _unprivileged(
            cmd: list[str], *a: object, **k: object
        ) -> subprocess.CompletedProcess[str]:
            del a, k
            if "su" in cmd and "-c" in cmd:
                return _proc(0, "value=1\n", "")
            return _proc(1, "", "sqlite3: unable to open database file (permission denied)")

        with patch("subprocess.run", side_effect=_unprivileged):
            result = api._check_magisk_zygisk_over_adb("emulator-5554", use_su=True)
        assert result.status == "pass"

    def test_denylist_check_routes_through_su_on_adb_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        pkg = "com.google.android.gms"

        def _unprivileged(
            cmd: list[str], *a: object, **k: object
        ) -> subprocess.CompletedProcess[str]:
            del a, k
            if "su" in cmd and "-c" in cmd:
                return _proc(0, f"package_name={pkg}\n", "")
            return _proc(1, "", "permission denied")

        with patch("subprocess.run", side_effect=_unprivileged):
            result = api._check_magisk_denylist_over_adb(
                "emulator-5554", pkg, enrolled=True, use_su=True
            )
        assert result.status == "pass"


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
            return_value=_proc(
                0, "emulator-5554\tdevice\nvalue=1\npackage_name=com.google.android.gms\n"
            ),
        ):
            results = api.adb_device_health(device)
        assert "compose.status" not in results
        assert results["frida.handshake"].status == "skip"

    def test_unprivileged_adbd_is_healthy_via_su(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Issue #159 end-to-end: on a genuine adopted phone adbd is uid
        # 2000, so the bare magisk read is permission-denied but the
        # su-wrapped read succeeds. The composed adb-backend health dict
        # must report magisk.zygisk/denylist as pass (not the
        # false-fail this bug caused), AND every magisk read it issued
        # must have gone through ``su -c``.
        device = self._stub()
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        monkeypatch.setattr("socket.create_connection", lambda *a, **k: contextlib.nullcontext())
        magisk_calls: list[list[str]] = []

        def _unprivileged(
            cmd: list[str], *a: object, **k: object
        ) -> subprocess.CompletedProcess[str]:
            del a, k
            cmd_str = " ".join(str(x) for x in cmd)
            if "devices" in cmd:
                return _proc(0, "List of devices attached\nemulator-5554\tdevice\n")
            if "magisk --sqlite" in cmd_str:
                magisk_calls.append(cmd)
                if "su" not in cmd or "-c" not in cmd:
                    return _proc(1, "", "permission denied")
                if "settings" in cmd_str:
                    return _proc(0, "value=1\n")
                return _proc(0, "package_name=com.google.android.gms\n")
            return _proc(0)

        with patch("subprocess.run", side_effect=_unprivileged):
            results = api.adb_device_health(device)
        assert results["magisk.zygisk"].status == "pass"
        assert results["magisk.denylist.com.google.android.gms"].status == "pass"
        # Every magisk read the adb backend issued used su -c.
        assert magisk_calls
        for cmd in magisk_calls:
            assert "su" in cmd
            assert "-c" in cmd


class TestInstanceHealthExitCodeCap:
    def test_doctor_exit_code_capped_at_255(
        self,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
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
