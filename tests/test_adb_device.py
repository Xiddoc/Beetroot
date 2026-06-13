"""Unit tests for the :class:`beetroot.backends.adb.AdbDevice` backend.

All ``subprocess.run`` calls are stubbed via :func:`monkeypatch.setattr`
so the suite is hermetic — no real ``adb`` is ever invoked. The capture
fixture stores the call argv lists so per-test assertions can verify
the exact ``adb -s <serial> ...`` shape that AdbDevice constructs.
"""
from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from conftest import StubRunFailures

from beetroot import api, frida_download, registry
from beetroot.backends import adb as adb_backend


@pytest.fixture
def captured_adb(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch subprocess.run inside backends.adb to capture argv lists."""
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
            stdout="List of devices attached\n",
            stderr="",
        )

    monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
    return captured


def _make_device(
    serial: str = "emulator-5554",
    *,
    host_port: int = 27042,
) -> adb_backend.AdbDevice:
    return adb_backend.AdbDevice(
        name="phone",
        config=registry.AdbBackendConfig(serial=serial),
        host_forward_port=host_port,
    )


class TestProperties:
    def test_name(self) -> None:
        assert _make_device().name == "phone"

    def test_kind_is_adb(self) -> None:
        assert _make_device().kind == "adb"

    def test_adb_address_is_serial(self) -> None:
        assert _make_device(serial="emulator-5556").adb_address == "emulator-5556"

    def test_frida_address_is_localhost_with_host_port(self) -> None:
        dev = _make_device(host_port=27052)
        assert dev.frida_address == "localhost:27052"

    def test_satisfies_device_backend_protocol(self) -> None:
        # The DeviceBackend Protocol is runtime-checkable; AdbDevice
        # must structurally match it so Manager.resolve can return it
        # uniformly.
        assert isinstance(_make_device(), api.DeviceBackend)


def _stub_adb_devices(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0
) -> None:
    """Stub adb_backend.subprocess.run to a single fake response."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def _fake_run(
        cmd: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del cmd, args, kwargs
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)


class TestIsAvailable:
    def test_returns_true_when_serial_listed_as_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_adb_devices(
            monkeypatch,
            "List of devices attached\n"
            "emulator-5554\tdevice\n"
            "emulator-5556\toffline\n",
        )
        assert _make_device(serial="emulator-5554").is_available is True

    def test_returns_false_when_serial_offline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_adb_devices(
            monkeypatch,
            "List of devices attached\nemulator-5554\toffline\n",
        )
        assert _make_device(serial="emulator-5554").is_available is False

    def test_returns_false_when_serial_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_adb_devices(
            monkeypatch, "List of devices attached\n",
        )
        assert _make_device(serial="ghost-9999").is_available is False

    def test_returns_false_when_adb_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_adb_devices(monkeypatch, "", returncode=1)
        assert _make_device().is_available is False

    def test_returns_false_when_adb_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no adb binary on PATH, is_available must report the device
        # as unavailable (so status/ls render a clean row) rather than
        # letting subprocess.run raise FileNotFoundError.
        monkeypatch.setattr(shutil, "which", lambda name: None)

        def _explode(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("subprocess.run must not run when adb is absent")

        monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _explode)
        assert _make_device().is_available is False


class TestInstallFrida:
    def test_emits_full_install_sequence(
        self,
        captured_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Stub frida_download.download so no network hit; return a fake
        # cached path. ``install_frida`` must push it, chmod, launch,
        # and forward the port — in that order.
        fake_cached = tmp_path / "frida-server-16.4.10"
        fake_cached.write_bytes(b"fake-binary")
        monkeypatch.setattr(
            frida_download, "download", lambda version: fake_cached,
        )
        # Stub shutil.which so install_frida's PATH guard sees adb present
        # (the CI runner has no real adb binary), letting the stubbed
        # subprocess.run drive the full install sequence.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        _make_device(serial="emulator-5554", host_port=27052).install_frida(
            "16.4.10"
        )
        assert len(captured_adb) == 4
        # 1. push
        assert captured_adb[0] == [
            "adb", "-s", "emulator-5554", "push",
            str(fake_cached), "/data/local/tmp/frida-server",
        ]
        # 2. chmod
        assert captured_adb[1] == [
            "adb", "-s", "emulator-5554", "shell",
            "chmod", "755", "/data/local/tmp/frida-server",
        ]
        # 3. launch via su
        assert captured_adb[2] == [
            "adb", "-s", "emulator-5554", "shell",
            "su", "-c", "/data/local/tmp/frida-server &",
        ]
        # 4. adb forward (host_port → device 27042)
        assert captured_adb[3] == [
            "adb", "-s", "emulator-5554",
            "forward", "tcp:27052", "tcp:27042",
        ]

    def test_raises_when_adb_not_on_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_cached = tmp_path / "frida-server-16.4.10"
        fake_cached.write_bytes(b"fake-binary")
        monkeypatch.setattr(
            frida_download, "download", lambda version: fake_cached,
        )
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(api.AdbNotInstalledError, match="adb not found on PATH"):
            _make_device().install_frida("16.4.10")

    def test_raises_when_adb_returns_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_cached = tmp_path / "frida-server-16.4.10"
        fake_cached.write_bytes(b"fake-binary")
        monkeypatch.setattr(
            frida_download, "download", lambda version: fake_cached,
        )

        def _fake_run(
            cmd: list[str],
            *args: object,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="device offline",
            )

        monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
        # Stub shutil.which so the PATH guard passes and we reach the
        # nonzero-returncode path under test (CI has no real adb binary).
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        with pytest.raises(RuntimeError, match="adb command"):
            _make_device().install_frida("16.4.10")


class TestShell:
    def test_invokes_adb_s_serial_shell(
        self,
        captured_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        rc = _make_device(serial="emulator-5554").shell()
        assert rc == 0
        assert captured_adb == [["adb", "-s", "emulator-5554", "shell"]]

    def test_raises_when_adb_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(api.AdbNotInstalledError):
            _make_device().shell()


class TestFridaCli:
    def test_invokes_frida_with_dash_h_and_args(
        self,
        captured_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        rc = _make_device(host_port=27052).frida_cli(["-n", "com.example.app"])
        assert rc == 0
        assert captured_adb == [
            ["frida", "-H", "localhost:27052", "-n", "com.example.app"]
        ]

    def test_raises_when_frida_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(api.FridaNotInstalledError):
            _make_device().frida_cli([])


class TestAddModule:
    def test_pushes_to_sdcard_download(
        self,
        captured_adb: list[list[str]],
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        zip_path = tmp_path / "MyModule.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        _make_device(serial="emulator-5554").add_module(str(zip_path))
        # adb push <local> /sdcard/Download/<basename>
        assert captured_adb == [
            [
                "adb", "-s", "emulator-5554", "push",
                str(zip_path),
                "/sdcard/Download/MyModule.zip",
            ],
        ]
        # The user-facing install instruction lands on stderr.
        err = capsys.readouterr().err
        assert "MyModule.zip" in err
        assert "Magisk app" in err
        assert "Modules tab" in err

    def test_rejects_nonexistent_path(
        self,
        captured_adb: list[list[str]],
        tmp_path: Path,
    ) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            _make_device().add_module(str(tmp_path / "missing.zip"))
        assert captured_adb == []

    def test_rejects_directory_instead_of_file(
        self,
        captured_adb: list[list[str]],
        tmp_path: Path,
    ) -> None:
        dir_path = tmp_path / "a_dir.zip"
        dir_path.mkdir()
        with pytest.raises(ValueError, match="directory"):
            _make_device().add_module(str(dir_path))
        assert captured_adb == []

    def test_rejects_non_zip_extension(
        self,
        captured_adb: list[list[str]],
        tmp_path: Path,
    ) -> None:
        not_zip = tmp_path / "module.apk"
        not_zip.write_bytes(b"PK\x03\x04fake")
        with pytest.raises(ValueError, match=r"\.zip"):
            _make_device().add_module(str(not_zip))
        assert captured_adb == []

    def test_sha256_is_ignored_on_safe_default(
        self,
        captured_adb: list[list[str]],
        tmp_path: Path,
    ) -> None:
        # The sha256 kwarg is enforced only by auto_install_modules; on
        # the safe-default push-to-Downloads path it stays a no-op. Pass
        # a deliberately-wrong hex to confirm the parameter is ignored
        # without error.
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        _make_device().add_module(str(zip_path), sha256="0" * 64)
        assert len(captured_adb) == 1


class TestAutoInstallModules:
    """The issue-#7 root-driven install path (`module --auto-install`)."""

    def test_emits_push_install_rm_sequence(
        self,
        captured_adb: list[list[str]],
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "MyModule.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        results = _make_device(serial="emulator-5554").auto_install_modules(
            [str(zip_path)]
        )
        assert captured_adb == [
            *preflight_argv,
            [
                "adb", "-s", "emulator-5554", "push",
                str(zip_path), "/data/local/tmp/beetroot-module-0.zip",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c",
                "'magisk --install-module /data/local/tmp/beetroot-module-0.zip'",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c", "'rm -f /data/local/tmp/beetroot-module-0.zip'",
            ],
        ]
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].source == str(zip_path)
        assert "magisk --install-module" in results[0].detail
        # The ok-row detail must show the command as actually executed
        # (outer-quoted), not a prettified unquoted variant.
        assert results[0].detail == (
            "installed via `su -c "
            "'magisk --install-module /data/local/tmp/beetroot-module-0.zip'`"
        )

    def test_remote_path_with_spaces_is_shell_quoted(
        self,
        captured_adb: list[list[str]],
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The su -c payload is shell-parsed TWICE on-device (device shell,
        # then MagiskSU's own `sh -c` re-join), so the untrusted local
        # basename must never appear in it: the zip is pushed under a
        # synthesized safe name and the command string is quoted for the
        # outer parse on top.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "My Module.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        results = _make_device().auto_install_modules([str(zip_path)])
        assert results[0].ok is True
        assert captured_adb == [
            *preflight_argv,
            [
                "adb", "-s", "emulator-5554", "push",
                str(zip_path), "/data/local/tmp/beetroot-module-0.zip",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c",
                "'magisk --install-module /data/local/tmp/beetroot-module-0.zip'",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c", "'rm -f /data/local/tmp/beetroot-module-0.zip'",
            ],
        ]

    def test_hostile_basename_never_reaches_the_device_shell(
        self,
        captured_adb: list[list[str]],
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A `$(...)` in the zip's filename would execute AS ROOT under
        # MagiskSU's second `sh -c` parse if the basename were embedded
        # in the command string. The synthesized remote name must contain
        # no shell metacharacters, and the hostile substring must appear
        # in no device-side command at all (the local host path in the
        # push argv is exec'd directly — argv never passes through a
        # shell on the host).
        import re

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "$(boom).zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        results = _make_device().auto_install_modules([str(zip_path)])
        assert results[0].ok is True
        remote = captured_adb[2][-1]  # the push target (after the two probes)
        assert re.fullmatch(r"(/[A-Za-z0-9._-]+)+", remote)
        assert captured_adb == [
            *preflight_argv,
            [
                "adb", "-s", "emulator-5554", "push",
                str(zip_path), "/data/local/tmp/beetroot-module-0.zip",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c",
                "'magisk --install-module /data/local/tmp/beetroot-module-0.zip'",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c", "'rm -f /data/local/tmp/beetroot-module-0.zip'",
            ],
        ]
        assert "$(boom)" not in remote
        # Every device-shell invocation (probes + install + rm) — only the
        # push argv may carry the hostile substring, in its host-local path.
        for cmd in (*captured_adb[:2], *captured_adb[3:]):
            for element in cmd:
                assert "$(boom)" not in element

    def test_matching_sha256_installs(
        self,
        captured_adb: list[list[str]],
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import hashlib

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        payload = b"PK\x03\x04fake"
        zip_path = tmp_path / "Pinned.zip"
        zip_path.write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        results = _make_device().auto_install_modules(
            [str(zip_path)], sha256s=[sha]
        )
        assert results[0].ok is True
        assert len(captured_adb) == len(preflight_argv) + 3

    def test_sha256_mismatch_refuses_to_push(
        self,
        captured_adb: list[list[str]],
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "Tampered.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        results = _make_device().auto_install_modules(
            [str(zip_path)], sha256s=["0" * 64]
        )
        # The pre-flight probes ran, but nothing was pushed.
        assert captured_adb == preflight_argv
        assert results[0].ok is False
        assert "sha256 mismatch" in results[0].detail
        assert zip_path.exists()

    def test_failed_install_reports_and_continues(
        self,
        stub_run_failures: StubRunFailures,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # First module's magisk --install-module exits non-zero; the
        # device is still listed by `adb devices`, so the re-probe keeps
        # the batch going — the second module is still processed and
        # succeeds. The temp zip of the failed module must still be rm'd.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        bad = tmp_path / "Bad.zip"
        bad.write_bytes(b"PK\x03\x04bad")
        good = tmp_path / "Good.zip"
        good.write_bytes(b"PK\x03\x04good")
        captured = stub_run_failures(
            lambda cmd: (
                "magisk --install-module" in cmd[-1]
                and "beetroot-module-0.zip" in cmd[-1]
            ),
            stderr="! Unable to install",
        )
        results = _make_device().auto_install_modules([str(bad), str(good)])
        assert [r.ok for r in results] == [False, True]
        assert "Unable to install" in results[0].detail
        assert "'rm -f /data/local/tmp/beetroot-module-0.zip'" in [
            cmd[-1] for cmd in captured
        ]
        assert "'magisk --install-module /data/local/tmp/beetroot-module-1.zip'" in [
            cmd[-1] for cmd in captured
        ]

    def test_failed_push_skips_rm_and_continues_to_next_module(
        self,
        stub_run_failures: StubRunFailures,
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # When `adb push` itself fails there is nothing on the device to
        # clean up: the failed module gets a failed row with NO install
        # and NO rm attempt, and the batch continues to the next module.
        # The device is still listed by `adb devices`, so the re-probe
        # keeps the batch going — its only extra call is that re-probe.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        bad = tmp_path / "Bad.zip"
        bad.write_bytes(b"PK\x03\x04bad")
        good = tmp_path / "Good.zip"
        good.write_bytes(b"PK\x03\x04good")
        captured = stub_run_failures(
            lambda cmd: cmd[3] == "push" and cmd[4] == str(bad),
            stderr="adb: error: failed to copy",
        )
        results = _make_device().auto_install_modules([str(bad), str(good)])
        assert [r.ok for r in results] == [False, True]
        assert "failed to copy" in results[0].detail
        # No install and no rm for the failed push — only the failed push
        # itself plus the connectivity re-probe, then the full sequence
        # for the second module.
        assert captured == [
            *preflight_argv,
            [
                "adb", "-s", "emulator-5554", "push",
                str(bad), "/data/local/tmp/beetroot-module-0.zip",
            ],
            ["adb", "devices"],
            [
                "adb", "-s", "emulator-5554", "push",
                str(good), "/data/local/tmp/beetroot-module-1.zip",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c",
                "'magisk --install-module /data/local/tmp/beetroot-module-1.zip'",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c", "'rm -f /data/local/tmp/beetroot-module-1.zip'",
            ],
        ]

    def test_failed_rm_does_not_mask_install_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")

        def _fake_run(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            failing = "rm -f" in cmd[-1]
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1 if failing else 0,
                stdout="",
                stderr="rm: read-only" if failing else "",
            )

        monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
        results = _make_device().auto_install_modules([str(zip_path)])
        assert results[0].ok is True

    def test_failed_rm_does_not_mask_failed_install_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Both the install AND the cleanup rm fail: the row must carry
        # the install error, never the rm error from the finally block.
        # The `adb devices` re-probe lists the device as available, so
        # the adb-level install failure becomes a per-module row.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")

        def _fake_run(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            if cmd == ["adb", "devices"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="List of devices attached\nemulator-5554\tdevice\n",
                    stderr="",
                )
            if "magisk --install-module" in cmd[-1]:
                stderr = "! Unable to install"
            elif "rm -f" in cmd[-1]:
                stderr = "rm: read-only"
            else:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr=stderr,
            )

        monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
        results = _make_device().auto_install_modules([str(zip_path)])
        assert results[0].ok is False
        assert "Unable to install" in results[0].detail
        assert "rm: read-only" not in results[0].detail

    def test_missing_zip_becomes_failed_result(
        self,
        captured_adb: list[list[str]],
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        results = _make_device().auto_install_modules(
            [str(tmp_path / "missing.zip")]
        )
        # The pre-flight probes ran, but nothing was pushed — and the
        # host-side ValueError never triggers an `adb devices` re-probe.
        assert captured_adb == preflight_argv
        assert results[0].ok is False
        assert "does not exist" in results[0].detail

    def test_raises_when_adb_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(api.AdbNotInstalledError, match="adb not found on PATH"):
            _make_device().auto_install_modules([str(tmp_path / "M.zip")])

    def test_rejects_mismatched_sha256s_length(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        with pytest.raises(ValueError, match="one digest per source"):
            _make_device().auto_install_modules(
                [str(zip_path)], sha256s=["0" * 64, "1" * 64]
            )

    def test_results_preserve_request_order(
        self,
        captured_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        first = tmp_path / "First.zip"
        first.write_bytes(b"PK\x03\x04a")
        second = tmp_path / "Second.zip"
        second.write_bytes(b"PK\x03\x04b")
        results = _make_device().auto_install_modules([str(first), str(second)])
        assert [r.source for r in results] == [str(first), str(second)]
        assert all(r.ok for r in results)


_OFFLINE_DEVICES = "List of devices attached\n"  # serial absent → unavailable


class TestAutoInstallPreflight:
    """Issue #38: whole-device failures abort with one friendly error.

    Connectivity is decided authoritatively by an ``adb devices``
    re-probe (``serial_is_available``), never by sniffing the probe's
    stderr — so every failure path here also emits ``["adb", "devices"]``,
    and the captured-argv assertions account for it.
    """

    def test_unrooted_device_fails_fast_before_any_push(
        self,
        stub_run_failures: StubRunFailures,
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        # Root probe fails but `adb devices` still lists the serial, so
        # the re-probe routes it to the no-usable-root diagnosis.
        captured = stub_run_failures(
            lambda cmd: cmd[-1] == "true",
            stdout="su: inaccessible or not found\n",
        )
        with pytest.raises(
            api.DevicePreflightError,
            match=r"device 'emulator-5554' has no usable root "
            r"\(su missing or denied root — check the device is rooted and "
            r"approve the Magisk superuser prompt\)",
        ) as exc_info:
            _make_device().auto_install_modules([str(zip_path)])
        # Only the root probe ran (then the connectivity re-probe) —
        # no magisk probe, no push.
        assert captured == [preflight_argv[0], ["adb", "devices"]]
        assert exc_info.value.results == []

    def test_missing_magisk_fails_fast_before_any_push(
        self,
        stub_run_failures: StubRunFailures,
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        captured = stub_run_failures(lambda cmd: cmd[-1] == "'command -v magisk'")
        with pytest.raises(
            api.DevicePreflightError,
            match=r"device 'emulator-5554' has root but no usable magisk binary "
            r"\(install or repair the Magisk app, then retry\)",
        ) as exc_info:
            _make_device().auto_install_modules([str(zip_path)])
        # Both probes ran (root passed, magisk failed), then the
        # connectivity re-probe — no push.
        assert captured == [*preflight_argv, ["adb", "devices"]]
        assert exc_info.value.results == []

    def test_device_offline_during_root_probe(
        self,
        stub_run_failures: StubRunFailures,
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        # Root probe fails AND the serial is absent from `adb devices` →
        # offline diagnosis, with the probe's stderr appended.
        captured = stub_run_failures(
            lambda cmd: cmd[-1] == "true",
            stderr="adb: device offline\n",
            devices_stdout=_OFFLINE_DEVICES,
        )
        with pytest.raises(
            api.DevicePreflightError,
            match=r"device 'emulator-5554' is offline or not connected "
            r"\(reconnect it, accept its USB-debugging authorization prompt "
            r"if one is shown, and check `adb devices`\) "
            r"— last adb error: adb: device offline",
        ):
            _make_device().auto_install_modules([str(zip_path)])
        assert captured == [preflight_argv[0], ["adb", "devices"]]

    def test_root_probe_stderr_cannot_force_offline_misclassification(
        self,
        stub_run_failures: StubRunFailures,
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Device-controlled probe stderr says "adb: device offline", but
        # `adb devices` still lists the serial as available. Connectivity
        # is decided by the re-probe, NOT the text, so this must be the
        # no-usable-root diagnosis — never offline.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        captured = stub_run_failures(
            lambda cmd: cmd[-1] == "true",
            stderr="adb: device offline\n",  # default devices_stdout lists the serial
        )
        with pytest.raises(api.DevicePreflightError, match="has no usable root"):
            _make_device().auto_install_modules([str(zip_path)])
        assert captured == [preflight_argv[0], ["adb", "devices"]]

    def test_device_offline_during_magisk_probe(
        self,
        stub_run_failures: StubRunFailures,
        preflight_argv: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Root passes; the magisk probe fails AND the serial is absent
        # from `adb devices` → offline, never blamed on a missing magisk
        # binary.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        captured = stub_run_failures(
            lambda cmd: cmd[-1] == "'command -v magisk'",
            stderr="adb: device offline\n",
            devices_stdout=_OFFLINE_DEVICES,
        )
        with pytest.raises(
            api.DevicePreflightError,
            match=r"is offline or not connected .* — last adb error: adb: device offline",
        ):
            _make_device().auto_install_modules([str(zip_path)])
        assert captured == [*preflight_argv, ["adb", "devices"]]

    def test_mid_batch_offline_aborts_with_partial_results(
        self,
        stub_run_failures: StubRunFailures,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # First module installs; the device drops before the second's
        # push. The second push fails (RuntimeError) and `adb devices` no
        # longer lists the serial → the batch aborts with the friendly
        # offline error carrying the first module's ok row. The third is
        # never pushed.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        first = tmp_path / "First.zip"
        first.write_bytes(b"PK\x03\x04a")
        second = tmp_path / "Second.zip"
        second.write_bytes(b"PK\x03\x04b")
        third = tmp_path / "Third.zip"
        third.write_bytes(b"PK\x03\x04c")
        captured = stub_run_failures(
            lambda cmd: cmd[3] == "push" and cmd[4] == str(second),
            stderr="adb: error: device offline\n",
            devices_stdout=_OFFLINE_DEVICES,
        )
        with pytest.raises(
            api.DevicePreflightError,
            match=r"is offline or not connected .* \(1 remaining module skipped\)",
        ) as exc_info:
            _make_device().auto_install_modules(
                [str(first), str(second), str(third)]
            )
        assert [(r.source, r.ok) for r in exc_info.value.results] == [
            (str(first), True),
        ]
        # The failed second push, then the connectivity re-probe, are the
        # last two adb calls — the third module is never pushed.
        assert captured[-2:] == [
            [
                "adb", "-s", "emulator-5554", "push",
                str(second), "/data/local/tmp/beetroot-module-1.zip",
            ],
            ["adb", "devices"],
        ]
        assert str(third) not in [arg for cmd in captured for arg in cmd]


class TestAutoInstallConnectivityReprobe:
    """Issue #38 regression: untrusted text must never decide connectivity."""

    def test_hostile_host_path_keeps_per_module_contract(
        self,
        stub_run_failures: StubRunFailures,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A nonexistent source whose NAME embeds "device offline" raises a
        # host-side ValueError. It must become a per-module failed row and
        # let the batch continue to a valid second module — and crucially
        # it must NEVER trigger an `adb devices` re-probe (the ValueError
        # branch can't mean the device is offline).
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        hostile = tmp_path / "device offline.zip"  # never created → ValueError
        good = tmp_path / "Good.zip"
        good.write_bytes(b"PK\x03\x04good")
        captured = stub_run_failures(lambda cmd: False)  # nothing on-device fails
        results = _make_device().auto_install_modules([str(hostile), str(good)])
        assert [(r.source, r.ok) for r in results] == [
            (str(hostile), False),
            (str(good), True),
        ]
        assert "does not exist" in results[0].detail
        # The ValueError path must never re-probe connectivity.
        assert ["adb", "devices"] not in captured

    def test_hostile_sha256_mismatch_keeps_per_module_contract(
        self,
        stub_run_failures: StubRunFailures,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A real zip whose name embeds "device offline" but whose sha256
        # mismatches (host-side ValueError) must also stay a per-module
        # row and never re-probe.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        hostile = tmp_path / "device offline.zip"
        hostile.write_bytes(b"PK\x03\x04fake")
        good = tmp_path / "Good.zip"
        good.write_bytes(b"PK\x03\x04good")
        captured = stub_run_failures(lambda cmd: False)
        results = _make_device().auto_install_modules(
            [str(hostile), str(good)], sha256s=["0" * 64, None]
        )
        assert [(r.source, r.ok) for r in results] == [
            (str(hostile), False),
            (str(good), True),
        ]
        assert "sha256 mismatch" in results[0].detail
        assert ["adb", "devices"] not in captured

    def test_hostile_install_stderr_cannot_abort_the_batch(
        self,
        stub_run_failures: StubRunFailures,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The first module's install fails with module-controlled stderr
        # spoofing "adb: device offline", but `adb devices` still lists
        # the serial → the failure stays a per-module row and the batch
        # continues to the (succeeding) second module. No abort.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        bad = tmp_path / "Bad.zip"
        bad.write_bytes(b"PK\x03\x04bad")
        good = tmp_path / "Good.zip"
        good.write_bytes(b"PK\x03\x04good")
        captured = stub_run_failures(
            lambda cmd: (
                "magisk --install-module" in cmd[-1]
                and "beetroot-module-0.zip" in cmd[-1]
            ),
            stderr="adb: device offline (spoofed by the module)",
        )
        results = _make_device().auto_install_modules([str(bad), str(good)])
        assert [(r.source, r.ok) for r in results] == [
            (str(bad), False),
            (str(good), True),
        ]
        assert "device offline (spoofed by the module)" in results[0].detail
        # The adb-level failure DID re-probe (and found the device
        # available), so the batch kept going.
        assert ["adb", "devices"] in captured


class TestCapabilityGating:
    """AdbDevice does not implement Lifecycle/Snapshottable — gating is at the CLI layer."""

    def test_adb_device_does_not_implement_lifecycle(self) -> None:
        dev = _make_device()
        assert not isinstance(dev, api.Lifecycle)

    def test_adb_device_does_not_implement_snapshottable(self) -> None:
        dev = _make_device()
        assert not isinstance(dev, api.Snapshottable)

    def test_adb_device_implements_module_installer(self) -> None:
        dev = _make_device()
        assert isinstance(dev, api.ModuleInstaller)

    def test_adb_device_implements_auto_module_installer(self) -> None:
        dev = _make_device()
        assert isinstance(dev, api.AutoModuleInstaller)

    def test_adb_device_implements_health_checkable(self) -> None:
        dev = _make_device()
        assert isinstance(dev, api.HealthCheckable)


class TestFromMeta:
    def test_constructs_from_registry_meta(
        self, isolated_registry: Path,
    ) -> None:
        del isolated_registry
        cfg = registry.AdbBackendConfig(serial="emulator-5554")
        registry.add_allocating("phone", backend=cfg)
        dev = adb_backend.AdbDevice.from_meta("phone", cfg)
        assert isinstance(dev, adb_backend.AdbDevice)
        assert dev.name == "phone"
        # Index 0 → frida port 27042 per stride-of-10 scheme.
        assert dev.frida_address == "localhost:27042"

    def test_rejects_wrong_config_kind(
        self, isolated_registry: Path,
    ) -> None:
        del isolated_registry
        wrong = registry.RedroidBackendConfig(absolute_path="/tmp/x")
        with pytest.raises(api.InstanceNotFoundError, match="AdbBackendConfig"):
            adb_backend.AdbDevice.from_meta("phone", wrong)

    def test_raises_when_name_not_in_registry(
        self, isolated_registry: Path,
    ) -> None:
        del isolated_registry
        cfg = registry.AdbBackendConfig(serial="emulator-5554")
        with pytest.raises(api.InstanceNotFoundError, match="phone"):
            adb_backend.AdbDevice.from_meta("phone", cfg)

    def test_host_port_derived_from_allocated_index(
        self, isolated_registry: Path,
    ) -> None:
        del isolated_registry
        # First-allocated → index 0 → frida 27042; second → 27052.
        registry.add_allocating(
            "first", backend=registry.AdbBackendConfig(serial="s1"),
        )
        registry.add_allocating(
            "second", backend=registry.AdbBackendConfig(serial="s2"),
        )
        second_cfg = registry.AdbBackendConfig(serial="s2")
        dev = adb_backend.AdbDevice.from_meta("second", second_cfg)
        assert dev.frida_address == "localhost:27052"


class TestHealth:
    """T7's :meth:`AdbDevice.health` wire-up follow-up."""

    def test_health_returns_check_result_dict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # AdbDevice.health() delegates to api.adb_device_health which
        # shells out via subprocess.run; stub shutil.which so all the
        # PATH probes report present, and stub subprocess.run for the
        # adb/magisk calls to a successful response. The frida.handshake
        # check uses socket.create_connection, so stub that too to keep
        # the suite hermetic — no real network connection.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            socket, "create_connection", lambda *a, **k: contextlib.nullcontext(),
        )

        def _ok(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            stdout = ""
            cmd_str = " ".join(str(x) for x in cmd)
            if cmd[:1] == ["adb"] and "devices" in cmd:
                stdout = "List of devices attached\nemulator-5554\tdevice\n"
            elif "magisk" in cmd and "settings" in cmd_str:
                stdout = "value=1\n"
            elif "magisk" in cmd and "denylist" in cmd_str:
                stdout = "package_name=com.google.android.gms\n"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", _ok)
        results = _make_device(serial="emulator-5554").health()
        # Same key vocabulary as Instance.health (minus compose.status).
        assert "compose.status" not in results
        assert "adb.serial" in results
        assert "magisk.zygisk" in results
        for r in results.values():
            assert isinstance(r, api.CheckResult)

    def test_health_method_delegates_to_free_function(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The method body MUST call the free function so the two stay
        # byte-identical — otherwise the back-compat shim drifts.
        calls: list[object] = []
        sentinel: dict[str, api.CheckResult] = {"adb.serial": api.CheckResult(status="pass")}

        def _spy(device: api.DeviceBackend) -> dict[str, api.CheckResult]:
            calls.append(device)
            return sentinel

        monkeypatch.setattr("beetroot.backends.adb.adb_device_health", _spy)
        dev = _make_device()
        out = dev.health()
        assert out is sentinel
        assert calls == [dev]


