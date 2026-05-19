"""
Cross-instance registry mapping instance name to metadata.

The registry is a single user-global JSON file (at
``$XDG_CONFIG_HOME/beetroot/instances.json``, defaulting to
``~/.config/beetroot/instances.json``) that records every instance on the
host regardless of where on disk its directory lives.

Container status is NOT cached here; query Docker live so we can't lie.
Only assignment-time data lives in the registry: the absolute path to the
instance directory, its allocated port index, and the created-at timestamp.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import paths, ports
from .config import load_yaml

SCHEMA_VERSION = 2

# Module-level flag so the "v0.2 registry at PWD" hint fires once per
# process. The check is cheap, but spamming the hint on every verb
# call would drown out the actual command output.
_V02_HINT_PRINTED = False


class RegistryError(RuntimeError):
    """Raised on registry consistency errors (e.g. unknown name lookups)."""


@contextlib.contextmanager
def _locked(path: Path, *, exclusive: bool = True) -> Iterator[Path]:
    """
    Advisory file lock around the registry — guards parallel mutations.

    The flock is held against a SEPARATE lock file (``<path>.lock``),
    never the registry file itself. ``_write`` replaces the registry
    file via an atomic rename, which would change ``path``'s inode
    out from under any flock held on the registry-file fd — defeating
    mutual exclusion. The dedicated lock file is never renamed, so
    every holder's flock is on the same inode.

    Readers (``list_instances``, ``get``) take a shared lock so they
    can run in parallel with other readers but block on a sibling
    holding an exclusive lock — preventing them from observing the
    truncation window between ``_write``'s tmp-write and atomic
    replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_suffix(path.suffix + ".lock")
    with lock_file.open("a+") as f:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(f.fileno(), flag)
        try:
            if exclusive and not path.exists():
                # First-time initialisation under the lock, so two
                # parallel creators don't both write the empty doc.
                # We're allowed to use the non-atomic write here
                # because no concurrent reader can have observed
                # ``path`` yet (it didn't exist a moment ago, and
                # we hold the exclusive lock).
                path.write_text(json.dumps(
                    {"version": SCHEMA_VERSION, "instances": {}}
                ))
            yield path
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any]:
    """
    Read and parse the registry, auto-handling the v1 → v2 migration.

    A v1 registry is renamed to ``<file>.bak`` and an empty v2 registry is
    returned. The user must re-register their instances with
    ``beetroot register <path>`` (paths can't be auto-migrated because
    v1 had no per-instance absolute path).
    """
    _check_v02_registry_at_cwd(path)
    if not path.exists():
        return {"version": SCHEMA_VERSION, "instances": {}}
    data: dict[str, Any] = json.loads(path.read_text())
    if data.get("version") != SCHEMA_VERSION:
        backup = path.with_suffix(path.suffix + ".bak")
        path.rename(backup)
        print(
            f"[beetroot] registry at {path} was schema v{data.get('version')!r}; "
            f"renamed to {backup.name}. Re-register your instances with "
            f"`beetroot register <path>`."
        )
        return {"version": SCHEMA_VERSION, "instances": {}}
    return data


def _check_v02_registry_at_cwd(xdg_path: Path) -> None:
    """
    Surface a hint if a v0.2-shaped instances.json sits at $PWD.

    v0.2 wrote ``instances.json`` at the repo root. v0.3 moved it to
    ``$XDG_CONFIG_HOME/beetroot/instances.json``. Auto-moving silently
    would break a user who keeps the v0.2 file under version control
    or in a different repo; the contract is to surface the situation
    once per process and let the user pick the migration path.
    """
    global _V02_HINT_PRINTED  # noqa: PLW0603
    if _V02_HINT_PRINTED:
        return
    candidate = Path.cwd() / "instances.json"
    if not candidate.is_file():
        return
    if xdg_path.is_file() and xdg_path.stat().st_size > 0:
        return
    try:
        data = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError):
        return
    # v0.2's instances.json was a flat ``{name: meta}`` mapping (no
    # ``version`` / ``instances`` wrapper). That shape is the
    # discriminator from v0.3's wrapped layout.
    if not isinstance(data, dict):
        return
    is_v1_shape = "version" not in data and "instances" not in data
    if not is_v1_shape:
        return
    print(
        f"[beetroot] detected v0.2 registry at {candidate} — move it to "
        f"{xdg_path} (or re-register each instance with "
        f"'beetroot register <path>').",
        file=sys.stderr,
    )
    _V02_HINT_PRINTED = True


def _write(path: Path, data: dict[str, Any]) -> None:
    # Atomic replace via a per-call unique tmp file: write the new
    # content to a sibling whose name includes pid+uuid so parallel
    # writers don't trample each other's tmp file, then os.replace
    # it on top of ``path``. Without the atomic-replace, concurrent
    # readers (list_instances, get) would occasionally observe
    # ``path`` in its truncated-but-not-yet-written state and raise
    # JSONDecodeError. Without the unique tmp name, two writers in
    # parallel processes would both write to the same tmp and the
    # second one's os.replace would FileNotFoundError after the
    # first process renamed the tmp out from under it.
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(path)
    finally:
        # If the replace happened, this is a no-op (tmp no longer
        # exists); on a failure path, this cleans up the orphan.
        if tmp.exists():
            tmp.unlink()


def list_instances() -> dict[str, dict[str, Any]]:
    """Return all known instances as name → metadata. Empty if registry is missing."""
    path = paths.user_registry_file()
    if not path.exists():
        # Fast-path: no registry yet → nothing to read, no need to
        # touch the lock file at all.
        _check_v02_registry_at_cwd(path)
        return {}
    with _locked(path, exclusive=False):
        return _read(path).get("instances", {})  # type: ignore[no-any-return]


def get(name: str) -> dict[str, Any] | None:
    """Return the metadata dict for ``name``, or ``None`` if not registered."""
    return list_instances().get(name)


def used_indices() -> set[int]:
    """Return the set of port indices currently allocated to registered instances."""
    return {meta["index"] for meta in list_instances().values()}


def add(name: str, absolute_path: Path, index: int) -> None:
    """
    Register a new instance in the registry under an exclusive file lock.

    Args:
        name: Instance name to register.
        absolute_path: Absolute path to the instance directory (the one
            containing ``beetroot.yaml``).
        index: Port index to assign to this instance.

    Raises:
        ValueError: If ``name`` is already in the registry.
    """
    path = paths.user_registry_file()
    with _locked(path):
        data = _read(path)
        if name in data["instances"]:
            raise ValueError(f"instance {name!r} already in registry")
        data["instances"][name] = {
            "absolute_path": str(absolute_path),
            "index": index,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write(path, data)


def add_allocating(name: str, absolute_path: Path) -> int:
    """
    Atomically allocate the lowest free port index AND register ``name``.

    Without this critical section, two parallel ``Instance.create`` calls
    could read ``used_indices()`` simultaneously, both get the same
    lowest-free index, and then both write to the registry — silently
    co-allocating the same port to two instances. The user only sees
    the failure at ``docker compose up`` time, when the second
    instance's bind fails.

    Args:
        name: Instance name to register.
        absolute_path: Absolute path to the instance directory.

    Returns:
        The port index that was allocated.

    Raises:
        ValueError: If ``name`` is already in the registry.
    """
    path = paths.user_registry_file()
    with _locked(path):
        data = _read(path)
        if name in data["instances"]:
            raise ValueError(f"instance {name!r} already in registry")
        used = {meta["index"] for meta in data["instances"].values()}
        index = ports.lowest_free_index(used)
        data["instances"][name] = {
            "absolute_path": str(absolute_path),
            "index": index,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write(path, data)
        return index


def remove(name: str) -> None:
    """
    Remove an instance from the registry under an exclusive file lock.

    A no-op if ``name`` is not present.

    Args:
        name: Instance name to deregister.
    """
    path = paths.user_registry_file()
    with _locked(path):
        data = _read(path)
        data["instances"].pop(name, None)
        _write(path, data)


def instance_path(name: str) -> Path:
    """
    Return the absolute path to an instance's directory, from the registry.

    Args:
        name: Instance name.

    Returns:
        The path recorded under ``absolute_path`` when the instance was
        registered.

    Raises:
        RegistryError: If ``name`` is not in the registry.
    """
    meta = get(name)
    if meta is None:
        raise RegistryError(f"unknown instance {name!r}; not in registry")
    return Path(meta["absolute_path"])


def all_resolved_ports() -> dict[str, dict[str, int]]:
    """
    Return resolved ports for every healthy registered instance.

    For each instance, loads its ``beetroot.yaml`` to pick up any
    ``ports:`` override block and merges it with the stride-of-10
    defaults derived from the registered index. Orphan entries
    (registered names whose ``beetroot.yaml`` is gone) are silently
    skipped — they're surfaced via ``Manager.list_orphans()`` and the
    ``beetroot ls`` skip-line, not via cascading failures in
    ``create``/``register``/``apply``/``restore``.

    Returns:
        A mapping ``instance_name → {"adb", "frida", "frida2"}`` covering
        every registered instance whose on-disk YAML still exists. Empty
        dict if the registry is empty or every entry is an orphan.
    """
    out: dict[str, dict[str, int]] = {}
    for name, meta in list_instances().items():
        try:
            cfg = load_yaml(paths.instance_yaml(Path(meta["absolute_path"])))
        except FileNotFoundError:
            continue
        out[name] = ports.resolve_ports(meta["index"], cfg.ports)
    return out


def find_port_collision(
    new_ports: dict[str, int],
    others: dict[str, dict[str, int]],
) -> tuple[int, str, str] | None:
    """
    Search ``others`` for any port that collides with ``new_ports``.

    Args:
        new_ports: Resolved port dict for the instance being staged
            (keys: ``adb``, ``frida``, ``frida2``).
        others: Mapping of other-instance-name → resolved port dict.
            The caller is responsible for excluding the staging instance
            itself from this mapping.

    Returns:
        ``(port, conflicting_instance, port_kind)`` on the first collision
        found — ``port_kind`` is the *new* instance's key (``adb`` /
        ``frida`` / ``frida2``). Returns ``None`` if no collision exists.
    """
    for kind, port in new_ports.items():
        for other_name, other_ports in others.items():
            if port in other_ports.values():
                return port, other_name, kind
    return None
