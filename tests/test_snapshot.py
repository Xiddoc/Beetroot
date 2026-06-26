"""Tests for snapshot.py — round-trip, manifest, registry interaction."""

from __future__ import annotations

import importlib.metadata
import io
import json
import tarfile
import time
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


def _list_archive_members(archive: Path) -> list[str]:
    dctx = zstandard.ZstdDecompressor()
    names: list[str] = []
    with archive.open("rb") as raw, dctx.stream_reader(raw) as zst:
        with tarfile.open(fileobj=zst, mode="r|") as tar:
            names.extend(m.name for m in tar)
    return names


def _read_manifest_bytes(archive: Path) -> bytes:
    dctx = zstandard.ZstdDecompressor()
    with archive.open("rb") as raw, dctx.stream_reader(raw) as zst:
        with tarfile.open(fileobj=zst, mode="r|") as tar:
            for member in tar:
                if Path(member.name).name == snapshot.MANIFEST_FILENAME:
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    return extracted.read()
    raise AssertionError("no manifest in archive")


def _repack_with_custom_manifest(
    archive: Path,
    new_archive: Path,
    manifest_bytes: bytes,
) -> Path:
    """Re-pack ``archive`` into ``new_archive`` swapping in a new manifest blob."""
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


class TestSnapshotRoundTrip:
    def test_round_trip_preserves_data_bytes(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "foo" / "alpha", data_bytes=b"\x00\xffmarker\x42")
        registry.add_allocating("alpha", src)

        archive = snapshot.snapshot(src, tmp_path / "snapshots" / "alpha-clean")
        assert archive.name == "alpha-clean.tar.zst"
        assert archive.is_file()

        registry.remove("alpha")
        target = tmp_path / "bar" / "beta"
        restored = snapshot.restore(archive, dest_name="beta", dest_path=target)

        assert restored == target.resolve()
        assert (restored / "data" / "marker.txt").read_bytes() == b"\x00\xffmarker\x42"
        assert (restored / "beetroot.yaml").read_text() == _MIN_YAML

        beta = registry.get("beta")
        assert beta is not None
        assert isinstance(beta.backend, registry.RedroidBackendConfig)
        assert Path(beta.backend.absolute_path) == target.resolve()
        assert beta.index == 0

    def test_round_trip_runs_quickly(self, isolated_registry: Path, tmp_path: Path) -> None:
        payload = b"X" * 100_000
        src = _make_instance(tmp_path / "alpha", data_bytes=payload)
        registry.add_allocating("alpha", src)

        start = time.perf_counter()
        archive = snapshot.snapshot(src, tmp_path / "out")
        restored = snapshot.restore(archive, dest_name="beta", dest_path=tmp_path / "beta")
        elapsed = time.perf_counter() - start

        assert elapsed < 2.0, f"round-trip too slow: {elapsed:.3f}s"
        assert (restored / "data" / "marker.txt").read_bytes() == payload


class TestSnapshotManifest:
    def test_manifest_has_all_required_keys(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        # Pre-fill indices 0, 1, 2 so "alpha" gets index 3.
        for i in range(3):
            dummy = tmp_path / f"dummy{i}"
            dummy.mkdir()
            registry.add_allocating(f"dummy{i}", dummy)
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        manifest = snapshot.read_manifest(archive)
        assert manifest.schema_version == 1
        assert manifest.name == "alpha"
        assert manifest.source_index == 3
        assert manifest.created_at.endswith("+00:00")
        assert manifest.beetroot_version == importlib.metadata.version("beetroot")
        assert manifest.path_layout == {}

    def test_manifest_default_path_layout_is_empty(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out.tar.zst")
        assert snapshot.read_manifest(archive).path_layout == {}

    def test_dest_extension_already_present_is_kept(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "alpha.tar.zst")
        assert archive.name == "alpha.tar.zst"


class TestSnapshotArchiveLayout:
    def test_env_is_excluded(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        (src / ".env").write_text("SECRET=1")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        members = _list_archive_members(archive)
        env_members = [m for m in members if m.endswith("/.env") or m == "./.env"]
        assert env_members == [], f"unexpected .env members in archive: {env_members}"

    def test_yaml_and_data_and_manifest_all_present(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        (src / "frida-server").write_bytes(b"")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        members = set(_list_archive_members(archive))
        assert "./beetroot.yaml" in members
        assert "./data/marker.txt" in members
        assert "./modules" in members
        assert "./frida-server" in members
        assert f"./{snapshot.MANIFEST_FILENAME}" in members


class TestRestorePortAllocation:
    def test_fresh_port_index_does_not_reuse_source(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        snapshot.restore(archive, dest_name="beta", dest_path=tmp_path / "beta")

        alpha = registry.get("alpha")
        assert alpha is not None
        assert alpha.index == 0
        beta = registry.get("beta")
        assert beta is not None
        assert beta.index == 1


class TestRestoreForce:
    def test_refuses_existing_non_empty_dir(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        target = tmp_path / "beta"
        target.mkdir()
        (target / "existing.txt").write_bytes(b"keep me")

        with pytest.raises(snapshot.SnapshotError, match="--force"):
            snapshot.restore(archive, dest_name="beta", dest_path=target)
        assert (target / "existing.txt").exists()

    def test_force_overwrites(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha", data_bytes=b"new")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        target = tmp_path / "beta"
        target.mkdir()
        (target / "stale.txt").write_bytes(b"to be wiped")

        snapshot.restore(archive, dest_name="beta", dest_path=target, force=True)
        assert not (target / "stale.txt").exists()
        assert (target / "data" / "marker.txt").read_bytes() == b"new"

    def test_force_refuses_to_overwrite_another_instances_dir(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Cross-instance attack: a malicious or careless --force
        # restore aimed at another registered instance's directory
        # would otherwise wipe a sibling's data. The fix refuses
        # the operation even with --force; the user must destroy
        # the sibling first or pick another path.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        # Register a peer instance at the dir we're about to point
        # --force at. The peer is a DIFFERENT name; the cross-instance
        # protection must fire.
        peer_dir = tmp_path / "peer"
        _make_instance(peer_dir, data_bytes=b"peer's precious data")
        registry.add_allocating("peer", peer_dir)

        with pytest.raises(snapshot.SnapshotError, match="peer"):
            snapshot.restore(
                archive,
                dest_name="beta",
                dest_path=peer_dir,
                force=True,
            )
        # Peer's data is intact.
        assert (peer_dir / "data" / "marker.txt").read_bytes() == (b"peer's precious data")
        # Beta did not get registered.
        assert registry.get("beta") is None

    def test_empty_existing_dir_is_allowed_without_force(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        target = tmp_path / "beta"
        target.mkdir()

        restored = snapshot.restore(archive, dest_name="beta", dest_path=target)
        assert restored == target.resolve()

    def test_force_corrupted_archive_does_not_destroy_target(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # T2 Agent 3 1.4: a corrupted archive + --force must NOT
        # destroy the target directory before the manifest read fails.
        # v0.3 ordered rmtree(target) before read_manifest, so a
        # malformed archive wiped the user's existing dir AND blew up
        # mid-restore — no way back.
        target = tmp_path / "beta"
        target.mkdir()
        marker = target / "important.txt"
        marker.write_bytes(b"do not lose me")
        nested = target / "data" / "subdir"
        nested.mkdir(parents=True)
        (nested / "more.txt").write_bytes(b"also important")

        # An obviously-broken "archive" — not a valid zstd stream.
        bad = tmp_path / "bad.tar.zst"
        bad.write_bytes(b"not a real archive")

        with pytest.raises(snapshot.SnapshotError):
            snapshot.restore(
                bad,
                dest_name="beta",
                dest_path=target,
                force=True,
            )

        # The target directory and ALL its contents survived.
        assert target.exists()
        assert marker.read_bytes() == b"do not lose me"
        assert (nested / "more.txt").read_bytes() == b"also important"
        # Beta did not get registered.
        assert registry.get("beta") is None


class TestRestoreErrors:
    def test_dest_name_already_registered(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        other = _make_instance(tmp_path / "preexisting" / "beta")
        registry.add_allocating("beta", other)

        with pytest.raises(snapshot.SnapshotError, match="already registered") as excinfo:
            snapshot.restore(archive, dest_name="beta", dest_path=tmp_path / "new-beta")
        message = str(excinfo.value)
        assert "--name" in message
        assert "--as" not in message

    def test_restore_refuses_port_collision_with_peer(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Snapshot a source that pins ADB to 5565 (so the source's
        # ports won't auto-conflict with anything stride-allocated at
        # index 0). Then register a *peer* instance at index 1 that
        # naturally uses the stride default 5565 for ADB. Restoring
        # the snapshot now must refuse: the restored instance's
        # resolved port 5565 collides with the peer's resolved 5565.
        src = _make_instance(tmp_path / "alpha")
        (src / "beetroot.yaml").write_text(
            "api_version: 3\nandroid:\n  version: 14\nports:\n  adb: 5565\n"
        )
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        registry.remove("alpha")

        # Pre-stage a peer at index 1 (stride default ADB = 5565).
        # Add a dummy at index 0 first so the peer gets index 1.
        dummy = tmp_path / "dummy"
        dummy.mkdir()
        registry.add_allocating("dummy", dummy)
        peer = _make_instance(tmp_path / "peer")
        registry.add_allocating("peer", peer)

        with pytest.raises(snapshot.SnapshotError, match="5565"):
            snapshot.restore(archive, dest_name="beta", dest_path=tmp_path / "beta")
        # On collision, no registry mutation occurs.
        assert registry.get("beta") is None


class TestSnapshotErrors:
    def test_snapshot_unregistered_instance_raises(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        with pytest.raises(snapshot.SnapshotError, match="not registered"):
            snapshot.snapshot(src, tmp_path / "out")

    def test_snapshot_missing_yaml_raises(self, isolated_registry: Path, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus"
        bogus.mkdir()
        with pytest.raises(snapshot.SnapshotError, match="not a Beetroot instance"):
            snapshot.snapshot(bogus, tmp_path / "out")

    def test_snapshot_skips_non_matching_registry_entries(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # An unrelated instance is registered first; the snapshot target is
        # registered second. The registry-entry lookup must skip past the
        # first non-matching entry, exercising the loop-continue branch.
        other = _make_instance(tmp_path / "other")
        registry.add_allocating("aaa-other", other)
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("zzz-alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        manifest = snapshot.read_manifest(archive)
        assert manifest.name == "zzz-alpha"
        assert manifest.source_index == 1


class TestNonRedroidBackendSnapshot:
    """#128: snapshotting / restoring a vm or adb instance fails clearly."""

    def test_snapshot_vm_instance_raises_redroid_only_error(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # A *registered* binder: vm instance must not get the misleading
        # "not registered" message — it gets the specific #128 error.
        root = _make_instance(tmp_path / "vmphone")
        registry.add_allocating(
            "vmphone", backend=registry.VmBackendConfig(absolute_path=str(root))
        )
        with pytest.raises(snapshot.SnapshotError) as excinfo:
            snapshot.snapshot(root, tmp_path / "out")
        message = str(excinfo.value)
        assert "only supported for the redroid backend" in message
        assert "vmphone" in message
        assert "vm backend" in message
        assert "#128" in message
        assert "not registered" not in message

    def test_snapshot_unregistered_path_with_a_vm_row_present_still_not_registered(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # A vm row exists in the registry but does NOT match the target
        # path: the vm-arm path comparison is False, so the genuine
        # "not registered" message must still surface (branch coverage
        # for the vm arm's path mismatch).
        vm_root = _make_instance(tmp_path / "vmphone")
        registry.add_allocating(
            "vmphone", backend=registry.VmBackendConfig(absolute_path=str(vm_root))
        )
        other = _make_instance(tmp_path / "unregistered")
        with pytest.raises(snapshot.SnapshotError, match="not registered"):
            snapshot.snapshot(other, tmp_path / "out")

    def test_restore_into_vm_named_instance_raises_redroid_only_error(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Build a valid redroid archive, then aim restore at a name that
        # is already a binder: vm instance. The clash gets the #128
        # message, not the generic "already registered" hint.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        vm_root = _make_instance(tmp_path / "vmphone")
        registry.add_allocating(
            "vmphone", backend=registry.VmBackendConfig(absolute_path=str(vm_root))
        )
        with pytest.raises(snapshot.SnapshotError) as excinfo:
            snapshot.restore(archive, dest_name="vmphone", dest_path=tmp_path / "dst")
        message = str(excinfo.value)
        assert "restore is only supported for the redroid backend" in message
        assert "#128" in message
        # The destination directory must not have been created/registered.
        assert not (tmp_path / "dst").exists()

    def test_restore_into_adb_named_instance_raises_redroid_only_error(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        registry.add_allocating(
            "adbphone", backend=registry.AdbBackendConfig(serial="emulator-5554")
        )
        with pytest.raises(snapshot.SnapshotError, match="adb backend") as excinfo:
            snapshot.restore(archive, dest_name="adbphone", dest_path=tmp_path / "dst")
        assert "#128" in str(excinfo.value)

    def test_unsupported_backend_message_names_verb_kind_and_issue(self) -> None:
        msg = snapshot.unsupported_backend_message("snapshot", "phone", "vm")
        assert "snapshot is only supported for the redroid backend" in msg
        assert "'phone'" in msg
        assert "vm backend" in msg
        assert "vm snapshot is not yet supported" in msg
        assert "#128" in msg


class TestPathLayoutForwardCompat:
    def test_non_empty_path_layout_round_trips(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        original = snapshot.snapshot(src, tmp_path / "out")

        # Surgery: rewrite the manifest with a v0.4-shaped path_layout.
        custom_layout = {
            "magisk_db": "/data/adb/.b7e2f1.db",
            "modules_dir": "/.b7e2f1_flash",
            "frida_bin": "/data/local/tmp/.b7e2f1",
        }
        raw = json.loads(_read_manifest_bytes(original))
        raw["path_layout"] = custom_layout
        new_archive = tmp_path / "out-stealth.tar.zst"
        _repack_with_custom_manifest(original, new_archive, json.dumps(raw).encode("utf-8"))

        manifest = snapshot.read_manifest(new_archive)
        assert manifest.path_layout == custom_layout

        registry.remove("alpha")
        restored = snapshot.restore(new_archive, dest_name="beta", dest_path=tmp_path / "beta")
        # Restored manifest on disk preserves the layout for v0.4 apply.
        assert (restored / snapshot.MANIFEST_FILENAME).is_file()
        on_disk = json.loads((restored / snapshot.MANIFEST_FILENAME).read_text())
        assert on_disk["path_layout"] == custom_layout
        # And read_manifest on the archive itself still returns the same.
        assert snapshot.read_manifest(new_archive).path_layout == custom_layout


class TestReadManifestErrors:
    def test_archive_with_no_manifest_raises(self, tmp_path: Path) -> None:
        archive = tmp_path / "bad.tar.zst"
        cctx = zstandard.ZstdCompressor()
        with archive.open("wb") as raw, cctx.stream_writer(raw) as zst:
            with tarfile.open(fileobj=zst, mode="w|") as tar:
                info = tarfile.TarInfo(name="./beetroot.yaml")
                payload = _MIN_YAML.encode("utf-8")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

        with pytest.raises(snapshot.SnapshotError, match="missing"):
            snapshot.read_manifest(archive)

    def test_manifest_not_json_raises(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        _repack_with_custom_manifest(archive, broken, b"{not-json")

        with pytest.raises(snapshot.SnapshotError, match="validation failed"):
            snapshot.read_manifest(broken)

    def test_manifest_top_level_must_be_object(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        _repack_with_custom_manifest(archive, broken, b"[1,2,3]")

        with pytest.raises(snapshot.SnapshotError, match="validation failed"):
            snapshot.read_manifest(broken)

    def test_manifest_missing_keys_raises(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        _repack_with_custom_manifest(
            archive, broken, json.dumps({"schema_version": 1}).encode("utf-8")
        )

        with pytest.raises(snapshot.SnapshotError, match="validation failed"):
            snapshot.read_manifest(broken)

    def test_manifest_wrong_schema_version_raises(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        bogus = {
            "schema_version": 99,
            "name": "alpha",
            "source_index": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "beetroot_version": "0.1.0",
            "path_layout": {},
        }
        _repack_with_custom_manifest(archive, broken, json.dumps(bogus).encode("utf-8"))

        with pytest.raises(snapshot.SnapshotError, match="schema_version"):
            snapshot.read_manifest(broken)

    def test_manifest_path_layout_must_be_object(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        broken = tmp_path / "broken.tar.zst"
        bogus = {
            "schema_version": 1,
            "name": "alpha",
            "source_index": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "beetroot_version": "0.1.0",
            "path_layout": ["not", "a", "dict"],
        }
        _repack_with_custom_manifest(archive, broken, json.dumps(bogus).encode("utf-8"))

        with pytest.raises(snapshot.SnapshotError, match="path_layout"):
            snapshot.read_manifest(broken)


class TestManifestShadowRegression:
    def test_resnapshot_of_restored_instance_returns_new_manifest(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # F5 guardrail. Restore preserves the manifest on disk at
        # <root>/.beetroot-snapshot.json. A naive snapshot of the
        # restored instance would pack BOTH the on-disk stale
        # manifest AND the freshly generated root manifest, and a
        # basename-only read_manifest would pick the first one (the
        # stale embedded copy). Pin: the second-generation manifest's
        # `name` must reflect the second-generation instance, not the
        # original.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        first = snapshot.snapshot(src, tmp_path / "first")

        # Restore the snapshot as a brand-new instance "beta".
        registry.remove("alpha")
        target = tmp_path / "beta"
        snapshot.restore(first, dest_name="beta", dest_path=target)
        # The on-disk manifest left over from extraction says "alpha"
        # — this is the stale copy that used to shadow the fresh one.
        on_disk = json.loads((target / snapshot.MANIFEST_FILENAME).read_text())
        assert on_disk["name"] == "alpha"

        # Now re-snapshot the restored instance and read back its
        # manifest. The returned manifest MUST be the fresh one
        # (name=beta), not the embedded stale alpha one.
        second = snapshot.snapshot(target, tmp_path / "second")
        manifest = snapshot.read_manifest(second)
        assert manifest.name == "beta", (
            f"manifest-shadow regression: expected 'beta', got {manifest.name!r}"
        )

    def test_resnapshot_archive_has_exactly_one_manifest_entry(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # The exclusion of the on-disk manifest in _add_instance_tree
        # is a structural guarantee: the archive must contain exactly
        # one manifest member at its root, regardless of whether the
        # source dir has a stale manifest already.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "first")
        registry.remove("alpha")
        target = tmp_path / "beta"
        snapshot.restore(archive, dest_name="beta", dest_path=target)
        # Re-snapshot.
        second = snapshot.snapshot(target, tmp_path / "second")
        members = _list_archive_members(second)
        manifest_entries = [m for m in members if Path(m).name == snapshot.MANIFEST_FILENAME]
        assert len(manifest_entries) == 1, (
            f"expected exactly one manifest entry, got: {manifest_entries}"
        )


class TestRestoreInvalidArchive:
    def test_archive_without_beetroot_yaml_raises(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        archive = tmp_path / "bad.tar.zst"
        manifest_bytes = json.dumps(
            {
                "schema_version": 1,
                "name": "alpha",
                "source_index": 0,
                "created_at": "2026-01-01T00:00:00+00:00",
                "beetroot_version": "0.1.0",
                "path_layout": {},
            }
        ).encode("utf-8")
        cctx = zstandard.ZstdCompressor()
        with archive.open("wb") as raw, cctx.stream_writer(raw) as zst:
            with tarfile.open(fileobj=zst, mode="w|") as tar:
                info = tarfile.TarInfo(name=f"./{snapshot.MANIFEST_FILENAME}")
                info.size = len(manifest_bytes)
                tar.addfile(info, io.BytesIO(manifest_bytes))

        with pytest.raises(snapshot.SnapshotError, match=r"beetroot\.yaml"):
            snapshot.restore(archive, dest_name="beta", dest_path=tmp_path / "beta")

    def test_corrupt_manifest_member_is_not_regular_file(self, tmp_path: Path) -> None:
        archive = tmp_path / "bad.tar.zst"
        cctx = zstandard.ZstdCompressor()
        with archive.open("wb") as raw, cctx.stream_writer(raw) as zst:
            with tarfile.open(fileobj=zst, mode="w|") as tar:
                info = tarfile.TarInfo(name=f"./{snapshot.MANIFEST_FILENAME}")
                info.type = tarfile.DIRTYPE
                tar.addfile(info)

        with pytest.raises(snapshot.SnapshotError, match="not a regular file"):
            snapshot.read_manifest(archive)

    def test_archive_not_zstd_stream_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.tar.zst"
        bad.write_bytes(b"this is not zstd")
        with pytest.raises(snapshot.SnapshotError, match="not a valid zstd"):
            snapshot.read_manifest(bad)

    def test_archive_malformed_tar_raises(self, tmp_path: Path) -> None:
        # A valid zstd stream wrapping non-tar garbage so tarfile raises TarError.
        bad = tmp_path / "bad.tar.zst"
        cctx = zstandard.ZstdCompressor()
        with bad.open("wb") as raw, cctx.stream_writer(raw) as zst:
            zst.write(b"definitely not a tar header")
        with pytest.raises(snapshot.SnapshotError, match="malformed tar"):
            snapshot.read_manifest(bad)


class TestRestoreCorruptedArchive:
    def test_restore_validates_manifest_before_touching_target(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Truncating the archive corrupts both the manifest stream and
        # the extract pass; restore() runs read_manifest() first, so
        # the SnapshotError is raised before the target dir is created
        # or the registry is mutated.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")

        raw = archive.read_bytes()
        archive.write_bytes(raw[: len(raw) - 1] + b"\x00")
        target = tmp_path / "beta"

        with pytest.raises(snapshot.SnapshotError):
            snapshot.restore(archive, dest_name="beta", dest_path=target)
        assert not target.exists()
        assert registry.get("beta") is None


def _make_archive_with_absolute_symlink(archive: Path, manifest_bytes: bytes) -> None:
    """Build a valid-manifest archive that also contains an absolute symlink member."""
    cctx = zstandard.ZstdCompressor()
    with archive.open("wb") as raw_out, cctx.stream_writer(raw_out) as zst:
        with tarfile.open(fileobj=zst, mode="w|") as tar:
            # Valid beetroot.yaml first so _extract_archive_into gets past it
            yaml_payload = _MIN_YAML.encode()
            yaml_info = tarfile.TarInfo(name="./beetroot.yaml")
            yaml_info.size = len(yaml_payload)
            yaml_info.mode = 0o644
            tar.addfile(yaml_info, io.BytesIO(yaml_payload))
            # Absolute symlink — triggers tarfile.AbsoluteLinkError under
            # filter="data".  This is the B7d scenario: a live container's
            # /data bind-mount can contain such symlinks, making the archive
            # unrestorable.
            link_info = tarfile.TarInfo(name="./data/evil_link")
            link_info.type = tarfile.SYMTYPE
            link_info.linkname = "/etc/passwd"
            link_info.mode = 0o755
            tar.addfile(link_info)
            # Manifest at the end so read_manifest() succeeds
            manifest_info = tarfile.TarInfo(name=f"./{snapshot.MANIFEST_FILENAME}")
            manifest_info.size = len(manifest_bytes)
            manifest_info.mode = 0o644
            tar.addfile(manifest_info, io.BytesIO(manifest_bytes))


_VALID_MANIFEST_BYTES = json.dumps(
    {
        "schema_version": 1,
        "name": "alpha",
        "source_index": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "beetroot_version": "0.1.0",
        "path_layout": {},
    }
).encode("utf-8")


class TestB7aExtractionRollback:
    """B7a: a corrupt/abusive archive that fails mid-extraction leaves no partial dir."""

    def test_abusive_archive_leaves_no_partial_dir(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Archive passes read_manifest() but fails _extract_archive_into()
        # because of an AbsoluteLinkError on the embedded symlink.
        archive = tmp_path / "abusive.tar.zst"
        _make_archive_with_absolute_symlink(archive, _VALID_MANIFEST_BYTES)

        target = tmp_path / "beta"
        with pytest.raises((snapshot.SnapshotError, tarfile.FilterError)):
            snapshot.restore(archive, dest_name="beta", dest_path=target)

        # B7a fix: the partial directory must NOT survive.
        assert not target.exists(), (
            "B7a regression: partial extracted directory left behind after mid-extraction failure"
        )
        assert registry.get("beta") is None

    def test_abusive_archive_force_mode_still_rolls_back(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Even with --force (which wipes an existing dir), a mid-extraction
        # failure must clean up the newly-created directory.
        archive = tmp_path / "abusive.tar.zst"
        _make_archive_with_absolute_symlink(archive, _VALID_MANIFEST_BYTES)

        target = tmp_path / "beta"
        target.mkdir()
        (target / "marker.txt").write_bytes(b"temporary")

        with pytest.raises((snapshot.SnapshotError, tarfile.FilterError)):
            snapshot.restore(archive, dest_name="beta", dest_path=target, force=True)

        assert not target.exists()
        assert registry.get("beta") is None


class TestB7bDestIsFile:
    """B7b: _prepare_destination raises SnapshotError when target is a file."""

    def test_restore_to_file_path_raises(self, isolated_registry: Path, tmp_path: Path) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        # Write a plain file at the destination path.
        file_dest = tmp_path / "not_a_dir.txt"
        file_dest.write_bytes(b"I am a file, not a directory")

        with pytest.raises(snapshot.SnapshotError, match="is a file"):
            snapshot.restore(archive, dest_name="beta", dest_path=file_dest)

        # The file must still exist and be untouched.
        assert file_dest.is_file()
        assert file_dest.read_bytes() == b"I am a file, not a directory"
        assert registry.get("beta") is None

    def test_restore_to_file_path_raises_with_force(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        file_dest = tmp_path / "not_a_dir.txt"
        file_dest.write_bytes(b"still a file")

        with pytest.raises(snapshot.SnapshotError, match="is a file"):
            snapshot.restore(archive, dest_name="beta", dest_path=file_dest, force=True)
        assert file_dest.is_file()


class TestB7cManifestSortedKeys:
    """B7c: _manifest_to_json produces sorted-key, byte-identical output."""

    def test_same_content_produces_identical_bytes(self) -> None:
        # Construct the same manifest twice and verify byte identity.
        m1 = snapshot.Manifest(
            name="alpha",
            source_index=0,
            created_at="2026-01-01T00:00:00+00:00",
            beetroot_version="0.1.0",
            path_layout={"z": "last", "a": "first"},
        )
        m2 = snapshot.Manifest(
            name="alpha",
            source_index=0,
            created_at="2026-01-01T00:00:00+00:00",
            beetroot_version="0.1.0",
            path_layout={"a": "first", "z": "last"},
        )
        b1 = snapshot._manifest_to_json(m1)
        b2 = snapshot._manifest_to_json(m2)
        assert b1 == b2, (
            "B7c regression: manifests with same content but different field-insertion "
            "order produced different bytes"
        )

    def test_manifest_output_is_sorted(self) -> None:
        m = snapshot.Manifest(
            name="z_name",
            source_index=5,
            created_at="2026-01-01T00:00:00+00:00",
            beetroot_version="0.1.0",
        )
        raw = snapshot._manifest_to_json(m)
        parsed = json.loads(raw)
        keys = list(parsed.keys())
        assert keys == sorted(keys), f"keys not sorted: {keys}"

    def test_round_trip_after_sort_keys(self, isolated_registry: Path, tmp_path: Path) -> None:
        # The byte-stable manifest must still be parseable by read_manifest.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        manifest = snapshot.read_manifest(archive)
        assert manifest.name == "alpha"


class TestAdbBackendBranchCoverage:
    """Cover the 'continue' branches that skip ADB-backed entries in find/prepare."""

    def test_find_registry_entry_skips_adb_backend(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Register an ADB-backed instance first, then the target redroid
        # instance.  _find_registry_entry must skip the ADB entry and
        # return the redroid entry.
        from beetroot import registry as reg

        # Register an ADB entry first (auto-allocates index 0), then
        # the redroid target (auto-allocates index 1).  The specific
        # indices don't matter — we're only verifying that
        # _find_registry_entry skips ADB entries and returns the redroid one.
        reg.add_allocating(
            "adb-device",
            backend=reg.AdbBackendConfig(serial="emulator-5554"),
        )
        src = _make_instance(tmp_path / "alpha")
        reg.add_allocating("alpha", backend=reg.RedroidBackendConfig(absolute_path=str(src)))
        archive = snapshot.snapshot(src, tmp_path / "out")
        manifest = snapshot.read_manifest(archive)
        assert manifest.name == "alpha"

    def test_prepare_destination_skips_adb_backend_in_collision_check(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # _prepare_destination's loop skips ADB-backed entries when
        # checking for cross-instance collisions.  Register an ADB
        # instance then try to restore a snapshot into an occupied dir.
        from beetroot import registry as reg

        src = _make_instance(tmp_path / "alpha")
        reg.add_allocating("alpha", backend=reg.RedroidBackendConfig(absolute_path=str(src)))
        archive = snapshot.snapshot(src, tmp_path / "out")
        reg.remove("alpha")

        # Register an ADB-backed instance (not a redroid instance) — the
        # collision guard must not fire for it.
        reg.add_allocating(
            "adb-device",
            backend=reg.AdbBackendConfig(serial="emulator-5554"),
        )

        # An occupied non-registered dir without force must raise the
        # non-empty error (NOT the cross-instance error, since the ADB
        # entry is skipped).
        occupied = tmp_path / "occupied"
        occupied.mkdir()
        (occupied / "some_file.txt").write_bytes(b"some content")

        with pytest.raises(snapshot.SnapshotError, match="--force"):
            snapshot.restore(archive, dest_name="beta", dest_path=occupied)

    def test_b7a_rollback_pre_existing_empty_target_skips_rmtree(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Branch coverage for the rollback path where created_dir=False
        # (target already existed, but was empty so _prepare_destination
        # returned without wiping it).  When extraction fails, rollback
        # must NOT try to rmtree — we didn't create the directory.
        archive = tmp_path / "abusive.tar.zst"
        _make_archive_with_absolute_symlink(archive, _VALID_MANIFEST_BYTES)

        target = tmp_path / "pre_existing_empty"
        target.mkdir()  # already exists, empty — created_dir will be False

        with pytest.raises((snapshot.SnapshotError, tarfile.FilterError)):
            snapshot.restore(archive, dest_name="beta", dest_path=target)

        # The pre-existing (empty) dir must still exist — rollback skips
        # rmtree when it didn't create the directory.
        assert target.exists()
        assert registry.get("beta") is None
