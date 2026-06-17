"""
Host capability survey — which Beetroot run-modes work on *this* host.

Beetroot can drive a device in several distinct ways, and which ones actually
work depends entirely on the host's kernel and installed tooling. Getting this
wrong is an easy and expensive mistake (mistaking "no ``/dev/kvm``" for "no VM
support", when the ``binder: vm`` TCG path is built precisely for KVM-less
hosts). This module turns that judgement into a probed, structured answer so
``beetroot modes`` can print a definitive matrix instead of a guess.

The two axes (see ``docs/how-it-works/binder-and-modes.md`` for the full
explanation):

* **Device backend** — ``redroid`` (boot a Dockerized redroid container that
  Beetroot manages) or ``adb`` (drive an *external* rooted device; Beetroot
  boots nothing).
* **The ``binder`` switch** (redroid only) — ``host``/``auto`` (use the host
  kernel's binder driver) or ``vm`` (boot redroid inside a QEMU micro-VM that
  ships its own binder kernel, with a **KVM** fast path or a **TCG** software
  fallback).

The classification is split into a pure :func:`classify_modes` (no I/O — every
branch unit-testable) and a thin :func:`survey` that gathers the live probes
and delegates to it.
"""

from __future__ import annotations

import shutil

from pydantic import BaseModel, ConfigDict

from . import hostcheck
from .settings import Settings
from .vm import qemu

# Per-mode verdict (the ``status`` field of :class:`ModeSupport`):
#
# * ``supported``   — works on this host as-is (modulo runtime inputs like a
#                     reachable device or built guest artifacts, noted in the
#                     reason/remedy).
# * ``needs-setup`` — the host *can* do this, but a one-time step is missing
#                     (load a module, install a package).
# * ``unsupported`` — cannot work on this host; no setup step would help.
# * ``unknown``     — couldn't determine (e.g. kernel config unreadable).

# The canonical mode identifiers, so callers/tests refer to stable strings.
MODE_REDROID_HOST = "redroid (binder: host / auto)"
MODE_VM_KVM = "redroid (binder: vm, KVM accel)"
MODE_VM_TCG = "redroid (binder: vm, TCG accel)"
MODE_ADB = "adb backend (adopt remote device)"

# Reused remedy/reason fragments (kept short so lines stay under the limit).
_QEMU_INSTALL = (
    "install QEMU (e.g. `apt-get install qemu-system-x86`), then `beetroot build --vm-kernel`"
)
_BUILD_HINT = "build artifacts with `beetroot build --vm-kernel`"
# The adb backend boots nothing of its own — it always needs a separate rooted
# device/emulator to drive. That's easy to miss, so it's surfaced in DETAIL in
# both the installed and not-yet-installed states.
_ADB_NEEDS_DEVICE = "needs an external rooted device/emulator to adopt"


class ModeSupport(BaseModel):
    """
    Whether one Beetroot run-mode works on this host.

    Attributes:
        mode: The mode identifier (one of the ``MODE_*`` constants).
        status: ``supported`` / ``needs-setup`` / ``unsupported`` / ``unknown``.
        reason: One-line description of what was found.
        remedy: One-line actionable next step, or ``""`` when none applies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: str
    status: str
    reason: str
    remedy: str


def _redroid_host(binder: hostcheck.BinderStatus, *, docker: bool) -> ModeSupport:
    """
    Classify the ``redroid`` backend on the host-binder path.

    Args:
        binder: The probed host binder capability.
        docker: Whether the Docker CLI is on ``PATH``.

    Returns:
        The :class:`ModeSupport` verdict for ``binder: host`` / ``auto``.
    """
    if binder.state == "ready":
        if docker:
            return ModeSupport(
                mode=MODE_REDROID_HOST, status="supported", reason=binder.reason, remedy=""
            )
        return ModeSupport(
            mode=MODE_REDROID_HOST,
            status="needs-setup",
            reason="host binder is ready, but the Docker CLI was not found",
            remedy="install Docker and ensure the daemon is running",
        )
    if binder.state == "loadable":
        return ModeSupport(
            mode=MODE_REDROID_HOST, status="needs-setup", reason=binder.reason, remedy=binder.remedy
        )
    if binder.state == "unsupported":
        return ModeSupport(
            mode=MODE_REDROID_HOST, status="unsupported", reason=binder.reason, remedy=binder.remedy
        )
    return ModeSupport(
        mode=MODE_REDROID_HOST, status="unknown", reason=binder.reason, remedy=binder.remedy
    )


def _vm_kvm(*, kvm: bool, qemu_present: bool) -> ModeSupport:
    """
    Classify the ``binder: vm`` KVM (near-native) fast path.

    Args:
        kvm: Whether a usable ``/dev/kvm`` is present.
        qemu_present: Whether the QEMU system emulator is on ``PATH``.

    Returns:
        The :class:`ModeSupport` verdict for ``binder: vm`` under KVM.
    """
    if not kvm:
        return ModeSupport(
            mode=MODE_VM_KVM,
            status="unsupported",
            reason="no usable /dev/kvm — hardware acceleration is unavailable here",
            remedy="use the TCG path (slower, no KVM needed), or a host/runner exposing /dev/kvm",
        )
    if not qemu_present:
        return ModeSupport(
            mode=MODE_VM_KVM,
            status="needs-setup",
            reason="/dev/kvm is usable but the QEMU system emulator was not found",
            remedy=_QEMU_INSTALL,
        )
    return ModeSupport(
        mode=MODE_VM_KVM,
        status="supported",
        reason=f"KVM available and QEMU installed (near-native); {_BUILD_HINT}",
        remedy="",
    )


def _vm_tcg(*, qemu_present: bool) -> ModeSupport:
    """
    Classify the ``binder: vm`` TCG (software-emulation) fallback path.

    This is the path for hosts with neither host binder nor KVM — it needs only
    QEMU and the built guest artifacts.

    Args:
        qemu_present: Whether the QEMU system emulator is on ``PATH``.

    Returns:
        The :class:`ModeSupport` verdict for ``binder: vm`` under TCG.
    """
    if not qemu_present:
        return ModeSupport(
            mode=MODE_VM_TCG,
            status="needs-setup",
            reason="the QEMU system emulator was not found",
            remedy=_QEMU_INSTALL,
        )
    return ModeSupport(
        mode=MODE_VM_TCG,
        status="supported",
        reason=f"QEMU installed; software emulation (~5-20x slower, no KVM needed) — {_BUILD_HINT}",
        remedy="",
    )


def _adb_adopt(*, adb_present: bool) -> ModeSupport:
    """
    Classify the ``adb`` backend (adopt an external rooted device).

    This mode needs no host kernel, binder, or Docker — only the ``adb`` client
    and a reachable device.

    Args:
        adb_present: Whether the ``adb`` client is on ``PATH``.

    Returns:
        The :class:`ModeSupport` verdict for the adb backend.
    """
    if not adb_present:
        return ModeSupport(
            mode=MODE_ADB,
            status="needs-setup",
            reason="the adb client was not found",
            remedy=(
                "install platform-tools (e.g. `apt-get install android-tools-adb`); "
                f"{_ADB_NEEDS_DEVICE}"
            ),
        )
    return ModeSupport(
        mode=MODE_ADB,
        status="supported",
        reason="drives an external rooted device over adb; needs no host kernel/binder/Docker",
        remedy=f"{_ADB_NEEDS_DEVICE} — point it at one: `beetroot adopt <serial|host:port>`",
    )


def classify_modes(
    *,
    binder: hostcheck.BinderStatus,
    kvm: bool,
    qemu_present: bool,
    docker: bool,
    adb_present: bool,
) -> list[ModeSupport]:
    """
    Fold the raw host probes into the per-mode support matrix.

    Pure (no I/O) so every mode/state combination is unit-testable without
    touching the real host.

    Args:
        binder: The probed host binder capability.
        kvm: Whether a usable ``/dev/kvm`` is present.
        qemu_present: Whether the QEMU system emulator is on ``PATH``.
        docker: Whether the Docker CLI is on ``PATH``.
        adb_present: Whether the ``adb`` client is on ``PATH``.

    Returns:
        One :class:`ModeSupport` per mode, in a stable display order.
    """
    return [
        _redroid_host(binder, docker=docker),
        _vm_kvm(kvm=kvm, qemu_present=qemu_present),
        _vm_tcg(qemu_present=qemu_present),
        _adb_adopt(adb_present=adb_present),
    ]


def survey(settings: Settings | None = None) -> list[ModeSupport]:
    """
    Probe the host and return the support matrix for every Beetroot run-mode.

    Gathers the live probes — host binder (:func:`hostcheck.binder_status`),
    KVM (via :func:`qemu.detect_accel`), and the presence of the QEMU / Docker
    / adb binaries — and delegates the verdict to :func:`classify_modes`.

    Args:
        settings: Injected settings (for the configured binary names).
            Defaults to a fresh :class:`Settings`.

    Returns:
        The per-mode support matrix.
    """
    cfg = settings if settings is not None else Settings()
    return classify_modes(
        binder=hostcheck.binder_status(),
        # ``detect_accel("auto")`` returns "kvm" only when /dev/kvm is usable.
        kvm=qemu.detect_accel("auto") == "kvm",
        qemu_present=shutil.which(cfg.qemu_bin) is not None,
        docker=shutil.which(cfg.docker_bin) is not None,
        adb_present=shutil.which("adb") is not None,
    )
