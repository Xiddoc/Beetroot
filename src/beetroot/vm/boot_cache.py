"""
Warm-start boot cache for the ``binder: vm`` backend (issue #49 / #83).

Booting redroid in the micro-VM under TCG is **CPU-bound** — emulating ART /
Zygote / ``system_server`` to ``sys.boot_completed`` costs ~3 min on a 4-core
host (Android 14), and the entropy / disk-cache micro-levers investigated in
issue #83 do **not** move it (see ``docs/design/vm-rnd-log.md`` Stage E). The
one lever that does is to **not boot at all** on repeat starts: checkpoint the
*running machine state* once, then resume it.

This module is the QEMU integration the
[savevm design note](../../docs/design/vm-savevm-cache.md) scoped as the
follow-up to the cache-key helper. It manages three things behind the opt-in
``vm.boot_cache`` config flag:

* a **qcow2 overlay** over the (read-only) raw rootfs, so QEMU can store an
  internal snapshot (savevm needs a qcow2 disk);
* a check for whether that overlay already carries the boot snapshot, so
  :meth:`beetroot.backends.vm.VmDeviceBackend.up` knows whether to resume
  (``-loadvm``) or cold-boot;
* issuing ``savevm`` over an HMP monitor socket after the first cold boot, so
  the next ``up`` resumes in ~10 s instead of cold-booting in ~minutes.

Measured on a binderless, KVM-less host (Android 14, pure TCG): cold boot to
first host ADB ~222 s; warm resume ~10 s — a ~22x speedup. The checkpoint is
a single qcow2 (~2 GiB) inside the instance directory.

The external tools (``qemu-img``) and the monitor socket are driven through the
stdlib so the logic stays unit-testable without a real VM: ``qemu-img`` calls go
through :mod:`subprocess` (monkeypatched in tests) and the monitor handshake
runs over a stdlib ``AF_UNIX`` socket (tests drive it with a real local server).
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from beetroot.settings import settings
from beetroot.vm.qemu import QemuLaunchError

# Tag of the internal qcow2 snapshot that holds the booted machine state.
SNAPSHOT_TAG = "beetroot-boot"

# Per-instance artifact names (kept beside the QEMU pidfile in the instance dir).
_OVERLAY_NAME = "vm-overlay.qcow2"
_MONITOR_NAME = "qemu-monitor.sock"

# How long to wait for the HMP monitor handshake + savevm acknowledgement.
# savevm of a multi-GB guest writes a few GiB to the overlay; under TCG that is
# ~30-60 s, so the timeout is generous (a slow host should not abort a valid
# checkpoint that is still being written).
_MONITOR_TIMEOUT_SECONDS = 300.0

# Read size for draining the monitor socket.
_MONITOR_CHUNK = 4096

# HMP prints this prompt after the connect banner and again after each command
# completes. Reading up to the *second* one is how we wait for ``savevm`` to
# finish (it is otherwise silent on success).
_HMP_PROMPT = b"(qemu)"


def overlay_path(instance_dir: Path) -> Path:
    """
    Return the qcow2 boot-cache overlay path for an instance.

    Args:
        instance_dir: The instance directory.

    Returns:
        ``<instance_dir>/vm-overlay.qcow2`` — the copy-on-write disk the warm
        start boots from and stores its snapshot inside.
    """
    return instance_dir / _OVERLAY_NAME


def monitor_path(instance_dir: Path) -> Path:
    """
    Return the HMP monitor UNIX-socket path for an instance.

    Args:
        instance_dir: The instance directory.

    Returns:
        ``<instance_dir>/qemu-monitor.sock`` — where QEMU listens for the
        ``savevm`` command after the first cold boot.
    """
    return instance_dir / _MONITOR_NAME


def create_overlay(base_rootfs: Path, overlay: Path) -> None:
    """
    Create a qcow2 overlay over the raw rootfs (``qemu-img create``).

    The overlay is copy-on-write: the base raw image stays pristine (so it can
    be shared by several instances and rebuilt independently), and the boot
    snapshot plus all guest writes land in the overlay. Delete the overlay to
    discard the cache and force a fresh cold boot.

    Args:
        base_rootfs: The raw rootfs image to use as the read-only backing file.
        overlay: Path the qcow2 overlay is written to.

    Raises:
        QemuLaunchError: If ``qemu-img`` is missing or exits non-zero.
    """
    try:
        subprocess.run(  # noqa: S603  # argv built from settings + validated paths
            [
                settings.qemu_img_bin,
                "create",
                "-q",
                "-f",
                "qcow2",
                "-F",
                "raw",
                "-b",
                str(base_rootfs),
                str(overlay),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise QemuLaunchError(
            f"failed to create the boot-cache overlay: {settings.qemu_img_bin!r} not found. "
            "Install qemu-utils, or set vm.boot_cache: false. "
            "Override the binary with BEETROOT_QEMU_IMG_BIN."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise QemuLaunchError(
            f"qemu-img create failed (exit {exc.returncode}): {(exc.stderr or '').strip()}"
        ) from exc


def snapshot_present(overlay: Path, tag: str = SNAPSHOT_TAG) -> bool:
    """
    Return True iff ``overlay`` already carries an internal snapshot named ``tag``.

    Used to decide between resuming (``-loadvm``) and cold-booting. A missing
    overlay, or a ``qemu-img`` that can't read it, reports ``False`` — the
    caller then cold-boots (and re-checkpoints), which is always safe.

    Args:
        overlay: The qcow2 overlay to inspect.
        tag: The snapshot tag to look for (default :data:`SNAPSHOT_TAG`).

    Returns:
        ``True`` if a snapshot with that tag exists, else ``False``.
    """
    if not overlay.is_file():
        return False
    try:
        result = subprocess.run(  # noqa: S603  # argv built from settings + validated paths
            [settings.qemu_img_bin, "snapshot", "-l", str(overlay)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    # `qemu-img snapshot -l` prints a table; the tag appears in the ID/TAG
    # column. A whitespace-delimited token match avoids a stray substring hit.
    return any(tag in line.split() for line in result.stdout.splitlines())


def save_snapshot(monitor: Path, tag: str = SNAPSHOT_TAG) -> bool:
    """
    Issue ``savevm <tag>`` over the HMP monitor socket (best-effort).

    HMP greets with a banner ending in a ``(qemu)`` prompt, then prints a fresh
    prompt after each command completes (``savevm`` is silent on success). So
    the protocol is: **drain the banner up to its prompt, send the command,
    then read up to the next prompt** — the second prompt is what proves
    ``savevm`` finished writing (this is the load-bearing fix: an earlier
    version broke on the *banner* prompt and returned before ``savevm`` ran).

    Best-effort by design: the VM keeps running regardless, so a failed
    checkpoint just means the next ``up`` cold-boots again. Any socket error
    (unreachable, reset, timeout mid-write) reports failure rather than raising.

    Args:
        monitor: Path to the HMP monitor UNIX socket QEMU created.
        tag: The snapshot tag to write (default :data:`SNAPSHOT_TAG`).

    Returns:
        ``True`` if ``savevm`` completed with no error in its reply, ``False``
        if the socket was unreachable / errored or QEMU reported an error.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_MONITOR_TIMEOUT_SECONDS)
            sock.connect(str(monitor))
            _read_to_prompt(sock)  # drain the connect banner up to its prompt
            sock.sendall(f"savevm {tag}\n".encode())
            reply = _read_to_prompt(sock)  # wait for savevm to finish + reprompt
    except OSError:
        return False
    return "error" not in reply.lower()


def _read_to_prompt(sock: socket.socket) -> str:
    """
    Read from ``sock`` until the HMP ``(qemu)`` prompt re-appears (or EOF).

    The prompt has no trailing newline (``"(qemu) "``), so we test the
    right-stripped buffer's tail. Decoding is lenient — the banner may carry
    control bytes. A closed connection (empty read) ends the loop with whatever
    was read so far.

    Args:
        sock: The connected monitor socket.

    Returns:
        Everything read up to (and including) the prompt, decoded as UTF-8.
    """
    buf = b""
    while not buf.rstrip().endswith(_HMP_PROMPT):
        chunk = sock.recv(_MONITOR_CHUNK)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace")
