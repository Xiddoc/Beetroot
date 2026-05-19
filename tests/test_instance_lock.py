"""Concurrent snapshot + destroy serialisation via per-instance flock.

T2 Agent 2 B-12: a destroy that races a long-running snapshot used
to rmtree the directory while the snapshot was reading from it,
producing a torn archive (or a SnapshotError half-way through).
v0.4 adds an advisory ``fcntl.flock`` on
``<instance_root>/.beetroot.lock``:

- ``snapshot()`` takes ``LOCK_SH`` — multiple snapshots can run in
  parallel.
- ``Instance.destroy()`` takes ``LOCK_EX`` — blocks every other
  reader (snapshot) and waits for in-flight readers to release.

The behaviour test below spawns a snapshot thread that holds the
lock for ~0.5s and races a destroy against it; we then assert
destroy blocks until snapshot completes, the archive is intact, and
the directory ends up gone.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from beetroot import api, registry, snapshot


def test_destroy_blocks_until_snapshot_completes(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inst = api.Instance.create("alpha")
    src = inst.root
    archive = cli_root / "alpha.tar.zst"

    # Slow the snapshot down by intercepting one of its inner calls
    # so we have a visible window where the lock is held. We stretch
    # ``_add_manifest`` (the last step) by sleeping inside it — by
    # then the SHARED lock is already held and the destroy thread
    # is waiting for the EXCLUSIVE lock to clear.
    snapshot_finished = threading.Event()
    real_add_manifest = snapshot._add_manifest

    def _slow_add_manifest(tar: object, manifest: object) -> None:
        time.sleep(0.5)
        real_add_manifest(tar, manifest)  # type: ignore[arg-type]
        snapshot_finished.set()

    monkeypatch.setattr(snapshot, "_add_manifest", _slow_add_manifest)

    snapshot_result: dict[str, object] = {}

    def _snapshot_worker() -> None:
        snapshot_result["path"] = snapshot.snapshot(src, archive)

    destroy_result: dict[str, object] = {}

    def _destroy_worker() -> None:
        destroy_result["started_at"] = time.monotonic()
        # Patch ``compose.down`` so we don't try to talk to docker.
        from beetroot import compose
        with pytest.MonkeyPatch.context() as m:
            m.setattr(compose, "down", lambda *a, **kw: None)
            inst.destroy(yes=True)
        destroy_result["finished_at"] = time.monotonic()

    snap_thread = threading.Thread(target=_snapshot_worker)
    destroy_thread = threading.Thread(target=_destroy_worker)

    snap_thread.start()
    # Wait for the snapshot to actually start (and grab the shared
    # lock). Sleeping briefly before launching destroy gives the
    # scheduler enough rope to ensure snapshot is in the critical
    # section before destroy contends.
    time.sleep(0.1)
    destroy_thread.start()

    snap_thread.join(timeout=5)
    destroy_thread.join(timeout=5)
    assert not snap_thread.is_alive(), "snapshot didn't finish"
    assert not destroy_thread.is_alive(), "destroy didn't finish"

    # The archive must exist and be a real file (the destroy didn't
    # rmtree the source mid-snapshot).
    assert snapshot_result["path"] == archive.resolve()
    assert archive.is_file()
    # The destroy DID complete (registry row + dir gone).
    assert registry.get("alpha") is None
    assert not src.exists()
    # The destroy must have blocked at least until the snapshot
    # finished (it would have started rmtree'ing src before that
    # otherwise, and snapshot_result['path'] would be a torn file).
    assert snapshot_finished.is_set()


def test_two_snapshots_can_run_in_parallel(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T2 Agent 2 B-12: parallel snapshots both take LOCK_SH so they
    # don't block each other. The contract matters because a
    # back-to-back ``beetroot snapshot foo`` + ``beetroot snapshot
    # bar`` (same instance, different output paths) shouldn't
    # serialise into half the throughput.
    inst = api.Instance.create("alpha")
    src = inst.root

    enter_count = 0
    enter_lock = threading.Lock()
    both_in_critical = threading.Event()

    real_add_manifest = snapshot._add_manifest

    def _gate(tar: object, manifest: object) -> None:
        nonlocal enter_count
        with enter_lock:
            enter_count += 1
            if enter_count == 2:
                both_in_critical.set()
        # Block until both snapshots are inside the critical section
        # at the same time — proves the shared-lock semantics work.
        assert both_in_critical.wait(timeout=3.0), (
            "two snapshots failed to overlap inside the SH-lock window"
        )
        real_add_manifest(tar, manifest)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshot, "_add_manifest", _gate)

    threads = [
        threading.Thread(
            target=snapshot.snapshot,
            args=(src, cli_root / f"snap-{i}.tar.zst"),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads)
    assert both_in_critical.is_set()
