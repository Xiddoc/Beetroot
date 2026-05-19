"""
Cross-instance registry mapping instance name to metadata.

The registry is a single user-global JSON file (at
``$XDG_CONFIG_HOME/beetroot/instances.json``, defaulting to
``~/.config/beetroot/instances.json``) that records every instance on the
host regardless of where on disk its directory lives.

Container status is NOT cached here; query Docker live so we can't lie.
Only assignment-time data lives in the registry: the discriminated-union
backend config, the allocated port index, and the created-at timestamp.

The on-disk schema is now (v3) defined by :class:`RegistryFile`: a
strongly-typed pydantic model that round-trips via
``model_validate_json`` / ``model_dump_json``. Backend configs are a
discriminated union over ``kind`` — ``RedroidBackendConfig`` (the
container-managed backend that v0.3 was hard-coded for) and
``AdbBackendConfig`` (the real-device backend that T5 will add). Third
parties register additional backends via the
``beetroot.backends`` entry-point group; their configs validate against
their own pydantic models and live in their own packages.
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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import paths, ports
from .config import load_yaml

SCHEMA_VERSION = 3

# Module-level flag so the "v0.2 registry at PWD" hint fires once per
# process. The check is cheap, but spamming the hint on every verb
# call would drown out the actual command output.
_V02_HINT_PRINTED = False

# Module-level flag so the legacy-registry migration hint fires once
# per process. _read() backs up the legacy file and emits a fresh v3
# document; this flag dedupes the stderr nudge so cascading reads
# (``beetroot ls`` → ``all_resolved_ports`` → ...) don't print it once
# per call.
_LEGACY_HINT_PRINTED = False


class RegistryError(RuntimeError):
    """Raised on registry consistency errors (e.g. unknown name lookups)."""


class RedroidBackendConfig(BaseModel):
    """
    Backend config for the v0.3-shaped Redroid-container backend.

    Attributes:
        kind: Discriminator tag — always ``"redroid"``.
        absolute_path: Absolute path to the instance directory (the
            directory containing ``beetroot.yaml``).
        stealth_paths: Reserved slot for the v0.4 stealth-posture
            plumbing. Empty in v0.4; v0.5's PR1 populates it with the
            randomized container-path layout produced by
            ``Instance.create``. Snapshot / restore round-trips the
            blob so a v0.5 snapshot lands cleanly on a v0.4 host.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    kind: Literal["redroid"] = "redroid"
    absolute_path: str
    stealth_paths: dict[str, str] = Field(default_factory=dict)


class AdbBackendConfig(BaseModel):
    """
    Backend config for the real-device-over-ADB backend that lands in T5.

    The full :class:`AdbDevice` class arrives in T5; T1 only ships the
    config model so the discriminated union in :class:`InstanceMeta`
    has both arms in place.

    Attributes:
        kind: Discriminator tag — always ``"adb"``.
        serial: The adb serial / endpoint identifier (e.g.
            ``"emulator-5554"`` or ``"192.168.1.10:5555"``). Passed
            verbatim to ``adb -s <serial>`` invocations.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    kind: Literal["adb"] = "adb"
    serial: str


# In-tree backends are pinned in the union; third-party backends
# register their concrete class at runtime via the entry-point
# mechanism in :mod:`beetroot.backends`. Their config validates against
# a *separate* pydantic model that they own — that model never goes
# through this union and so doesn't need to be registered here.
BackendConfig = Annotated[
    RedroidBackendConfig | AdbBackendConfig,
    Field(discriminator="kind"),
]


class InstanceMeta(BaseModel):
    """
    Per-instance metadata stored in the registry.

    Replaces the v0.3 ``dict[str, Any]`` payload. Every consumer that
    used to subscript ``meta["absolute_path"]`` now reaches through
    ``meta.backend.absolute_path`` (for redroid) or ``meta.backend.serial``
    (for adb).

    Attributes:
        backend: Discriminated-union backend config (``kind: "redroid"``
            or ``kind: "adb"``).
        index: Stride-of-10 port index allocated to this instance.
        created_at: ISO-8601 UTC timestamp when the entry was added.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    backend: BackendConfig
    index: int
    created_at: datetime


class RegistryFile(BaseModel):
    """
    On-disk shape of ``instances.json``.

    The discriminator-bearing ``version`` field gates schema
    compatibility — :func:`_read` falls through to the legacy-migration
    path for any mismatch.

    Attributes:
        version: Always ``3`` for this Beetroot release.
        instances: Mapping of instance name → :class:`InstanceMeta`.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[3] = 3
    instances: dict[str, InstanceMeta] = Field(default_factory=dict)


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
                path.write_text(RegistryFile().model_dump_json(indent=2))
            yield path
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> RegistryFile:
    """
    Read and parse the registry, auto-handling legacy-schema migrations.

    A v1 / v2 registry is renamed to ``<file>.bak`` and an empty v3
    registry is returned. The user must re-register their instances
    with ``beetroot register <path>`` (paths can't be auto-migrated
    because v1 had no per-instance absolute path, and v2's payload was
    a free-form dict that won't survive validation against
    :class:`InstanceMeta`).
    """
    global _LEGACY_HINT_PRINTED  # noqa: PLW0603
    _check_v02_registry_at_cwd(path)
    if not path.exists():
        return RegistryFile()
    raw_text = path.read_text()
    try:
        return RegistryFile.model_validate_json(raw_text)
    except ValidationError:
        # Either an outright legacy registry (v1 / v2 / no version key)
        # or a v3-shaped doc that fails strict validation. In either
        # case we back the file up rather than risk corrupting it, and
        # return a fresh empty registry so the caller can continue.
        try:
            parsed_version = json.loads(raw_text).get("version")
        except (json.JSONDecodeError, AttributeError):
            parsed_version = None
        backup = path.with_suffix(path.suffix + ".bak")
        path.rename(backup)
        if not _LEGACY_HINT_PRINTED:
            print(  # noqa: T201  # stderr migration hint — typer.echo is unavailable from non-CLI callers
                f"[beetroot] registry at {path} was schema "
                f"v{parsed_version!r}; renamed to {backup.name}. "
                f"Re-register your instances with "
                f"`beetroot register <path>`.",
                file=sys.stderr,
            )
            _LEGACY_HINT_PRINTED = True
        return RegistryFile()


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
    print(  # noqa: T201  # stderr migration hint — typer.echo is unavailable from non-CLI callers
        f"[beetroot] detected v0.2 registry at {candidate} — move it to "
        f"{xdg_path} (or re-register each instance with "
        f"'beetroot register <path>').",
        file=sys.stderr,
    )
    _V02_HINT_PRINTED = True


def _write(path: Path, data: RegistryFile) -> None:
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
        tmp.write_text(data.model_dump_json(indent=2))
        tmp.replace(path)
    finally:
        # If the replace happened, this is a no-op (tmp no longer
        # exists); on a failure path, this cleans up the orphan.
        if tmp.exists():
            tmp.unlink()


def list_instances() -> dict[str, InstanceMeta]:
    """Return all known instances as name → metadata. Empty if registry is missing."""
    path = paths.user_registry_file()
    if not path.exists():
        # Fast-path: no registry yet → nothing to read, no need to
        # touch the lock file at all.
        _check_v02_registry_at_cwd(path)
        return {}
    with _locked(path, exclusive=False):
        return _read(path).instances


def get(name: str) -> InstanceMeta | None:
    """Return the :class:`InstanceMeta` for ``name``, or ``None`` if not registered."""
    return list_instances().get(name)


def used_indices() -> set[int]:
    """Return the set of port indices currently allocated to registered instances."""
    return {meta.index for meta in list_instances().values()}


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
        if name in data.instances:
            raise ValueError(f"instance {name!r} already in registry")
        data.instances[name] = InstanceMeta(
            backend=RedroidBackendConfig(absolute_path=str(absolute_path)),
            index=index,
            created_at=datetime.now(UTC),
        )
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
        if name in data.instances:
            raise ValueError(f"instance {name!r} already in registry")
        used = {meta.index for meta in data.instances.values()}
        index = ports.lowest_free_index(used)
        data.instances[name] = InstanceMeta(
            backend=RedroidBackendConfig(absolute_path=str(absolute_path)),
            index=index,
            created_at=datetime.now(UTC),
        )
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
        data.instances.pop(name, None)
        _write(path, data)


def instance_path(name: str) -> Path:
    """
    Return the absolute path to an instance's directory, from the registry.

    Only redroid-kind instances have a meaningful on-disk root; adb-kind
    instances raise :class:`RegistryError` because they're not backed by
    a directory.

    Args:
        name: Instance name.

    Returns:
        The path recorded under the backend config's ``absolute_path``
        when the instance was registered.

    Raises:
        RegistryError: If ``name`` is not in the registry, or if the
            registered backend is not directory-backed.
    """
    meta = get(name)
    if meta is None:
        raise RegistryError(f"unknown instance {name!r}; not in registry")
    backend = meta.backend
    if not isinstance(backend, RedroidBackendConfig):
        raise RegistryError(
            f"instance {name!r} is a {backend.kind!r} backend; "
            "it has no on-disk directory"
        )
    return Path(backend.absolute_path)


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
        if not isinstance(meta.backend, RedroidBackendConfig):
            continue
        try:
            cfg = load_yaml(paths.instance_yaml(Path(meta.backend.absolute_path)))
        except FileNotFoundError:
            continue
        out[name] = ports.resolve_ports(meta.index, cfg.ports)
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
