"""
Cross-instance registry mapping instance name to metadata.

The registry is a single user-global JSON file (at
``$XDG_CONFIG_HOME/beetroot/instances.json``, defaulting to
``~/.config/beetroot/instances.json``) that records every instance on the
host regardless of where on disk its directory lives.

Container status is NOT cached here; query Docker live so we can't lie.
Only assignment-time data lives in the registry: the open backend config,
the allocated port index, and the created-at timestamp.

The on-disk schema is now (v3) defined by :class:`RegistryFile`: a
strongly-typed pydantic model that round-trips via
``model_validate_json`` / ``model_dump_json``. Backend configs use an
**open registration-based** scheme: in-tree backends (``redroid``,
``adb``) are pre-registered; third-party backends register their own
:class:`BackendConfigBase` subclass via :func:`register_backend_config`.
An unknown ``kind`` is preserved as an opaque :class:`UnresolvedBackendConfig`
that round-trips byte-for-byte — it is never silently wiped. Only a genuinely
corrupt envelope (bad JSON / missing version) triggers ``.bak``-and-empty.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from . import console, paths, ports
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
    """
    Raised on registry consistency errors (e.g. unknown name lookups).
    """


class BackendConfigBase(BaseModel):
    """
    Base class for all backend config models.

    Every in-tree and third-party backend config must subclass this and
    pin a :class:`~typing.Literal` ``kind`` discriminator.  The base
    carries ``extra="forbid"`` and ``frozen=False`` to match the
    existing in-tree config models.

    Attributes:
        kind: Backend kind discriminator string (e.g. ``"redroid"``,
            ``"adb"``).  Subclasses pin a ``Literal[...]`` value.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    kind: str


class RedroidBackendConfig(BackendConfigBase):
    """
    Backend config for the v0.3-shaped Redroid-container backend.

    Attributes:
        kind: Discriminator tag — always ``"redroid"``.
        absolute_path: Absolute path to the instance directory (the
            directory containing ``beetroot.yaml``).
        stealth_paths: Reserved slot for the v0.4 stealth-posture
            plumbing. Empty in v0.4; a future release's stealth work
            populates it with the randomized container-path layout
            produced by ``Instance.create``. Snapshot / restore
            round-trips the blob so a future snapshot lands cleanly on
            a v0.4 host.
    """

    kind: Literal["redroid"] = "redroid"  # type: ignore[mutable-override]  # Literal narrows the base str; required for pydantic discriminated dispatch
    absolute_path: str
    stealth_paths: dict[str, str] = Field(default_factory=dict)


class AdbBackendConfig(BackendConfigBase):
    """
    Backend config for the real-device-over-ADB backend (shipped in v0.4, T5).

    Attributes:
        kind: Discriminator tag — always ``"adb"``.
        serial: The adb serial / endpoint identifier (e.g.
            ``"emulator-5554"`` or ``"192.168.1.10:5555"``). Passed
            verbatim to ``adb -s <serial>`` invocations.
    """

    kind: Literal["adb"] = "adb"  # type: ignore[mutable-override]  # Literal narrows the base str; required for pydantic discriminated dispatch
    serial: str


class VmBackendConfig(BackendConfigBase):
    """
    Backend config for the QEMU micro-VM backend (``binder: vm``).

    A directory-backed backend, like :class:`RedroidBackendConfig`: the
    instance directory holds the ``beetroot.yaml`` (which carries the
    ``vm:`` tunables) plus the QEMU pidfile written at ``up`` time. The
    guest kernel + rootfs images are host paths referenced from the config
    / settings, not stored in the registry.

    Attributes:
        kind: Discriminator tag — always ``"vm"``.
        absolute_path: Absolute path to the instance directory (the
            directory containing ``beetroot.yaml``).
    """

    kind: Literal["vm"] = "vm"  # type: ignore[mutable-override]  # Literal narrows the base str; required for pydantic discriminated dispatch
    absolute_path: str


class UnresolvedBackendConfig(BackendConfigBase):
    """
    Opaque placeholder for an unknown backend kind.

    When :func:`_read` encounters a ``kind`` that is not registered in
    :data:`_BACKEND_CONFIG_REGISTRY`, the raw dict is preserved here so
    the row survives a read/write cycle byte-for-byte.  Callers that
    need to operate on the backend (e.g. :meth:`Manager.resolve`) will
    get an :class:`~beetroot.api.InstanceNotFoundError` with an
    "install the package providing kind X" message.

    Attributes:
        kind: The unknown kind discriminator (preserved verbatim).
        _raw: The full raw dict from the JSON file, including all
            fields that the registered model would validate.
    """

    model_config = ConfigDict(extra="allow", frozen=False)

    _raw: dict[str, object]

    def __init__(self, kind: str, raw: dict[str, object]) -> None:
        """
        Wrap an unknown-kind row for opaque round-tripping.

        Args:
            kind: The unrecognised kind string.
            raw: The full backend sub-dict from the registry JSON,
                preserved verbatim for :func:`_write` to re-emit.
        """
        super().__init__(kind=kind)
        object.__setattr__(self, "_raw", raw)


# Open backend-config registry: kind → pydantic model class.
# Third parties call :func:`register_backend_config` to add their arm.
_BACKEND_CONFIG_REGISTRY: dict[str, type[BackendConfigBase]] = {
    "redroid": RedroidBackendConfig,
    "adb": AdbBackendConfig,
    "vm": VmBackendConfig,
}

# Type alias for back-compat: callers that imported ``BackendConfig``
# from this module (the discriminated-union annotation) continue to
# work.  The open-union equivalent is ``BackendConfigBase``.
BackendConfig = BackendConfigBase


def register_backend_config(cls: type[BackendConfigBase]) -> None:
    """
    Register a third-party backend config class under its ``kind`` discriminator.

    The class must be a :class:`BackendConfigBase` subclass with a
    ``kind`` field pinned to a ``Literal[...]``.  The kind is derived
    from ``cls.__fields__["kind"].default`` — the Literal's sole value.

    This must be called **before** any :func:`_read` that could encounter
    the corresponding rows in the registry file.  The best place is a
    package's ``__init__.py`` or entry-point loader.

    Args:
        cls: The pydantic model class to register.

    Raises:
        ValueError: If the class has no pinned ``kind`` default, or if
            the kind is already registered to a different class.
    """
    # Derive the kind from the model's field default.
    kind_field = cls.model_fields.get("kind")
    if kind_field is None or kind_field.default is None:
        raise ValueError(
            f"{cls.__name__} must have a ``kind`` field with a Literal default "
            "(e.g. ``kind: Literal['mykind'] = 'mykind'``)"
        )
    kind = kind_field.default
    existing = _BACKEND_CONFIG_REGISTRY.get(kind)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"backend config kind {kind!r} is already registered to "
            f"{existing.__name__}; cannot overwrite with {cls.__name__}"
        )
    _BACKEND_CONFIG_REGISTRY[kind] = cls


def _dump_backend_config(cfg: BackendConfigBase) -> dict[str, object]:
    """
    Serialize a backend config to its raw dict representation.

    This is the single authoritative path for opaque-row serialization —
    both :meth:`InstanceMeta._serialize_backend` (the pydantic
    field_serializer) and :func:`_registry_to_json` (the live write path)
    delegate here so data-loss behaviour has exactly one implementation.

    Args:
        cfg: The backend config to serialize.

    Returns:
        For :class:`UnresolvedBackendConfig`, the original raw dict
        preserved verbatim.  For all registered types, the pydantic
        ``model_dump`` output.
    """
    if isinstance(cfg, UnresolvedBackendConfig):
        return cfg._raw  # noqa: SLF001  # internal slot; the raw dict IS the serialized form
    return cfg.model_dump()


def _parse_backend_config(raw: dict[str, object]) -> BackendConfigBase:
    """
    Parse a raw backend sub-dict into the appropriate config class.

    Looks up ``raw["kind"]`` in :data:`_BACKEND_CONFIG_REGISTRY`.
    Registered kinds are validated against their model; unknown kinds
    are wrapped in :class:`UnresolvedBackendConfig` and preserved
    verbatim so the row round-trips without data loss.

    Args:
        raw: The raw dict from the registry JSON's ``backend`` field.

    Returns:
        A :class:`BackendConfigBase` subclass instance.
    """
    kind_val = raw.get("kind", "")
    kind = kind_val if isinstance(kind_val, str) else ""
    cls = _BACKEND_CONFIG_REGISTRY.get(kind)
    if cls is None:
        return UnresolvedBackendConfig(kind=kind, raw=raw)
    return cls.model_validate(raw)


class InstanceMeta(BaseModel):
    """
    Per-instance metadata stored in the registry.

    Replaces the v0.3 ``dict[str, Any]`` payload. Every consumer that
    used to subscript ``meta["absolute_path"]`` now reaches through
    ``meta.backend.absolute_path`` (for redroid) or ``meta.backend.serial``
    (for adb).

    Attributes:
        backend: Backend config (open-union :class:`BackendConfigBase`).
        index: Stride-of-10 port index allocated to this instance.
        created_at: ISO-8601 UTC timestamp when the entry was added.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    backend: BackendConfigBase
    index: int
    created_at: datetime

    @field_serializer("backend")
    def _serialize_backend(self, v: BackendConfigBase) -> dict[str, object]:
        """
        Serialize the concrete subclass fields, not just the base class fields.
        """
        return _dump_backend_config(v)


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

    Per-row validation uses :func:`_parse_backend_config` so an
    unknown ``kind`` is preserved as :class:`UnresolvedBackendConfig`
    rather than triggering a file-level backup-and-empty. Only a
    corrupt envelope (bad JSON / non-3 version) triggers the `.bak`
    fallback. A single unknown-kind row NEVER wipes the file.

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
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        backup = path.with_suffix(path.suffix + ".bak")
        path.rename(backup)
        if not _LEGACY_HINT_PRINTED:
            console.note(
                f"registry at {path} could not be parsed as JSON; "
                f"renamed to {backup.name}. "
                f"Re-register your instances with "
                f"`beetroot register <path>`."
            )
            _LEGACY_HINT_PRINTED = True
        return RegistryFile()

    # Version check: only v3 is supported; anything else triggers backup.
    version = raw.get("version") if isinstance(raw, dict) else None
    if version != SCHEMA_VERSION:
        backup = path.with_suffix(path.suffix + ".bak")
        path.rename(backup)
        if not _LEGACY_HINT_PRINTED:
            console.note(
                f"registry at {path} was schema "
                f"v{version!r}; renamed to {backup.name}. "
                f"Re-register your instances with "
                f"`beetroot register <path>`."
            )
            _LEGACY_HINT_PRINTED = True
        return RegistryFile()

    # Parse per-row with open-union backend dispatch. Unknown kinds
    # become UnresolvedBackendConfig and are preserved — they do NOT
    # wipe the file.
    instances: dict[str, InstanceMeta] = {}
    raw_instances = raw.get("instances", {})
    if not isinstance(raw_instances, dict):
        raw_instances = {}
    for name, meta_dict in raw_instances.items():
        if not isinstance(meta_dict, dict):
            continue
        backend_raw = meta_dict.get("backend")
        if not isinstance(backend_raw, dict):
            continue
        try:
            backend = _parse_backend_config(backend_raw)
            # Validate the rest of the meta fields via pydantic,
            # substituting the pre-parsed backend so it doesn't go
            # through the closed union.
            meta = InstanceMeta.model_validate({**meta_dict, "backend": backend})
            instances[name] = meta
        except Exception:  # noqa: BLE001, S112  # corrupt row (ValidationError, ValueError, etc.) — skip silently; the envelope is valid
            continue
    return RegistryFile(instances=instances)


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
        # Serialize: opaque rows must be re-emitted with their raw dict.
        payload = _registry_to_json(data)
        tmp.write_text(payload)
        tmp.replace(path)
    finally:
        # If the replace happened, this is a no-op (tmp no longer
        # exists); on a failure path, this cleans up the orphan.
        if tmp.exists():
            tmp.unlink()


def _registry_to_json(data: RegistryFile) -> str:
    """
    Serialize a :class:`RegistryFile` to a JSON string.

    :class:`UnresolvedBackendConfig` rows are re-emitted from their
    raw dict (byte-for-byte round-trip). Registered rows go through
    pydantic's ``model_dump``.  Both paths delegate to
    :func:`_dump_backend_config` — the single authoritative
    serialization helper — so opaque-row round-tripping has exactly one
    implementation.
    """
    instances_out: dict[str, dict[str, object]] = {}
    for name, meta in data.instances.items():
        instances_out[name] = {
            "backend": _dump_backend_config(meta.backend),
            "index": meta.index,
            "created_at": meta.created_at.isoformat(),
        }
    out: dict[str, object] = {"version": data.version, "instances": instances_out}
    return json.dumps(out, indent=2)


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
    console.note(
        f"detected v0.2 registry at {candidate} — move it to "
        f"{xdg_path} (or re-register each instance with "
        f"'beetroot register <path>')."
    )
    _V02_HINT_PRINTED = True


def list_instances() -> dict[str, InstanceMeta]:
    """
    Return all known instances as name → metadata. Empty if registry is missing.
    """
    path = paths.user_registry_file()
    if not path.exists():
        # Fast-path: no registry yet → nothing to read, no need to
        # touch the lock file at all.
        _check_v02_registry_at_cwd(path)
        return {}
    with _locked(path, exclusive=False):
        return _read(path).instances


def get(name: str) -> InstanceMeta | None:
    """
    Return the :class:`InstanceMeta` for ``name``, or ``None`` if not registered.
    """
    return list_instances().get(name)


def used_indices() -> set[int]:
    """
    Return the set of port indices currently allocated to registered instances.
    """
    return {meta.index for meta in list_instances().values()}


def add_allocating(
    name: str,
    absolute_path: Path | None = None,
    *,
    backend: BackendConfigBase | None = None,
) -> int:
    """
    Atomically allocate the lowest free port index AND register ``name``.

    Without this critical section, two parallel ``Instance.create`` calls
    could read ``used_indices()`` simultaneously, both get the same
    lowest-free index, and then both write to the registry — silently
    co-allocating the same port to two instances. The user only sees
    the failure at ``docker compose up`` time, when the second
    instance's bind fails.

    The ``backend`` argument is keyword-only to prevent accidental
    positional misuse (the old dual-form ``add`` had a positional
    ``index`` footgun).  Pass ``absolute_path`` for the legacy
    redroid-shorthand form; pass ``backend=<config>`` for any
    pre-built :class:`BackendConfigBase` arm.

    Args:
        name: Instance name to register.
        absolute_path: Absolute path to the instance directory.
            Required when ``backend`` is None; ignored otherwise.
        backend: Pre-built backend config (any
            :class:`BackendConfigBase` subclass). When omitted, the
            redroid shape is synthesised from ``absolute_path``.

    Returns:
        The port index that was allocated.

    Raises:
        ValueError: If ``name`` is already in the registry or if neither
            ``absolute_path`` nor ``backend`` is supplied.
    """
    if backend is None:
        if absolute_path is None:
            raise ValueError(
                "registry.add_allocating requires either ``absolute_path`` "
                "(for the redroid shorthand) or ``backend`` (for an explicit "
                "BackendConfigBase subclass).",
            )
        backend = RedroidBackendConfig(absolute_path=str(absolute_path))
    path = paths.user_registry_file()
    with _locked(path):
        data = _read(path)
        if name in data.instances:
            raise ValueError(f"instance {name!r} already in registry")
        used = {meta.index for meta in data.instances.values()}
        index = ports.lowest_free_index(used)
        data.instances[name] = InstanceMeta(
            backend=backend,
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


def set_stealth_paths(name: str, stealth_paths: dict[str, str]) -> None:
    """
    Replace the stealth-path blob on an existing redroid instance row.

    Used by :func:`snapshot.restore` (T4) to replay a path-layout blob
    from the snapshot manifest into the freshly-allocated registry entry.
    Keeping the mutation on its own helper (rather than threading the blob
    through ``add_allocating``) keeps the hot create-path's signature stable
    and avoids coupling snapshot/restore plumbing to ``AdbBackendConfig``
    work.

    .. note::
        This helper and the ``stealth_paths`` slot are **provisional** and
        may change before v1.0 — stealth path-randomization work is
        deferred to a future release.

    Args:
        name: Instance name to update.
        stealth_paths: New stealth-path mapping. A copy is taken so the
            caller can mutate the dict afterwards without retroactively
            changing the registry row.

    Raises:
        RegistryError: If ``name`` is not registered, or if the
            registered backend is not directory-backed (the
            ``stealth_paths`` slot lives on
            :class:`RedroidBackendConfig` only — adb-backed devices
            don't have container paths to randomize).
    """
    path = paths.user_registry_file()
    with _locked(path):
        data = _read(path)
        meta = data.instances.get(name)
        if meta is None:
            raise RegistryError(f"unknown instance {name!r}; not in registry")
        backend = meta.backend
        if not isinstance(backend, RedroidBackendConfig):
            raise RegistryError(
                f"instance {name!r} is a {backend.kind!r} backend; "
                "stealth_paths is a redroid-only slot"
            )
        backend.stealth_paths = dict(stealth_paths)
        _write(path, data)


def reconcile_backend_kind(name: str, binder: str) -> bool:
    """
    Sync a directory-backed row's kind to its config's ``binder`` mode.

    ``binder: vm`` instances must be registered as :class:`VmBackendConfig`
    so :meth:`beetroot.api.Manager.resolve` dispatches to the QEMU micro-VM
    engine; every other mode is the redroid-over-compose backend. When a
    user hand-edits ``binder`` in ``beetroot.yaml`` after creation, the next
    ``beetroot apply`` calls this to flip the registry kind to match —
    preserving ``absolute_path`` and the allocated index.

    Only the redroid ↔ vm pair is reconciled (both are directory-backed and
    share the ``absolute_path`` field); adb and third-party rows are left
    untouched.

    Args:
        name: Instance name to reconcile.
        binder: The instance config's ``binder`` value (``auto`` / ``host``
            / ``vm``).

    Returns:
        True if the row's kind was changed, False if it already matched (or
        the row isn't a directory-backed redroid/vm kind).
    """
    want_vm = binder == "vm"
    path = paths.user_registry_file()
    with _locked(path):
        data = _read(path)
        meta = data.instances.get(name)
        if meta is None:
            return False
        backend = meta.backend
        if not isinstance(backend, RedroidBackendConfig | VmBackendConfig):
            return False
        is_vm = isinstance(backend, VmBackendConfig)
        if is_vm == want_vm:
            return False
        abs_path = backend.absolute_path
        meta.backend = (
            VmBackendConfig(absolute_path=abs_path)
            if want_vm
            else RedroidBackendConfig(absolute_path=abs_path)
        )
        _write(path, data)
        return True


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
    if not isinstance(backend, RedroidBackendConfig | VmBackendConfig):
        raise RegistryError(
            f"instance {name!r} is a {backend.kind!r} backend; it has no on-disk directory"
        )
    return Path(backend.absolute_path)


def all_resolved_host_ports() -> dict[str, set[int]]:
    """
    Return the set of allocated host ports for every registered instance.

    For redroid / vm instances, loads their ``beetroot.yaml`` to resolve the
    full ``ports:`` list — including **arbitrary** (non-well-known) mappings,
    not just the three well-known services — so a cross-instance collision
    over any host port is caught (issue #108). For adb-kind (and other)
    instances, uses the stride-of-10 well-known defaults derived from the
    registered index (they have no ``beetroot.yaml`` to consult). Orphan
    directory-backed entries (registered names whose ``beetroot.yaml`` is
    gone) are silently skipped.

    Returns:
        A mapping ``instance_name → {host_port, ...}`` covering every
        registered instance. Empty dict if the registry is empty or every
        directory-backed entry is an orphan.
    """
    out: dict[str, set[int]] = {}
    for name, meta in list_instances().items():
        backend = meta.backend
        if isinstance(backend, RedroidBackendConfig | VmBackendConfig):
            try:
                cfg = load_yaml(paths.instance_yaml(Path(backend.absolute_path)))
            except FileNotFoundError:
                continue
            out[name] = {rp.host for rp in ports.resolve_ports(meta.index, cfg.ports)}
        else:
            # adb-kind and other backends: use stride defaults (no yaml).
            out[name] = set(ports.ports_for_index(meta.index).values())
    return out


def find_port_collision(
    new_ports: list[ports.ResolvedPort],
    others: dict[str, set[int]],
) -> tuple[int, str, str] | None:
    """
    Search ``others`` for any host port that collides with ``new_ports``.

    Args:
        new_ports: Resolved port list for the instance being staged.
        others: Mapping of other-instance-name → set of allocated host
            ports. The caller is responsible for excluding the staging
            instance itself from this mapping.

    Returns:
        ``(host_port, conflicting_instance, service)`` on the first collision
        found — ``service`` is the *new* instance's service label for the
        colliding entry (``str(service)`` so an unlabelled arbitrary mapping
        renders as ``"None"``). Returns ``None`` if no collision exists.
    """
    for rp in new_ports:
        for other_name, other_ports in others.items():
            if rp.host in other_ports:
                return rp.host, other_name, str(rp.service)
    return None
