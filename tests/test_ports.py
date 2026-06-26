"""Tests for ports.py — stride-10 allocation + named guest→host mappings."""

from __future__ import annotations

import pytest

from beetroot.config import PortMapping, _default_port_mappings
from beetroot.ports import (
    _MAX_PORT_INDEX,
    ADB_BASE,
    EXTRA_POOL_BASE,
    FRIDA_BASE,
    FRIDA_CONTROL_BASE,
    STRIDE,
    PortCollisionError,
    ResolvedPort,
    lowest_free_index,
    ports_for_index,
    resolve_ports,
    well_known,
)


def _seed() -> list[PortMapping]:
    """Return the default seeded well-known mappings (host unset)."""
    return _default_port_mappings()


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

    def test_max_port_index_value(self) -> None:
        # The cap is now bound by the extra-pool base (40000), not the
        # well-known bands: the bands must stay strictly below 40000, so
        # (40000 - 1 - 27043) // 10 = 1295, tighter than the "extra pool <=
        # 65535" constraint (2552).
        assert _MAX_PORT_INDEX == 1295

    def test_index_at_max_boundary_accepted(self) -> None:
        p = ports_for_index(_MAX_PORT_INDEX)
        assert p["adb"] <= 65535
        assert p["frida"] <= 65535
        assert p["frida_control"] <= 65535
        # The whole well-known band at the cap stays strictly below the extra
        # pool, so it can never falsely collide with another instance's extra
        # entries.
        assert max(p.values()) < EXTRA_POOL_BASE

    def test_max_index_extra_pool_window_stays_in_range(self) -> None:
        # Boundary: at the max index, a full per-instance window of arbitrary
        # entries (slots 0..STRIDE-1) must still resolve <= 65535. The (STRIDE)th
        # slot would itself be rejected by the per-instance bound, so the highest
        # *valid* extra host is base + STRIDE-1.
        entries = [PortMapping(service="adb", guest=5555)]
        entries += [PortMapping(guest=6000 + s) for s in range(STRIDE)]
        resolved = resolve_ports(_MAX_PORT_INDEX, entries)
        assert max(rp.host for rp in resolved) <= 65535
        assert max(rp.host for rp in resolved) == EXTRA_POOL_BASE + _MAX_PORT_INDEX * STRIDE + (
            STRIDE - 1
        )

    def test_index_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match="extra-pool"):
            ports_for_index(_MAX_PORT_INDEX + 1)

    def test_index_far_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match="extra-pool"):
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


def _host_for(resolved: list[ResolvedPort], service: str) -> int:
    return next(rp.host for rp in resolved if rp.service == service)


class TestResolvePortsWellKnown:
    def test_no_overrides_returns_stride_defaults(self) -> None:
        r = resolve_ports(0, _seed())
        assert well_known(r) == {"adb": 5555, "frida": 27042, "frida_control": 27043}

    def test_no_overrides_index_one(self) -> None:
        r = resolve_ports(1, _seed())
        assert well_known(r) == {"adb": 5565, "frida": 27052, "frida_control": 27053}

    def test_no_overrides_index_five(self) -> None:
        r = resolve_ports(5, _seed())
        assert well_known(r) == {"adb": 5605, "frida": 27092, "frida_control": 27093}

    def test_explicit_host_wins_for_well_known(self) -> None:
        seed = [
            PortMapping(service="adb", guest=5555, host=8080),
            PortMapping(service="frida", guest=27042),
            PortMapping(service="frida_control", guest=27043),
        ]
        r = resolve_ports(0, seed)
        assert well_known(r) == {"adb": 8080, "frida": 27042, "frida_control": 27043}

    def test_resolved_preserves_order_and_guest(self) -> None:
        r = resolve_ports(2, _seed())
        assert [rp.service for rp in r] == ["adb", "frida", "frida_control"]
        assert [rp.guest for rp in r] == [5555, 27042, 27043]

    def test_returns_resolvedport_namedtuples(self) -> None:
        r = resolve_ports(0, _seed())
        assert all(isinstance(rp, ResolvedPort) for rp in r)
        assert r[0].service == "adb"
        assert r[0].guest == 5555
        assert r[0].host == 5555

    def test_negative_index_still_raises(self) -> None:
        with pytest.raises(ValueError, match="port index must be >= 0"):
            resolve_ports(-1, _seed())


class TestResolvePortsArbitrary:
    def test_arbitrary_explicit_host_used_verbatim(self) -> None:
        r = resolve_ports(0, [PortMapping(guest=8080, host=9000)])
        assert r == [ResolvedPort(service=None, guest=8080, host=9000)]

    def test_arbitrary_auto_allocates_from_extra_pool(self) -> None:
        r = resolve_ports(0, [PortMapping(guest=8081)])
        assert r == [ResolvedPort(service=None, guest=8081, host=EXTRA_POOL_BASE)]

    def test_extra_pool_base_value(self) -> None:
        # Pin the exact base so the crux artifact ("40000:8081") is locked.
        assert EXTRA_POOL_BASE == 40000

    def test_arbitrary_auto_allocates_by_slot(self) -> None:
        r = resolve_ports(0, [PortMapping(guest=8081), PortMapping(guest=8082)])
        assert [rp.host for rp in r] == [EXTRA_POOL_BASE, EXTRA_POOL_BASE + 1]

    def test_arbitrary_auto_allocates_per_index(self) -> None:
        r = resolve_ports(3, [PortMapping(guest=8081)])
        assert r[0].host == EXTRA_POOL_BASE + 3 * STRIDE

    def test_explicit_host_does_not_consume_a_slot(self) -> None:
        # An explicit-host arbitrary entry must not advance the auto slot.
        r = resolve_ports(
            0,
            [
                PortMapping(guest=8080, host=9000),
                PortMapping(guest=8081),
            ],
        )
        assert r[0].host == 9000
        assert r[1].host == EXTRA_POOL_BASE

    def test_labelled_non_well_known_uses_extra_pool(self) -> None:
        r = resolve_ports(0, [PortMapping(service="metrics", guest=9100)])
        assert r[0].host == EXTRA_POOL_BASE
        # A non-well-known label is excluded from well_known().
        assert well_known(r) == {}

    def test_full_mixed_list_crux(self) -> None:
        seed = [
            *_seed(),
            PortMapping(guest=8080, host=9000),
            PortMapping(guest=8081),
        ]
        r = resolve_ports(0, seed)
        host_guest = {f"{rp.host}:{rp.guest}" for rp in r}
        assert host_guest == {
            "5555:5555",
            "27042:27042",
            "27043:27043",
            "9000:8080",
            "40000:8081",
        }


class TestResolvePortsSelfCollision:
    def test_explicit_host_collides_with_well_known_stride(self) -> None:
        # arbitrary explicit host == adb stride default at index 0.
        seed = [*_seed(), PortMapping(guest=8080, host=5555)]
        with pytest.raises(PortCollisionError, match="5555"):
            resolve_ports(0, seed)

    def test_two_explicit_hosts_collide(self) -> None:
        # The schema validator catches duplicate explicit hosts before resolve,
        # but resolve must also catch it defensively when fed by hand.
        seed = [
            PortMapping(guest=1, host=9000),
            PortMapping(guest=2, host=9000),
        ]
        with pytest.raises(PortCollisionError, match="9000"):
            resolve_ports(0, seed)

    def test_too_many_arbitrary_auto_overrun_collides(self) -> None:
        # More than STRIDE arbitrary auto entries: slot at index 0 reaches
        # EXTRA_POOL_BASE + STRIDE, which is index-1's first extra slot — but a
        # single instance's own entries never overlap, so build the collision by
        # pinning one explicit host onto a later auto slot.
        seed = [
            PortMapping(guest=1),  # auto -> EXTRA_POOL_BASE
            PortMapping(guest=2, host=EXTRA_POOL_BASE + 1),  # explicit
            PortMapping(guest=3),  # auto slot 1 -> EXTRA_POOL_BASE + 1 collide
        ]
        with pytest.raises(PortCollisionError, match=str(EXTRA_POOL_BASE + 1)):
            resolve_ports(0, seed)

    def test_stride_arbitrary_auto_entries_ok(self) -> None:
        # Exactly STRIDE host-unset arbitrary entries fit the per-instance
        # window (slots 0..STRIDE-1); this is the last accepted count.
        entries = [PortMapping(guest=6000 + s) for s in range(STRIDE)]
        resolved = resolve_ports(0, entries)
        assert [rp.host for rp in resolved] == [EXTRA_POOL_BASE + s for s in range(STRIDE)]

    def test_more_than_stride_arbitrary_auto_entries_raises(self) -> None:
        # STRIDE+1 host-unset arbitrary entries: the (STRIDE)th slot would spill
        # into the next index's window. Enforced eagerly during allocation (a
        # monotonic slot sequence never duplicates a host, so the Counter
        # self-collision check would not catch it).
        entries = [PortMapping(guest=6000 + s) for s in range(STRIDE + 1)]
        with pytest.raises(PortCollisionError, match="spill into the next index"):
            resolve_ports(0, entries)

    def test_error_message_points_at_yaml(self) -> None:
        seed = [*_seed(), PortMapping(guest=8080, host=5555)]
        with pytest.raises(PortCollisionError) as exc_info:
            resolve_ports(0, seed)
        assert "beetroot.yaml" in str(exc_info.value)

    def test_collision_is_valueerror_subclass(self) -> None:
        assert issubclass(PortCollisionError, ValueError)


class TestWellKnown:
    def test_filters_to_well_known_only(self) -> None:
        r = resolve_ports(
            0,
            [
                PortMapping(service="adb", guest=5555),
                PortMapping(service="metrics", guest=9100),
                PortMapping(guest=8080, host=9000),
            ],
        )
        assert well_known(r) == {"adb": 5555}

    def test_empty_when_no_well_known(self) -> None:
        r = resolve_ports(0, [PortMapping(guest=8080, host=9000)])
        assert well_known(r) == {}


class TestPrivilegedPortAdvisory:
    def test_warns_on_privileged_host_port(self, capsys: pytest.CaptureFixture[str]) -> None:
        resolve_ports(0, [PortMapping(service="adb", guest=5555, host=80)])
        err = capsys.readouterr().err
        assert "privileged" in err
        assert "80" in err

    def test_no_warning_for_normal_ports(self, capsys: pytest.CaptureFixture[str]) -> None:
        resolve_ports(0, _seed())
        assert "privileged" not in capsys.readouterr().err

    def test_advisory_deduped_per_call(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Two privileged ports in one resolve emit a single note (deduped list),
        # not one line per port.
        resolve_ports(
            0,
            [
                PortMapping(service="adb", guest=5555, host=80),
                PortMapping(guest=8080, host=443),
            ],
        )
        err = capsys.readouterr().err
        assert err.count("privileged") == 1
