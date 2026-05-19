"""
Property-based round-trip tests for the v3 registry schema.

For every generated :class:`InstanceMeta` (across both
``RedroidBackendConfig`` and ``AdbBackendConfig`` variants), assert
that ``model_validate_json(model_dump_json())`` is the identity
function. This is the strongest possible JSON-round-trip guarantee
short of formal verification — if it ever fails, the registry's
schema is no longer self-describing.

Pinned to derandomized hypothesis settings so CI failures reproduce
exactly.
"""
from __future__ import annotations

from datetime import UTC, datetime

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

from beetroot.registry import (
    AdbBackendConfig,
    InstanceMeta,
    RedroidBackendConfig,
    RegistryFile,
)

# Beetroot v0.3 didn't constrain the absolute_path or serial shape
# tightly, but the JSON round-trip is the only assertion here — any
# Unicode-safe string works.
_safe_string = st.text(
    alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00"),
    min_size=1,
    max_size=64,
)


@st.composite
def redroid_meta(draw: st.DrawFn) -> InstanceMeta:
    """Generate a valid :class:`InstanceMeta` with a Redroid backend."""
    backend = RedroidBackendConfig(
        absolute_path=draw(_safe_string),
        stealth_paths=draw(st.dictionaries(_safe_string, _safe_string, max_size=4)),
    )
    return InstanceMeta(
        backend=backend,
        index=draw(st.integers(min_value=0, max_value=10_000)),
        created_at=draw(st.datetimes(min_value=datetime(2020, 1, 1, tzinfo=UTC).replace(tzinfo=None), max_value=datetime(2050, 1, 1, tzinfo=UTC).replace(tzinfo=None))).replace(tzinfo=UTC),
    )


@st.composite
def adb_meta(draw: st.DrawFn) -> InstanceMeta:
    """Generate a valid :class:`InstanceMeta` with an ADB backend."""
    backend = AdbBackendConfig(serial=draw(_safe_string))
    return InstanceMeta(
        backend=backend,
        index=draw(st.integers(min_value=0, max_value=10_000)),
        created_at=draw(st.datetimes(min_value=datetime(2020, 1, 1, tzinfo=UTC).replace(tzinfo=None), max_value=datetime(2050, 1, 1, tzinfo=UTC).replace(tzinfo=None))).replace(tzinfo=UTC),
    )


@given(meta=st.one_of(redroid_meta(), adb_meta()))
@settings(
    deadline=None,
    derandomize=True,
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_instance_meta_json_round_trip_is_identity(meta: InstanceMeta) -> None:
    """JSON round-trip preserves every InstanceMeta field exactly."""
    raw = meta.model_dump_json()
    rebuilt = InstanceMeta.model_validate_json(raw)
    assert rebuilt == meta


@given(
    instances=st.dictionaries(
        _safe_string,
        st.one_of(redroid_meta(), adb_meta()),
        max_size=8,
    ),
)
@settings(
    deadline=None,
    derandomize=True,
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_registry_file_json_round_trip_is_identity(
    instances: dict[str, InstanceMeta],
) -> None:
    """RegistryFile round-trips through model_dump_json / model_validate_json."""
    original = RegistryFile(version=3, instances=instances)
    raw = original.model_dump_json()
    rebuilt = RegistryFile.model_validate_json(raw)
    assert rebuilt == original
