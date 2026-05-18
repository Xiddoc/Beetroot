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
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths, ports
from .config import load_yaml

SCHEMA_VERSION = 2


class RegistryError(RuntimeError):
    """Raised on registry consistency errors (e.g. unknown name lookups)."""


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[Path]:
    """Advisory file lock around the registry — guards parallel mutations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps({"version": SCHEMA_VERSION, "instances": {}}))
    with path.open("r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
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


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def list_instances() -> dict[str, dict[str, Any]]:
    """Return all known instances as name → metadata. Empty if registry is missing."""
    return _read(paths.user_registry_file()).get("instances", {})  # type: ignore[no-any-return]


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
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write(path, data)


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
    Return resolved ports for every registered instance.

    For each instance, loads its ``beetroot.yaml`` to pick up any
    ``ports:`` override block and merges it with the stride-of-10
    defaults derived from the registered index.

    Returns:
        A mapping ``instance_name → {"adb", "frida", "frida2"}`` covering
        every registered instance. Empty dict if the registry is empty.
    """
    out: dict[str, dict[str, int]] = {}
    for name, meta in list_instances().items():
        cfg = load_yaml(paths.instance_yaml(Path(meta["absolute_path"])))
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
