"""F6 guardrail — parallel ``Instance.create`` does not co-allocate ports.

The original ``Instance.create`` did:

  1. ``ports.lowest_free_index(registry.used_indices())``  ← read
  2. ``ports.resolve_ports(index, cfg.ports)``             ← compute
  3. ``_check_port_collisions(name, new_ports)``           ← read
  4. ``registry.add(name, root, index)``                   ← write

The registry's ``fcntl.flock`` only guards step 4. Two parallel
create() calls could race steps 1-3 simultaneously, both pick the
lowest free index, and both write — silently co-allocating the same
stride slot to two instances. Users only see the failure at
``docker compose up`` time when the second bind fails.

The post-CR fix collapses 1+4 into a single critical section via
``registry.add_allocating``. This test pins the contract: spawn 5
processes in parallel, each registering a different instance, and
assert every instance got a unique index.

We use ``multiprocessing`` (not threads) because ``fcntl.flock`` is
per-fd-table; threads in the same process share the fd-table and
trivially serialize.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from beetroot import api, registry


def _spawn_worker(
    xdg_config: str, xdg_cache: str, target_root: str, name: str
) -> tuple[str, int | None, str | None]:
    # Module-level helper so multiprocessing can pickle it.
    import traceback

    os.environ["XDG_CONFIG_HOME"] = xdg_config
    os.environ["XDG_CACHE_HOME"] = xdg_cache
    try:
        # Stub frida_download.download so the worker doesn't hit the network.
        from beetroot import frida_download

        def _fake_download(
            version: str,
            *,
            expected_sha256: str | None = None,
        ) -> Path:
            out = frida_download.cached_binary(version)
            out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                out.write_bytes(b"fake-frida")
                out.chmod(0o755)
            return out

        frida_download.download = _fake_download
        inst = api.Instance.create(name, path=Path(target_root) / name)
        return name, inst.index, None
    except Exception:
        return name, None, traceback.format_exc()


def test_parallel_create_allocates_distinct_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_config = tmp_path / "config"
    xdg_cache = tmp_path / "cache"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Use "fork" so the workers inherit the test process's monkeypatches.
    # macOS defaults to "spawn"; we don't run CI on macOS for this test.
    ctx = mp.get_context("fork")
    names = [f"phone-{i}" for i in range(5)]
    args = [(str(xdg_config), str(xdg_cache), str(workspace), name) for name in names]
    with ctx.Pool(processes=5) as pool:
        results = pool.starmap(_spawn_worker, args)

    failures = [(n, tb) for n, _, tb in results if tb is not None]
    assert not failures, f"workers raised:\n{failures}"
    indices = [idx for _, idx, _ in results]
    assert len(set(indices)) == len(indices), f"parallel create co-allocated indices: {results}"
    # And the registry, viewed from the test process's XDG dirs,
    # reflects every successful create.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    listed = registry.list_instances()
    assert set(listed.keys()) == set(names)
    listed_indices = {meta.index for meta in listed.values()}
    assert len(listed_indices) == len(names)


def test_add_allocating_is_atomic_with_lowest_free_index(
    isolated_registry: Path, tmp_path: Path
) -> None:
    # Sanity test for the new add_allocating helper: under a single
    # process, repeated calls produce strictly-increasing-by-lowest-free
    # indices and never collide.
    alloc1 = registry.add_allocating("alpha", tmp_path / "alpha")
    alloc2 = registry.add_allocating("bravo", tmp_path / "bravo")
    alloc3 = registry.add_allocating("charlie", tmp_path / "charlie")
    assert {alloc1, alloc2, alloc3} == {0, 1, 2}


def test_add_allocating_reuses_freed_slot(isolated_registry: Path, tmp_path: Path) -> None:
    # Same lowest-free-index semantic as Instance.create: a removed
    # name's slot is reused by the next allocation.
    registry.add_allocating("alpha", tmp_path / "alpha")
    registry.add_allocating("bravo", tmp_path / "bravo")
    registry.remove("alpha")
    next_alloc = registry.add_allocating("charlie", tmp_path / "charlie")
    assert next_alloc == 0


def test_add_allocating_refuses_duplicate(isolated_registry: Path, tmp_path: Path) -> None:
    registry.add_allocating("alpha", tmp_path / "alpha")
    with pytest.raises(ValueError, match="already in registry"):
        registry.add_allocating("alpha", tmp_path / "alpha-2")


def test_write_cleans_up_orphan_tmp_on_failure(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If os.replace fails after the tmp is written, _write must clean
    # up the orphan tmp file so a future run doesn't leak garbage
    # next to the registry.
    registry.add_allocating("alpha", tmp_path / "alpha")
    real_replace = Path.replace

    def _flaky_replace(self: Path, target: Path) -> Path:
        # Trigger after the tmp file is created.
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", _flaky_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        registry.add_allocating("bravo", tmp_path / "bravo")
    monkeypatch.setattr(Path, "replace", real_replace)
    # The registry file is intact (alpha still there).
    instances = registry.list_instances()
    assert "alpha" in instances
    # No leftover *.tmp file alongside it.
    reg_dir = tmp_path / "config" / "beetroot"
    if reg_dir.is_dir():
        tmps = list(reg_dir.glob("*.tmp"))
        assert tmps == [], f"orphan tmp files left behind: {tmps}"
