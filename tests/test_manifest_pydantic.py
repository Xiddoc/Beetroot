"""Pydantic Manifest schema tests for T1's snapshot.py refactor.

The dataclass + ``_coerce_manifest`` pair gave way to a strict pydantic
model with ``frozen=True, extra="forbid"``. These tests pin the
forward-compat shape (the ``kind`` discriminator + ``path_layout``) and
the rejection of unknown future keys.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard
from pydantic import ValidationError

from beetroot import registry, snapshot

_MIN_YAML = "api_version: 3\nandroid:\n  version: 14\n"


def _make_instance(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "beetroot.yaml").write_text(_MIN_YAML)
    (root / "data").mkdir()
    (root / "modules").mkdir()
    return root


def _repack_with_custom_manifest(
    archive: Path, new_archive: Path, manifest_bytes: bytes,
) -> Path:
    dctx = zstandard.ZstdDecompressor()
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    with archive.open("rb") as raw, dctx.stream_reader(raw) as zst:
        with tarfile.open(fileobj=zst, mode="r|") as tar:
            for member in tar:
                if Path(member.name).name == snapshot.MANIFEST_FILENAME:
                    continue
                if member.isfile():
                    extracted = tar.extractfile(member)
                    payload = extracted.read() if extracted is not None else b""
                else:
                    payload = b""
                members.append((member, payload))
    cctx = zstandard.ZstdCompressor()
    with new_archive.open("wb") as raw_out, cctx.stream_writer(raw_out) as zst:
        with tarfile.open(fileobj=zst, mode="w|") as tar:
            for info, payload in members:
                tar.addfile(info, io.BytesIO(payload) if info.isfile() else None)
            info = tarfile.TarInfo(name=f"./{snapshot.MANIFEST_FILENAME}")
            info.size = len(manifest_bytes)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(manifest_bytes))
    return new_archive


class TestManifestModel:
    def test_default_kind_is_redroid(self) -> None:
        m = snapshot.Manifest(
            name="alpha",
            source_index=0,
            created_at="2026-05-19T00:00:00+00:00",
            beetroot_version="0.4.0",
        )
        assert m.kind == "redroid"

    def test_path_layout_defaults_to_empty(self) -> None:
        m = snapshot.Manifest(
            name="alpha",
            source_index=0,
            created_at="2026-05-19T00:00:00+00:00",
            beetroot_version="0.4.0",
        )
        assert m.path_layout == {}

    def test_frozen_blocks_mutation(self) -> None:
        m = snapshot.Manifest(
            name="alpha",
            source_index=0,
            created_at="2026-05-19T00:00:00+00:00",
            beetroot_version="0.4.0",
        )
        with pytest.raises(ValidationError):
            m.name = "bravo"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            snapshot.Manifest.model_validate(
                {
                    "schema_version": 1,
                    "name": "alpha",
                    "source_index": 0,
                    "created_at": "2026-05-19T00:00:00+00:00",
                    "beetroot_version": "0.4.0",
                    "future_field": "unknown",
                }
            )


class TestManifestArchiveRoundTrip:
    def test_archive_round_trips_path_layout(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Snapshot today writes ``path_layout: {}``; the archive
        # round-trip preserves whatever the manifest carries.
        src = _make_instance(tmp_path / "alpha")
        registry.add("alpha", src, 0)
        archive = snapshot.snapshot(src, tmp_path / "out")
        parsed = snapshot.read_manifest(archive)
        assert parsed.kind == "redroid"
        assert parsed.path_layout == {}

    def test_populated_path_layout_round_trips(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Hand-craft a manifest with a populated path_layout (the v0.5
        # stealth-posture story) and verify the strict reader round-trips
        # it intact through ``read_manifest``.
        src = _make_instance(tmp_path / "alpha")
        registry.add("alpha", src, 0)
        archive = snapshot.snapshot(src, tmp_path / "out")
        replaced = tmp_path / "replaced.tar.zst"
        forged = {
            "schema_version": 1,
            "name": "alpha",
            "source_index": 0,
            "created_at": "2026-05-19T00:00:00+00:00",
            "beetroot_version": "0.4.0",
            "kind": "redroid",
            "path_layout": {"frida": "/data/local/tmp/x"},
        }
        _repack_with_custom_manifest(
            archive, replaced, json.dumps(forged).encode("utf-8"),
        )
        parsed = snapshot.read_manifest(replaced)
        assert parsed.path_layout == {"frida": "/data/local/tmp/x"}

    def test_unknown_future_key_rejected_cleanly(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add("alpha", src, 0)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        forged = {
            "schema_version": 1,
            "name": "alpha",
            "source_index": 0,
            "created_at": "2026-05-19T00:00:00+00:00",
            "beetroot_version": "0.4.0",
            "kind": "redroid",
            "path_layout": {},
            "future_field_v0_6": "surprise",
        }
        _repack_with_custom_manifest(
            archive, broken, json.dumps(forged).encode("utf-8"),
        )
        with pytest.raises(snapshot.SnapshotError, match="validation failed"):
            snapshot.read_manifest(broken)

    def test_wrong_kind_rejected(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # v0.4 snapshots are redroid-only; an adb-shaped manifest must
        # be rejected so a future v0.6 cross-backend snapshot story
        # doesn't accidentally land on a v0.4 host.
        src = _make_instance(tmp_path / "alpha")
        registry.add("alpha", src, 0)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        forged = {
            "schema_version": 1,
            "name": "alpha",
            "source_index": 0,
            "created_at": "2026-05-19T00:00:00+00:00",
            "beetroot_version": "0.4.0",
            "kind": "adb",
            "path_layout": {},
        }
        _repack_with_custom_manifest(
            archive, broken, json.dumps(forged).encode("utf-8"),
        )
        with pytest.raises(snapshot.SnapshotError, match="validation failed"):
            snapshot.read_manifest(broken)
