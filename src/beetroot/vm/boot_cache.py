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

import hashlib
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
# Sidecar recording the digest of the kernel+rootfs the overlay was built from,
# so a kernel/rootfs rebuild auto-invalidates a now-stale checkpoint (issue #126).
_OVERLAY_KEY_NAME = "vm-overlay.cache-key"

# Streaming read size for hashing the (multi-GB) rootfs without loading it all.
_IDENTITY_CHUNK = 1024 * 1024
# How much of the identity digest to keep. 16 hex (64 bits) is far past
# collision risk for one instance's two input files and stays readable.
_IDENTITY_HEX_LEN = 16

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


def overlay_key_path(instance_dir: Path) -> Path:
    """
    Return the cache-key sidecar path recording the overlay's base identity.

    Args:
        instance_dir: The instance directory.

    Returns:
        ``<instance_dir>/vm-overlay.cache-key`` — the digest of the kernel +
        rootfs the overlay was built from (issue #126).
    """
    return instance_dir / _OVERLAY_KEY_NAME


# Memoized {(path, st_size, st_mtime_ns): sha256-hexdigest}. The rootfs is a
# multi-GB *immutable* artifact, so a full SHA-256 re-stream on every warm-resume
# staleness check (`base_identity` runs on every `up`) is pure waste. Keying on
# (path, size, mtime_ns) reuses the digest whenever the file is byte-identical
# and recomputes only when size/mtime say it changed — same final fingerprint,
# no re-hash on the hot path (issue #254).
_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_IDENTITY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    hexdigest = _stream_sha256(path)
    _HASH_CACHE[key] = hexdigest
    return hexdigest


def base_identity(kernel: Path, rootfs: Path, smp: int, memory_mib: int) -> str:
    """
    Compute a stable digest over the kernel + rootfs + geometry an overlay uses.

    Mirrors ``scripts/vm_cache_key.compute_cache_key``'s algorithm — each file's
    basename plus its streamed SHA-256, folded in (basename, content-hash) order
    so the result is independent of argument order even when two inputs share a
    basename (issue #235). The resolved ``-smp``/``-m`` geometry is folded in too
    (as decimal bytes): a ``-loadvm`` resume into a different vCPU/RAM geometry is
    rejected by QEMU, so a geometry change must invalidate the checkpoint (issue
    #161). A kernel/rootfs rebuild or a geometry edit changes the digest, which is
    what lets :func:`overlay_is_stale` invalidate a checkpoint taken against the
    old artifacts (issue #126 / #49).

    Args:
        kernel: The guest ``bzImage`` the overlay boots.
        rootfs: The raw rootfs image the overlay backs onto.
        smp: The resolved (concrete, not ``"auto"``) vCPU count QEMU launches with.
        memory_mib: The guest RAM in MiB QEMU launches with.

    Returns:
        A 16-hex-character identity digest.

    Raises:
        FileNotFoundError: If either input file does not exist.
    """
    # Hash each input file exactly once (the rootfs is multi-GB; hashing it
    # inside the sort key AND again in the fold would double its cost on every
    # `up`). Precompute {path: digest}, then sort + fold off that dict.
    digests = {path: _hash_file(path) for path in (kernel, rootfs)}
    combined = hashlib.sha256()
    for path in sorted(digests, key=lambda p: (p.name, digests[p])):
        combined.update(path.name.encode())
        combined.update(b"\0")
        combined.update(digests[path].encode())
        combined.update(b"\0")
    combined.update(str(smp).encode())
    combined.update(b"\0")
    combined.update(str(memory_mib).encode())
    combined.update(b"\0")
    return combined.hexdigest()[:_IDENTITY_HEX_LEN]


def record_identity(
    instance_dir: Path, kernel: Path, rootfs: Path, smp: int, memory_mib: int
) -> None:
    """
    Write the overlay's base-identity sidecar (kernel + rootfs + geometry digest).

    Called when the overlay is (re)created so a later kernel/rootfs change or a
    ``-smp``/``-m`` geometry edit is detectable. Arguments mirror
    :func:`base_identity`. The sidecar is written atomically (temp file +
    ``os.replace``) so an interrupted ``record_identity`` never leaves a
    keyless overlay that the next ``up`` would judge stale and discard (issue
    #175).

    Args:
        instance_dir: The instance directory the sidecar is written into.
        kernel: The guest ``bzImage`` the overlay boots.
        rootfs: The raw rootfs image the overlay backs onto.
        smp: The resolved vCPU count QEMU launches with.
        memory_mib: The guest RAM in MiB QEMU launches with.
    """
    target = overlay_key_path(instance_dir)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(base_identity(kernel, rootfs, smp, memory_mib), encoding="utf-8")
    tmp.replace(target)


def read_identity(instance_dir: Path) -> str | None:
    """
    Return the recorded overlay base identity, or ``None`` if absent/unreadable.

    Args:
        instance_dir: The instance directory holding the sidecar.

    Returns:
        The recorded digest, or ``None`` when the sidecar is missing or empty.
    """
    try:
        return overlay_key_path(instance_dir).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def overlay_is_stale(
    instance_dir: Path, kernel: Path, rootfs: Path, smp: int, memory_mib: int
) -> bool:
    """
    Return True iff the overlay's recorded identity ≠ the current kernel/rootfs/geometry.

    A missing/unreadable sidecar (e.g. an overlay built before issue #126) also
    counts as stale: we can't prove it matches the current artifacts, and
    resuming a stale checkpoint is worse than one cold boot. A ``-smp``/``-m``
    geometry edit flips this too (issue #161) — QEMU rejects a ``-loadvm`` into a
    mismatched geometry. The caller then discards + recreates the overlay and
    re-checkpoints.

    Args:
        instance_dir: The instance directory holding the overlay + sidecar.
        kernel: The currently-resolved guest kernel.
        rootfs: The currently-resolved raw rootfs.
        smp: The resolved vCPU count QEMU will launch with.
        memory_mib: The guest RAM in MiB QEMU will launch with.

    Returns:
        ``True`` if the checkpoint should be invalidated, else ``False``.
    """
    return read_identity(instance_dir) != base_identity(kernel, rootfs, smp, memory_mib)


def discard_overlay(instance_dir: Path) -> None:
    """
    Remove the boot-cache overlay and its identity sidecar (forces a cold boot).

    Args:
        instance_dir: The instance directory.
    """
    overlay_path(instance_dir).unlink(missing_ok=True)
    overlay_key_path(instance_dir).unlink(missing_ok=True)


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
