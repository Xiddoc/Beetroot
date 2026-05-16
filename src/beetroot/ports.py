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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Ports

ADB_BASE = 5555
FRIDA_BASE = 27042
FRIDA2_BASE = 27043
STRIDE = 10


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
        index: The instance's port index (non-negative integer).

    Returns:
        A dict with keys ``adb``, ``frida``, and ``frida2`` mapping to
        the host port numbers for this instance.

    Raises:
        ValueError: If ``index`` is negative.
    """
    if index < 0:
        raise ValueError(f"port index must be >= 0 (got {index})")
    return {
        "adb": ADB_BASE + index * STRIDE,
        "frida": FRIDA_BASE + index * STRIDE,
        "frida2": FRIDA2_BASE + index * STRIDE,
    }


def resolve_ports(index: int, override: Ports) -> dict[str, int]:
    """
    Merge the stride-of-10 allocation for ``index`` with optional overrides.

    Each field on ``override`` is independently optional — a field set to
    ``None`` falls back to the stride allocation; a field set to an integer
    pins that port to the given value. ``override.frida_control`` maps to
    the ``frida2`` key so the result aligns with ``render_env``'s vocabulary.

    Args:
        index: The instance's port index (non-negative integer).
        override: The ``Ports`` override block from the instance's config.

    Returns:
        A dict with keys ``adb``, ``frida``, ``frida2`` — the resolved host
        port numbers after applying overrides on top of the stride defaults.

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
        "frida2": (
            override.frida_control
            if override.frida_control is not None
            else stride["frida2"]
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
    """Return the smallest non-negative integer not in ``used``. Reuses freed slots."""
    i = 0
    while i in used:
        i += 1
    return i
