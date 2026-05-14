"""
Port allocation for instances.

Stride-of-10 scheme: instance index N maps to ADB port 5555 + N*10 and
Frida ports 27042/27043 + N*10. Indices are stable across the lifetime
of an instance and freed on destroy. Freed slots are reused — allocation
always picks the lowest free index.
"""
from __future__ import annotations

ADB_BASE = 5555
FRIDA_BASE = 27042
FRIDA2_BASE = 27043
STRIDE = 10


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


def lowest_free_index(used: set[int]) -> int:
    """Return the smallest non-negative integer not in ``used``. Reuses freed slots."""
    i = 0
    while i in used:
        i += 1
    return i
