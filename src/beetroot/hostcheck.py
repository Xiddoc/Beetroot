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


# The configured binder strategy from ``InstanceConfig.binder`` (kept here,
# next to the probes it consumes, so :func:`plan_binder_runtime` and the
# config schema share one vocabulary).
BinderMode = Literal["auto", "host", "vm"]

# What ``beetroot up`` should do once the configured mode is folded against
# the live host probe:
#
# * ``proceed`` — start the container; the host can satisfy binder.
# * ``warn``    — start anyway, but print a one-line advisory (the host
#                 can't satisfy binder and the mode is the lenient
#                 ``auto``; the container starts but Android may not boot).
# * ``block``   — refuse to start (strict ``host`` mode on a host that
#                 can't provide binder).
# * ``vm``      — the user opted into the emulated micro-VM path.
BinderAction = Literal["proceed", "warn", "block", "vm"]

# Appended to the ``auto``-mode advisory so a stuck user discovers the
# explicit escape hatch. Phrased as a suggestion, not a default — the slow
# emulated path is never engaged without the user asking for it.
_VM_HINT: Final = (
    "Or set `binder: vm` in beetroot.yaml to run redroid inside an "
    "emulated micro-VM that ships its own binder kernel (slower; no host "
    "binder required)"
)

# Selecting ``binder: vm`` before the micro-VM engine is wired into the
# CLI surfaces this, rather than silently doing nothing. The validated
# recipe + roadmap live in the design doc.
_VM_NOT_WIRED: Final = (
    "binder: vm selected, but the emulated micro-VM backend is not yet "
    "wired into the CLI — it is the tracked optimization sprint. A "
    "proof-of-concept already boots redroid this way; see "
    "docs/design/binderless-hosts-qemu-tcg.md. For now use `binder: host` "
    "(or `binder: auto`) on a host that provides the kernel binder driver"
)


class BinderPlan(BaseModel):
    """
    The decision for ``beetroot up`` given a configured mode + host probe.

    Produced by :func:`plan_binder_runtime` — a pure fold of the
    :class:`BinderStatus` against the instance's configured
    :data:`BinderMode`. Kept separate from :class:`BinderStatus` (which
    only describes the *host*) so the policy (what to do about it) is
    unit-testable without a Typer runner.

    Attributes:
        action: What ``up`` should do — see :data:`BinderAction`.
        reason: One-line human-readable explanation for the action.
        remedy: One-line actionable next step, or ``""`` when none
            applies (``proceed``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: BinderAction
    reason: str
    remedy: str


def plan_binder_runtime(mode: BinderMode, status: BinderStatus) -> BinderPlan:
    """
    Fold a configured binder mode against the live host probe into a plan.

    Pure (no I/O) so every branch is unit-testable. Implements the
    "auto-use the cheap/correct path, gate the expensive path behind an
    explicit opt-in" policy:

    * ``vm`` always returns ``action="vm"`` (the user asked for the
      emulated path explicitly — the caller decides whether the engine
      is available yet).
    * a binder-ready host always returns ``proceed`` for ``auto`` /
      ``host``.
    * ``host`` on a non-ready host returns ``block`` (fail fast).
    * ``auto`` on a non-ready host returns ``warn`` (start anyway, advise)
      and appends the ``binder: vm`` hint to the remedy so the user
      discovers the escape hatch — but it is **never** auto-engaged.

    Args:
        mode: The instance's configured :data:`BinderMode`.
        status: The live host :class:`BinderStatus`.

    Returns:
        The :class:`BinderPlan` describing what ``up`` should do.
    """
    if mode == "vm":
        return BinderPlan(action="vm", reason=_VM_NOT_WIRED, remedy="")
    if status.available:
        return BinderPlan(action="proceed", reason=status.reason, remedy="")
    if mode == "host":
        return BinderPlan(action="block", reason=status.reason, remedy=status.remedy)
    remedy = f"{status.remedy}. {_VM_HINT}" if status.remedy else _VM_HINT
    return BinderPlan(action="warn", reason=status.reason, remedy=remedy)
