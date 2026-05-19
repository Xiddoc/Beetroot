"""
Beetroot — multi-instance Magisk-Android research lab.

This top-level package re-exports the high-level OOP surface so research
scripts can write ``from beetroot import Instance`` instead of reaching
into ``beetroot.api`` (or, worse, the procedural modules). The
procedural modules (:mod:`beetroot.compose`, :mod:`beetroot.config`,
:mod:`beetroot.frida_dl`, :mod:`beetroot.modules_dl`,
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
    "FridaNotInstalledError",
    "Instance",
    "InstanceNotFoundError",
    "Manager",
    "register_backend",
]
