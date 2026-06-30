"""
Port allocation for instances.

Stride-of-10 scheme: instance index N maps to ADB port 5555 + N*10 and
Frida ports 27042/27043 + N*10. Indices are stable across the lifetime
of an instance and freed on destroy. Freed slots are reused — allocation
always picks the lowest free index.

Per-instance overrides — including arbitrary guest→host mappings beyond
the three well-known services — are supported via the ``ports:`` list in
``beetroot.yaml``; see :func:`resolve_ports` and the ``PortMapping`` model
in ``config.py``.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Final, NamedTuple

from . import console
from .config import WELL_KNOWN_SERVICES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .config import PortMapping

ADB_BASE = 5555
FRIDA_BASE = 27042
FRIDA_CONTROL_BASE = 27043
STRIDE = 10

# Highest valid TCP port — the upper bound every resolved host port must respect.
_MAX_PORT: Final = 65535

# Ports below this are the privileged range (0..1023) whose binding traditionally
# needs elevated privileges; :func:`resolve_ports` emits a single advisory (once
# per call, deduped over the resolved set) when any resolved host port lands there
# so a pinned ``host: 80`` fails loudly at bind time instead of silently — unless
# the caller passes ``quiet=True`` to suppress it on read-only cross-instance scans.
_PRIVILEGED_PORT_CEILING: Final = 1024

# Host-side stride base for each well-known service, keyed by service name.
# Derived once so :func:`resolve_ports` and the documented stride table stay
# in lock-step with the ``WELL_KNOWN_SERVICES`` guest-port source of truth.
_WELL_KNOWN_BASE: Final[dict[str, int]] = {
    "adb": ADB_BASE,
    "frida": FRIDA_BASE,
    "frida_control": FRIDA_CONTROL_BASE,
}

# Base for the band that auto-allocates arbitrary (non-well-known) entries
# whose ``host`` is unset. Chosen well clear of both the ADB band (5555+) and
# the Frida band (27042+) so an instance's arbitrary auto entries never
# overlap the stride-allocated well-known ports. An arbitrary entry at slot S
# of instance index N gets host ``EXTRA_POOL_BASE + N*STRIDE + S``; because S
# shares the per-instance stride window, an instance may auto-allocate at most
# ``STRIDE`` arbitrary entries before the next index's band would overlap. That
# per-instance bound is enforced eagerly in :func:`resolve_ports` (the slot
# would spill into the next index's window) rather than left to the
# post-resolution self-collision check, which a strictly-monotonic slot
# sequence never trips.
EXTRA_POOL_BASE = 40000

# Maximum port index before stride-of-10 allocation would either push a
# well-known port into the extra pool or push an extra-pool port past 65535.
# Two constraints bind, and the cap is the tighter of the two:
#   1. the highest well-known band (FRIDA_CONTROL_BASE + index*STRIDE) must stay
#      strictly below EXTRA_POOL_BASE, or a high-index well-known port would
#      climb into the 40000+ extra-pool range and falsely collide cross-instance;
#   2. the extra pool (EXTRA_POOL_BASE + index*STRIDE, plus up to STRIDE slots in
#      the per-instance window) must stay <= 65535.
# With EXTRA_POOL_BASE=40000 the first constraint (1295) binds, well below the
# second (2552), so the extra-pool base — not the well-known bands — is now what
# bounds the maximum index. Raising EXTRA_POOL_BASE relaxes (1) and tightens (2).
_MAX_PORT_INDEX: Final = min(
    (EXTRA_POOL_BASE - 1 - max(_WELL_KNOWN_BASE.values())) // STRIDE,
    (_MAX_PORT - EXTRA_POOL_BASE - STRIDE) // STRIDE,
)


class ResolvedPort(NamedTuple):
    """
    A fully-resolved guest→host port mapping for an instance.

    Attributes:
        service: The service label (``adb`` / ``frida`` / ``frida_control``
            for well-known services, a free-form label, or ``None``).
        guest: The container-side port.
        host: The resolved host-side port.
    """

    service: str | None
    guest: int
    host: int


class PortCollisionError(ValueError):
    """
    Raised when :func:`resolve_ports` produces duplicate host ports.

    This happens when a partial ``ports:`` override pins one entry to the
    stride-of-10 / extra-pool default of a sibling entry that wasn't
    overridden. The pydantic schema only validates distinctness among the
    *explicit* host ports it receives, so this resolver-side check catches
    the collisions the model can't see without knowing the index.

    It is also raised when an instance asks for more than ``STRIDE``
    host-unset arbitrary entries: the extra-pool slot would spill into the
    next index's per-instance window. That bound is enforced eagerly while
    allocating (the monotonic slot sequence never duplicates a host, so the
    post-resolution self-collision Counter would not catch it).
    """


def ports_for_index(index: int) -> dict[str, int]:
    """
    Compute the ADB and Frida host port numbers for a given instance index.

    Args:
        index: The instance's port index (non-negative integer, at most
            :data:`_MAX_PORT_INDEX`).

    Returns:
        A dict with keys ``adb``, ``frida``, and ``frida_control`` mapping
        to the host port numbers for this instance — the well-known stride
        defaults only (arbitrary mappings are not represented here).

    Raises:
        ValueError: If ``index`` is negative or above :data:`_MAX_PORT_INDEX`
            (the extra-pool base bounds the maximum index).
    """
    _check_index(index)
    return {service: base + index * STRIDE for service, base in _WELL_KNOWN_BASE.items()}


def _check_index(index: int) -> None:
    """
    Validate ``index`` against the supported stride range.

    Args:
        index: The instance's port index.

    Raises:
        ValueError: If ``index`` is negative or above :data:`_MAX_PORT_INDEX`.
    """
    if index < 0:
        raise ValueError(f"port index must be >= 0 (got {index})")
    if index > _MAX_PORT_INDEX:
        highest = max(_WELL_KNOWN_BASE.values())
        raise ValueError(
            f"port index {index} exceeds the maximum supported index "
            f"{_MAX_PORT_INDEX}: the highest well-known band "
            f"({highest} + index*{STRIDE} = {highest + index * STRIDE}) would "
            f"reach the {EXTRA_POOL_BASE}+ extra-pool range, risking a false "
            f"cross-instance collision (and an arbitrary port could exceed 65535). "
            f"The extra-pool base bounds the maximum index."
        )


def resolve_ports(
    index: int, ports: Sequence[PortMapping], *, quiet: bool = False
) -> list[ResolvedPort]:
    """
    Resolve every ``PortMapping`` to a concrete host port for ``index``.

    Resolution rules, in order of precedence:

    * an explicit ``host`` always wins;
    * a well-known service (``adb`` / ``frida`` / ``frida_control``) with
      ``host`` unset gets its stride base (``base + index*STRIDE``);
    * any other entry with ``host`` unset gets an extra-pool slot
      (``EXTRA_POOL_BASE + index*STRIDE + slot``, where ``slot`` is its
      0-based position among the instance's auto-allocated arbitrary
      entries).

    Args:
        index: The instance's port index (non-negative integer).
        ports: The instance's ``PortMapping`` list (from its config).
        quiet: When ``True``, suppress the privileged-port advisory. Read-only
            cross-instance scans (which re-resolve every registered instance)
            pass this so a privileged-port instance's advisory isn't re-emitted
            once per scan and misattributed to an unrelated operation (#224).

    Returns:
        A list of :class:`ResolvedPort`, one per input mapping, in order.

    Raises:
        PortCollisionError: If two resolved mappings land on the same host
            port (e.g. a partial override colliding with a stride default),
            or if more than ``STRIDE`` host-unset arbitrary entries are
            requested (the extra-pool slot would spill into the next index).
    """
    _check_index(index)
    resolved: list[ResolvedPort] = []
    extra_slot = 0
    for mapping in ports:
        if mapping.host is not None:
            host = mapping.host
        elif mapping.service in _WELL_KNOWN_BASE:
            host = _WELL_KNOWN_BASE[mapping.service] + index * STRIDE
        else:
            if extra_slot >= STRIDE:
                raise PortCollisionError(
                    f"instance at index {index} requested more than {STRIDE} "
                    "host-unset arbitrary ports: entries; the auto-allocated "
                    "extra-pool slot would spill into the next index's window "
                    f"(host {EXTRA_POOL_BASE + index * STRIDE + extra_slot} belongs "
                    f"to index {index + 1}). Pin explicit host ports in "
                    "beetroot.yaml's ports: list for the entries beyond the first "
                    f"{STRIDE}."
                )
            host = EXTRA_POOL_BASE + index * STRIDE + extra_slot
            extra_slot += 1
        resolved.append(ResolvedPort(service=mapping.service, guest=mapping.guest, host=host))
    counts = Counter(rp.host for rp in resolved)
    dupes = {
        host: sorted(str(rp.service) for rp in resolved if rp.host == host)
        for host, n in counts.items()
        if n > 1
    }
    if dupes:
        raise PortCollisionError(
            f"resolved host ports collide on this instance: {dupes}. "
            "Pin explicit host ports in beetroot.yaml's ports: list to avoid "
            "colliding with stride-of-10 / extra-pool defaults."
        )
    privileged = sorted({rp.host for rp in resolved if rp.host < _PRIVILEGED_PORT_CEILING})
    if privileged and not quiet:
        console.note(
            f"resolved host port(s) {privileged} are below {_PRIVILEGED_PORT_CEILING}; "
            "binding privileged ports may require elevated privileges (run Docker with "
            "the capability, or pin host ports >= 1024 in beetroot.yaml's ports: list)."
        )
    return resolved


def well_known(resolved: Sequence[ResolvedPort]) -> dict[str, int]:
    """
    Project the resolved list to a ``{service: host}`` dict of well-known services.

    Only entries whose ``service`` is one of the well-known names
    (``adb`` / ``frida`` / ``frida_control``) are included — the accessor
    the adb-address / frida-address consumers key off.

    Args:
        resolved: The resolved port list from :func:`resolve_ports`.

    Returns:
        A dict mapping each present well-known service name to its host port.
    """
    return {
        rp.service: rp.host
        for rp in resolved
        if rp.service is not None and rp.service in WELL_KNOWN_SERVICES
    }


def lowest_free_index(used: set[int]) -> int:
    """
    Return the smallest non-negative integer not in ``used``. Reuses freed slots.
    """
    i = 0
    while i in used:
        i += 1
    return i
