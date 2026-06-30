"""
Snapshot and restore Beetroot instances as zstandard-compressed tar archives.

A snapshot captures the host-side state of one instance — its
``beetroot.yaml``, its persisted ``data/`` directory, any staged
``modules/``, and the optional ``frida-server`` placeholder — into a
single ``.tar.zst`` archive. The container's overlay layer is NOT
captured by design: redroid regenerates it deterministically from the
base image plus the persisted ``/data`` bind mount, so re-running
``beetroot up`` after a restore produces an equivalent container.

The archive carries a ``.beetroot-snapshot.json`` manifest at its root.
The manifest's ``path_layout`` field carries the source instance's
``RedroidBackendConfig.stealth_paths`` blob (T4) so a randomized layout
round-trips through ``snapshot → restore`` into the new instance's
registry entry. v0.4 itself defaults the slot to the empty dict, so
v0.4 → v0.4 round-trips preserve ``{}``; a future release's stealth
work will populate the slot in ``Instance.create``'s generator and the
same round-trip will preserve those randomized paths.

The ``.env`` file is deliberately excluded — it's regenerated from
``beetroot.yaml`` on the next ``beetroot apply``.
"""

from __future__ import annotations

import importlib.metadata
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
import zstandard
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config, paths, ports, registry

MANIFEST_FILENAME = ".beetroot-snapshot.json"
INSTANCE_LOCK_FILENAME = ".beetroot.lock"
SCHEMA_VERSION = 1
_ARCHIVE_SUFFIX = ".tar.zst"
# .env is regenerated from beetroot.yaml on the next apply. The
# manifest itself is excluded because the archive's *root*-level
# manifest is the authoritative one; if a previous restore left a
# stale copy on disk we'd otherwise re-pack it and confuse
# basename-based readers. The lock file is a per-host artefact;
# carrying it through a snapshot would also export a now-broken
# kernel flock state to a different host.
# compose.override.yaml is regenerated from beetroot.yaml on the next apply,
# like .env (issue #108) — exclude it so a restored archive re-derives it.
_COMPOSE_OVERRIDE_FILENAME = "compose.override.yaml"
_EXCLUDED_TOP_LEVEL = frozenset(
    {
        ".env",
        _COMPOSE_OVERRIDE_FILENAME,
        MANIFEST_FILENAME,
        INSTANCE_LOCK_FILENAME,
    }
)


class SnapshotError(RuntimeError):
    """
    Raised on snapshot/restore failures (missing source, bad archive, etc.).
    """


class Manifest(BaseModel):
    """
    Per-snapshot metadata embedded as ``.beetroot-snapshot.json`` in the archive.

    Frozen + ``extra="forbid"`` so an archive carrying an unknown
    future key surfaces a :class:`ValidationError` at restore time
    rather than silently dropping the field. v0.4 snapshots are
    redroid-only by design (``kind: Literal["redroid"]``); the field
    exists so a future cross-backend snapshot story doesn't need a
    second schema bump.

    Attributes:
        schema_version: Manifest schema version. Currently ``1``.
        name: Source instance name at snapshot time.
        source_index: Source instance's allocated port index.
        created_at: ISO-8601 UTC timestamp of when the snapshot was taken.
        beetroot_version: Beetroot release that produced the snapshot.
        kind: Backend kind discriminator. v0.4 snapshots are
            redroid-only.
        path_layout: Stealth-posture path mapping carried alongside the
            instance. Populated from the source's
            ``RedroidBackendConfig.stealth_paths`` at snapshot time
            (T4) and replayed into the destination's slot on restore.
            Default ``{}`` in v0.4; v0.6's ``Instance.create`` generator
            will populate the slot per-instance.
        lifecycle: The source instance's persistence intent
            (``ephemeral`` / ``durable``) at snapshot time (#124). Stamped
            so a restored archive carries the same intent; an archive
            produced before this field existed has no ``lifecycle`` key and
            restores as ``durable`` (the default), preserving today's
            contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    name: str
    source_index: int
    created_at: str
    beetroot_version: str
    kind: Literal["redroid"] = "redroid"
    path_layout: dict[str, str] = Field(default_factory=dict)
    lifecycle: Literal["ephemeral", "durable"] = "durable"


def _ensure_suffix(dest: Path) -> Path:
    """
    Return ``dest`` with ``.tar.zst`` appended if it doesn't already end in it.
    """
    if dest.name.endswith(_ARCHIVE_SUFFIX):
        return dest
    return dest.with_name(dest.name + _ARCHIVE_SUFFIX)


def _build_manifest(
    name: str,
    source_index: int,
    path_layout: dict[str, str],
    lifecycle: Literal["ephemeral", "durable"] = "durable",
) -> Manifest:
    """
    Build a fresh manifest carrying the source's ``stealth_paths`` blob (T4).

    ``lifecycle`` records the source instance's persistence intent (#124).
    """
    return Manifest(
        name=name,
        source_index=source_index,
        created_at=datetime.now(UTC).isoformat(),
        beetroot_version=importlib.metadata.version("beetroot"),
        path_layout=path_layout,
        lifecycle=lifecycle,
    )


def _read_lifecycle(yaml_path: Path) -> Literal["ephemeral", "durable"]:
    """
    Best-effort read of the source config's ``lifecycle`` (defaults to durable).

    A malformed config falls back to ``durable`` rather than failing the
    snapshot — the archive's own ``beetroot.yaml`` is the source of truth, and
    the manifest field is advisory metadata (#124).
    """
    try:
        return config.load_yaml(yaml_path).lifecycle
    except (OSError, ValueError, yaml.YAMLError):
        return "durable"


def _manifest_to_json(manifest: Manifest) -> bytes:
    """
    Serialise a ``Manifest`` to UTF-8 JSON bytes (sorted keys, two-space indent).
    """
    # ``model_dump_json`` serialises in field-declaration order, which varies
    # between Python versions and pydantic builds and breaks the byte-identical
    # guarantee.  Round-tripping through ``json.dumps(sort_keys=True)`` produces
    # a stable, deterministic encoding: two archives from identical state produce
    # the same manifest bytes, which lets content-addressable tooling compare
    # snapshots without re-parsing every field.
    return json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2).encode("utf-8")


def snapshot(instance_root: Path, dest: Path) -> Path:
    """
    Pack an instance directory into a zstandard-compressed tar archive.

    The archive is rooted at the instance directory itself (entries are
    relative paths like ``./beetroot.yaml``, ``./data/...``,
    ``./modules/...``). The ``.env`` file is deliberately excluded —
    it's regenerated from ``beetroot.yaml`` on the next
    ``beetroot apply``. The manifest is written as the archive's last
    member.

    Holds a SHARED ``fcntl.flock`` on ``<instance_root>/.beetroot.lock``
    for the duration of the archive write — multiple snapshots can run
    in parallel, but a concurrent :meth:`Instance.destroy` (which
    takes the exclusive lock) blocks until snapshotting finishes.
    Without this, a destroy race would rmtree the directory mid-read
    and produce a torn archive. (T2 Agent 2 B-12.)

    Warning:
        Snapshotting a **running** container is unsupported. A live
        ``/data`` bind-mount commonly contains absolute symlinks created
        by Android init or the Magisk daemon.  These fail the
        ``filter="data"`` extraction guard (tarfile raises
        ``AbsoluteLinkError``) so the resulting archive cannot be
        restored on most hosts.  :func:`restore` rolls back cleanly when
        extraction fails (B7a), but the snapshot itself will be
        unrestorable.  Always run ``beetroot down <name>`` before
        snapshotting.

    Args:
        instance_root: The source instance directory (the one containing
            ``beetroot.yaml``). The instance must be registered.
        dest: Destination path for the archive. ``.tar.zst`` is appended
            if the caller omits it. Parent directories are created.

    Returns:
        The final archive path (after the ``.tar.zst`` extension fix-up).

    Raises:
        SnapshotError: If ``instance_root`` has no ``beetroot.yaml`` or
            isn't registered under any name.
    """
    yaml_path = paths.instance_yaml(instance_root)
    if not yaml_path.is_file():
        raise SnapshotError(f"no beetroot.yaml at {yaml_path}; not a Beetroot instance directory")
    name, meta, backend = _find_registry_entry(instance_root)
    final_dest = _ensure_suffix(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)

    # Take a copy of the dict so the manifest model holds an
    # independent snapshot of the source's path layout — a later
    # mutation of the registry entry must not retroactively change
    # an already-written manifest.
    manifest = _build_manifest(
        name=name,
        source_index=meta.index,
        path_layout=dict(backend.stealth_paths),
        lifecycle=_read_lifecycle(yaml_path),
    )

    # Local import — api imports snapshot at module load, so a
    # top-level ``from .api import instance_lock`` would loop.
    from .api import instance_lock  # noqa: PLC0415

    cctx = zstandard.ZstdCompressor()
    with (
        instance_lock(instance_root, exclusive=False),
        final_dest.open("wb") as raw_out,
        cctx.stream_writer(raw_out) as zst,
        tarfile.open(fileobj=zst, mode="w|") as tar,
    ):
        _add_instance_tree(tar, instance_root, final_dest)
        _add_manifest(tar, manifest)
    return final_dest


def unsupported_backend_message(verb: str, name: str, kind: str) -> str:
    """
    Build the "snapshot/restore is redroid-only" error message for a backend kind.

    Single source of truth for the wording shared by :func:`_find_registry_entry`
    (the programmatic ``snapshot`` path) and the CLI ``snapshot`` / ``restore``
    verbs, so a ``binder: vm`` (or adb) instance gets one consistent, actionable
    error instead of the misleading "not registered" message that
    :func:`_find_registry_entry` used to raise for any non-redroid row.

    Args:
        verb: The verb being attempted (``"snapshot"`` or ``"restore"``).
        name: The offending instance name.
        kind: The instance's registered backend kind (e.g. ``"vm"``, ``"adb"``).

    Returns:
        A one-line error string naming the verb, the instance, and the
        unsupported backend, and pointing at issue #128.
    """
    return (
        f"{verb} is only supported for the redroid backend; instance {name!r} uses the "
        f"{kind} backend — {kind} {verb} is not yet supported (see issue #128)."
    )


def _find_registry_entry(
    instance_root: Path,
) -> tuple[str, registry.InstanceMeta, registry.RedroidBackendConfig]:
    """
    Look up the registry entry matching ``instance_root``.

    Returns the matched name, the full meta row, AND the narrowed
    :class:`registry.RedroidBackendConfig` — callers need the backend
    type-narrowed so they can reach ``backend.stealth_paths`` without
    re-asserting the kind (S101 forbids ``assert isinstance(...)``
    bridges in src).

    Snapshots are redroid-only (the ``kind: Literal["redroid"]``
    discriminator on :class:`Manifest`). A directory-backed but
    non-redroid row matching ``instance_root`` — a ``binder: vm``
    instance — raises the actionable
    :func:`unsupported_backend_message` error rather than the
    "not registered" message, which would mislead a user whose instance
    *is* registered (#128). Adb-backed rows carry a serial, not a path,
    so they can never match ``instance_root`` here; the CLI catches the
    adb ``snapshot`` case via the capability gate instead.
    """
    target = instance_root.resolve()
    for name, meta in registry.list_instances().items():
        backend = meta.backend
        if isinstance(backend, registry.RedroidBackendConfig):
            if Path(backend.absolute_path).resolve() == target:
                return name, meta, backend
        elif (
            isinstance(backend, registry.VmBackendConfig)
            and Path(backend.absolute_path).resolve() == target
        ):
            raise SnapshotError(unsupported_backend_message("snapshot", name, backend.kind))
    raise SnapshotError(
        f"instance at {instance_root} is not registered; run `beetroot register <path>` first"
    )


def _add_instance_tree(tar: tarfile.TarFile, instance_root: Path, final_dest: Path) -> None:
    """
    Recursively add every file under ``instance_root`` except excluded entries.

    Skips both the name-based ``_EXCLUDED_TOP_LEVEL`` set and the
    destination archive itself: the CLI default writes ``<name>.tar.zst``
    into the cwd, which is normally the instance dir, so the just-created
    (still-open, partially-flushed) archive would otherwise be packed into
    itself as a phantom ``./<name>.tar.zst`` member and re-extracted on
    restore. The exclusion applies at ANY directory depth (via the
    ``tar.add`` ``filter`` callback), so a dest nested in a subdir such as
    ``data/<name>.tar.zst`` — the CLI default resolved against a cwd inside
    ``data/`` (#173) — is dropped too, not just a top-level dest. The match
    is by resolved absolute path, so a destination outside ``instance_root``
    is unaffected and a same-named file elsewhere in the tree is not wrongly
    excluded.

    Args:
        tar: The open tar stream to append members to.
        instance_root: The instance directory whose contents are packed.
        final_dest: The resolved destination archive path to skip if it
            falls inside ``instance_root``.
    """
    dest_resolved = final_dest.resolve()

    def _skip_dest(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # ``info.name`` is the arcname (e.g. ``./data/alpha.tar.zst``);
        # resolving it against ``instance_root`` recovers the on-disk path so
        # the still-open destination archive is dropped at any depth (#173).
        if (instance_root / info.name).resolve() == dest_resolved:
            return None
        return info

    for entry in sorted(instance_root.iterdir()):
        if entry.name in _EXCLUDED_TOP_LEVEL:
            continue
        if entry.resolve() == dest_resolved:
            continue
        tar.add(entry, arcname=f"./{entry.name}", recursive=True, filter=_skip_dest)


def _add_manifest(tar: tarfile.TarFile, manifest: Manifest) -> None:
    """
    Append the ``.beetroot-snapshot.json`` manifest member to the archive.
    """
    payload = _manifest_to_json(manifest)
    info = tarfile.TarInfo(name=f"./{MANIFEST_FILENAME}")
    info.size = len(payload)
    info.mtime = int(datetime.now(UTC).timestamp())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(payload))


def read_manifest(archive: Path) -> Manifest:
    """
    Extract and parse the manifest from a snapshot archive.

    Args:
        archive: Path to a ``.tar.zst`` snapshot archive.

    Returns:
        The parsed ``Manifest`` from the archive's
        ``.beetroot-snapshot.json`` member.

    Raises:
        SnapshotError: If the archive has no manifest, the manifest can't
            be parsed, or its schema version is not supported.
    """
    raw = _extract_manifest_bytes(archive)
    try:
        # ``decode`` raises UnicodeDecodeError on non-UTF-8 manifest bytes
        # BEFORE validation — catch it alongside ValidationError so a
        # corrupt manifest surfaces as the documented SnapshotError rather
        # than a raw traceback through restore() / the CLI.
        return Manifest.model_validate_json(raw.decode("utf-8"))
    except (ValidationError, UnicodeDecodeError) as e:
        raise SnapshotError(f"manifest validation failed: {e}") from e


def _extract_manifest_bytes(archive: Path) -> bytes:
    """
    Stream-read the archive and return the raw manifest bytes.
    """
    dctx = zstandard.ZstdDecompressor()
    try:
        with (
            archive.open("rb") as raw_in,
            dctx.stream_reader(raw_in) as zst,
            tarfile.open(fileobj=zst, mode="r|") as tar,
        ):
            for member in tar:
                if _is_manifest_member(member):
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise SnapshotError(
                            f"manifest member {member.name!r} is not a regular file"
                        )
                    return extracted.read()
    except zstandard.ZstdError as e:
        raise SnapshotError(f"archive {archive} is not a valid zstd stream: {e}") from e
    except tarfile.TarError as e:
        raise SnapshotError(f"archive {archive} contains a malformed tar stream: {e}") from e
    raise SnapshotError(f"archive {archive} is missing its {MANIFEST_FILENAME} manifest")


_MANIFEST_ARCNAMES = frozenset(
    {
        f"./{MANIFEST_FILENAME}",
        MANIFEST_FILENAME,
    }
)


def _is_manifest_member(member: tarfile.TarInfo) -> bool:
    """
    Return True if ``member`` is the canonical archive-root manifest entry.

    A basename-only match would also pick up a stale
    ``data/.beetroot-snapshot.json`` left over from a previous
    restore. Require an exact-path match against the archive root.
    """
    return member.name in _MANIFEST_ARCNAMES


def _prepare_destination(target: Path, *, force: bool) -> None:
    """
    Validate / clear ``target`` before extraction. Pulled out of :func:`restore`.

    Raises :class:`SnapshotError` if the destination is a plain file (not a
    directory), if it is occupied by a sibling registered instance (refused
    even under ``--force``), or if it is non-empty without ``--force``.
    """
    try:
        occupied = target.exists() and any(target.iterdir())
    except NotADirectoryError:
        # ``target`` is an existing regular file; iterdir() blows up.
        # Re-raise as a SnapshotError so callers see a consistent type.
        raise SnapshotError(f"{target} exists and is a file, not a directory") from None
    # The cross-instance overlap loop runs UNCONDITIONALLY — before the
    # ``occupied`` early-return — because the predicate below is a pure
    # path comparison that is just as valid for a not-yet-existent target.
    # Gating it behind ``occupied`` (#172) let a restore into a brand-new
    # nested path like ``<registered-peer>/sub`` slip past the guard and
    # register a nested instance inside a registered peer with no --force.
    for other_name, meta in registry.list_instances().items():
        # Both redroid and vm backends are directory-backed and carry an
        # ``absolute_path``; an adb row carries a serial, not a path, so
        # it can never collide here. Guarding only the redroid type let a
        # ``--force`` restore wipe a registered ``binder: vm`` instance.
        if not isinstance(meta.backend, (registry.RedroidBackendConfig, registry.VmBackendConfig)):
            continue
        reg_dir = Path(meta.backend.absolute_path).resolve()
        # Refuse on ANY path-prefix overlap in either direction: ``target``
        # IS the registered dir, an ANCESTOR of it (``rmtree(target)`` on a
        # parent destroys the nested instance just as surely as rmtree-ing
        # the dir itself), or a DESCENDANT of it (``rmtree`` of a subdir
        # wipes part of the live instance, #154). Exact-equality-only let a
        # ``--force`` restore into a parent or child directory wipe it.
        if reg_dir == target or target in reg_dir.parents or reg_dir in target.parents:
            raise SnapshotError(
                f"{target} overlaps the registered directory of "
                f"instance {other_name!r}; refusing to overwrite (even "
                f"with --force). 'beetroot destroy {other_name}' first, "
                "or pick a different --path."
            )
    # Overlap guard cleared: a non-existent or empty target needs no
    # further clearing, so return before the force/overwrite tail.
    if not occupied:
        return
    if not force:
        raise SnapshotError(
            f"{target} already exists and is non-empty; "
            "pass --force to overwrite, or pick another path"
        )
    shutil.rmtree(target)


def _check_restored_port_collision(dest_name: str, index: int, target: Path) -> None:
    """
    Refuse if the restored instance's ports collide with a registered peer.

    Without this check, restoring a snapshot that pins ``ports.adb: 5555``
    next to an existing instance using ``5555`` would register cleanly
    and only fail at compose-up time.
    """
    cfg = config.load_yaml(paths.instance_yaml(target))
    new_ports = ports.resolve_ports(index, cfg.ports)
    others = {n: p for n, p in registry.all_resolved_host_ports().items() if n != dest_name}
    collision = registry.find_port_collision(new_ports, others)
    if collision is None:
        return
    port, other_name, kind = collision
    raise SnapshotError(
        f"port {port} ({kind}) collides with instance {other_name!r}; "
        "edit the restored beetroot.yaml's ports: list before retrying"
    )


def restore(
    archive: Path,
    *,
    dest_name: str,
    dest_path: Path,
    force: bool = False,
) -> Path:
    """
    Unpack a snapshot archive into a new instance directory and register it.

    A fresh port index is allocated via
    :func:`ports.lowest_free_index` — the source's index is NOT reused,
    so an instance can be restored alongside its source.

    The manifest's ``path_layout`` is replayed into the new instance's
    :class:`registry.RedroidBackendConfig.stealth_paths` slot via
    :func:`registry.set_stealth_paths`. A v0.4 snapshot ships
    ``{}``, so the assignment is a no-op for today's snapshots — but
    a v0.6 snapshot carrying randomized paths round-trips into a
    matching slot on the new instance, ready for ``render_env`` to
    consume on the next ``apply``.

    Args:
        archive: Path to a ``.tar.zst`` snapshot archive.
        dest_name: Registry name to assign to the restored instance.
        dest_path: Directory to extract into (created if absent).
        force: If True, an existing non-empty ``dest_path`` is wiped before
            extraction. Defaults to False (refuse and raise).

    Returns:
        The absolute path of the restored instance directory.

    Raises:
        SnapshotError: If ``dest_name`` is already registered (with a
            redroid-specific message when the existing row is non-redroid,
            #128), if ``dest_path`` exists and is non-empty without
            ``force``, or if the archive is invalid.
        ValueError: If ``dest_name`` does not match the instance-name
            grammar. The CLI default derives ``dest_name`` from the
            attacker-controlled ``manifest.name``; validating here — the
            API boundary, so both the CLI default and programmatic callers
            are covered — rejects path separators, ``..``, and absolute
            paths before any filesystem mutation or registry write, so a
            malicious archive cannot escape into an attacker-chosen
            directory.
    """
    # Local import — api imports snapshot at module load, so a top-level
    # ``from .api import _validate_instance_name`` would loop. Validate
    # BEFORE any filesystem mutation or registry write so an attacker
    # cannot drive ``mkdir`` / extraction into a traversed path.
    from .api import _validate_instance_name  # noqa: PLC0415

    _validate_instance_name(dest_name)
    existing = registry.get(dest_name)
    if existing is not None:
        if not isinstance(existing.backend, registry.RedroidBackendConfig):
            # The name is taken by a vm / adb instance. Restore only ever
            # produces a redroid instance, so name it explicitly (#128)
            # rather than the generic "already registered" hint reserved
            # for a redroid-vs-redroid name clash.
            raise SnapshotError(
                unsupported_backend_message("restore", dest_name, existing.backend.kind)
            )
        raise SnapshotError(
            f"instance {dest_name!r} already registered; pick a different --name <name>"
        )
    target = dest_path.resolve()
    # Validate the archive's manifest BEFORE any destructive action on
    # the target directory. v0.3 ordered ``rmtree(target)`` first and
    # ``read_manifest(archive)`` second — a corrupted archive paired
    # with ``--force`` wiped the user's existing directory and THEN
    # discovered the archive was unreadable, leaving no way back.
    # (T2 Agent 3 1.4.)
    manifest = read_manifest(archive)
    _prepare_destination(target, force=force)
    # ``created_dir`` is True iff Beetroot now owns the directory; the
    # rollback path uses it to decide whether to ``rmtree``.
    created_dir = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    # Local import — api imports snapshot at module load, so a top-level
    # ``from . import api`` would loop.
    from . import api  # noqa: PLC0415

    # Atomic allocation + registration under one file lock.
    index = registry.add_allocating(
        dest_name,
        backend=registry.RedroidBackendConfig(absolute_path=str(target)),
    )
    try:
        # B7a: extraction is INSIDE the rollback try/except so a malformed
        # archive member (tarfile FilterError / TarError, zstd error)
        # mid-extraction triggers the same rollback that cleans up the
        # partial directory.  v0.3 called _extract_archive_into before
        # the try block, so a corrupt member left a partially-extracted
        # tree behind that the user had to clean up manually.
        _extract_archive_into(archive, target)
        # #171: reconcile the EXTRACTED beetroot.yaml's binder mode. The
        # registry row is always written as RedroidBackendConfig above, but
        # the archived config is the source of truth for the backend — a
        # ``binder: vm`` archive (e.g. an unapplied edit on the source)
        # would otherwise restore as a redroid row and dispatch the wrong
        # backend silently. Snapshots are redroid-only (#128), so refuse it
        # here, inside the rollback try/except so the half-registered row +
        # extracted tree are torn down. Mirrors the source-side gate in
        # ``_find_registry_entry``.
        if config.load_yaml(paths.instance_yaml(target)).binder == "vm":
            raise SnapshotError(unsupported_backend_message("restore", dest_name, "vm"))
        # T4: replay the snapshot's path_layout into the new registry
        # entry's stealth_paths slot. v0.4 manifests carry ``{}`` so
        # this is a structural no-op today; a v0.6 snapshot carrying
        # randomized paths round-trips into a matching slot on the new
        # instance. Done INSIDE the rollback try/except so a malformed
        # blob (e.g. an unrecognised key in a future schema bump)
        # still tears down the half-registered row cleanly.
        if manifest.path_layout:
            registry.set_stealth_paths(dest_name, manifest.path_layout)
        _check_restored_port_collision(dest_name, index, target)
        # Stage .env + frida placeholder + dirs now so `beetroot up
        # <name>` works without a follow-up `beetroot apply`. Mirrors
        # what Instance.create / Instance.register do. Only the LOCAL
        # stage step is rollback-fatal (T2 Agent 2 B-2); the network
        # step runs post-commit via the soft-fail helper outside this
        # try block.
        restored_inst = api.Instance.load(dest_name)
        restored_inst._stage_local()  # noqa: SLF001  # snapshot ↔ api are siblings; _stage_local is the inter-module re-stage hook
    except BaseException:
        # Roll back BOTH the registry row AND the extracted directory
        # (if we created it). Without the rmtree, a failed restore
        # leaves a half-extracted tree behind that the user has to
        # clean up manually before the next attempt.
        registry.remove(dest_name)
        if created_dir and target.exists():
            shutil.rmtree(target)
        raise
    # Soft-fail network stage runs AFTER the rollback try/except so a
    # Frida 404 doesn't destroy a freshly-extracted instance the user
    # can recover via ``beetroot apply``.
    api._stage_network_soft(restored_inst)  # noqa: SLF001  # snapshot ↔ api are siblings
    return target


def _extract_archive_into(archive: Path, target: Path) -> None:
    """
    Decompress and untar ``archive`` into ``target``.

    The manifest is **preserved on disk** at
    ``<target>/.beetroot-snapshot.json`` so v0.4's stealth-posture
    ``beetroot apply`` can read it and replay ``path_layout`` into the
    new instance's ``BEETROOT_*`` env vars. v0.3 doesn't act on the
    manifest itself, but it never strips or rewrites ``path_layout``
    either — so a v0.4-produced archive restored by v0.3 keeps its
    layout intact for the next read.

    ``restore`` runs :func:`read_manifest` first as a structural
    validator (zstd-stream + tar-stream + JSON shape), so this helper
    can assume the archive is well-formed and skip its own redundant
    error wrapping.
    """
    dctx = zstandard.ZstdDecompressor()
    with (
        archive.open("rb") as raw_in,
        dctx.stream_reader(raw_in) as zst,
        tarfile.open(fileobj=zst, mode="r|") as tar,
    ):
        for member in tar:
            tar.extract(member, path=target, filter="data")
    if not paths.instance_yaml(target).is_file():
        raise SnapshotError(
            f"archive {archive} did not contain a beetroot.yaml; refusing to register"
        )
