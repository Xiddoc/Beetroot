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
import io
import json
import os
import signal
import socket
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

# Socket-level receive timeout for the QMP savevm exchange. ``savevm`` of a
# multi-GB running guest can take tens of seconds while QEMU writes RAM into
# the qcow2, so the per-read timeout is generous.
_QMP_RECV_TIMEOUT = 300.0


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


def build_qemu_argv(  # noqa: PLR0913  # each kw is a distinct QEMU invocation parameter
    *,
    qemu_bin: str,
    accel: ResolvedAccel,
    kernel: Path,
    rootfs: Path,
    smp: int,
    memory_mib: int,
    host_adb_port: int,
    rootfs_format: Literal["raw", "qcow2"] = "raw",
    qmp_socket: Path | None = None,
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

    Two entropy levers were investigated as TCG cold-boot speedups (issue #83)
    — a ``virtio-rng-pci`` device and ``random.trust_cpu=on`` — and
    deliberately *not* adopted. The guest CRNG already reaches ``crng init
    done`` ~0.15 s into boot (x86_64 ``defconfig`` ships
    ``CONFIG_RANDOM_TRUST_CPU=y`` and ``-cpu max`` exposes ``RDRAND``), ~100 s
    before ``sys.boot_completed``, so entropy is not on the critical path and
    ``random.trust_cpu=on`` is a no-op against that default. The guest kernel
    also lacks the ``virtio-rng`` driver (``rng_current=none``), so a
    ``virtio-rng-pci`` device would be inert without a kernel rebuild — for
    zero measured boot benefit. See ``docs/design/vm-cold-boot-perf.md`` for
    the measurements and root-cause evidence.

    The ``vm.snapshot`` warm-start cache (issue #49) drives the three
    optional parameters: ``rootfs_format="qcow2"`` boots the per-instance
    qcow2 overlay (which can hold internal ``savevm`` snapshots, unlike a raw
    disk), ``qmp_socket`` adds a ``-qmp`` monitor socket so a helper can issue
    ``savevm`` after boot, and ``loadvm`` adds ``-loadvm <tag>`` so a launch
    resumes a previously-checkpointed machine instead of cold-booting. The
    defaults (``raw`` / no monitor / no loadvm) reproduce the plain cold-boot
    invocation exactly, so non-snapshot instances are unchanged.

    Args:
        qemu_bin: The QEMU binary (path or name resolved via PATH).
        accel: The resolved accelerator (``"kvm"`` or ``"tcg"``).
        kernel: Host path to the guest ``bzImage``.
        rootfs: Host path to the guest root disk (the raw ext4 image, or a
            qcow2 overlay over it when ``rootfs_format="qcow2"``).
        smp: Number of guest vCPUs (``-smp``).
        memory_mib: Guest RAM in MiB (``-m``).
        host_adb_port: Host loopback port the guest's ADB is forwarded to.
        rootfs_format: ``"raw"`` (default) or ``"qcow2"`` — the ``-drive``
            ``format=`` for the root disk. ``qcow2`` is required for the
            ``savevm`` warm-start overlay.
        qmp_socket: When set, adds a ``-qmp unix:<path>,server,nowait``
            monitor socket so :func:`qmp_savevm` can checkpoint the guest.
        loadvm: When set, adds ``-loadvm <tag>`` so QEMU resumes the named
            internal snapshot instead of cold-booting.

    Returns:
        The full argv list, ready for :class:`subprocess.Popen`.
    """
    if accel == "kvm":
        accel_args = ["-accel", "kvm", "-cpu", "host"]
    else:
        accel_args = ["-accel", "tcg,thread=multi,tb-size=1024", "-cpu", "max"]
    hostfwd = f"hostfwd=tcp:127.0.0.1:{host_adb_port}-:{_GUEST_ADB_PORT}"
    monitor_args = ["-qmp", f"unix:{qmp_socket},server,nowait"] if qmp_socket is not None else []
    loadvm_args = ["-loadvm", loadvm] if loadvm is not None else []
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
        *monitor_args,
        "-kernel",
        str(kernel),
        "-drive",
        f"file={rootfs},format={rootfs_format},if=virtio",
        "-netdev",
        f"user,id=net0,{hostfwd}",
        "-device",
        "virtio-net-pci,netdev=net0",
        *loadvm_args,
        "-append",
        "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off",
    ]


def create_overlay(*, base_rootfs: Path, overlay: Path, qemu_img_bin: str) -> None:
    """
    Create (or recreate) a qcow2 overlay backed by the raw rootfs.

    The overlay is the per-instance writable boot disk for the ``vm.snapshot``
    warm-start cache: it holds the instance's disk deltas *and* the internal
    ``savevm`` checkpoints (a raw disk can hold neither). An existing overlay
    is removed first so a fresh cold boot always starts from the pristine
    backing image.

    Args:
        base_rootfs: Host path to the pristine raw ext4 rootfs (the qcow2
            backing file).
        overlay: Host path to write the qcow2 overlay to.
        qemu_img_bin: The ``qemu-img`` binary (path or name resolved via PATH).

    Raises:
        QemuLaunchError: If ``qemu-img`` cannot be spawned or fails.
    """
    with contextlib.suppress(OSError):
        overlay.unlink()
    try:
        subprocess.run(  # noqa: S603  # qemu_img_bin resolved via PATH; paths from validated config
            [
                qemu_img_bin,
                "create",
                "-f",
                "qcow2",
                "-b",
                str(base_rootfs),
                "-F",
                "raw",
                str(overlay),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise QemuLaunchError(
            f"failed to run {qemu_img_bin!r}: {exc}. Is qemu-img installed and on "
            "PATH? Override the binary with BEETROOT_QEMU_IMG_BIN."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise QemuLaunchError(
            f"qemu-img could not create the snapshot overlay {overlay}: "
            f"{(exc.stderr or '').strip()}"
        ) from exc


def qmp_savevm(*, socket_path: Path, tag: str) -> None:
    """
    Checkpoint the running guest by issuing ``savevm <tag>`` over QMP.

    Connects to the QEMU QMP monitor unix socket (added by
    :func:`build_qemu_argv` when ``qmp_socket`` is set), negotiates
    capabilities, and runs ``savevm`` via ``human-monitor-command``. The HMP
    ``savevm`` prints nothing on success and an error string on failure, so a
    non-empty ``return`` (or a QMP ``error`` object) is surfaced as a
    :class:`QemuLaunchError`.

    Args:
        socket_path: Path to the QEMU QMP unix socket.
        tag: The snapshot tag to write (resumed later with ``-loadvm <tag>``).

    Raises:
        QemuLaunchError: If the socket can't be reached, the connection drops
            mid-exchange, or QEMU reports a ``savevm`` failure.
    """
    sock = _qmp_connect(socket_path)
    try:
        with sock.makefile("rwb") as stream:
            greeting = stream.readline()
            if not greeting:
                raise QemuLaunchError(f"QMP socket {socket_path} closed before the greeting")
            _qmp_command(stream, "qmp_capabilities")
            reply = _qmp_command(
                stream,
                "human-monitor-command",
                {"command-line": f"savevm {tag}"},
            )
    finally:
        sock.close()
    if "error" in reply:
        raise QemuLaunchError(f"QEMU savevm failed: {reply['error']}")
    result = reply.get("return", "")
    if isinstance(result, str) and result.strip():
        raise QemuLaunchError(f"QEMU savevm failed: {result.strip()}")


def _qmp_connect(socket_path: Path) -> socket.socket:
    """
    Connect to the QEMU QMP unix socket, or raise :class:`QemuLaunchError`.

    The ``server,nowait`` monitor socket is created synchronously when QEMU
    launches — long before ``savevm`` is issued (the guest has booted to adb
    by then) — so a single connect attempt suffices.

    Args:
        socket_path: Path to the QEMU QMP unix socket.

    Returns:
        The connected socket (caller closes it).

    Raises:
        QemuLaunchError: If the socket cannot be connected.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_QMP_RECV_TIMEOUT)
    try:
        sock.connect(str(socket_path))
    except OSError as exc:
        sock.close()
        raise QemuLaunchError(
            f"could not connect to the QEMU QMP socket {socket_path}: {exc}"
        ) from exc
    return sock


def _qmp_command(
    stream: io.BufferedIOBase,
    execute: str,
    arguments: dict[str, str] | None = None,
) -> dict[str, object]:
    """
    Send one QMP command and return the first non-event reply.

    Args:
        stream: The buffered read/write file object over the QMP socket.
        execute: The QMP command name.
        arguments: Optional command arguments.

    Returns:
        The decoded reply object (the first message carrying ``return`` or
        ``error``; asynchronous ``event`` messages are skipped).

    Raises:
        QemuLaunchError: If the connection closes before a reply arrives.
    """
    payload: dict[str, object] = {"execute": execute}
    if arguments is not None:
        payload["arguments"] = arguments
    stream.write(json.dumps(payload).encode() + b"\n")
    stream.flush()
    while True:
        line = stream.readline()
        if not line:
            raise QemuLaunchError("QMP connection closed before a reply arrived")
        message: dict[str, object] = json.loads(line)
        if "event" in message:
            continue
        return message


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
