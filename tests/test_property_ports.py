"""
Property-based invariants for the stride-of-10 port allocator.

For arbitrary ``(used_indices, allocation_count)`` inputs, asserts:

1. ``lowest_free_index`` never returns an index already in ``used``.
2. Successive allocations (each one adding the returned index to the
   "used" set) never collide with each other or with the original set.
3. ``ports_for_index(N)`` is always strictly aligned to ``N * STRIDE``
   offsets from each base port (ADB / Frida / Frida-control), and the
   three ports are pairwise distinct for any non-negative N.

The allocator is the load-bearing primitive for the "many phones on
one host" use case; if the property tests ever fail, the failure mode
is a silent port collision and the user sees it only at
``docker compose up`` time.

Hypothesis is pinned to derandomized settings so CI reproduces
exactly.
"""
from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from beetroot.ports import (
    _MAX_PORT_INDEX,
    ADB_BASE,
    FRIDA_BASE,
    FRIDA_CONTROL_BASE,
    STRIDE,
    lowest_free_index,
    ports_for_index,
)


@given(
    used=st.sets(st.integers(min_value=0, max_value=1024), max_size=64),
    allocation_count=st.integers(min_value=0, max_value=20),
)
@settings(deadline=None, derandomize=True, max_examples=200)
def test_successive_allocations_never_collide(
    used: set[int], allocation_count: int,
) -> None:
    """Each successive `lowest_free_index` call returns a previously-unused index."""
    working = set(used)
    for _ in range(allocation_count):
        idx = lowest_free_index(working)
        assert idx >= 0
        assert idx not in working
        working.add(idx)


@given(used=st.sets(st.integers(min_value=0, max_value=1024), max_size=64))
@settings(deadline=None, derandomize=True, max_examples=200)
def test_lowest_free_index_returns_smallest_gap(used: set[int]) -> None:
    """Allocator returns the literal smallest non-negative integer not in `used`."""
    idx = lowest_free_index(used)
    assert idx not in used
    # Every smaller non-negative integer must be in ``used`` — otherwise
    # there's a smaller gap the allocator missed.
    for i in range(idx):
        assert i in used, f"missed gap at {i} for used={used}"


@given(index=st.integers(min_value=0, max_value=_MAX_PORT_INDEX))
@settings(deadline=None, derandomize=True, max_examples=200)
def test_ports_for_index_is_stride_aligned(index: int) -> None:
    """Every returned port is exactly `<base> + index*STRIDE` and pairwise distinct."""
    p = ports_for_index(index)
    assert p["adb"] == ADB_BASE + index * STRIDE
    assert p["frida"] == FRIDA_BASE + index * STRIDE
    assert p["frida_control"] == FRIDA_CONTROL_BASE + index * STRIDE
    # The three ports differ by constant offsets (0, 27042-5555, etc.),
    # so they're necessarily pairwise-distinct for any N >= 0.
    assert len({p["adb"], p["frida"], p["frida_control"]}) == 3
    # D5: no port exceeds 65535 within the valid index range.
    assert p["adb"] <= 65535
