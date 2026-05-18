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
The manifest's ``path_layout`` field is reserved for the v0.4
stealth-posture work: it will record the source instance's randomized
container-path mapping so the restored instance can replay it. v0.3
always writes ``{}``; restore preserves whatever the manifest carries.

The ``.env`` file is deliberately excluded — it's regenerated from
``beetroot.yaml`` on the next ``beetroot apply``.
"""
from __future__ import annotations

import importlib.metadata
import io
import json
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zstandard

from . import paths, ports, registry

MANIFEST_FILENAME = ".beetroot-snapshot.json"
SCHEMA_VERSION = 1
_ARCHIVE_SUFFIX = ".tar.zst"
_EXCLUDED_TOP_LEVEL = frozenset({".env"})


class SnapshotError(RuntimeError):
    """Raised on snapshot/restore failures (missing source, bad archive, etc.)."""


@dataclass(frozen=True)
class Manifest:
    """
    Per-snapshot metadata embedded as ``.beetroot-snapshot.json`` in the archive.

    Attributes:
        schema_version: Manifest schema version. Currently ``1``.
        name: Source instance name at snapshot time.
        source_index: Source instance's allocated port index.
        created_at: ISO-8601 UTC timestamp of when the snapshot was taken.
        beetroot_version: Beetroot release that produced the snapshot.
        path_layout: Reserved for v0.4 stealth-posture work. v0.3 always
            writes ``{}``; restore preserves whatever the manifest carries.
    """

    schema_version: int
    name: str
    source_index: int
    created_at: str
    beetroot_version: str
    path_layout: dict[str, str]


def _coerce_manifest(raw: dict[str, Any]) -> Manifest:
    """Validate and convert a parsed JSON dict into a ``Manifest``."""
    required = ("schema_version", "name", "source_index", "created_at",
                "beetroot_version", "path_layout")
    missing = [k for k in required if k not in raw]
    if missing:
        raise SnapshotError(
            f"manifest is missing required keys: {', '.join(missing)}"
        )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise SnapshotError(
            f"manifest schema_version {raw['schema_version']!r} is not supported "
            f"by this Beetroot release (expects {SCHEMA_VERSION})"
        )
    path_layout = raw["path_layout"]
    if not isinstance(path_layout, dict):
        raise SnapshotError("manifest.path_layout must be a JSON object")
    return Manifest(
        schema_version=int(raw["schema_version"]),
        name=str(raw["name"]),
        source_index=int(raw["source_index"]),
        created_at=str(raw["created_at"]),
        beetroot_version=str(raw["beetroot_version"]),
        path_layout={str(k): str(v) for k, v in path_layout.items()},
    )


def _ensure_suffix(dest: Path) -> Path:
    """Return ``dest`` with ``.tar.zst`` appended if it doesn't already end in it."""
    if dest.name.endswith(_ARCHIVE_SUFFIX):
        return dest
    return dest.with_name(dest.name + _ARCHIVE_SUFFIX)


def _build_manifest(name: str, source_index: int) -> Manifest:
    """Build a fresh v0.3 manifest with an empty ``path_layout``."""
    return Manifest(
        schema_version=SCHEMA_VERSION,
        name=name,
        source_index=source_index,
        created_at=datetime.now(timezone.utc).isoformat(),
        beetroot_version=importlib.metadata.version("beetroot"),
        path_layout={},
    )


def _manifest_to_json(manifest: Manifest) -> bytes:
    """Serialise a ``Manifest`` to UTF-8 JSON bytes (sorted keys, two-space indent)."""
    payload: dict[str, Any] = {
        "schema_version": manifest.schema_version,
        "name": manifest.name,
        "source_index": manifest.source_index,
        "created_at": manifest.created_at,
        "beetroot_version": manifest.beetroot_version,
        "path_layout": manifest.path_layout,
    }
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def snapshot(instance_root: Path, dest: Path) -> Path:
    """
    Pack an instance directory into a zstandard-compressed tar archive.

    The archive is rooted at the instance directory itself (entries are
    relative paths like ``./beetroot.yaml``, ``./data/...``,
    ``./modules/...``). The ``.env`` file is deliberately excluded —
    it's regenerated from ``beetroot.yaml`` on the next
    ``beetroot apply``. The manifest is written as the archive's last
    member.

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
        raise SnapshotError(
            f"no beetroot.yaml at {yaml_path}; not a Beetroot instance directory"
        )
    name, meta = _find_registry_entry(instance_root)
    final_dest = _ensure_suffix(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)

    manifest = _build_manifest(name=name, source_index=int(meta["index"]))

    cctx = zstandard.ZstdCompressor()
    with final_dest.open("wb") as raw_out, cctx.stream_writer(raw_out) as zst:
        with tarfile.open(fileobj=zst, mode="w|") as tar:
            _add_instance_tree(tar, instance_root)
            _add_manifest(tar, manifest)
    return final_dest


def _find_registry_entry(instance_root: Path) -> tuple[str, dict[str, Any]]:
    """Look up the registry entry whose ``absolute_path`` matches ``instance_root``."""
    target = instance_root.resolve()
    for name, meta in registry.list_instances().items():
        if Path(meta["absolute_path"]).resolve() == target:
            return name, meta
    raise SnapshotError(
        f"instance at {instance_root} is not registered; "
        "run `beetroot register <path>` first"
    )


def _add_instance_tree(tar: tarfile.TarFile, instance_root: Path) -> None:
    """Recursively add every file under ``instance_root`` except excluded names."""
    for entry in sorted(instance_root.iterdir()):
        if entry.name in _EXCLUDED_TOP_LEVEL:
            continue
        tar.add(entry, arcname=f"./{entry.name}", recursive=True)


def _add_manifest(tar: tarfile.TarFile, manifest: Manifest) -> None:
    """Append the ``.beetroot-snapshot.json`` manifest member to the archive."""
    payload = _manifest_to_json(manifest)
    info = tarfile.TarInfo(name=f"./{MANIFEST_FILENAME}")
    info.size = len(payload)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
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
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise SnapshotError(f"manifest is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise SnapshotError("manifest must be a JSON object")
    return _coerce_manifest(parsed)


def _extract_manifest_bytes(archive: Path) -> bytes:
    """Stream-read the archive and return the raw manifest bytes."""
    dctx = zstandard.ZstdDecompressor()
    with archive.open("rb") as raw_in, dctx.stream_reader(raw_in) as zst:
        with tarfile.open(fileobj=zst, mode="r|") as tar:
            for member in tar:
                if _is_manifest_member(member):
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise SnapshotError(
                            f"manifest member {member.name!r} is not a regular file"
                        )
                    return extracted.read()
    raise SnapshotError(
        f"archive {archive} is missing its {MANIFEST_FILENAME} manifest"
    )


def _is_manifest_member(member: tarfile.TarInfo) -> bool:
    """Return True if ``member`` is the snapshot manifest entry."""
    return Path(member.name).name == MANIFEST_FILENAME


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

    The manifest's ``path_layout`` is preserved as-is: v0.3 writes ``{}``
    and the restore path doesn't act on it, but v0.4's stealth-posture
    work will replay a populated layout into the new instance's env vars.

    Args:
        archive: Path to a ``.tar.zst`` snapshot archive.
        dest_name: Registry name to assign to the restored instance.
        dest_path: Directory to extract into (created if absent).
        force: If True, an existing non-empty ``dest_path`` is wiped before
            extraction. Defaults to False (refuse and raise).

    Returns:
        The absolute path of the restored instance directory.

    Raises:
        SnapshotError: If ``dest_name`` is already registered, if
            ``dest_path`` exists and is non-empty without ``force``, or
            if the archive is invalid.
    """
    if registry.get(dest_name) is not None:
        raise SnapshotError(
            f"instance {dest_name!r} already registered; "
            "pick a different --as <name>"
        )
    target = dest_path.resolve()
    if target.exists() and any(target.iterdir()):
        if not force:
            raise SnapshotError(
                f"{target} already exists and is non-empty; "
                "pass --force to overwrite, or pick another path"
            )
        shutil.rmtree(target)
    read_manifest(archive)
    target.mkdir(parents=True, exist_ok=True)

    _extract_archive_into(archive, target)

    index = ports.lowest_free_index(registry.used_indices())
    registry.add(dest_name, target, index)
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
    """
    dctx = zstandard.ZstdDecompressor()
    with archive.open("rb") as raw_in, dctx.stream_reader(raw_in) as zst:
        with tarfile.open(fileobj=zst, mode="r|") as tar:
            for member in tar:
                tar.extract(member, path=target, filter="data")
    if not paths.instance_yaml(target).is_file():
        raise SnapshotError(
            f"archive {archive} did not contain a beetroot.yaml; refusing to register"
        )
