"""
Beetroot — multi-instance Magisk-Android research lab.

This top-level package re-exports the high-level OOP surface so research
scripts can write ``from beetroot import Instance`` instead of reaching
into ``beetroot.api`` (or, worse, the procedural modules). The
procedural modules (:mod:`beetroot.compose`, :mod:`beetroot.config`,
:mod:`beetroot.frida_download`, :mod:`beetroot.modules_download`,
:mod:`beetroot.paths`, :mod:`beetroot.ports`, :mod:`beetroot.registry`,
:mod:`beetroot.snapshot`, :mod:`beetroot.builder`) are still part of
the public surface — :class:`Instance` composes them, doesn't replace
them.
"""

from __future__ import annotations

from .api import (
    AdbNotInstalledError,
    BackendCapabilityError,
    DeviceBackend,
    DevicePreflightError,
    FridaNotInstalledError,
    Instance,
    InstanceNotFoundError,
    Manager,
)
from .backends import register_backend

__all__ = [
    "AdbNotInstalledError",
    "BackendCapabilityError",
    "DeviceBackend",
    "DevicePreflightError",
    "FridaNotInstalledError",
    "Instance",
    "InstanceNotFoundError",
    "Manager",
    "register_backend",
]
