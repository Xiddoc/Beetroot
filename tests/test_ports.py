"""Tests for ports.py — stride-10 port allocation."""
from __future__ import annotations

import pytest

from beetroot.ports import (
    ADB_BASE,
    FRIDA2_BASE,
    FRIDA_BASE,
    STRIDE,
    lowest_free_index,
    ports_for_index,
)


class TestPortsForIndex:
    def test_index_zero_is_base_ports(self) -> None:
        p = ports_for_index(0)
        assert p["adb"] == ADB_BASE
        assert p["frida"] == FRIDA_BASE
        assert p["frida2"] == FRIDA2_BASE

    def test_index_n_applies_stride(self) -> None:
        n = 5
        p = ports_for_index(n)
        assert p["adb"] == ADB_BASE + n * STRIDE
        assert p["frida"] == FRIDA_BASE + n * STRIDE
        assert p["frida2"] == FRIDA2_BASE + n * STRIDE

    def test_index_one(self) -> None:
        p = ports_for_index(1)
        assert p["adb"] == ADB_BASE + STRIDE
        assert p["frida"] == FRIDA_BASE + STRIDE
        assert p["frida2"] == FRIDA2_BASE + STRIDE

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="port index must be >= 0"):
            ports_for_index(-1)

    def test_negative_large_raises(self) -> None:
        with pytest.raises(ValueError, match="port index must be >= 0"):
            ports_for_index(-100)

    def test_returns_all_three_keys(self) -> None:
        p = ports_for_index(0)
        assert set(p.keys()) == {"adb", "frida", "frida2"}

    def test_frida2_is_one_above_frida(self) -> None:
        for i in (0, 3, 9):
            p = ports_for_index(i)
            assert p["frida2"] == p["frida"] + 1


class TestLowestFreeIndex:
    def test_empty_set_returns_zero(self) -> None:
        assert lowest_free_index(set()) == 0

    def test_contiguous_from_zero_returns_next(self) -> None:
        assert lowest_free_index({0, 1, 2}) == 3

    def test_gap_at_zero_is_reused(self) -> None:
        assert lowest_free_index({1, 2, 3}) == 0

    def test_gap_in_middle_is_reused(self) -> None:
        assert lowest_free_index({0, 1, 3, 4}) == 2

    def test_single_element_zero(self) -> None:
        assert lowest_free_index({0}) == 1

    def test_single_element_nonzero(self) -> None:
        assert lowest_free_index({5}) == 0

    def test_result_never_in_used(self) -> None:
        used = {0, 2, 4, 6}
        result = lowest_free_index(used)
        assert result not in used
