"""Regression test for the snapshot self-inclusion phantom member bug.

The CLI default writes ``<name>.tar.zst`` into the cwd, which is normally
the instance directory itself, so the just-created archive used to be
packed into itself as a spurious ``./<name>.tar.zst`` member that
``restore`` then re-extracted. This drives the full ``snapshot →
archive`` path with a destination INSIDE the instance dir and asserts on
the real artifact's member list.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import zstandard

from beetroot import registry, snapshot

_MIN_YAML = "api_version: 3\nandroid:\n  version: 14\n"


def _make_instance(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "beetroot.yaml").write_text(_MIN_YAML)
    (root / "data").mkdir()
    (root / "data" / "marker.txt").write_bytes(b"payload")
    (root / "modules").mkdir()
    (root / "modules" / "mod.zip").write_bytes(b"zip")
    return root


def _list_archive_members(archive: Path) -> list[str]:
    dctx = zstandard.ZstdDecompressor()
    with archive.open("rb") as raw, dctx.stream_reader(raw) as zst:
        with tarfile.open(fileobj=zst, mode="r|") as tar:
            return [member.name for member in tar]


def test_snapshot_does_not_pack_its_own_output_archive(
    isolated_registry: Path, tmp_path: Path
) -> None:
    src = _make_instance(tmp_path / "alpha")
    registry.add_allocating("alpha", src)

    # Mimic the CLI default: ``dest = ./<name>.tar.zst`` resolved against
    # the cwd, which is normally the instance directory itself.
    dest = src / "alpha.tar.zst"
    archive = snapshot.snapshot(src, dest)
    assert archive == dest

    members = _list_archive_members(archive)

    # The destination archive's own filename must NOT be a member.
    assert "./alpha.tar.zst" not in members
    assert not any(Path(name).name == "alpha.tar.zst" for name in members)

    # The real instance files ARE present.
    assert "./beetroot.yaml" in members
    assert "./data/marker.txt" in members
    assert "./modules/mod.zip" in members


def test_snapshot_keeps_same_named_archive_outside_instance_dir(
    isolated_registry: Path, tmp_path: Path
) -> None:
    """A same-named file inside the instance tree is not wrongly excluded."""
    src = _make_instance(tmp_path / "alpha")
    # A decoy file sharing the destination's basename but living in a
    # subtree — it must still be packed, since the exclusion matches by
    # resolved absolute path, not basename.
    (src / "data" / "alpha.tar.zst").write_bytes(b"decoy")
    registry.add_allocating("alpha", src)

    # Write the real archive OUTSIDE the instance dir this time.
    dest = tmp_path / "out" / "alpha.tar.zst"
    archive = snapshot.snapshot(src, dest)

    members = _list_archive_members(archive)
    assert "./data/alpha.tar.zst" in members
