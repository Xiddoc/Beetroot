"""Tests for ports.py — stride-10 port allocation."""
from __future__ import annotations

import pytest

from beetroot.config import Ports
from beetroot.ports import (
    ADB_BASE,
    FRIDA_CONTROL_BASE,
    FRIDA_BASE,
    STRIDE,
    _MAX_PORT_INDEX,
    PortCollisionError,
    lowest_free_index,
    ports_for_index,
    resolve_ports,
)


class TestPortsForIndex:
    def test_index_zero_is_base_ports(self) -> None:
        p = ports_for_index(0)
        assert p["adb"] == ADB_BASE
        assert p["frida"] == FRIDA_BASE
        assert p["frida_control"] == FRIDA_CONTROL_BASE

    def test_index_n_applies_stride(self) -> None:
        n = 5
        p = ports_for_index(n)
        assert p["adb"] == ADB_BASE + n * STRIDE
        assert p["frida"] == FRIDA_BASE + n * STRIDE
        assert p["frida_control"] == FRIDA_CONTROL_BASE + n * STRIDE

    def test_index_one(self) -> None:
        p = ports_for_index(1)
        assert p["adb"] == ADB_BASE + STRIDE
        assert p["frida"] == FRIDA_BASE + STRIDE
        assert p["frida_control"] == FRIDA_CONTROL_BASE + STRIDE

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="port index must be >= 0"):
            ports_for_index(-1)

    def test_negative_large_raises(self) -> None:
        with pytest.raises(ValueError, match="port index must be >= 0"):
            ports_for_index(-100)

    def test_returns_all_three_keys(self) -> None:
        p = ports_for_index(0)
        assert set(p.keys()) == {"adb", "frida", "frida_control"}

    def test_frida_control_is_one_above_frida(self) -> None:
        for i in (0, 3, 9):
            p = ports_for_index(i)
            assert p["frida_control"] == p["frida"] + 1

    def test_index_at_max_boundary_accepted(self) -> None:
        # D5: index == _MAX_PORT_INDEX must succeed (just touches the limit).
        p = ports_for_index(_MAX_PORT_INDEX)
        assert p["adb"] <= 65535

    def test_index_above_max_raises(self) -> None:
        # D5: an index that would push ADB above 65535 must raise early.
        with pytest.raises(ValueError, match="65535"):
            ports_for_index(_MAX_PORT_INDEX + 1)

    def test_index_far_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match="65535"):
            ports_for_index(_MAX_PORT_INDEX + 100)


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


class TestResolvePorts:
    def test_no_overrides_returns_stride_defaults(self) -> None:
        assert resolve_ports(0, Ports()) == {"adb": 5555, "frida": 27042, "frida_control": 27043}

    def test_no_overrides_index_one(self) -> None:
        assert resolve_ports(1, Ports()) == {"adb": 5565, "frida": 27052, "frida_control": 27053}

    def test_no_overrides_index_five(self) -> None:
        assert resolve_ports(5, Ports()) == {"adb": 5605, "frida": 27092, "frida_control": 27093}

    def test_adb_only_override(self) -> None:
        assert resolve_ports(0, Ports(adb=8080)) == {
            "adb": 8080,
            "frida": 27042,
            "frida_control": 27043,
        }

    def test_frida_only_override(self) -> None:
        assert resolve_ports(0, Ports(frida=9000)) == {
            "adb": 5555,
            "frida": 9000,
            "frida_control": 27043,
        }

    def test_frida_control_only_override(self) -> None:
        assert resolve_ports(0, Ports(frida_control=9001)) == {
            "adb": 5555,
            "frida": 27042,
            "frida_control": 9001,
        }

    def test_all_three_overrides(self) -> None:
        assert resolve_ports(0, Ports(adb=1, frida=2, frida_control=3)) == {
            "adb": 1,
            "frida": 2,
            "frida_control": 3,
        }

    def test_partial_adb_and_frida(self) -> None:
        assert resolve_ports(2, Ports(adb=8080, frida=9000)) == {
            "adb": 8080,
            "frida": 9000,
            "frida_control": 27063,
        }

    def test_partial_adb_and_frida_control(self) -> None:
        assert resolve_ports(2, Ports(adb=8080, frida_control=9001)) == {
            "adb": 8080,
            "frida": 27062,
            "frida_control": 9001,
        }

    def test_partial_frida_and_frida_control(self) -> None:
        assert resolve_ports(2, Ports(frida=9000, frida_control=9001)) == {
            "adb": 5575,
            "frida": 9000,
            "frida_control": 9001,
        }

    def test_override_can_match_stride_default(self) -> None:
        # Pinning to the same number the stride would produce is a no-op semantically.
        assert resolve_ports(0, Ports(adb=5555)) == resolve_ports(0, Ports())

    def test_negative_index_still_raises(self) -> None:
        with pytest.raises(ValueError, match="port index must be >= 0"):
            resolve_ports(-1, Ports())


class TestResolvePortsSelfCollision:
    def test_resolve_collides_when_frida_override_matches_default_frida_control(self) -> None:
        # Index 0 stride: frida=27042, frida_control=27043. Override frida=27043 →
        # resolved frida and frida_control both 27043.
        with pytest.raises(PortCollisionError, match="27043"):
            resolve_ports(0, Ports(frida=27043))

    def test_resolve_collides_when_frida_control_override_matches_default_frida(self) -> None:
        # Index 0 stride: frida=27042, frida_control=27043. Override frida_control=27042 →
        # resolved frida and frida_control both 27042.
        with pytest.raises(PortCollisionError, match="27042"):
            resolve_ports(0, Ports(frida_control=27042))

    def test_resolve_collides_when_adb_override_matches_default_frida(self) -> None:
        # Index 0 stride: adb=5555, frida=27042. Override adb=27042 →
        # resolved adb and frida both 27042.
        with pytest.raises(PortCollisionError, match="27042"):
            resolve_ports(0, Ports(adb=27042))

    def test_resolve_no_collision_with_partial_override(self) -> None:
        # Pinning frida to a clearly non-stride value leaves frida_control on 27043.
        assert resolve_ports(0, Ports(frida=29000)) == {
            "adb": 5555,
            "frida": 29000,
            "frida_control": 27043,
        }

    def test_resolve_error_message_names_colliding_ports(self) -> None:
        with pytest.raises(PortCollisionError) as exc_info:
            resolve_ports(0, Ports(frida=27043))
        msg = str(exc_info.value)
        assert "27043" in msg
        assert "frida" in msg
        assert "frida_control" in msg
        assert "beetroot.yaml" in msg

    def test_resolve_collision_at_nonzero_index(self) -> None:
        # Index 2 stride: frida=27062, frida_control=27063. Override frida=27063 → collide.
        with pytest.raises(PortCollisionError, match="27063"):
            resolve_ports(2, Ports(frida=27063))

    def test_resolve_collision_is_valueerror_subclass(self) -> None:
        # PortCollisionError must remain a ValueError so naive callers still catch it.
        assert issubclass(PortCollisionError, ValueError)
