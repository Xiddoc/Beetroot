"""
QEMU accelerator detection, ``argv`` construction, and process management.

Three concerns, kept deliberately separate so each is unit-testable in
isolation without booting a real VM:

* :func:`detect_accel` — fold the configured accelerator (``auto`` / ``kvm``
  / ``tcg``) against the live ``/dev/kvm`` probe into the concrete
  accelerator QEMU should use. ``auto`` prefers KVM when ``/dev/kvm`` is
  readable+writable, else TCG; an explicit ``kvm`` on a host without a
  usable ``/dev/kvm`` is a hard :class:`QemuLaunchError` (no silent slow
  fallback — the capability-ladder UX in
  ``docs/design/binderless-hosts-qemu-tcg.md`` §7 makes the expensive path
  loud, never surprising).
* :func:`build_qemu_argv` — a *pure* function returning the exact
  ``qemu-system-x86_64`` argv per the design doc §4.4, parametrised by the
  resolved accelerator, vCPU count, memory, kernel/rootfs paths, and the
  host-forwarded ADB port (guest 5555 → host port via user-net
  ``hostfwd``). TCG gets MTTCG (``thread=multi``), the single biggest
  emulation-speed lever.
* :class:`QemuProcess` — start QEMU detached via ``subprocess.Popen``,
  persist a pidfile in the instance directory, and terminate it cleanly on
  ``down``.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Literal

# Resolved accelerator — what QEMU is actually told to use, after folding
# the configured ``auto``/``kvm``/``tcg`` against the live host probe.
ResolvedAccel = Literal["kvm", "tcg"]

# The guest-side ADB port redroid listens on. The user-net ``hostfwd`` maps
# this to a per-instance host port so ``adb connect localhost:<host>`` reaches
# the guest's adbd.
_GUEST_ADB_PORT = 5555

# The QEMU pidfile name inside the instance directory.
_PIDFILE_NAME = "qemu.pid"

# How long ``terminate`` waits for a clean SIGTERM exit before escalating
# to SIGKILL, and how often it polls liveness during that window.
_TERM_GRACE_SECONDS = 5.0
_TERM_POLL_SECONDS = 0.1


class QemuLaunchError(RuntimeError):
    """
    Raised when QEMU cannot be launched (missing accel, binary, or artifacts).
    """


def _dev_kvm_usable() -> bool:
    """
    Return True iff ``/dev/kvm`` exists and is readable + writable.

    KVM acceleration needs read+write access to the device node, not just
    its presence — a host that exposes ``/dev/kvm`` but denies the user
    access (no ``kvm`` group membership) cannot accelerate, so we probe
    ``os.access`` with ``R_OK | W_OK`` rather than ``Path.exists``.
    """
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def detect_accel(requested: Literal["auto", "kvm", "tcg"]) -> ResolvedAccel:
    """
    Resolve the configured accelerator against the live ``/dev/kvm`` probe.

    Args:
        requested: The configured ``vm.accel`` value (``auto`` / ``kvm`` /
            ``tcg``).

    Returns:
        ``"kvm"`` or ``"tcg"`` — the concrete accelerator to pass to QEMU.

    Raises:
        QemuLaunchError: If ``requested`` is ``"kvm"`` but ``/dev/kvm`` is
            absent or not read/writable. The expensive TCG path is never
            silently substituted for an explicit KVM request.
    """
    if requested == "tcg":
        return "tcg"
    if requested == "kvm":
        if not _dev_kvm_usable():
            raise QemuLaunchError(
                "vm.accel: kvm was requested but /dev/kvm is absent or not "
                "read/writable on this host. Use accel: tcg (slow, software "
                "emulation) or accel: auto (auto-falls back to tcg), or grant "
                "the user access to /dev/kvm (e.g. add it to the `kvm` group)."
            )
        return "kvm"
    # ``auto``: prefer KVM when usable, else fall back to TCG.
    return "kvm" if _dev_kvm_usable() else "tcg"


def build_qemu_argv(  # noqa: PLR0913  # 7 keyword-only knobs; each is a distinct QEMU invocation parameter
    *,
    qemu_bin: str,
    accel: ResolvedAccel,
    kernel: Path,
    rootfs: Path,
    smp: int,
    memory_mib: int,
    host_adb_port: int,
) -> list[str]:
    """
    Build the ``qemu-system-x86_64`` argv per the design doc §4.4.

    The invocation is a ``q35`` machine with a virtio root disk, a serial
    console, ``-no-reboot`` (so a guest panic surfaces as a clean QEMU exit
    rather than a reboot loop), and a user-mode NIC whose ``hostfwd`` maps
    the guest's adbd port (5555) to ``host_adb_port`` on the host loopback.

    The accelerator-specific flags carry the performance levers:

    * **tcg** → ``-accel tcg,thread=multi,tb-size=1024`` (MTTCG: one host
      thread per guest vCPU — the single biggest emulation-speed lever) and
      ``-cpu max`` (exposes SSE4/AVX that ART/bionic expect).
    * **kvm** → ``-accel kvm`` and ``-cpu host`` (pass the host CPU through
      for near-native speed).

    Args:
        qemu_bin: The QEMU binary (path or name resolved via PATH).
        accel: The resolved accelerator (``"kvm"`` or ``"tcg"``).
        kernel: Host path to the guest ``bzImage``.
        rootfs: Host path to the guest ext4 root image.
        smp: Number of guest vCPUs (``-smp``).
        memory_mib: Guest RAM in MiB (``-m``).
        host_adb_port: Host loopback port the guest's ADB is forwarded to.

    Returns:
        The full argv list, ready for :class:`subprocess.Popen`.
    """
    if accel == "kvm":
        accel_args = ["-accel", "kvm", "-cpu", "host"]
    else:
        accel_args = ["-accel", "tcg,thread=multi,tb-size=1024", "-cpu", "max"]
    hostfwd = f"hostfwd=tcp:127.0.0.1:{host_adb_port}-:{_GUEST_ADB_PORT}"
    return [
        qemu_bin,
        "-M",
        "q35",
        *accel_args,
        "-smp",
        str(smp),
        "-m",
        str(memory_mib),
        "-nographic",
        "-display",
        "none",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-drive",
        f"file={rootfs},format=raw,if=virtio",
        "-netdev",
        f"user,id=net0,{hostfwd}",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-append",
        "console=ttyS0 root=/dev/vda rw init=/init panic=1",
    ]


class QemuProcess:
    """
    Manage a detached ``qemu-system-x86_64`` process for one instance.

    The PID is persisted to ``<instance_dir>/qemu.pid`` so a later
    ``beetroot down`` (a fresh process that didn't start the VM) can still
    find and terminate it. ``is_running`` reads the pidfile and probes the
    process with ``signal 0``.

    Attributes:
        _instance_dir: The instance directory the pidfile lives in.
    """

    def __init__(self, instance_dir: Path) -> None:
        """
        Bind the manager to an instance directory.

        Args:
            instance_dir: The instance directory (the pidfile is written
                under it).
        """
        self._instance_dir = instance_dir

    @property
    def pidfile(self) -> Path:
        """
        Return the path to this instance's QEMU pidfile.
        """
        return self._instance_dir / _PIDFILE_NAME

    def read_pid(self) -> int | None:
        """
        Return the PID recorded in the pidfile, or ``None`` if absent/garbage.

        Returns:
            The integer PID, or ``None`` when the pidfile is missing or its
            contents don't parse as an integer (a half-written or
            hand-corrupted file is treated as "no process").
        """
        try:
            text = self.pidfile.read_text().strip()
        except OSError:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def is_running(self) -> bool:
        """
        Return True iff the recorded PID names a live process.

        Probes with ``os.kill(pid, 0)`` — signal 0 performs the existence
        and permission check without delivering a signal. A missing pidfile
        or a stale PID (process gone) returns False.
        """
        pid = self.read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError as exc:
            # ESRCH = no such process (stale pid). EPERM = process exists but
            # we can't signal it — still counts as running.
            return exc.errno == errno.EPERM
        return True

    def start(self, argv: list[str]) -> int:
        """
        Launch QEMU detached and persist its PID to the pidfile.

        Args:
            argv: The full QEMU argv (from :func:`build_qemu_argv`).

        Returns:
            The launched process's PID.

        Raises:
            QemuLaunchError: If a VM is already running for this instance,
                or if the QEMU binary cannot be spawned.
        """
        if self.is_running():
            raise QemuLaunchError(
                f"a QEMU micro-VM is already running for this instance "
                f"(pid {self.read_pid()}); run `beetroot down` first."
            )
        self._instance_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(  # noqa: S603  # argv built by build_qemu_argv from validated config; qemu_bin resolved via PATH
                argv,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise QemuLaunchError(
                f"failed to launch QEMU ({argv[0]!r}): {exc}. "
                "Is qemu-system-x86_64 installed and on PATH? "
                "Override the binary with BEETROOT_QEMU_BIN."
            ) from exc
        self.pidfile.write_text(str(proc.pid))
        return proc.pid

    def _pid_alive(self, pid: int) -> bool:
        """
        Return True iff ``pid`` names a live process (signal-0 probe).
        """
        try:
            os.kill(pid, 0)
        except OSError as exc:
            return exc.errno == errno.EPERM
        return True

    def terminate(self) -> bool:
        """
        Terminate the recorded QEMU process and remove the pidfile.

        Sends ``SIGTERM`` first (QEMU exits cleanly on ``SIGTERM``), then
        polls for up to :data:`_TERM_GRACE_SECONDS`. If the process is still
        alive after the grace window — a wedged TCG guest can ignore
        ``SIGTERM`` — it is force-killed with ``SIGKILL`` so ``down`` never
        leaves a runaway emulator behind. A missing pidfile or an
        already-dead process is a no-op. The pidfile is removed regardless
        so a subsequent ``up`` starts fresh.

        Returns:
            True if a signal was delivered to a live process, False if there
            was nothing to terminate.
        """
        pid = self.read_pid()
        signalled = False
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                signalled = True
            except OSError:
                # Process already gone (ESRCH) or unsignalable — nothing to do.
                signalled = False
            if signalled:
                self._escalate_if_alive(pid)
        with contextlib.suppress(OSError):
            self.pidfile.unlink()
        return signalled

    def _escalate_if_alive(self, pid: int) -> None:
        """
        Force-kill ``pid`` if it ignores SIGTERM past the grace window.

        Polls liveness every :data:`_TERM_POLL_SECONDS` for up to
        :data:`_TERM_GRACE_SECONDS`; the moment the process exits, returns
        without escalating. A process still alive at the deadline gets
        ``SIGKILL`` (best-effort — a race where it dies between the final
        poll and the signal is harmless).

        Args:
            pid: The PID already sent ``SIGTERM``.
        """
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return
            time.sleep(_TERM_POLL_SECONDS)
        if self._pid_alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
