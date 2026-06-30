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
_CONSOLE_LOG_NAME = "qemu-console.log"

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
                "the user access to /dev/kvm (e.g. add it to the `kvm` group). "
                "Run `beetroot modes` to see what this host supports."
            )
        return "kvm"
    # ``auto``: prefer KVM when usable, else fall back to TCG.
    return "kvm" if _dev_kvm_usable() else "tcg"


def host_physical_cores() -> int:
    """
    Return the host's physical core count (HyperThread siblings collapsed).

    Under TCG every guest vCPU is one execution-bound host thread, so the
    Stage B sweep in ``docs/design/vm-rnd-log.md`` §B.5 found ``-smp`` is
    fastest pinned to the host's **physical** core count: more vCPUs than
    physical cores oversubscribe the emulator (HT siblings share execution
    units and cross-thread MTTCG sync becomes pure overhead — ``-smp 8``
    regressed vs ``-smp 4`` on a 4-core host). A logical-CPU count
    (``os.process_cpu_count``) would pick ``-smp 8`` on a 4c/8t box and hit
    exactly that regression; counting physical cores avoids it.

    Physical cores are counted from the distinct ``(physical id, core id)``
    pairs in ``/proc/cpuinfo`` and then capped by the CPUs this process may
    actually run on (``sched_getaffinity`` — so a cgroup/taskset-limited CI
    container is respected). Either probe failing falls back to the logical
    CPU count; the result is always at least 1.

    Returns:
        The host physical core count (>= 1).
    """
    logical = _affinity_cpu_count()
    physical = _cpuinfo_physical_cores()
    if physical is None:
        return max(1, logical)
    return max(1, min(physical, logical))


def _affinity_cpu_count() -> int:
    """
    Return the number of CPUs this process may run on (>= 1).

    Prefers ``os.sched_getaffinity`` (respects cgroup/taskset limits on a CI
    container), falling back to ``os.cpu_count`` where the affinity call is
    unavailable.
    """
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        return max(1, len(sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


def _cpuinfo_physical_cores() -> int | None:
    """
    Count distinct ``(physical id, core id)`` pairs in ``/proc/cpuinfo``.

    Returns:
        The physical core count, or ``None`` if ``/proc/cpuinfo`` is absent
        or carries no topology fields (a non-Linux host or an exotic arch) —
        the caller then falls back to the logical CPU count.
    """
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return None
    cores: set[tuple[str, str]] = set()
    phys = core = None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            # Blank line: end of one processor's block — record and reset.
            if phys is not None and core is not None:
                cores.add((phys, core))
            phys = core = None
            continue
        key = key.strip()
        if key == "physical id":
            phys = value.strip()
        elif key == "core id":
            core = value.strip()
    if phys is not None and core is not None:
        cores.add((phys, core))
    return len(cores) or None


def resolve_smp(configured: int | Literal["auto"]) -> int:
    """
    Resolve a configured ``vm.smp`` into a concrete positive vCPU count.

    ``"auto"`` (the schema default) pins ``-smp`` to the host's **physical**
    core count (see :func:`host_physical_cores`) — the vm-rnd-log §B.5
    measured optimum under TCG, where oversubscribing past physical cores
    regresses. An explicit positive integer is honoured verbatim, so a user
    can still pin a smaller count or override the auto-size.

    Args:
        configured: The ``vm.smp`` value from the config (an explicit vCPU
            count or ``"auto"``).

    Returns:
        A concrete vCPU count >= 1.
    """
    if configured == "auto":
        return host_physical_cores()
    return configured


def build_qemu_argv(  # noqa: PLR0913  # distinct QEMU invocation knobs; each is its own parameter
    *,
    qemu_bin: str,
    accel: ResolvedAccel,
    kernel: Path,
    rootfs: Path,
    smp: int,
    memory_mib: int,
    host_adb_port: int,
    disk_format: Literal["raw", "qcow2"] = "raw",
    disk_cache: str | None = None,
    monitor_socket: Path | None = None,
    loadvm: str | None = None,
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

    The kernel command line also carries ``mitigations=off``: the guest is
    an ephemeral, single-tenant research sandbox, so the CPU
    speculative-execution mitigations (retpolines, lfence barriers, …) buy
    nothing here — and they are pure overhead either way (extra *emulated*
    work under TCG, real serialization under KVM). Turning them off shaves
    boot and steady-state CPU time with no relevant security loss for a
    throwaway VM.

    The ``disk_format`` / ``disk_cache`` / ``monitor_socket`` / ``loadvm``
    knobs are the hooks the ``vm.boot_cache`` warm-start path needs (issue
    #49/#83): a **qcow2** root disk so internal snapshots are possible, an
    HMP **monitor** socket so a checkpoint can be issued after boot, and a
    ``-loadvm`` launch mode so a cached snapshot is *resumed* (~10 s) instead
    of cold-booted (~minutes under TCG). They all default to the original
    cold-boot behaviour (a raw disk, no monitor, no resume) so the plain
    ``up`` path is byte-for-byte unchanged.

    Args:
        qemu_bin: The QEMU binary (path or name resolved via PATH).
        accel: The resolved accelerator (``"kvm"`` or ``"tcg"``).
        kernel: Host path to the guest ``bzImage``.
        rootfs: Host path to the guest root image (raw, or a qcow2 overlay
            when ``disk_format="qcow2"``).
        smp: Number of guest vCPUs (``-smp``).
        memory_mib: Guest RAM in MiB (``-m``).
        host_adb_port: Host loopback port the guest's ADB is forwarded to.
        disk_format: ``"raw"`` (default) or ``"qcow2"`` — the on-disk format
            of ``rootfs``. The warm-start overlay is qcow2 (savevm stores its
            RAM+device snapshot inside it).
        disk_cache: Optional ``cache=`` mode for the root drive (e.g.
            ``"unsafe"``). ``None`` (default) omits it, leaving QEMU's
            default.
        monitor_socket: Optional path for an HMP monitor UNIX socket
            (``-monitor unix:<path>,server,nowait``). The warm-start path
            connects here to issue ``savevm`` once the guest is up.
        loadvm: Optional internal-snapshot tag to resume at launch
            (``-loadvm <tag>``). When set, the guest boots straight into the
            checkpointed running state instead of from ``init``.

    Returns:
        The full argv list, ready for :class:`subprocess.Popen`.
    """
    if accel == "kvm":
        accel_args = ["-accel", "kvm", "-cpu", "host"]
    else:
        accel_args = ["-accel", "tcg,thread=multi,tb-size=1024", "-cpu", "max"]
    hostfwd = f"hostfwd=tcp:127.0.0.1:{host_adb_port}-:{_GUEST_ADB_PORT}"
    drive = f"file={rootfs},format={disk_format},if=virtio"
    if disk_cache is not None:
        drive += f",cache={disk_cache}"
    argv = [
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
        drive,
        "-netdev",
        f"user,id=net0,{hostfwd}",
        "-device",
        "virtio-net-pci,netdev=net0",
    ]
    if monitor_socket is not None:
        argv += ["-monitor", f"unix:{monitor_socket},server,nowait"]
    if loadvm is not None:
        argv += ["-loadvm", loadvm]
    argv += [
        "-append",
        "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off",
    ]
    return argv


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

    def __init__(self, instance_dir: Path, host_adb_port: int | None = None) -> None:
        """
        Bind the manager to an instance directory and (optionally) its ADB port.

        Args:
            instance_dir: The instance directory (the pidfile is written
                under it).
            host_adb_port: The instance's host-loopback ADB-forward port, used
                as the per-instance identity token to confirm a recorded PID is
                really *this* instance's QEMU (every launch — plain and
                boot_cache — carries it in the ``hostfwd`` argv element, unlike
                the instance dir, which only the boot_cache argv references via
                its overlay/monitor paths). See :meth:`_pid_is_qemu` (#162).
                ``None`` is the config-gone orphan-teardown case (the
                ``beetroot.yaml`` was deleted, so the port can't be resolved):
                identity then falls back to a best-effort ``qemu-system`` check.
        """
        self._instance_dir = instance_dir
        self._host_adb_port = host_adb_port

    @property
    def pidfile(self) -> Path:
        """
        Return the path to this instance's QEMU pidfile.
        """
        return self._instance_dir / _PIDFILE_NAME

    @property
    def console_log(self) -> Path:
        """
        Return the path the guest serial console is persisted to.

        QEMU runs ``-nographic`` (ttyS0 → process stdout), and :meth:`start`
        redirects that stdout here so the boot trace survives after ``up``
        returns — the file ``beetroot logs`` reads for a ``vm`` instance.
        """
        return self._instance_dir / _CONSOLE_LOG_NAME

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

    def _read_proc_cmdline(self, pid: int) -> str | None:
        """
        Return ``/proc/<pid>/cmdline`` as NUL-delimited text, or ``None``.

        The kernel separates argv entries with NUL bytes; we keep them so a
        caller can substring-match a whole argument. Any read failure (the
        process exited between the liveness probe and this read, or a host
        with no ``/proc``) returns ``None``.
        """
        try:
            return Path(f"/proc/{pid}/cmdline").read_text()
        except OSError:
            return None

    def _pid_is_qemu(self, pid: int) -> bool:
        """
        Return True iff ``pid`` is THIS instance's QEMU process.

        The pidfile is persistent and the kernel recycles PIDs, so a recorded
        PID that is merely *live* is not enough — a stale entry can name an
        unrelated process that reused the number. We require
        ``/proc/<pid>/cmdline`` to both look like a ``qemu-system`` invocation
        and carry this instance's host-ADB ``hostfwd`` port, which the
        stride allocator makes unique per instance and which **every** launch
        path embeds in its argv (``-netdev user,…,hostfwd=tcp:127.0.0.1:<port>-:…``).
        Matching the instance *directory* instead would false-negative on the
        default plain ``up`` path, whose kernel/rootfs live in a shared
        artifacts cache outside the instance dir (only the boot_cache argv
        references the instance dir, via its overlay/monitor paths) — that
        gap is exactly what made a healthy plain-path QEMU report not-running
        (#162). A reused PID belonging to anything else, or to another
        instance's QEMU (different port), reports False and is never
        signalled. A missing ``/proc`` entry (the process is gone) is False.
        """
        cmdline = self._read_proc_cmdline(pid)
        if cmdline is None:
            return False
        names_qemu = "qemu-system" in cmdline
        if self._host_adb_port is None:
            # Orphan teardown: the config (hence the port) is gone, so we can't
            # pin the identity to this instance — best-effort 'is it QEMU?'.
            return names_qemu
        names_instance = f"hostfwd=tcp:127.0.0.1:{self._host_adb_port}-" in cmdline
        return names_qemu and names_instance

    def is_running(self) -> bool:
        """
        Return True iff the recorded PID is this instance's live QEMU process.

        Probes with ``os.kill(pid, 0)`` — signal 0 performs the existence and
        permission check without delivering a signal — then confirms the PID
        still *names* this instance's QEMU via :meth:`_pid_is_qemu`. A missing
        pidfile, a stale PID (process gone), or a live-but-reused PID that now
        belongs to an unrelated process (issue #162) all return False.
        """
        pid = self.read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError as exc:
            # ESRCH = no such process (stale pid). EPERM = process exists but
            # we can't signal it — still alive, pending the identity check.
            if exc.errno != errno.EPERM:
                return False
        return self._pid_is_qemu(pid)

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
            # Redirect the guest serial console (ttyS0 → QEMU stdout under
            # -nographic) to a persisted file so `beetroot logs` can surface
            # the boot trace after `up` returns. Truncated each start, so a
            # fresh boot gets a fresh log. The child dups the fd at fork, so
            # the handle is safe to close once Popen has launched.
            log_handle = self.console_log.open("wb")
            try:
                proc = subprocess.Popen(  # noqa: S603  # argv built by build_qemu_argv from validated config; qemu_bin resolved via PATH
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_handle.close()
        except OSError as exc:
            raise QemuLaunchError(
                f"failed to launch QEMU ({argv[0]!r}): {exc}. "
                "Is qemu-system-x86_64 installed and on PATH? "
                "Override the binary with BEETROOT_QEMU_BIN."
            ) from exc
        self.pidfile.write_text(str(proc.pid))
        return proc.pid

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

        The recorded PID is verified to still NAME this instance's QEMU
        (:meth:`_pid_is_qemu`) before any signal is sent: the pidfile is
        persistent and PIDs are recycled, so a stale entry pointing at a
        reused PID must never SIGTERM/SIGKILL an unrelated process
        (issue #162). A live-but-mismatched PID is left untouched and only
        its stale pidfile is cleared.

        Returns:
            True if a signal was delivered to this instance's QEMU, False if
            there was nothing of ours to terminate.
        """
        pid = self.read_pid()
        signalled = False
        # Only signal a PID that still names THIS instance's QEMU. A reused
        # PID (or a process we can no longer see in /proc) is left alone — the
        # stale pidfile is cleared below.
        if pid is not None and self._pid_is_qemu(pid):
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

        Polls every :data:`_TERM_POLL_SECONDS` for up to
        :data:`_TERM_GRACE_SECONDS`; the moment the process is no longer this
        instance's QEMU (it exited, or — racing the kernel — its PID was
        recycled), returns without escalating. A process still ours at the
        deadline gets ``SIGKILL`` (best-effort — a race where it dies between
        the final poll and the signal is harmless). Re-checking identity
        (:meth:`_pid_is_qemu`) rather than bare liveness keeps the SIGKILL
        from landing on a reused PID (issue #162).

        Args:
            pid: The PID already sent ``SIGTERM``.
        """
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not self._pid_is_qemu(pid):
                return
            time.sleep(_TERM_POLL_SECONDS)
        if self._pid_is_qemu(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
