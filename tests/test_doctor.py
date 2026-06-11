"""Tests for the ``beetroot doctor <name>`` verb + ``Instance.health()``."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, paths, registry

runner = CliRunner()

# A stable ``magisk --sqlite ... zygisk`` output line that matches the
# substring ``value=1`` the health check looks for.
_ZYGISK_ON: Final = "value=1\n"
_ZYGISK_OFF: Final = "value=0\n"
_DENYLIST_GMS_ENROLLED: Final = "package_name=com.google.android.gms\n"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _healthy_subprocess(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Stub subprocess.run to make every doctor probe return healthy."""
    cmd = args[0] if args else []
    if not isinstance(cmd, list):
        return _proc()
    cmd_str = " ".join(str(x) for x in cmd)
    # docker compose ps --format json → "running"; magisk settings →
    # ``value=1``; magisk denylist → enrolled; adb devices → device;
    # nc / fallthrough → succeed. Single return at the end keeps
    # PLR0911 happy without splintering the dispatch into helpers.
    stdout = ""
    if "compose" in cmd and "ps" in cmd:
        stdout = '{"State": "running"}\n'
    elif cmd[:1] == ["adb"] and "magisk" in cmd and "settings" in cmd_str:
        stdout = _ZYGISK_ON
    elif cmd[:1] == ["adb"] and "magisk" in cmd and "denylist" in cmd_str:
        stdout = _DENYLIST_GMS_ENROLLED
    elif cmd[:1] == ["adb"] and "devices" in cmd:
        stdout = "List of devices attached\nemulator-5554\tdevice\n"
    return _proc(stdout=stdout)


class TestDoctorRedroid:
    def test_healthy_instance_exits_0(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with patch("subprocess.run", side_effect=_healthy_subprocess):
            result = runner.invoke(cli.app, ["doctor", "alpha"])
        assert result.exit_code == 0, (result.stdout, result.stderr)
        assert "compose.status: pass" in result.stdout
        assert "adb.connect: pass" in result.stdout
        assert "magisk.zygisk: pass" in result.stdout
        # Frida is opt-out by default since v0.3 — minimal-default
        # InstanceConfig has frida=None so frida.handshake skips.
        assert "frida.handshake: skip" in result.stdout

    def test_zygisk_disabled_exits_with_fail_count(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])

        def _proc_side(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else []
            if isinstance(cmd, list) and "magisk" in cmd and "settings" in " ".join(str(x) for x in cmd):
                return _proc(stdout=_ZYGISK_OFF)
            return _healthy_subprocess(*args, **kwargs)

        with patch("subprocess.run", side_effect=_proc_side):
            result = runner.invoke(cli.app, ["doctor", "alpha"])
        assert result.exit_code == 1, (result.stdout, result.stderr)
        assert "magisk.zygisk: fail expected 1, got 0" in result.stdout

    def test_multi_fail_exit_code_equals_fail_count(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])

        def _proc_side(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            cmd = args[0] if args else []
            if not isinstance(cmd, list):
                return _proc(returncode=1)
            # compose ps reports "exited" → compose.status fail.
            if "compose" in cmd and "ps" in cmd:
                return _proc(stdout='{"State": "exited"}\n')
            # adb connect fails.
            if cmd[:2] == ["adb", "connect"]:
                return _proc(returncode=1, stderr="cannot connect")
            # magisk zygisk read returns OFF (fail).
            if cmd[:1] == ["adb"] and "magisk" in cmd:
                return _proc(stdout=_ZYGISK_OFF)
            return _proc()

        with patch("subprocess.run", side_effect=_proc_side):
            result = runner.invoke(cli.app, ["doctor", "alpha"])
        # compose.status, adb.connect, magisk.zygisk, magisk.denylist.gms → 4 fails.
        assert result.exit_code == 4, (result.stdout, result.stderr)
        assert "compose.status: fail" in result.stdout
        assert "adb.connect: fail" in result.stdout
        assert "magisk.zygisk: fail" in result.stdout

    def test_frida_disabled_skips_handshake(self, cli_root: Path) -> None:
        # Default InstanceConfig has frida=None — the skip path.
        runner.invoke(cli.app, ["create", "alpha"])
        with patch("subprocess.run", side_effect=_healthy_subprocess):
            result = runner.invoke(cli.app, ["doctor", "alpha"])
        assert "frida.handshake: skip frida not configured" in result.stdout

    def test_frida_enabled_handshake_runs(self, cli_root: Path) -> None:
        del cli_root  # fixture present only for XDG isolation
        # Make frida configured, then re-apply so the instance picks
        # it up. With frida enabled, the socket probe runs.
        runner.invoke(cli.app, ["create", "alpha"])
        root = registry.instance_path("alpha")
        yaml_path = root / "beetroot.yaml"
        yaml_path.write_text(
            "api_version: 3\n"
            "android:\n  version: 14\n"
            "frida:\n  version: 16.4.10\n",
        )
        # The frida probe now connects directly via socket — mock it to
        # succeed so the handshake reports a live listener.
        with (
            patch("subprocess.run", side_effect=_healthy_subprocess),
            patch("socket.create_connection"),
        ):
            result = runner.invoke(cli.app, ["doctor", "alpha"])
        assert "frida.handshake: pass" in result.stdout


class TestDoctorAdb:
    def _seed_adb_instance(self, cli_root: Path, name: str = "phone") -> None:
        del cli_root  # fixture present only for XDG isolation
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = registry.InstanceMeta(
            backend=registry.AdbBackendConfig(serial="emulator-5554"),
            index=0,
            created_at=datetime.now(UTC),
        )
        doc = registry.RegistryFile(instances={name: meta})
        path.write_text(doc.model_dump_json(indent=2))

    def test_healthy_adb_omits_compose_status(self, cli_root: Path) -> None:
        # adb_device_health is the dispatch target for adb-kind. T5
        # hasn't landed AdbDevice yet, so we register a minimal stub
        # via beetroot.backends so Manager.resolve can hand back an
        # object satisfying DeviceBackend.
        from beetroot import backends

        class _StubAdb:
            def __init__(self, name: str, serial: str) -> None:
                self._name = name
                self._serial = serial

            @property
            def name(self) -> str:
                return self._name

            @property
            def kind(self) -> str:
                return "adb"

            @property
            def adb_address(self) -> str:
                return self._serial

            @property
            def frida_address(self) -> str:
                return "localhost:27042"

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

            @classmethod
            def from_meta(cls, name: str, backend: registry.BackendConfig) -> _StubAdb:
                assert isinstance(backend, registry.AdbBackendConfig)
                return cls(name, backend.serial)

        # Avoid colliding with a real registration if T5 lands before
        # this test runs: only insert the stub if no "adb" backend is
        # registered yet, and only pop our stub on the way out (don't
        # delete the real AdbDevice T5 will register at import time).
        inserted_stub = "adb" not in backends._BACKEND_REGISTRY
        if inserted_stub:
            backends.register_backend("adb", _StubAdb)
        try:
            self._seed_adb_instance(cli_root)
            with (
                patch("subprocess.run", side_effect=_healthy_subprocess),
                patch("socket.create_connection"),
            ):
                result = runner.invoke(cli.app, ["doctor", "phone"])
        finally:
            if inserted_stub:
                backends._BACKEND_REGISTRY.pop("adb", None)
        assert result.exit_code == 0, (result.stdout, result.stderr)
        assert "compose.status" not in result.stdout
        assert "adb.serial: pass" in result.stdout
        assert "magisk.zygisk: pass" in result.stdout


class TestInstanceHealthAPI:
    def test_health_returns_check_result_dict(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        inst = api.Instance.load("alpha")
        with patch("subprocess.run", side_effect=_healthy_subprocess):
            results = inst.health()
        assert "compose.status" in results
        assert "magisk.zygisk" in results
        for r in results.values():
            assert isinstance(r, api.CheckResult)

    def test_check_result_is_frozen(self) -> None:
        import pydantic

        cr = api.CheckResult(status="pass")
        with pytest.raises(pydantic.ValidationError):
            cr.status = "fail"  # type: ignore[misc]  # asserting frozen behaviour

