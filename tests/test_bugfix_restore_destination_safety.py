"""Regression tests for the snapshot RESTORE destination-safety bugs.

Covers three related fixes in ``snapshot.restore`` /
``snapshot.read_manifest``:

1. A manifest ``name`` that contains path separators / ``..`` / is
   absolute must be rejected against the instance-name grammar BEFORE
   any filesystem mutation or registry write, so a malicious archive
   cannot drive extraction into an attacker-chosen directory.
2. The cross-instance ``--force`` overwrite guard must protect BOTH
   redroid- and vm-backed registered directories, and must refuse when
   the restore target EQUALS *or is an ancestor of* a registered
   instance dir.
3. ``read_manifest`` on an archive whose manifest bytes are not valid
   UTF-8 must raise ``SnapshotError`` (the documented contract), not a
   raw ``UnicodeDecodeError``.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard

from beetroot import registry, snapshot

_MIN_YAML = "api_version: 3\nandroid:\n  version: 14\n"


def _make_instance(root: Path, *, data_bytes: bytes = b"hello") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "beetroot.yaml").write_text(_MIN_YAML)
    (root / "data").mkdir()
    (root / "data" / "marker.txt").write_bytes(data_bytes)
    (root / "modules").mkdir()
    return root


def _repack_with_manifest_bytes(
    archive: Path,
    new_archive: Path,
    manifest_bytes: bytes,
) -> Path:
    """Re-pack ``archive`` into ``new_archive`` swapping in raw manifest bytes.

    Unlike the helper in ``test_snapshot.py`` this takes *bytes* directly
    (not a ``Manifest``), so it can inject a non-UTF-8 payload that never
    round-trips through the pydantic model.
    """
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


def _archive_with_manifest_name(isolated_root: Path, tmp_path: Path, malicious_name: str) -> Path:
    """Build a valid redroid archive, then swap the manifest ``name`` field."""
    src = _make_instance(tmp_path / "alpha")
    registry.add_allocating("alpha", src)
    archive = snapshot.snapshot(src, tmp_path / "out")
    registry.remove("alpha")

    # Start from the real manifest so every other field stays valid;
    # only override ``name`` with the traversal payload.
    good = snapshot.read_manifest(archive)
    blob = good.model_dump(mode="json")
    blob["name"] = malicious_name
    poisoned = json.dumps(blob, sort_keys=True, indent=2).encode("utf-8")
    return _repack_with_manifest_bytes(archive, tmp_path / "poisoned.tar.zst", poisoned)


class TestManifestNameTraversal:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../evil",
            "../../../tmp/evil",
            "sub/dir",
            "/abs/evil",
            "..",
        ],
    )
    def test_restore_rejects_traversal_dest_name_before_any_side_effect(
        self, isolated_registry: Path, tmp_path: Path, bad_name: str
    ) -> None:
        # The CLI default derives dest_name from manifest.name when no
        # --name is given; a hostile archive must not escape the intended
        # area. ``restore`` validates the name at the API boundary, so
        # both the manifest-derived default AND a programmatic caller
        # passing a bad dest_name are rejected — before mkdir/extraction.
        archive = _archive_with_manifest_name(isolated_registry, tmp_path, bad_name)

        # Mirror the CLI default: dest_name = manifest.name, dest_path
        # resolved from it.
        manifest = snapshot.read_manifest(archive)
        dest_name = manifest.name
        dest_path = (tmp_path / "cwd" / dest_name).resolve()

        before = sorted(p.name for p in tmp_path.iterdir())
        with pytest.raises((snapshot.SnapshotError, ValueError)):
            snapshot.restore(archive, dest_name=dest_name, dest_path=dest_path)

        # No registry row was written for the hostile name.
        assert registry.get(dest_name) is None
        assert registry.list_instances() == {}
        # No extraction happened: no new top-level entries appeared under
        # tmp_path (the ``..`` case resolves dest_path back onto an
        # existing dir, so the absence of NEW entries — not the path's
        # nonexistence — is the load-bearing signal).
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_restore_accepts_a_normal_manifest_name(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Guard against over-rejection: a plain name still round-trips.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        restored = snapshot.restore(archive, dest_name="alpha", dest_path=tmp_path / "dst")
        assert restored == (tmp_path / "dst").resolve()
        assert registry.get("alpha") is not None


class TestForceProtectsRegisteredDirs:
    def test_force_refuses_to_wipe_registered_vm_instance_dir(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # A binder: vm instance is registered as VmBackendConfig (also
        # directory-backed). The guard used to inspect only redroid rows,
        # so a --force restore aimed at the vm dir wiped its files. The
        # broadened guard refuses and the vm data is untouched.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        vm_dir = tmp_path / "vmphone"
        _make_instance(vm_dir, data_bytes=b"vm precious data")
        registry.add_allocating(
            "vmphone", backend=registry.VmBackendConfig(absolute_path=str(vm_dir))
        )

        with pytest.raises(snapshot.SnapshotError, match="vmphone"):
            snapshot.restore(
                archive,
                dest_name="beta",
                dest_path=vm_dir,
                force=True,
            )
        # The vm instance's files survived intact.
        assert (vm_dir / "data" / "marker.txt").read_bytes() == b"vm precious data"
        assert (vm_dir / "beetroot.yaml").read_text() == _MIN_YAML
        # Beta was not registered.
        assert registry.get("beta") is None

    def test_force_refuses_ancestor_of_registered_redroid_instance_dir(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # rmtree(target) on a PARENT directory destroys a nested
        # registered instance just as surely as rmtree-ing the dir
        # itself. Exact-equality-only let a --force restore into the
        # parent wipe it; the ancestor check refuses.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        parent = tmp_path / "workspace"
        nested = parent / "peer"
        _make_instance(nested, data_bytes=b"nested precious data")
        registry.add_allocating("peer", nested)

        with pytest.raises(snapshot.SnapshotError, match="peer"):
            snapshot.restore(
                archive,
                dest_name="beta",
                dest_path=parent,
                force=True,
            )
        # The nested registered instance survived intact.
        assert (nested / "data" / "marker.txt").read_bytes() == b"nested precious data"
        assert registry.get("beta") is None

    def test_force_refuses_ancestor_of_registered_vm_instance_dir(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # The ancestor + vm-backend cases compose: a parent of a
        # registered binder: vm dir is refused too.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        parent = tmp_path / "vmworkspace"
        vm_nested = parent / "vmphone"
        _make_instance(vm_nested, data_bytes=b"vm nested data")
        registry.add_allocating(
            "vmphone", backend=registry.VmBackendConfig(absolute_path=str(vm_nested))
        )

        with pytest.raises(snapshot.SnapshotError, match="vmphone"):
            snapshot.restore(
                archive,
                dest_name="beta",
                dest_path=parent,
                force=True,
            )
        assert (vm_nested / "data" / "marker.txt").read_bytes() == b"vm nested data"
        assert registry.get("beta") is None


class TestReadManifestNonUtf8:
    def test_read_manifest_wraps_non_utf8_bytes_in_snapshot_error(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # A manifest member whose bytes are not valid UTF-8 raises
        # UnicodeDecodeError on decode BEFORE validation. read_manifest
        # must re-raise it as the documented SnapshotError so restore()
        # and the CLI (which catch SnapshotError) don't leak a traceback.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        # 0xFF is never a valid UTF-8 lead byte.
        bad = _repack_with_manifest_bytes(archive, tmp_path / "bad.tar.zst", b"\xff\xfe not utf-8")

        with pytest.raises(snapshot.SnapshotError, match="manifest validation failed"):
            snapshot.read_manifest(bad)
