"""Issue #153 — legacy-registry migration must run under an EXCLUSIVE lock.

``registry._read()`` renames a legacy/corrupt registry aside
(``path.rename(<file>.bak)``) at two sites — the non-JSON branch and
the wrong-``version`` branch. But ``list_instances()`` / ``get()``
called ``_read()`` holding only a **shared** flock, which permitted
concurrent readers to perform that destructive mutation simultaneously.

The fix lives in ``list_instances`` (the only shared-lock caller): it
detects a legacy registry under the shared lock via the pure
``_needs_legacy_migration`` predicate, drops the shared lock, and
re-enters under an **exclusive** lock so the rename is serialised.
``_read`` itself is unchanged — it is also reached by the four
exclusive-lock writers and by ~20 tests that call it directly.

These tests pin: (1) the predicate mirrors ``_read``'s two
fall-through branches; (2) the migration rename fires only while a
``LOCK_EX`` flock is held; (3) N parallel readers hitting a legacy
registry at once produce exactly one ``.bak`` and leave a valid empty
v3 registry behind (no lost updates, no double-rename); (4) the
valid-v3 fast path still reads under the shared lock.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest

from beetroot import paths, registry


def _legacy_worker(xdg_config: str, xdg_cache: str) -> tuple[list[str], str | None]:
    # Module-level so multiprocessing (fork) can pickle it. Each worker
    # sets the XDG dirs explicitly (matching test_registry_race's
    # _spawn_worker) rather than relying on inherited env, then lists
    # the registry — which triggers the detect-then-escalate path.
    import traceback

    os.environ["XDG_CONFIG_HOME"] = xdg_config
    os.environ["XDG_CACHE_HOME"] = xdg_cache
    try:
        listed = registry.list_instances()
        return sorted(listed.keys()), None
    except Exception:
        return [], traceback.format_exc()


def _write_legacy_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "instances": {"old": {}}}))


def test_needs_legacy_migration_covers_all_four_predicates(
    isolated_registry: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    bad_json = tmp_path / "bad.json"
    wrong_version = tmp_path / "v1.json"
    valid_v3 = tmp_path / "v3.json"

    bad_json.write_text("not json at all {{{")
    wrong_version.write_text(json.dumps({"version": 1, "instances": {}}))
    valid_v3.write_text(json.dumps({"version": registry.SCHEMA_VERSION, "instances": {}}))

    # not-exists → False; JSON-error → True; version-mismatch → True;
    # version-match → False. All four branches of the predicate.
    assert registry._needs_legacy_migration(missing) is False
    assert registry._needs_legacy_migration(bad_json) is True
    assert registry._needs_legacy_migration(wrong_version) is True
    assert registry._needs_legacy_migration(valid_v3) is False


def test_migration_rename_runs_under_exclusive_lock(
    isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spy on flock to record the operation held at the moment _read's
    # destructive rename fires. The acceptance criterion is that the
    # rename happens under LOCK_EX, never LOCK_SH.
    reg_path = paths.user_registry_file()
    _write_legacy_registry(reg_path)

    real_flock = fcntl.flock
    held_ops: list[int] = []

    def _spy_flock(fd: int, operation: int) -> None:
        # Capture-and-delegate: track the lock operations as they are
        # taken/released so we can read the live state at rename time.
        held_ops.append(operation)
        real_flock(fd, operation)

    real_rename = Path.rename
    op_at_rename: list[int] = []

    def _spy_rename(self: Path, target: str | Path) -> Path:
        # The last lock-acquire op before any release reflects what is
        # currently held. LOCK_EX == 2, LOCK_SH == 1, LOCK_UN == 8.
        acquires = [op for op in held_ops if op in (fcntl.LOCK_EX, fcntl.LOCK_SH)]
        op_at_rename.append(acquires[-1])
        return real_rename(self, target)

    monkeypatch.setattr(fcntl, "flock", _spy_flock)
    monkeypatch.setattr(Path, "rename", _spy_rename)

    result = registry.list_instances()

    assert result == {}
    assert op_at_rename == [fcntl.LOCK_EX], (
        f"legacy-registry rename must fire under an exclusive lock, not {op_at_rename}"
    )
    # The migration produced exactly one backup holding the legacy doc.
    backup = reg_path.with_suffix(reg_path.suffix + ".bak")
    assert backup.exists()
    assert json.loads(backup.read_text())["version"] == 1
    # In the single-process winner case the legacy file is renamed away
    # and nothing re-creates it (the re-create-empty branch only fires
    # for a reader that locks AFTER the file is already gone).
    assert not reg_path.exists()


def test_parallel_readers_on_legacy_registry_single_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N processes call list_instances() against the SAME legacy
    # registry at once. The shared-lock readers detect the legacy
    # shape, escalate to the exclusive lock, and serialise the
    # rename: exactly one .bak file, no lost updates, no crash.
    xdg_config = tmp_path / "config"
    xdg_cache = tmp_path / "cache"
    reg_path = xdg_config / "beetroot" / "instances.json"
    _write_legacy_registry(reg_path)

    ctx = mp.get_context("fork")
    args = [(str(xdg_config), str(xdg_cache)) for _ in range(8)]
    with ctx.Pool(processes=8) as pool:
        results = pool.starmap(_legacy_worker, args)

    failures = [tb for _, tb in results if tb is not None]
    assert not failures, f"workers raised:\n{failures}"
    # Every reader saw an empty registry (the legacy rows can't migrate).
    assert all(keys == [] for keys, _ in results)

    # Exactly one .bak: the rename was serialised under the exclusive
    # lock, so only the single winner renamed — no lost-update where
    # two readers each rename a copy aside.
    backups = list(reg_path.parent.glob("instances.json.bak*"))
    assert len(backups) == 1, f"expected exactly one .bak, got {backups}"
    assert json.loads(backups[0].read_text())["version"] == 1

    # A follow-up read settles the registry into a known state and must
    # NOT produce a second backup — whatever the workers left behind
    # (a re-created empty v3, or a missing file) is no longer legacy.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    assert registry.list_instances() == {}
    assert len(list(reg_path.parent.glob("instances.json.bak*"))) == 1


def test_valid_v3_registry_fast_path_stays_shared(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fast path: a valid v3 registry is read under the SHARED lock
    # and never escalates. Assert the round-trip is correct and the
    # exclusive lock is never taken for a plain read.
    registry.add_allocating("alpha", tmp_path / "alpha")

    real_flock = fcntl.flock
    ops: list[int] = []

    def _spy_flock(fd: int, operation: int) -> None:
        ops.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", _spy_flock)
    listed = registry.list_instances()

    assert set(listed.keys()) == {"alpha"}
    assert fcntl.LOCK_SH in ops
    assert fcntl.LOCK_EX not in ops, "valid-v3 read must not escalate to an exclusive lock"
