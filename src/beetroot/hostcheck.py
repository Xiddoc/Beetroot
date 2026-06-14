"""
Host-side capability probes for the redroid backend.

redroid runs Android's userspace **directly against the host kernel** —
it is a container, not an emulator, so it ships no kernel of its own.
The one hard, non-negotiable requirement is the kernel *binder* driver:
Android's ``init``, ``servicemanager``, and ``zygote`` all block on
``/dev/binder`` at boot, and redroid provides no userspace substitute.
``privileged: true`` (the Docker flag) can be narrowed on a host that
already has binder, but binder itself cannot be emulated away.

This module probes whether the host can satisfy that requirement so
``beetroot up`` and ``beetroot doctor`` can fail fast with an actionable
diagnosis instead of leaving a container that *starts* but never boots
Android (``docker compose up -d`` returns success the moment the
container is created — the binder failure only surfaces later in the
logs).

Four host states are distinguished (see :class:`BinderStatus`):

* ``ready`` — binder device nodes already exist (``/dev/binder``) or the
  kernel exposes binderfs (``binder`` in ``/proc/filesystems``); redroid
  can boot.
* ``loadable`` — the kernel was built with binder as a module
  (``CONFIG_ANDROID_BINDER_IPC=m`` / ``=y``) but no device nodes or
  binderfs are present yet; the remedy is ``modprobe binder_linux``
  (which works on GitHub-hosted CI runners, where you have ``sudo``).
* ``unsupported`` — the kernel has binder compiled out
  (``# CONFIG_ANDROID_BINDER_IPC is not set``); no Docker flag can make
  redroid boot here. The remedy is the adb backend against a remote
  device, or moving to a host whose kernel provides binder.
* ``unknown`` — binder isn't currently present and the kernel config
  couldn't be read (e.g. macOS, or a locked-down ``/proc``), so we can't
  tell whether binder is supported. Treated as a soft warning rather
  than a hard failure.
"""

from __future__ import annotations

import gzip
import platform
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

# The three binder device nodes redroid's boot path consumes. Any one of
# them existing is enough to prove a usable binder driver is present (a
# bare-bones host may only expose ``/dev/binder``).
_BINDER_DEVICE_NODES: Final = (
    Path("/dev/binder"),
    Path("/dev/hwbinder"),
    Path("/dev/vndbinder"),
)
_PROC_FILESYSTEMS: Final = Path("/proc/filesystems")
_PROC_CONFIG_GZ: Final = Path("/proc/config.gz")
_KERNEL_CONFIG_KEY: Final = "CONFIG_ANDROID_BINDER_IPC"

# Result of reading ``CONFIG_ANDROID_BINDER_IPC`` from the kernel config:
# ``"y"`` (built-in) / ``"m"`` (module) enable binder; ``"not-set"`` is
# the explicit ``# ... is not set`` comment; ``None`` means the config
# was unreadable or the key was absent entirely.
KernelBinderConfig = Literal["y", "m", "not-set"]

BinderState = Literal["ready", "loadable", "unsupported", "unknown"]


class BinderStatus(BaseModel):
    """
    The host's binder-driver capability, as probed by :func:`binder_status`.

    Attributes:
        state: One of ``ready`` / ``loadable`` / ``unsupported`` /
            ``unknown`` (see the module docstring for the full meaning
            of each).
        reason: One-line human-readable description of what was found.
        remedy: One-line actionable next step, or ``""`` when none
            applies (``ready``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    state: BinderState
    reason: str
    remedy: str

    @property
    def available(self) -> bool:
        """Return True iff redroid can boot on this host right now (``state == "ready"``)."""
        return self.state == "ready"


def _dev_binder_present() -> bool:
    """Return True iff any of the binder device nodes exists under ``/dev``."""
    return any(node.exists() for node in _BINDER_DEVICE_NODES)


def _binderfs_supported() -> bool:
    """Return True iff the kernel lists the ``binder`` filesystem in ``/proc/filesystems``."""
    try:
        text = _PROC_FILESYSTEMS.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[-1] == "binder":
            return True
    return False


def _boot_config_path() -> Path:
    """Return the on-disk kernel-config snapshot path (``/boot/config-<release>``)."""
    return Path(f"/boot/config-{platform.uname().release}")


def _kernel_config_lines() -> list[str]:
    """
    Return the host kernel's build config as a list of lines (empty if unreadable).

    Tries the gzipped in-memory config at ``/proc/config.gz`` first,
    then the on-disk ``/boot/config-<release>`` snapshot. Returns an
    empty list when neither is present (the common case on macOS /
    Windows and on locked-down container hosts).
    """
    try:
        return gzip.decompress(_PROC_CONFIG_GZ.read_bytes()).decode("utf-8", "replace").splitlines()
    except OSError:
        pass
    try:
        return _boot_config_path().read_text().splitlines()
    except OSError:
        return []


def _kernel_config_binder() -> KernelBinderConfig | None:
    """
    Report whether the host kernel was built with binder support.

    Returns:
        ``"y"`` / ``"m"`` if binder is built in / a module, ``"not-set"``
        for the explicit ``# CONFIG_ANDROID_BINDER_IPC is not set``
        comment, or ``None`` when the config couldn't be read or the key
        is absent.
    """
    not_set_comment = f"# {_KERNEL_CONFIG_KEY} is not set"
    for raw in _kernel_config_lines():
        line = raw.strip()
        if line.startswith(f"{_KERNEL_CONFIG_KEY}="):
            value = line.split("=", 1)[1].strip().strip('"')
            if value in ("y", "m"):
                return value  # type: ignore[return-value]  # narrowed by the membership test
        elif line == not_set_comment:
            return "not-set"
    return None


def _classify(
    *,
    dev_present: bool,
    binderfs: bool,
    kconfig: KernelBinderConfig | None,
) -> BinderStatus:
    """
    Fold the raw host probes into a :class:`BinderStatus`.

    Pure (no I/O) so every state is unit-testable without touching the
    real host. ``ready`` short-circuits on device nodes or binderfs; the
    remaining states are driven by the kernel config.

    Args:
        dev_present: Whether a ``/dev/binder*`` node exists.
        binderfs: Whether the kernel lists the ``binder`` filesystem.
        kconfig: The ``CONFIG_ANDROID_BINDER_IPC`` value (or ``None``).

    Returns:
        The classified :class:`BinderStatus`.
    """
    if dev_present or binderfs:
        return BinderStatus(
            state="ready",
            reason="binder is available (device nodes or binderfs present)",
            remedy="",
        )
    if kconfig in ("y", "m"):
        return BinderStatus(
            state="loadable",
            reason=(
                f"the kernel supports binder ({_KERNEL_CONFIG_KEY}={kconfig}) "
                "but it is not loaded — no /dev/binder, no binderfs"
            ),
            remedy=(
                "load it on the host with "
                "`sudo modprobe binder_linux devices=binder,hwbinder,vndbinder` "
                "(works on GitHub-hosted CI runners), then retry"
            ),
        )
    if kconfig == "not-set":
        return BinderStatus(
            state="unsupported",
            reason=(
                f"this kernel has binder compiled out ({_KERNEL_CONFIG_KEY} is not set) — "
                "redroid cannot boot here regardless of Docker privileges"
            ),
            remedy=(
                "use a host whose kernel provides binder, or adopt a remote rooted "
                "device with `beetroot adopt <serial>` (no kernel access needed)"
            ),
        )
    return BinderStatus(
        state="unknown",
        reason=(
            "binder is not present and the kernel config could not be read, "
            "so binder support can't be determined"
        ),
        remedy=(
            "if redroid fails to boot, ensure the host kernel provides binder, "
            "or adopt a remote rooted device with `beetroot adopt <serial>`"
        ),
    )


def binder_status() -> BinderStatus:
    """
    Probe the host and return its binder-driver capability.

    Combines the device-node, binderfs, and kernel-config probes into a
    single :class:`BinderStatus`. Cheap and side-effect-free — callers
    (``beetroot up`` preflight, ``beetroot doctor``) invoke it freely.

    Returns:
        The host's :class:`BinderStatus`.
    """
    return _classify(
        dev_present=_dev_binder_present(),
        binderfs=_binderfs_supported(),
        kconfig=_kernel_config_binder(),
    )
