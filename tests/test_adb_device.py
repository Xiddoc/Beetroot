"""Unit tests for the :class:`beetroot.backends.adb.AdbDevice` backend.

All ``subprocess.run`` calls are stubbed via :func:`monkeypatch.setattr`
so the suite is hermetic — no real ``adb`` is ever invoked. The capture
fixture stores the call argv lists so per-test assertions can verify
the exact ``adb -s <serial> ...`` shape that AdbDevice constructs.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

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

    def test_sha256_is_currently_advisory(
        self,
        captured_adb: list[list[str]],
        tmp_path: Path,
    ) -> None:
        # The sha256 kwarg is reserved for the v0.5 auto-install variant
        # and is intentionally a no-op for v0.4. Pass a deliberately-
        # wrong hex to confirm the parameter is ignored without error.
        zip_path = tmp_path / "M.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        _make_device().add_module(str(zip_path), sha256="0" * 64)
        assert len(captured_adb) == 1


class TestLifecycleStubs:
    """Lifecycle verbs all raise :class:`BackendCapabilityError`."""

    @pytest.mark.parametrize("method", ["up", "down", "restart", "apply"])
    def test_zero_arg_lifecycle_methods_raise(self, method: str) -> None:
        dev = _make_device()
        with pytest.raises(api.BackendCapabilityError, match="adb-backed"):
            getattr(dev, method)()

    def test_destroy_raises(self) -> None:
        with pytest.raises(api.BackendCapabilityError, match="adb-backed"):
            _make_device().destroy(yes=True)

    def test_snapshot_raises(self, tmp_path: Path) -> None:
        with pytest.raises(api.BackendCapabilityError, match="adb-backed"):
            _make_device().snapshot(tmp_path / "out.tar.zst")

    def test_down_error_contains_real_name_not_literal_brace(self) -> None:
        dev = _make_device(serial="emulator-5554")
        with pytest.raises(api.BackendCapabilityError, match="phone"):
            dev.down()

    def test_down_error_does_not_contain_v05_parenthetical(self) -> None:
        dev = _make_device()
        with pytest.raises(api.BackendCapabilityError) as exc_info:
            dev.down()
        assert "(v0.5)" not in str(exc_info.value)
        assert "{name}" not in str(exc_info.value)

    def test_destroy_error_contains_real_name_not_literal_brace(self) -> None:
        dev = _make_device(serial="emulator-5554")
        with pytest.raises(api.BackendCapabilityError, match="phone"):
            dev.destroy(yes=True)

    def test_destroy_error_does_not_contain_v05_parenthetical(self) -> None:
        dev = _make_device()
        with pytest.raises(api.BackendCapabilityError) as exc_info:
            dev.destroy(yes=True)
        assert "(v0.5)" not in str(exc_info.value)
        assert "{name}" not in str(exc_info.value)


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
        with pytest.raises(TypeError, match="AdbBackendConfig"):
            adb_backend.AdbDevice.from_meta("phone", wrong)

    def test_raises_when_name_not_in_registry(
        self, isolated_registry: Path,
    ) -> None:
        del isolated_registry
        cfg = registry.AdbBackendConfig(serial="emulator-5554")
        with pytest.raises(LookupError, match="phone"):
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
        # adb/nc/magisk calls to a successful response.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

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


