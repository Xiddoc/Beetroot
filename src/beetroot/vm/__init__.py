"""
QEMU micro-VM engine for the ``binder: vm`` backend.

This package houses the pieces the emulated micro-VM backend needs to
boot redroid on a host with no kernel binder driver: accelerator
detection (KVM vs TCG), the QEMU ``argv`` builder, and the process
manager that launches / terminates ``qemu-system-x86_64`` and persists a
pidfile in the instance directory.

The validated proof-of-concept this engine implements lives at
``docs/design/binderless-hosts-qemu-tcg.md``; the high-value performance
levers (MTTCG, ``-cpu max``, KVM fast path) are encoded in
:func:`beetroot.vm.qemu.build_qemu_argv`.
"""

from __future__ import annotations

from .qemu import (
    QemuLaunchError,
    QemuProcess,
    build_qemu_argv,
    detect_accel,
)

__all__ = [
    "QemuLaunchError",
    "QemuProcess",
    "build_qemu_argv",
    "detect_accel",
]
