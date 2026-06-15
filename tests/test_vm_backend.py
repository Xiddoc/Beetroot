"""Tests for beetroot.backends.vm.VmDeviceBackend."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from beetroot import api, config, frida_download, registry
from beetroot.backends import vm as vm_backend
from beetroot.settings import Settings
from beetroot.vm import qemu


def _write_yaml(root: Path, cfg: config.InstanceConfig) -> None:
    config.write_yaml(root / "beetroot.yaml", cfg)


def _make_backend(
    tmp_path: Path,
    *,
    cfg: config.InstanceConfig | None = None,
    index: int = 0,
) -> vm_backend.VmDeviceBackend:
    root = tmp_path / "inst"
    root.mkdir(exist_ok=True)
    effective = cfg if cfg is not None else config.InstanceConfig(binder="vm")
    _write_yaml(root, effective)
    return vm_backend.VmDeviceBackend(name="vmphone", root=root, cfg=effective, index=index)


# ---------------------------------------------------------------------------
# from_meta
# ---------------------------------------------------------------------------


class TestFromMeta:
    def test_from_meta_builds_backend(self, isolated_registry: Path) -> None:
        root = isolated_registry / "inst"
        root.mkdir()
        _write_yaml(root, config.InstanceConfig(binder="vm"))
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        registry.add_allocating("vmphone", backend=cfg)
        backend = vm_backend.VmDeviceBackend.from_meta("vmphone", cfg)
        assert backend.kind == "vm"
        assert backend.name == "vmphone"
        assert backend.root == root

    def test_from_meta_rejects_wrong_config_kind(self, isolated_registry: Path) -> None:
        bad = registry.AdbBackendConfig(serial="x")
        with pytest.raises(api.InstanceNotFoundError, match="VmBackendConfig"):
            vm_backend.VmDeviceBackend.from_meta("vmphone", bad)

    def test_from_meta_missing_registry_row(self, isolated_registry: Path) -> None:
        cfg = registry.VmBackendConfig(absolute_path=str(isolated_registry / "inst"))
        with pytest.raises(api.InstanceNotFoundError, match="cannot derive ports"):
            vm_backend.VmDeviceBackend.from_meta("ghost", cfg)

    def test_from_meta_orphan_missing_yaml(self, isolated_registry: Path) -> None:
        root = isolated_registry / "gone"
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        registry.add_allocating("vmphone", backend=cfg)
        with pytest.raises(api.InstanceNotFoundError, match=r"no beetroot\.yaml"):
            vm_backend.VmDeviceBackend.from_meta("vmphone", cfg)


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


class TestProtocolSurface:
    def test_satisfies_protocols(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        assert isinstance(backend, api.DeviceBackend)
        assert isinstance(backend, api.Lifecycle)
        assert isinstance(backend, api.HealthCheckable)

    def test_addresses_use_resolved_ports(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path, index=1)
        assert backend.adb_address == "localhost:5565"
        assert backend.frida_address == "localhost:27052"
        assert backend.config.binder == "vm"

    def test_is_available_reflects_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: True)
        assert backend.is_available is True
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)
        assert backend.is_available is False


# ---------------------------------------------------------------------------
# install_frida / shell / frida_cli
# ---------------------------------------------------------------------------


class TestInstallFrida:
    def test_uses_config_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _make_backend(
            tmp_path, cfg=config.InstanceConfig(binder="vm", frida=config.Frida(version="16.4.10")),
        )
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            frida_download,
            "stage_for_instance",
            lambda root, version: seen.update(root=root, version=version),
        )
        backend.install_frida()
        assert seen["version"] == "16.4.10"

    def test_explicit_version_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            frida_download,
            "stage_for_instance",
            lambda root, version: seen.update(version=version),
        )
        backend.install_frida("16.5.0")
        assert seen["version"] == "16.5.0"

    def test_no_frida_block_and_no_version_errors(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)  # default cfg has no frida block
        with pytest.raises(ValueError, match="no frida: block"):
            backend.install_frida()


class TestShell:
    def test_shell_connects_then_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        captured: list[list[str]] = []

        def _run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _run)
        rc = backend.shell(["-c", "id"])
        assert rc == 0
        assert captured[0] == ["adb", "connect", "localhost:5555"]
        assert captured[1] == ["adb", "-s", "localhost:5555", "shell", "-c", "id"]

    def test_shell_without_adb_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        with pytest.raises(api.AdbNotInstalledError):
            backend.shell()


class TestFridaCli:
    def test_frida_cli_prepends_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        captured: list[list[str]] = []

        def _run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=3)

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _run)
        rc = backend.frida_cli(["-n", "com.app"])
        assert rc == 3
        assert captured[0] == ["frida", "-H", "localhost:27042", "-n", "com.app"]

    def test_frida_cli_without_frida_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        with pytest.raises(api.FridaNotInstalledError):
            backend.frida_cli(["-n", "com.app"])


# ---------------------------------------------------------------------------
# Lifecycle (up/down/restart) + argv build
# ---------------------------------------------------------------------------


def _stage_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point settings at on-disk kernel/rootfs and return their paths."""
    kernel = tmp_path / "bzImage"
    rootfs = tmp_path / "rootdisk.img"
    kernel.write_bytes(b"k")
    rootfs.write_bytes(b"r")
    monkeypatch.setattr(
        vm_backend,
        "settings",
        Settings(vm_kernel=str(kernel), vm_rootfs=str(rootfs)),
    )
    return kernel, rootfs


class TestLifecycle:
    def test_build_argv_from_config_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = tmp_path / "k.img"
        rootfs = tmp_path / "r.img"
        kernel.write_bytes(b"k")
        rootfs.write_bytes(b"r")
        cfg = config.InstanceConfig(
            binder="vm",
            vm={
                "kernel": str(kernel),
                "rootfs": str(rootfs),
                "accel": "tcg",
                "smp": 2,
                "memory_mib": 1024,
            },  # type: ignore[arg-type]
        )
        backend = _make_backend(tmp_path, cfg=cfg, index=2)
        argv = backend.build_argv("tcg")
        assert argv[argv.index("-kernel") + 1] == str(kernel)
        assert argv[argv.index("-smp") + 1] == "2"
        assert argv[argv.index("-m") + 1] == "1024"
        # index 2 → adb port 5575.
        assert "hostfwd=tcp:127.0.0.1:5575-:5555" in argv[argv.index("-netdev") + 1]

    def test_build_argv_falls_back_to_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel, rootfs = _stage_artifacts(monkeypatch, tmp_path)
        backend = _make_backend(tmp_path)
        argv = backend.build_argv("kvm")
        assert argv[argv.index("-kernel") + 1] == str(kernel)
        assert argv[argv.index("-drive") + 1] == f"file={rootfs},format=raw,if=virtio"

    def test_build_argv_missing_kernel_config_and_env_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vm_backend, "settings", Settings(vm_kernel="", vm_rootfs=""))
        backend = _make_backend(tmp_path)
        with pytest.raises(qemu.QemuLaunchError, match="no VM kernel configured"):
            backend.build_argv("tcg")

    def test_build_argv_missing_rootfs_file_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = tmp_path / "bz"
        kernel.write_bytes(b"k")
        monkeypatch.setattr(
            vm_backend, "settings", Settings(vm_kernel=str(kernel), vm_rootfs="/nope/disk.img")
        )
        backend = _make_backend(tmp_path)
        with pytest.raises(qemu.QemuLaunchError, match=r"rootfs '/nope/disk\.img' does not exist"):
            backend.build_argv("tcg")

    def test_up_resolves_accel_builds_and_launches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_artifacts(monkeypatch, tmp_path)
        backend = _make_backend(tmp_path)  # accel auto
        monkeypatch.setattr(qemu, "detect_accel", lambda _req: "tcg")
        launched: dict[str, list[str]] = {}

        def _start(_self: object, argv: list[str]) -> int:
            launched["argv"] = argv
            return 1234

        monkeypatch.setattr(qemu.QemuProcess, "start", _start)
        backend.up()
        assert launched["argv"][0] == "qemu-system-x86_64"

    def test_up_propagates_accel_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_artifacts(monkeypatch, tmp_path)
        backend = _make_backend(
            tmp_path,
            cfg=config.InstanceConfig(binder="vm", vm={"accel": "kvm"}),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(qemu, "_dev_kvm_usable", lambda: False)
        with pytest.raises(qemu.QemuLaunchError, match="/dev/kvm is absent"):
            backend.up()

    def test_down_terminates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _make_backend(tmp_path)
        calls: list[str] = []

        def _terminate(_self: object) -> bool:
            calls.append("term")
            return True

        monkeypatch.setattr(qemu.QemuProcess, "terminate", _terminate)
        backend.down()
        assert calls == ["term"]

    def test_restart_down_then_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _make_backend(tmp_path)
        order: list[str] = []
        monkeypatch.setattr(backend, "down", lambda: order.append("down"))
        monkeypatch.setattr(backend, "up", lambda: order.append("up"))
        backend.restart()
        assert order == ["down", "up"]


# ---------------------------------------------------------------------------
# health() checks
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_includes_vm_rows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: True)
        monkeypatch.setattr(qemu, "detect_accel", lambda _req: "kvm")
        # adb_device_health shells out — stub adb absent so it returns skip rows.
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        rows = backend.health()
        assert rows["vm.process"].status == "pass"
        assert rows["vm.accel"].status == "pass"
        assert "near-native" in (rows["vm.accel"].reason or "")
        # Shared adb/frida rows are present (uniform check names).
        assert "magisk.zygisk" in rows

    def test_health_process_down_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)
        monkeypatch.setattr(qemu, "detect_accel", lambda _req: "tcg")
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        rows = backend.health()
        assert rows["vm.process"].status == "fail"
        assert rows["vm.accel"].status == "pass"
        assert "5-20x" in (rows["vm.accel"].reason or "")


class TestApplyDestroy:
    def test_apply_renders_env_and_reconciles(self, isolated_registry: Path) -> None:
        root = isolated_registry / "inst"
        root.mkdir()
        _write_yaml(root, config.InstanceConfig(binder="vm"))
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        index = registry.add_allocating("vmphone", backend=cfg)
        backend = vm_backend.VmDeviceBackend(
            name="vmphone", root=root, cfg=config.InstanceConfig(binder="vm"), index=index
        )
        backend.apply()
        env = (root / ".env").read_text()
        assert "INSTANCE_NAME=vmphone" in env
        # Still vm kind (binder unchanged) → reconcile is a no-op.
        meta = registry.get("vmphone")
        assert meta is not None
        assert meta.backend.kind == "vm"

    def test_apply_flips_kind_when_binder_changes(self, isolated_registry: Path) -> None:
        root = isolated_registry / "inst"
        root.mkdir()
        # On-disk yaml now says host (user hand-edited away from vm).
        _write_yaml(root, config.InstanceConfig(binder="host"))
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        index = registry.add_allocating("vmphone", backend=cfg)
        backend = vm_backend.VmDeviceBackend(
            name="vmphone", root=root, cfg=config.InstanceConfig(binder="vm"), index=index
        )
        backend.apply()
        meta = registry.get("vmphone")
        assert meta is not None
        assert meta.backend.kind == "redroid"

    def test_apply_port_collision_raises(self, isolated_registry: Path) -> None:
        # Two instances forced onto the same pinned adb port.
        other = isolated_registry / "other"
        other.mkdir()
        _write_yaml(other, config.InstanceConfig(ports={"adb": 6000}))  # type: ignore[arg-type]
        registry.add_allocating(
            "other", backend=registry.RedroidBackendConfig(absolute_path=str(other))
        )
        root = isolated_registry / "inst"
        root.mkdir()
        _write_yaml(root, config.InstanceConfig(binder="vm", ports={"adb": 6000}))  # type: ignore[arg-type]
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        index = registry.add_allocating("vmphone", backend=cfg)
        backend = vm_backend.VmDeviceBackend(
            name="vmphone", root=root, cfg=config.InstanceConfig(binder="vm"), index=index
        )
        with pytest.raises(ValueError, match="collides"):
            backend.apply()

    def test_apply_stages_frida_when_configured(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = isolated_registry / "inst"
        root.mkdir()
        cfg_obj = config.InstanceConfig(binder="vm", frida=config.Frida(version="16.4.10"))
        _write_yaml(root, cfg_obj)
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        index = registry.add_allocating("vmphone", backend=cfg)
        staged: dict[str, object] = {}
        monkeypatch.setattr(
            frida_download,
            "stage_for_instance",
            lambda r, version, expected_sha256=None: staged.update(version=version),
        )
        backend = vm_backend.VmDeviceBackend(name="vmphone", root=root, cfg=cfg_obj, index=index)
        backend.apply()
        assert staged["version"] == "16.4.10"

    def test_destroy_requires_yes(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        with pytest.raises(ValueError, match="yes=True"):
            backend.destroy()

    def test_destroy_terminates_and_removes(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = isolated_registry / "inst"
        root.mkdir()
        _write_yaml(root, config.InstanceConfig(binder="vm"))
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        index = registry.add_allocating("vmphone", backend=cfg)
        backend = vm_backend.VmDeviceBackend(
            name="vmphone", root=root, cfg=config.InstanceConfig(binder="vm"), index=index
        )
        terminated: list[str] = []

        def _terminate(_self: object) -> bool:
            terminated.append("t")
            return True

        monkeypatch.setattr(qemu.QemuProcess, "terminate", _terminate)
        backend.destroy(yes=True)
        assert terminated == ["t"]
        assert registry.get("vmphone") is None
        assert not root.exists()

    def test_destroy_root_already_gone(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = isolated_registry / "gone"
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        registry.add_allocating("vmphone", backend=cfg)
        backend = vm_backend.VmDeviceBackend(
            name="vmphone", root=root, cfg=config.InstanceConfig(binder="vm"), index=0
        )
        monkeypatch.setattr(qemu.QemuProcess, "terminate", lambda _self: False)
        backend.destroy(yes=True)
        assert registry.get("vmphone") is None


class TestAccelCheck:
    def test_kvm_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "kvm")
        row = vm_backend._accel_check("auto")
        assert row.status == "pass"
        assert "near-native" in (row.reason or "")

    def test_tcg_pass_with_loud_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        row = vm_backend._accel_check("auto")
        assert row.status == "pass"
        assert "5-20x" in (row.reason or "")

    def test_explicit_kvm_unavailable_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qemu, "_dev_kvm_usable", lambda: False)
        row = vm_backend._accel_check("kvm")
        assert row.status == "fail"
        assert "/dev/kvm" in (row.reason or "")
