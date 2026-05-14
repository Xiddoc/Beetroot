"""
instances.json — the registry mapping instance name to metadata.

Container status is NOT cached here; query Docker live so we can't lie.
Only assignment-time data lives in the registry: the port index and the
created-at timestamp.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

SCHEMA_VERSION = 1


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
    if not path.exists():
        return {"version": SCHEMA_VERSION, "instances": {}}
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def list_instances() -> dict[str, dict[str, Any]]:
    """Return all known instances as name → metadata. Empty if registry is missing."""
    return _read(paths.registry_file()).get("instances", {})  # type: ignore[no-any-return]


def get(name: str) -> dict[str, Any] | None:
    """Return the metadata dict for ``name``, or ``None`` if not registered."""
    return list_instances().get(name)


def used_indices() -> set[int]:
    """Return the set of port indices currently allocated to registered instances."""
    return {meta["index"] for meta in list_instances().values()}


def add(name: str, index: int) -> None:
    """
    Register a new instance in the registry under an exclusive file lock.

    Args:
        name: Instance name to register.
        index: Port index to assign to this instance.

    Raises:
        ValueError: If ``name`` is already in the registry.
    """
    path = paths.registry_file()
    with _locked(path):
        data = _read(path)
        if name in data["instances"]:
            raise ValueError(f"instance {name!r} already in registry")
        data["instances"][name] = {
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
    path = paths.registry_file()
    with _locked(path):
        data = _read(path)
        data["instances"].pop(name, None)
        _write(path, data)
