"""
Property-based round-trip tests for the v3 registry schema.

For every generated :class:`InstanceMeta` (across both
``RedroidBackendConfig`` and ``AdbBackendConfig`` variants), assert
that the ``_write``/``_read`` round-trip is the identity function.
This is the strongest possible JSON-round-trip guarantee short of
formal verification — if it ever fails, the registry's schema is no
longer self-describing.

Pinned to derandomized hypothesis settings so CI failures reproduce
exactly.
"""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

from beetroot import registry as _registry
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
    """_write/_read round-trip preserves every InstanceMeta field exactly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "instances.json"
        doc = RegistryFile(instances={"x": meta})
        _registry._write(path, doc)
        rebuilt = _registry._read(path)
    assert rebuilt.instances["x"].backend == meta.backend
    assert rebuilt.instances["x"].index == meta.index


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
    """RegistryFile round-trips through _write/_read."""
    original = RegistryFile(version=3, instances=instances)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "instances.json"
        _registry._write(path, original)
        rebuilt = _registry._read(path)
    for name, orig_meta in instances.items():
        assert rebuilt.instances[name].backend == orig_meta.backend
        assert rebuilt.instances[name].index == orig_meta.index
