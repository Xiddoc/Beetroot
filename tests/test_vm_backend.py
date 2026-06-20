"""Tests for beetroot.backends.vm.VmDeviceBackend."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from beetroot import api, config, frida_download, registry
from beetroot.backends import vm as vm_backend
from beetroot.settings import Settings
from beetroot.vm import boot_cache, qemu


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
        # Frida is unsupported on the vm backend (#44) — no working endpoint.
        assert backend.frida_address == "unsupported"
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
    def test_install_frida_is_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Frida is not wired through the network-isolated guest (issue
        # #44): install_frida raises a friendly, actionable error and never
        # stages a binary.
        backend = _make_backend(
            tmp_path,
            cfg=config.InstanceConfig(binder="vm", frida=config.Frida(version="16.4.10")),
        )

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("vm install_frida must not stage a frida-server")

        monkeypatch.setattr(frida_download, "stage_for_instance", _boom)
        monkeypatch.setattr(frida_download, "stage_empty", _boom)
        with pytest.raises(api.BackendCapabilityError, match="not yet supported on the 'vm'"):
            backend.install_frida()

    def test_install_frida_unsupported_even_with_explicit_version(self, tmp_path: Path) -> None:
        backend = _make_backend(tmp_path)
        with pytest.raises(api.BackendCapabilityError, match="network-isolated"):
            backend.install_frida("16.5.0")


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
    def test_frida_cli_is_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guest never exposes a Frida port, so frida_cli raises rather
        # than spawning a `frida` that hangs against a dead endpoint.
        backend = _make_backend(tmp_path)

        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("vm frida_cli must not spawn the frida CLI")

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _boom)
        with pytest.raises(api.BackendCapabilityError, match="not yet supported on the 'vm'"):
            backend.frida_cli(["-n", "com.app"])

    def test_frida_address_reports_unsupported(self, tmp_path: Path) -> None:
        # No working endpoint is ever advertised in ls/status rows.
        backend = _make_backend(tmp_path)
        assert backend.frida_address == "unsupported"


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

    def test_build_argv_auto_smp_tracks_host_cpu_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default ``vm.smp: auto`` must resolve, through the full
        # config → build_argv path, to the host's physical core count — not a
        # hardcoded constant. (Behavior test: user input → final argv.)
        kernel = tmp_path / "k.img"
        rootfs = tmp_path / "r.img"
        kernel.write_bytes(b"k")
        rootfs.write_bytes(b"r")
        cfg = config.InstanceConfig(
            binder="vm",
            vm={"kernel": str(kernel), "rootfs": str(rootfs), "accel": "tcg"},  # type: ignore[arg-type]
        )
        assert cfg.vm.smp == "auto"  # defaulted, not pinned
        monkeypatch.setattr("beetroot.vm.qemu.host_physical_cores", lambda: 3)
        backend = _make_backend(tmp_path, cfg=cfg, index=2)
        argv = backend.build_argv("tcg")
        assert argv[argv.index("-smp") + 1] == "3"

    def test_build_argv_falls_back_to_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel, rootfs = _stage_artifacts(monkeypatch, tmp_path)
        backend = _make_backend(tmp_path)
        argv = backend.build_argv("kvm")
        assert argv[argv.index("-kernel") + 1] == str(kernel)
        assert argv[argv.index("-drive") + 1] == f"file={rootfs},format=raw,if=virtio"

    def test_build_argv_expands_tilde_in_config_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shipped examples/vm.yaml points kernel/rootfs at
        # ~/.cache/beetroot/vm/... — the tilde must be expanded or the
        # documented config never boots ("does not exist on the host").
        home = tmp_path / "home"
        cache = home / ".cache" / "beetroot" / "vm"
        cache.mkdir(parents=True)
        (cache / "bzImage").write_bytes(b"k")
        (cache / "rootdisk.img").write_bytes(b"r")
        monkeypatch.setenv("HOME", str(home))
        cfg = config.InstanceConfig(
            binder="vm",
            vm=config.Vm(
                kernel="~/.cache/beetroot/vm/bzImage",
                rootfs="~/.cache/beetroot/vm/rootdisk.img",
            ),
        )
        backend = _make_backend(tmp_path, cfg=cfg)
        argv = backend.build_argv("tcg")
        assert argv[argv.index("-kernel") + 1] == str(cache / "bzImage")
        assert (
            argv[argv.index("-drive") + 1] == f"file={cache / 'rootdisk.img'},format=raw,if=virtio"
        )

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
        monkeypatch.setattr(vm_backend.VmDeviceBackend, "_wait_for_adb_connect", lambda _self: None)
        backend.up()
        assert launched["argv"][0] == "qemu-system-x86_64"

    def test_up_rejects_non_vm_binder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A vm-registry row whose yaml was hand-edited back to binder: host
        # must fail fast (run `apply`) rather than boot QEMU anyway.
        backend = _make_backend(tmp_path, cfg=config.InstanceConfig(binder="host"))

        def _no_launch(_self: object, _argv: list[str]) -> int:
            raise AssertionError("QEMU must not launch when the yaml is no longer binder: vm")

        monkeypatch.setattr(qemu.QemuProcess, "start", _no_launch)
        with pytest.raises(api.BackendCapabilityError, match="run `beetroot apply"):
            backend.up()

    def test_up_full_composition_argv_contents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Full path: on-disk yaml (binder: vm, non-default smp/memory) →
        # resolve artifacts → up() → assert the captured QEMU argv carries
        # the resolved hostfwd port, -smp, -m, and the TCG accel flags.
        kernel = tmp_path / "bzImage"
        rootfs = tmp_path / "rootdisk.img"
        kernel.write_bytes(b"k")
        rootfs.write_bytes(b"r")
        cfg = config.InstanceConfig(
            binder="vm",
            vm={
                "kernel": str(kernel),
                "rootfs": str(rootfs),
                "accel": "tcg",
                "smp": 6,
                "memory_mib": 3072,
            },  # type: ignore[arg-type]
        )
        backend = _make_backend(tmp_path, cfg=cfg, index=3)  # index 3 → adb 5585
        launched: dict[str, list[str]] = {}

        def _start(_self: object, argv: list[str]) -> int:
            launched["argv"] = argv
            return 1

        monkeypatch.setattr(qemu.QemuProcess, "start", _start)
        monkeypatch.setattr(vm_backend.VmDeviceBackend, "_wait_for_adb_connect", lambda _self: None)
        backend.up()
        argv = launched["argv"]
        assert argv[0] == "qemu-system-x86_64"
        assert "hostfwd=tcp:127.0.0.1:5585-:5555" in argv[argv.index("-netdev") + 1]
        assert argv[argv.index("-smp") + 1] == "6"
        assert argv[argv.index("-m") + 1] == "3072"
        assert argv[argv.index("-accel") + 1] == "tcg,thread=multi,tb-size=1024"
        assert argv[argv.index("-cpu") + 1] == "max"

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

    def _cached_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> vm_backend.VmDeviceBackend:
        kernel, rootfs = _stage_artifacts(monkeypatch, tmp_path)
        cfg = config.InstanceConfig(
            binder="vm",
            vm={"kernel": str(kernel), "rootfs": str(rootfs), "boot_cache": True},  # type: ignore[arg-type]
        )
        backend = _make_backend(tmp_path, cfg=cfg)
        monkeypatch.setattr(qemu, "detect_accel", lambda _req: "tcg")
        monkeypatch.setattr(vm_backend.VmDeviceBackend, "_wait_for_adb_connect", lambda _self: None)
        # Default: the guest boots (cold path gates savevm on this). The
        # not-booted branch is exercised explicitly below.
        monkeypatch.setattr(
            vm_backend.VmDeviceBackend, "_wait_for_boot_completed", lambda _self: True
        )
        return backend

    def test_up_cached_cold_creates_overlay_and_checkpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = self._cached_backend(tmp_path, monkeypatch)
        events: list[str] = []
        launched: dict[str, list[str]] = {}
        monkeypatch.setattr(
            "beetroot.vm.boot_cache.create_overlay", lambda *_a: events.append("overlay")
        )
        monkeypatch.setattr("beetroot.vm.boot_cache.snapshot_present", lambda _o: False)

        def _save(_m: Path) -> bool:
            events.append("saved")
            return True

        monkeypatch.setattr("beetroot.vm.boot_cache.save_snapshot", _save)

        def _start(_self: object, argv: list[str]) -> int:
            launched["argv"] = argv
            return 1

        monkeypatch.setattr(qemu.QemuProcess, "start", _start)
        backend.up()
        argv = launched["argv"]
        # Cold cached boot: qcow2 overlay disk + monitor socket, NO -loadvm,
        # then a checkpoint is taken for next time.
        assert ",format=qcow2," in argv[argv.index("-drive") + 1]
        assert "-monitor" in argv
        assert "-loadvm" not in argv
        assert events == ["overlay", "saved"]

    def test_up_cached_warm_resumes_without_recheckpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = self._cached_backend(tmp_path, monkeypatch)
        # Overlay already present → create_overlay must NOT run; snapshot exists
        # → resume with -loadvm and do NOT re-checkpoint.
        boot_cache.overlay_path(backend.root).write_bytes(b"qcow2")
        events: list[str] = []
        launched: dict[str, list[str]] = {}
        monkeypatch.setattr(
            "beetroot.vm.boot_cache.create_overlay", lambda *_a: events.append("overlay")
        )
        monkeypatch.setattr("beetroot.vm.boot_cache.snapshot_present", lambda _o: True)
        monkeypatch.setattr(
            "beetroot.vm.boot_cache.save_snapshot", lambda _m: events.append("saved")
        )

        def _start(_self: object, argv: list[str]) -> int:
            launched["argv"] = argv
            return 1

        monkeypatch.setattr(qemu.QemuProcess, "start", _start)
        backend.up()
        argv = launched["argv"]
        assert argv[argv.index("-loadvm") + 1] == boot_cache.SNAPSHOT_TAG
        assert events == []  # neither overlay creation nor checkpoint

    def test_up_cached_warns_when_checkpoint_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = self._cached_backend(tmp_path, monkeypatch)
        warnings: list[str] = []
        monkeypatch.setattr("beetroot.vm.boot_cache.create_overlay", lambda *_a: None)
        monkeypatch.setattr("beetroot.vm.boot_cache.snapshot_present", lambda _o: False)
        monkeypatch.setattr("beetroot.vm.boot_cache.save_snapshot", lambda _m: False)
        monkeypatch.setattr(qemu.QemuProcess, "start", lambda _self, _argv: 1)
        monkeypatch.setattr("beetroot.console.warn", warnings.append)
        backend.up()
        assert any("cold-boot again" in w for w in warnings)

    def test_up_cached_cold_skips_checkpoint_when_boot_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the guest never reaches boot_completed, savevm must NOT run (a
        # half-booted checkpoint is worse than a cold boot).
        backend = self._cached_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(
            vm_backend.VmDeviceBackend, "_wait_for_boot_completed", lambda _self: False
        )
        events: list[str] = []
        warnings: list[str] = []
        monkeypatch.setattr("beetroot.vm.boot_cache.create_overlay", lambda *_a: None)
        monkeypatch.setattr("beetroot.vm.boot_cache.snapshot_present", lambda _o: False)
        monkeypatch.setattr(
            "beetroot.vm.boot_cache.save_snapshot", lambda _m: events.append("saved")
        )
        monkeypatch.setattr(qemu.QemuProcess, "start", lambda _self, _argv: 1)
        monkeypatch.setattr("beetroot.console.warn", warnings.append)
        backend.up()
        assert events == []  # savevm never attempted
        assert any("did not reach sys.boot_completed" in w for w in warnings)

    def test_wait_for_boot_completed_polls_getprop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel, rootfs = _stage_artifacts(monkeypatch, tmp_path)
        backend = _make_backend(
            tmp_path,
            cfg=config.InstanceConfig(
                binder="vm",
                vm={"kernel": str(kernel), "rootfs": str(rootfs), "boot_cache": True},  # type: ignore[arg-type]
            ),
        )
        monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/adb")
        # getprop returns "" twice (not booted), then "1".
        replies = iter(["", "", "1"])

        def _run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            out = next(replies) if cmd[:4] == ["adb", "-s", backend.adb_address, "shell"] else ""
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _run)
        monkeypatch.setattr("beetroot.backends.vm.time.sleep", lambda _s: None)
        assert backend._wait_for_boot_completed() is True

    def test_wait_for_boot_completed_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = self._cached_backend(tmp_path, monkeypatch)
        # Undo the _cached_backend stub so the real method runs.
        monkeypatch.undo()
        monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/adb")
        # getprop never reads "1"; collapse the deadline so the loop exits fast.
        monkeypatch.setattr("beetroot.backends.vm._BOOT_COMPLETED_TIMEOUT_SECONDS", 0.0)
        monkeypatch.setattr(
            "beetroot.backends.vm.subprocess.run",
            lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=0, stdout="0"),
        )
        assert backend._wait_for_boot_completed() is False

    def test_wait_for_boot_completed_requires_adb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = self._cached_backend(tmp_path, monkeypatch)
        monkeypatch.undo()
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        with pytest.raises(api.AdbNotInstalledError):
            backend._wait_for_boot_completed()

    def test_boot_completed_handles_adb_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise OSError("adb gone")

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _boom)
        assert vm_backend.VmDeviceBackend._boot_completed("localhost:5555") is False

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


class TestRootfsVersionSkewWarning:
    """issue #82: up/apply warn when the baked rootfs version != config."""

    def _backend_with_rootfs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_version: int,
        marker_version: int | None,
    ) -> vm_backend.VmDeviceBackend:
        from beetroot import builder

        kernel = tmp_path / "bzImage"
        rootfs = tmp_path / "rootdisk.img"
        kernel.write_bytes(b"k")
        rootfs.write_bytes(b"r")
        if marker_version is not None:
            builder.rootfs_version_marker(rootfs).write_text(
                f"{marker_version}\n", encoding="utf-8"
            )
        monkeypatch.setattr(
            vm_backend, "settings", Settings(vm_kernel=str(kernel), vm_rootfs=str(rootfs))
        )
        cfg = config.InstanceConfig(binder="vm", android={"version": config_version})  # type: ignore[arg-type]
        return _make_backend(tmp_path, cfg=cfg)

    def _silence_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qemu, "detect_accel", lambda _req: "tcg")
        monkeypatch.setattr(qemu.QemuProcess, "start", lambda _self, _argv: 1)
        monkeypatch.setattr(vm_backend.VmDeviceBackend, "_wait_for_adb_connect", lambda _self: None)

    def test_up_warns_on_version_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        backend = self._backend_with_rootfs(
            tmp_path, monkeypatch, config_version=14, marker_version=11
        )
        self._silence_launch(monkeypatch)
        backend.up()
        err = capsys.readouterr().err
        assert "baked for Android 11" in err
        assert "android.version: 14" in err

    def test_up_silent_when_versions_match(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        backend = self._backend_with_rootfs(
            tmp_path, monkeypatch, config_version=14, marker_version=14
        )
        self._silence_launch(monkeypatch)
        backend.up()
        assert "baked for Android" not in capsys.readouterr().err

    def test_up_silent_when_no_marker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Backward compat: a pre-#82 rootfs has no marker — stay silent.
        backend = self._backend_with_rootfs(
            tmp_path, monkeypatch, config_version=14, marker_version=None
        )
        self._silence_launch(monkeypatch)
        backend.up()
        assert "baked for Android" not in capsys.readouterr().err

    def test_skew_check_silent_when_rootfs_unresolvable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # No kernel/rootfs configured at all — the skew check must not raise;
        # build_argv surfaces the missing-artifact error downstream instead.
        monkeypatch.setattr(vm_backend, "settings", Settings(vm_kernel="", vm_rootfs=""))
        backend = _make_backend(tmp_path)
        backend._warn_on_rootfs_version_skew()
        assert "baked for Android" not in capsys.readouterr().err

    def test_apply_warns_on_version_mismatch(
        self,
        isolated_registry: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from beetroot import builder

        root = isolated_registry / "inst"
        root.mkdir()
        rootfs = isolated_registry / "rootdisk.img"
        rootfs.write_bytes(b"r")
        builder.rootfs_version_marker(rootfs).write_text("11\n", encoding="utf-8")
        monkeypatch.setattr(
            vm_backend, "settings", Settings(vm_kernel=str(rootfs), vm_rootfs=str(rootfs))
        )
        cfg = config.InstanceConfig(binder="vm", android={"version": 14})  # type: ignore[arg-type]
        _write_yaml(root, cfg)
        backend_cfg = registry.VmBackendConfig(absolute_path=str(root))
        registry.add_allocating("vmphone", backend=backend_cfg)
        backend = vm_backend.VmDeviceBackend.from_meta("vmphone", backend_cfg)
        backend.apply()
        assert "baked for Android 11" in capsys.readouterr().err


class TestWaitForAdbConnect:
    def _connect_result(self, *, ok: bool) -> subprocess.CompletedProcess[str]:
        if ok:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="connected", stderr="")
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="cannot connect to localhost:5555"
        )

    def test_retries_until_connect_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # adbd binds its TCP port a few seconds after boot, so the first
        # connects are refused; up() must retry and ultimately attach.
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        attempts: list[list[str]] = []

        def _run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            attempts.append(list(cmd))
            return self._connect_result(ok=len(attempts) >= 3)

        sleeps: list[float] = []
        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _run)
        monkeypatch.setattr("beetroot.backends.vm.time.sleep", sleeps.append)
        backend._wait_for_adb_connect()
        assert len(attempts) == 3
        assert attempts[0] == ["adb", "connect", "localhost:5555"]
        # Two refusals → two backoff sleeps before the third (winning) attempt.
        assert len(sleeps) == 2

    def test_happy_path_first_try_does_not_sleep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        attempts: list[list[str]] = []

        def _run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            attempts.append(list(cmd))
            return self._connect_result(ok=True)

        def _no_sleep(_s: float) -> None:
            raise AssertionError("happy path must not sleep — endpoint accepted first try")

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _run)
        monkeypatch.setattr("beetroot.backends.vm.time.sleep", _no_sleep)
        backend._wait_for_adb_connect()
        assert len(attempts) == 1

    def test_never_connects_raises_friendly_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            "beetroot.backends.vm.subprocess.run",
            lambda *_a, **_k: self._connect_result(ok=False),
        )
        # Advance a synthetic clock past the deadline on the second read so
        # the loop exits without real waiting.
        ticks = iter([0.0, 0.0, 999.0])
        monkeypatch.setattr("beetroot.backends.vm.time.monotonic", lambda: next(ticks))
        monkeypatch.setattr("beetroot.backends.vm.time.sleep", lambda _s: None)
        with pytest.raises(qemu.QemuLaunchError, match="did not expose ADB"):
            backend._wait_for_adb_connect()

    def test_subprocess_error_treated_as_not_connected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An adb connect that times out / fails to spawn is "not connected",
        # not a crash — the poll keeps trying until the deadline.
        def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="adb", timeout=5)

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _boom)
        assert vm_backend.VmDeviceBackend._adb_connect_ok("localhost:5555") is False

    def test_without_adb_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda _n: None)
        with pytest.raises(api.AdbNotInstalledError):
            backend._wait_for_adb_connect()

    def test_up_full_path_launches_then_waits_for_adb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Full composition: on-disk binder: vm → up() launches QEMU → then
        # polls adb connect, retrying past the early refusals to attach.
        _stage_artifacts(monkeypatch, tmp_path)
        backend = _make_backend(tmp_path)
        monkeypatch.setattr(qemu, "detect_accel", lambda _req: "tcg")
        monkeypatch.setattr(qemu.QemuProcess, "start", lambda _self, _argv: 4321)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        attempts: list[list[str]] = []

        def _run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            attempts.append(list(cmd))
            return self._connect_result(ok=len(attempts) >= 2)

        monkeypatch.setattr("beetroot.backends.vm.subprocess.run", _run)
        monkeypatch.setattr("beetroot.backends.vm.time.sleep", lambda _s: None)
        backend.up()
        assert attempts[0] == ["adb", "connect", "localhost:5555"]
        assert len(attempts) == 2


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
        # Shared adb/magisk rows are present (uniform check names).
        assert "magisk.zygisk" in rows
        # Frida can never pass on the network-isolated guest (#44), so the
        # handshake row is omitted rather than a permanent fail.
        assert "frida.handshake" not in rows

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

    def test_apply_does_not_stage_frida(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even with a frida: block, apply()/`_stage` must NOT stage a
        # frida-server for the vm backend — the guest can't read it (#44).
        root = isolated_registry / "inst"
        root.mkdir()
        cfg_obj = config.InstanceConfig(binder="vm", frida=config.Frida(version="16.4.10"))
        _write_yaml(root, cfg_obj)
        cfg = registry.VmBackendConfig(absolute_path=str(root))
        index = registry.add_allocating("vmphone", backend=cfg)

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("vm _stage must not touch frida staging")

        monkeypatch.setattr(frida_download, "stage_for_instance", _boom)
        monkeypatch.setattr(frida_download, "stage_empty", _boom)
        backend = vm_backend.VmDeviceBackend(name="vmphone", root=root, cfg=cfg_obj, index=index)
        backend.apply()
        # The .env still renders + modules dir is created.
        assert "INSTANCE_NAME=vmphone" in (root / ".env").read_text()
        assert not (root / "frida-server").exists()

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
