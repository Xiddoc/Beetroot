"""Tests for beetroot.vm.qemu — accel detection, argv builder, process mgmt."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
from pathlib import Path

import pytest

from beetroot.vm import qemu

# ---------------------------------------------------------------------------
# detect_accel
# ---------------------------------------------------------------------------


class TestDetectAccel:
    def test_tcg_is_honoured_without_probing_kvm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Explicit tcg never touches /dev/kvm.
        def _boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("os.access must not be probed for explicit tcg")

        monkeypatch.setattr("beetroot.vm.qemu.os.access", _boom)
        assert qemu.detect_accel("tcg") == "tcg"

    def test_auto_prefers_kvm_when_usable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.os.access", lambda *_a, **_k: True)
        assert qemu.detect_accel("auto") == "kvm"

    def test_auto_falls_back_to_tcg_when_no_kvm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.os.access", lambda *_a, **_k: False)
        assert qemu.detect_accel("auto") == "tcg"

    def test_explicit_kvm_honoured_when_usable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.os.access", lambda *_a, **_k: True)
        assert qemu.detect_accel("kvm") == "kvm"

    def test_explicit_kvm_raises_when_no_dev_kvm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.os.access", lambda *_a, **_k: False)
        with pytest.raises(qemu.QemuLaunchError, match="/dev/kvm is absent") as exc:
            qemu.detect_accel("kvm")
        assert "beetroot modes" in str(exc.value)

    def test_dev_kvm_usable_probes_rw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _access(path: str, mode: int) -> bool:
            captured["path"] = path
            captured["mode"] = mode
            return True

        monkeypatch.setattr("beetroot.vm.qemu.os.access", _access)
        assert qemu._dev_kvm_usable() is True
        assert captured["path"] == "/dev/kvm"
        assert captured["mode"] == os.R_OK | os.W_OK


class TestDetectAccelHostArch:
    """issue #190: KVM cross-arch is impossible; the guard is arch-aware."""

    def test_host_is_guest_arch_true_on_x86_64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.platform.machine", lambda: "x86_64")
        assert qemu.host_is_guest_arch() is True

    def test_host_is_guest_arch_true_on_amd64_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.platform.machine", lambda: "AMD64")
        assert qemu.host_is_guest_arch() is True

    def test_host_is_guest_arch_false_on_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.platform.machine", lambda: "aarch64")
        assert qemu.host_is_guest_arch() is False

    def test_explicit_kvm_raises_cross_arch_on_foreign_host_even_with_dev_kvm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A foreign-arch host may have its own native /dev/kvm, but it can't
        # accelerate the x86_64 guest — the cross-arch rejection precedes the
        # /dev/kvm probe, so os.access must never be reached.
        monkeypatch.setattr("beetroot.vm.qemu.platform.machine", lambda: "aarch64")

        def _boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("cross-arch kvm must reject before probing /dev/kvm")

        monkeypatch.setattr("beetroot.vm.qemu.os.access", _boom)
        with pytest.raises(qemu.QemuLaunchError, match="cannot accelerate the x86_64") as exc:
            qemu.detect_accel("kvm")
        assert "aarch64" in str(exc.value)
        assert "beetroot modes" in str(exc.value)

    def test_auto_resolves_to_tcg_on_foreign_host_even_with_dev_kvm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``auto`` on a foreign arch must be TCG regardless of a present, usable
        # native /dev/kvm — KVM can't virtualize the x86_64 guest here.
        monkeypatch.setattr("beetroot.vm.qemu.platform.machine", lambda: "aarch64")
        monkeypatch.setattr("beetroot.vm.qemu.os.access", lambda *_a, **_k: True)
        assert qemu.detect_accel("auto") == "tcg"

    def test_x86_64_host_behavior_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On x86_64 the /dev/kvm probe still decides, unchanged.
        monkeypatch.setattr("beetroot.vm.qemu.platform.machine", lambda: "x86_64")
        monkeypatch.setattr("beetroot.vm.qemu.os.access", lambda *_a, **_k: True)
        assert qemu.detect_accel("auto") == "kvm"
        assert qemu.detect_accel("kvm") == "kvm"


# ---------------------------------------------------------------------------
# build_qemu_argv
# ---------------------------------------------------------------------------


def _argv(accel: qemu.ResolvedAccel, **over: object) -> list[str]:
    kwargs: dict[str, object] = {
        "qemu_bin": "qemu-system-x86_64",
        "accel": accel,
        "kernel": Path("/img/bzImage"),
        "rootfs": Path("/img/rootdisk.img"),
        "smp": 4,
        "memory_mib": 8192,
        "host_adb_port": 5555,
    }
    kwargs.update(over)
    return qemu.build_qemu_argv(**kwargs)  # type: ignore[arg-type]


class TestBuildQemuArgv:
    def test_tcg_uses_mttcg_and_cpu_max(self) -> None:
        argv = _argv("tcg")
        assert "-accel" in argv
        assert "tcg,thread=multi,tb-size=1024" in argv
        # MTTCG flag sits immediately after its -accel.
        assert argv[argv.index("-accel") + 1] == "tcg,thread=multi,tb-size=1024"
        assert argv[argv.index("-cpu") + 1] == "max"

    def test_kvm_uses_cpu_host(self) -> None:
        argv = _argv("kvm")
        assert argv[argv.index("-accel") + 1] == "kvm"
        assert argv[argv.index("-cpu") + 1] == "host"

    def test_forwards_adb_port_via_hostfwd(self) -> None:
        argv = _argv("tcg", host_adb_port=5575)
        netdev = argv[argv.index("-netdev") + 1]
        assert "hostfwd=tcp:127.0.0.1:5575-:5555" in netdev

    def test_smp_and_memory_are_stringified(self) -> None:
        argv = _argv("kvm", smp=8, memory_mib=4096)
        assert argv[argv.index("-smp") + 1] == "8"
        assert argv[argv.index("-m") + 1] == "4096"

    def test_kernel_and_rootfs_paths_appear(self) -> None:
        argv = _argv("tcg", kernel=Path("/k/bz"), rootfs=Path("/r/disk.img"))
        assert argv[argv.index("-kernel") + 1] == "/k/bz"
        drive = argv[argv.index("-drive") + 1]
        assert drive == "file=/r/disk.img,format=raw,if=virtio"

    def test_qemu_bin_is_argv0(self) -> None:
        argv = _argv("tcg", qemu_bin="/opt/qemu")
        assert argv[0] == "/opt/qemu"

    def test_headless_and_no_reboot_flags(self) -> None:
        argv = _argv("tcg")
        assert "-nographic" in argv
        assert "-no-reboot" in argv
        assert (
            argv[argv.index("-append") + 1]
            == "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off"
        )

    def test_disables_cpu_mitigations_on_both_accels(self) -> None:
        # An ephemeral research sandbox doesn't need speculative-execution
        # mitigations — they are pure (emulated under TCG / real under KVM)
        # overhead. The lever lives on the shared kernel cmdline, so it must
        # be present regardless of accelerator.
        for accel in ("tcg", "kvm"):
            argv = _argv(accel)
            assert "mitigations=off" in argv[argv.index("-append") + 1]

    def test_defaults_carry_no_monitor_or_loadvm(self) -> None:
        # The cold-boot default path must be byte-for-byte unchanged: raw disk,
        # no monitor socket, no -loadvm.
        argv = _argv("tcg")
        assert argv[argv.index("-drive") + 1] == "file=/img/rootdisk.img,format=raw,if=virtio"
        assert "-monitor" not in argv
        assert "-loadvm" not in argv

    def test_qcow2_disk_format_and_cache(self) -> None:
        argv = _argv("tcg", disk_format="qcow2", disk_cache="unsafe")
        assert (
            argv[argv.index("-drive") + 1]
            == "file=/img/rootdisk.img,format=qcow2,if=virtio,cache=unsafe"
        )

    def test_monitor_socket_appended(self) -> None:
        argv = _argv("tcg", monitor_socket=Path("/inst/qemu-monitor.sock"))
        assert argv[argv.index("-monitor") + 1] == "unix:/inst/qemu-monitor.sock,server,nowait"

    def test_loadvm_tag_appended(self) -> None:
        argv = _argv("tcg", loadvm="beetroot-boot")
        assert argv[argv.index("-loadvm") + 1] == "beetroot-boot"

    def test_loadvm_precedes_append(self) -> None:
        # -loadvm must sit before -append so the kernel cmdline stays the last
        # positional pair (mirrors the cold layout).
        argv = _argv("tcg", monitor_socket=Path("/m.sock"), loadvm="beetroot-boot")
        assert argv.index("-loadvm") < argv.index("-append")
        assert argv.index("-monitor") < argv.index("-append")


# ---------------------------------------------------------------------------
# resolve_smp / host_physical_cores
# ---------------------------------------------------------------------------


_CPUINFO_4C_8T = "\n".join(
    f"processor\t: {i}\nphysical id\t: 0\ncore id\t: {i % 4}\n" for i in range(8)
)


class TestResolveSmp:
    def test_explicit_count_is_honoured_verbatim(self) -> None:
        assert qemu.resolve_smp(6) == 6
        assert qemu.resolve_smp(2) == 2

    def test_auto_uses_physical_cores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("beetroot.vm.qemu.host_physical_cores", lambda: 4)
        assert qemu.resolve_smp("auto") == 4


class TestHostPhysicalCores:
    def test_collapses_hyperthread_siblings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 4 physical cores, 8 logical (HT): the cmdline must request 4, not 8
        # (a logical count would pick 8 and hit the §B.5 oversubscription regression).
        monkeypatch.setattr(Path, "read_text", lambda _self: _CPUINFO_4C_8T)
        monkeypatch.setattr("beetroot.vm.qemu.os.sched_getaffinity", lambda _pid: set(range(8)))
        assert qemu.host_physical_cores() == 4

    def test_capped_by_affinity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cgroup/taskset-limited container (2 logical CPUs available) caps
        # the result below the 4 physical cores the host reports.
        monkeypatch.setattr(Path, "read_text", lambda _self: _CPUINFO_4C_8T)
        monkeypatch.setattr("beetroot.vm.qemu.os.sched_getaffinity", lambda _pid: {0, 1})
        assert qemu.host_physical_cores() == 2

    def test_falls_back_to_logical_without_cpuinfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_self: Path) -> str:
            raise OSError("no /proc/cpuinfo")

        monkeypatch.setattr(Path, "read_text", _boom)
        monkeypatch.setattr("beetroot.vm.qemu.os.sched_getaffinity", lambda _pid: {0, 1, 2})
        assert qemu.host_physical_cores() == 3

    def test_falls_back_when_topology_fields_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Some arches omit physical/core id — fall back to the logical count.
        monkeypatch.setattr(Path, "read_text", lambda _self: "processor\t: 0\n\nprocessor\t: 1\n")
        monkeypatch.setattr("beetroot.vm.qemu.os.sched_getaffinity", lambda _pid: {0, 1})
        assert qemu.host_physical_cores() == 2

    def test_affinity_falls_back_to_cpu_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No sched_getaffinity (non-Linux): use os.cpu_count.
        monkeypatch.delattr("beetroot.vm.qemu.os.sched_getaffinity", raising=False)
        monkeypatch.setattr("beetroot.vm.qemu.os.cpu_count", lambda: 6)
        assert qemu._affinity_cpu_count() == 6

    def test_affinity_cpu_count_none_clamps_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr("beetroot.vm.qemu.os.sched_getaffinity", raising=False)
        monkeypatch.setattr("beetroot.vm.qemu.os.cpu_count", lambda: None)
        assert qemu._affinity_cpu_count() == 1


# ---------------------------------------------------------------------------
# QemuProcess
# ---------------------------------------------------------------------------


class TestQemuProcessPidfile:
    def test_pidfile_path(self, tmp_path: Path) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)
        assert proc.pidfile == tmp_path / "qemu.pid"

    def test_read_pid_missing_file(self, tmp_path: Path) -> None:
        assert qemu.QemuProcess(tmp_path, 5555).read_pid() is None

    def test_read_pid_parses_integer(self, tmp_path: Path) -> None:
        (tmp_path / "qemu.pid").write_text("4242\n")
        assert qemu.QemuProcess(tmp_path, 5555).read_pid() == 4242

    def test_read_pid_garbage_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "qemu.pid").write_text("not-a-pid")
        assert qemu.QemuProcess(tmp_path, 5555).read_pid() is None


class TestQemuProcessIsRunning:
    def test_no_pidfile_not_running(self, tmp_path: Path) -> None:
        assert qemu.QemuProcess(tmp_path, 5555).is_running() is False

    def test_live_pid_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "qemu.pid").write_text("99")
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda *_a: None)
        # Live AND the /proc cmdline names this instance's QEMU.
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: True)
        assert qemu.QemuProcess(tmp_path, 5555).is_running() is True

    def test_stale_pid_esrch_not_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "qemu.pid").write_text("99")

        def _kill(_pid: int, _sig: int) -> None:
            raise OSError(errno.ESRCH, "no such process")

        monkeypatch.setattr("beetroot.vm.qemu.os.kill", _kill)
        assert qemu.QemuProcess(tmp_path, 5555).is_running() is False

    def test_eperm_alive_but_identity_confirmed_is_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "qemu.pid").write_text("99")

        def _kill(_pid: int, _sig: int) -> None:
            raise OSError(errno.EPERM, "operation not permitted")

        monkeypatch.setattr("beetroot.vm.qemu.os.kill", _kill)
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: True)
        assert qemu.QemuProcess(tmp_path, 5555).is_running() is True

    def test_live_but_not_qemu_pid_not_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PID reuse: the recorded PID is live (signal-0 succeeds) but /proc says
        # it is NOT this instance's QEMU — is_running must report False so the
        # already-running guard and the vm.process doctor row don't false-green
        # (issue #162).
        (tmp_path / "qemu.pid").write_text("99")
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda *_a: None)
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: False)
        assert qemu.QemuProcess(tmp_path, 5555).is_running() is False


class TestQemuProcessPidIdentity:
    """The PID-identity guard that stops a reused PID being signalled (#162)."""

    def test_pid_is_qemu_matches_our_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)
        # A real QEMU argv carries qemu-system AND this instance's hostfwd
        # ADB port — both must be present to match (#162).
        cmdline = (
            "qemu-system-x86_64\x00-netdev\x00"
            "user,id=net0,hostfwd=tcp:127.0.0.1:5555-:5555\x00"
        )
        monkeypatch.setattr(qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: cmdline)
        assert proc._pid_is_qemu(123) is True

    def test_pid_is_qemu_matches_plain_path_argv_outside_instance_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for the #162/#174/#176 plain-path miss: the default
        # `up` argv resolves kernel/rootfs from a SHARED artifacts cache
        # (outside the instance dir) and references nothing under it, so an
        # instance-dir-based identity check false-negatived a healthy QEMU.
        # Drive a REAL build_qemu_argv whose paths live elsewhere and assert
        # the (port-based) identity check still matches.
        shared = tmp_path / "cache"
        shared.mkdir()
        (shared / "bzImage").write_bytes(b"x")
        (shared / "rootfs.img").write_bytes(b"x")
        inst = tmp_path / "alpha"
        proc = qemu.QemuProcess(inst, 5575)
        argv = qemu.build_qemu_argv(
            qemu_bin="qemu-system-x86_64",
            accel="tcg",
            kernel=shared / "bzImage",
            rootfs=shared / "rootfs.img",
            smp=2,
            memory_mib=2048,
            host_adb_port=5575,
        )
        assert str(inst) not in "\x00".join(argv)  # the gap that broke #162
        cmdline = "\x00".join(argv) + "\x00"
        monkeypatch.setattr(qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: cmdline)
        assert proc._pid_is_qemu(123) is True

    def test_pid_is_qemu_rejects_non_qemu_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)
        # A reused PID running something else, even if it somehow references the
        # instance path, is not QEMU.
        cmdline = f"/bin/sh\x00-c\x00ls {tmp_path}\x00"
        monkeypatch.setattr(qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: cmdline)
        assert proc._pid_is_qemu(123) is False

    def test_pid_is_qemu_rejects_qemu_for_other_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = qemu.QemuProcess(tmp_path / "alpha", 5555)
        # A QEMU process, but for a DIFFERENT instance (different hostfwd port).
        cmdline = (
            "qemu-system-x86_64\x00-netdev\x00"
            "user,id=net0,hostfwd=tcp:127.0.0.1:5565-:5555\x00"
        )
        monkeypatch.setattr(qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: cmdline)
        assert proc._pid_is_qemu(123) is False

    def test_pid_is_qemu_orphan_no_port_falls_back_to_qemu_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Config-gone orphan teardown (port unknown): identity falls back to a
        # best-effort 'is it a qemu-system process?' check.
        proc = qemu.QemuProcess(tmp_path)  # no host_adb_port
        qemu_cmdline = "qemu-system-x86_64\x00-M\x00q35\x00"
        monkeypatch.setattr(
            qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: qemu_cmdline
        )
        assert proc._pid_is_qemu(123) is True
        # A reused PID that is NOT qemu is still rejected.
        monkeypatch.setattr(
            qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: "/bin/sh\x00-c\x00ls\x00"
        )
        assert proc._pid_is_qemu(123) is False

    def test_pid_is_qemu_false_when_proc_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)
        monkeypatch.setattr(qemu.QemuProcess, "_read_proc_cmdline", lambda _self, _pid: None)
        assert proc._pid_is_qemu(123) is False

    def test_read_proc_cmdline_reads_proc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)
        monkeypatch.setattr(Path, "read_text", lambda _self: "qemu-system-x86_64\x00")
        assert proc._read_proc_cmdline(123) == "qemu-system-x86_64\x00"

    def test_read_proc_cmdline_none_on_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)

        def _boom(_self: Path) -> str:
            raise OSError("no /proc")

        monkeypatch.setattr(Path, "read_text", _boom)
        assert proc._read_proc_cmdline(123) is None


class TestQemuProcessStart:
    def test_start_proceeds_when_pid_is_live_but_not_qemu(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PID reuse: a stale pidfile names a live-but-unrelated PID. start() must
        # NOT falsely abort with "already running" — is_running routes through
        # the identity guard, sees the PID is not our QEMU, and the launch
        # proceeds (issue #162).
        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "qemu.pid").write_text("4242")
        # Live signal-0 probe, but /proc says the PID is NOT our QEMU.
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda *_a: None)
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: False)

        class _FakePopen:
            pid = 5151

            def __init__(self, argv: list[str], **kwargs: object) -> None: ...

        monkeypatch.setattr("beetroot.vm.qemu.subprocess.Popen", _FakePopen)
        proc = qemu.QemuProcess(inst, 5555)
        # No QemuLaunchError raised — the reused PID did not block the start.
        assert proc.start(["qemu-system-x86_64"]) == 5151
        assert proc.read_pid() == 5151

    def test_start_writes_pidfile_and_returns_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class _FakePopen:
            pid = 7777

            def __init__(self, argv: list[str], **kwargs: object) -> None:
                captured["argv"] = argv
                captured["kwargs"] = kwargs

        monkeypatch.setattr("beetroot.vm.qemu.subprocess.Popen", _FakePopen)
        # Not already running.
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)
        proc = qemu.QemuProcess(tmp_path / "inst", 5555)
        pid = proc.start(["qemu-system-x86_64", "-M", "q35"])
        assert pid == 7777
        assert proc.read_pid() == 7777
        assert captured["argv"] == ["qemu-system-x86_64", "-M", "q35"]
        # Detached + no stdin so the VM survives the CLI process.
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_console_log_path_beside_pidfile(self, tmp_path: Path) -> None:
        proc = qemu.QemuProcess(tmp_path, 5555)
        assert proc.console_log == tmp_path / "qemu-console.log"

    def test_start_redirects_serial_console_to_log_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class _FakePopen:
            pid = 8888

            def __init__(self, argv: list[str], **kwargs: object) -> None:
                captured["argv"] = argv
                captured["kwargs"] = kwargs

        monkeypatch.setattr("beetroot.vm.qemu.subprocess.Popen", _FakePopen)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)
        proc = qemu.QemuProcess(tmp_path / "inst", 5555)
        proc.start(["qemu-system-x86_64"])
        # The console log file is created (truncated) so `beetroot logs` has
        # something to read, and QEMU's stdout/stderr are pointed at it.
        assert proc.console_log.is_file()
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["stderr"] == subprocess.STDOUT
        # stdout is the opened (now-closed) console-log handle.
        assert getattr(kwargs["stdout"], "name", None) == str(proc.console_log)

    def test_start_truncates_stale_console_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakePopen:
            pid = 9

            def __init__(self, argv: list[str], **kwargs: object) -> None: ...

        monkeypatch.setattr("beetroot.vm.qemu.subprocess.Popen", _FakePopen)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)
        proc = qemu.QemuProcess(tmp_path / "inst", 5555)
        proc.console_log.parent.mkdir(parents=True)
        proc.console_log.write_text("STALE FROM A PRIOR BOOT")
        proc.start(["qemu-system-x86_64"])
        assert proc.console_log.read_text() == ""

    def test_start_refuses_when_already_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "qemu.pid").write_text("5")
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: True)
        with pytest.raises(qemu.QemuLaunchError, match="already running"):
            qemu.QemuProcess(tmp_path, 5555).start(["qemu-system-x86_64"])

    def test_start_wraps_oserror(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)

        def _popen(*_a: object, **_k: object) -> None:
            raise OSError("no such binary")

        monkeypatch.setattr("beetroot.vm.qemu.subprocess.Popen", _popen)
        with pytest.raises(qemu.QemuLaunchError, match="failed to launch QEMU"):
            qemu.QemuProcess(tmp_path, 5555).start(["nope"])


class TestQemuProcessTerminate:
    def test_terminate_signals_and_removes_pidfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "qemu.pid").write_text("321")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda pid, sig: sent.append((pid, sig)))
        # PID still names this instance's QEMU on the pre-signal check, then the
        # process exits promptly → no SIGKILL escalation.
        identity = iter([True, False])
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: next(identity))
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is True
        assert sent == [(321, signal.SIGTERM)]
        assert not (tmp_path / "qemu.pid").exists()

    def test_terminate_escalates_to_sigkill_when_wedged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wedged guest that never dies after SIGTERM must be SIGKILLed
        # once the grace window elapses.
        (tmp_path / "qemu.pid").write_text("321")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda pid, sig: sent.append((pid, sig)))
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: True)
        # Collapse the grace window so the test doesn't burn real seconds.
        monkeypatch.setattr("beetroot.vm.qemu._TERM_GRACE_SECONDS", 0.0)
        monkeypatch.setattr("beetroot.vm.qemu.time.sleep", lambda _s: None)
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is True
        assert (321, signal.SIGTERM) in sent
        assert (321, signal.SIGKILL) in sent
        assert not (tmp_path / "qemu.pid").exists()

    def test_terminate_polls_then_exits_before_sigkill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Identity holds on the pre-signal check and the first poll, then the
        # process is gone: SIGTERM is sent, the poll loop sleeps once, then the
        # PID no longer names our QEMU → no SIGKILL.
        (tmp_path / "qemu.pid").write_text("321")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda pid, sig: sent.append((pid, sig)))
        identity = iter([True, True, False])
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: next(identity))
        slept: list[float] = []

        def _sleep(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr("beetroot.vm.qemu.time.sleep", _sleep)
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is True
        assert sent == [(321, signal.SIGTERM)]
        assert slept  # the poll loop slept at least once

    def test_terminate_no_sigkill_if_process_gone_at_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Grace window already elapsed (0s → loop body never runs), but the
        # process has exited by the final identity check → no SIGKILL.
        (tmp_path / "qemu.pid").write_text("321")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda pid, sig: sent.append((pid, sig)))
        monkeypatch.setattr("beetroot.vm.qemu._TERM_GRACE_SECONDS", 0.0)
        # True for the pre-signal gate, False at the deadline check.
        identity = iter([True, False])
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: next(identity))
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is True
        assert sent == [(321, signal.SIGTERM)]

    def test_terminate_refuses_live_but_not_qemu_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PID reuse: the recorded PID is live but belongs to an unrelated
        # process. terminate() must NOT signal it (no SIGTERM, no SIGKILL),
        # return False, and clear the stale pidfile (issue #162).
        (tmp_path / "qemu.pid").write_text("321")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda pid, sig: sent.append((pid, sig)))
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: False)
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is False
        assert sent == []
        assert not (tmp_path / "qemu.pid").exists()

    def test_terminate_no_pidfile_is_noop(self, tmp_path: Path) -> None:
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is False

    def test_terminate_dead_process_removes_pidfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The recorded PID is gone (its /proc entry is absent), so _pid_is_qemu
        # is False, os.kill is never reached, and the stale pidfile is cleared.
        (tmp_path / "qemu.pid").write_text("321")
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: False)
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is False
        assert not (tmp_path / "qemu.pid").exists()

    def test_terminate_sigterm_race_already_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Identity holds at the gate but the process dies before SIGTERM lands
        # (ESRCH) → signalled flips back to False, no escalation, pidfile gone.
        (tmp_path / "qemu.pid").write_text("321")
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: True)

        def _kill(_pid: int, _sig: int) -> None:
            raise OSError(errno.ESRCH, "no such process")

        monkeypatch.setattr("beetroot.vm.qemu.os.kill", _kill)
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is False
        assert not (tmp_path / "qemu.pid").exists()

    def test_terminate_tolerates_pidfile_unlink_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An already-vanished pidfile (e.g. concurrent down) must not raise.
        (tmp_path / "qemu.pid").write_text("321")
        monkeypatch.setattr("beetroot.vm.qemu.os.kill", lambda *_a: None)
        # True at the gate, then gone on the first escalation check → no SIGKILL.
        identity = iter([True, False])
        monkeypatch.setattr(qemu.QemuProcess, "_pid_is_qemu", lambda _self, _pid: next(identity))

        def _unlink(_self: Path, *_a: object, **_k: object) -> None:
            raise OSError("gone")

        monkeypatch.setattr(Path, "unlink", _unlink)
        # contextlib.suppress(OSError) swallows it — terminate still returns True.
        assert qemu.QemuProcess(tmp_path, 5555).terminate() is True
