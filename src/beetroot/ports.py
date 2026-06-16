"""
Port allocation for instances.

Stride-of-10 scheme: instance index N maps to ADB port 5555 + N*10 and
Frida ports 27042/27043 + N*10. Indices are stable across the lifetime
of an instance and freed on destroy. Freed slots are reused — allocation
always picks the lowest free index.

Per-instance overrides are supported via the ``ports:`` block in
``beetroot.yaml`` — see :func:`resolve_ports` and the ``Ports`` model in
``config.py``.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .config import Ports

ADB_BASE = 5555
FRIDA_BASE = 27042
FRIDA_CONTROL_BASE = 27043
STRIDE = 10

# Maximum port index before stride-of-10 would push any port above 65535.
# FRIDA_CONTROL_BASE (27043) is the binding constraint — it is the highest of
# the three base ports. At index 3849 frida_control reaches 65533, still valid.
# Index 3850 would push frida_control to 65543 — an invalid TCP port. ADB at
# the same index would be only 44045, well within range, but frida_control
# determines the cap: floor((65535 - 27043) / 10) = 3849.
_MAX_PORT_INDEX: Final = (65535 - FRIDA_CONTROL_BASE) // STRIDE


class PortCollisionError(ValueError):
    """
    Raised when :func:`resolve_ports` produces a dict with duplicate values.

    This happens when a partial ``ports:`` override pins one slot to the
    stride-of-10 default of a sibling slot that wasn't overridden — e.g.
    ``Ports(frida=27043)`` at index 0 leaves ``frida_control`` on the
    stride default of ``27043``, colliding with the override. ``Ports``
    itself only validates distinctness among the *non-None* fields it
    receives, so this resolver-side check catches the case the model
    can't see without knowing the index.
    """


def ports_for_index(index: int) -> dict[str, int]:
    """
    Compute the ADB and Frida port numbers for a given instance index.

    Args:
        index: The instance's port index (non-negative integer, at most
            :data:`_MAX_PORT_INDEX`).

    Returns:
        A dict with keys ``adb``, ``frida``, and ``frida_control`` mapping
        to the host port numbers for this instance.

    Raises:
        ValueError: If ``index`` is negative or would push frida_control above
            port 65535 (frida_control has the highest base port and is the
            binding constraint on the maximum index).
    """
    if index < 0:
        raise ValueError(f"port index must be >= 0 (got {index})")
    if index > _MAX_PORT_INDEX:
        raise ValueError(
            f"port index {index} would assign frida_control port "
            f"{FRIDA_CONTROL_BASE + index * STRIDE} > 65535 "
            f"(frida_control is the highest base and bounds the maximum index). "
            f"Maximum supported index is {_MAX_PORT_INDEX}."
        )
    return {
        "adb": ADB_BASE + index * STRIDE,
        "frida": FRIDA_BASE + index * STRIDE,
        "frida_control": FRIDA_CONTROL_BASE + index * STRIDE,
    }


def resolve_ports(index: int, override: Ports) -> dict[str, int]:
    """
    Merge the stride-of-10 allocation for ``index`` with optional overrides.

    Each field on ``override`` is independently optional — a field set to
    ``None`` falls back to the stride allocation; a field set to an integer
    pins that port to the given value.

    Args:
        index: The instance's port index (non-negative integer).
        override: The ``Ports`` override block from the instance's config.

    Returns:
        A dict with keys ``adb``, ``frida``, ``frida_control`` — the resolved
        host port numbers after applying overrides on top of the stride
        defaults.

    Raises:
        PortCollisionError: If the resolved dict has duplicate port values.
            This happens when a partial override pins one field to the
            stride-of-10 default of a different field that wasn't
            overridden — the pydantic ``Ports`` validator can't catch this
            because it doesn't know the index.
    """
    stride = ports_for_index(index)
    resolved = {
        "adb": override.adb if override.adb is not None else stride["adb"],
        "frida": override.frida if override.frida is not None else stride["frida"],
        "frida_control": (
            override.frida_control
            if override.frida_control is not None
            else stride["frida_control"]
        ),
    }
    counts = Counter(resolved.values())
    dupes = {
        port: sorted(k for k, v in resolved.items() if v == port)
        for port, n in counts.items()
        if n > 1
    }
    if dupes:
        raise PortCollisionError(
            f"resolved ports collide on this instance: {dupes}. "
            "Override ports.adb / ports.frida / ports.frida_control in "
            "beetroot.yaml to avoid colliding with stride-of-10 defaults."
        )
    return resolved


def lowest_free_index(used: set[int]) -> int:
    """
    Return the smallest non-negative integer not in ``used``. Reuses freed slots.
    """
    i = 0
    while i in used:
        i += 1
    return i
