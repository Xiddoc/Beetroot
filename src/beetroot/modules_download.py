"""
Stage Magisk module zips into an instance's ``modules/`` directory.

Each module entry in ``beetroot.yaml`` is either a URL (downloaded and
cached) or a host path. Relative ``path:`` entries are resolved relative
to the instance directory itself (the one containing ``beetroot.yaml``)
and are **contained** to it — a relative path that escapes the instance
dir (e.g. ``../../etc/shadow``) is rejected, mirroring the ``file://``
URL block. An **absolute** ``path:`` the user types explicitly is
permitted (it bypasses the instance dir by design) and remains a
supported feature.
An optional ``sha256`` field is verified when present to guard against
corruption or supply-chain substitution.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import console, frida_download, paths
from .config import InstanceConfig, Module
from .settings import settings

_CHUNK_SIZE = 1 << 16  # 64 KiB per read; balances memory and progress granularity

# Only HTTP(S) module URLs are allowed. Allowing ``file://`` would let a
# malicious ``beetroot.yaml`` exfiltrate arbitrary host files into the
# module cache (e.g. ``url: file:///etc/passwd``). Other schemes
# (``ftp:``, ``gopher:``) are not in scope either.
_ALLOWED_URL_SCHEMES: tuple[str, ...] = ("http://", "https://")


class ModuleFetchError(RuntimeError):
    """
    Raised when a module zip cannot be downloaded from its URL.
    """


class ModuleResolveError(ValueError):
    """
    Raised when a local ``path:`` module entry cannot be resolved safely.

    Currently fires when a RELATIVE ``path:`` escapes the instance
    directory (the path-traversal analogue of the file:// URL block).
    """


def _is_within(path: Path, base: Path) -> bool:
    """
    Return True if ``path`` is ``base`` itself or a descendant of it.

    Both are assumed already ``.resolve()``-d by the caller. Uses
    ``Path.is_relative_to`` (3.9+) so a sibling like ``/inst_evil`` next to
    ``/inst`` is correctly rejected (a prefix-string comparison would not).
    ``is_relative_to`` already returns True when ``path == base``.
    """
    return path.is_relative_to(base)


def _module_cache_dir() -> Path:
    return paths.user_cache_dir("modules")


def _filename_from_url(url: str) -> str:
    """
    Return the basename of the URL *path*, or ``module.zip`` if empty.

    Derived from the path component only (via :func:`urllib.parse.urlsplit`) so a
    trailing ``?query`` or ``#fragment`` never leaks into the staged filename —
    otherwise a staged ``m.zip?v=2`` would never match the ``*.zip`` flash glob
    in ``flash-modules.sh`` and the module would be silently skipped (#168).
    """
    return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or "module.zip"


def _cache_path_for_url(url: str) -> Path:
    """
    Return the cache path for a module URL.

    Two modules from different domains with the same filename would collide
    if the cache key were the basename alone. To prevent silent wrong-file
    returns, the cache file is placed inside a subdirectory named after the
    first 12 hex characters of the SHA-256 of the full URL, making the key
    unique per URL while keeping the filename human-readable.

    Args:
        url: The full module download URL.

    Returns:
        ``<module_cache_dir>/<url_hash_prefix>/<basename>``
    """
    url_prefix = hashlib.sha256(url.encode()).hexdigest()[:12]
    return _module_cache_dir() / url_prefix / _filename_from_url(url)


def _fetch_url(url: str) -> Path:
    if not url.startswith(_ALLOWED_URL_SCHEMES):
        # Belt-and-suspenders: the Module pydantic validator already
        # rejects non-http(s) schemes, but defending here too means a
        # raw _fetch_url call from a third-party script can't bypass
        # the allowlist via a hand-built string.
        raise ModuleFetchError(
            f"module url {url!r} uses an unsupported scheme; only http:// and https:// are allowed"
        )
    cache = _cache_path_for_url(url)
    if cache.exists() and cache.stat().st_size > 0:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    filename = _filename_from_url(url)
    # Stage into a process-unique temp on the cache filesystem so two concurrent
    # fetches of the same URL can't write the same fixed ``.tmp`` and publish a
    # cross-contaminated zip via the atomic rename (#185). The payload streams
    # straight to disk chunk-by-chunk rather than buffering the whole zip in RAM
    # (#227), mirroring ``rootfs_download``.
    fd, tmp_name = tempfile.mkstemp(dir=cache.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with (
            os.fdopen(fd, "wb") as out,
            urllib.request.urlopen(url, timeout=settings.http_timeout) as resp,  # noqa: S310  # scheme validated by Module pydantic model + _fetch_url allowlist
        ):
            raw_length = resp.headers.get("Content-Length")
            # Only honor a well-formed numeric header; a missing or malformed
            # Content-Length leaves the total unknown (indeterminate bar, no
            # truncation check) rather than crashing the download.
            expected_bytes: int | None = (
                int(raw_length) if isinstance(raw_length, str) and raw_length.isdigit() else None
            )
            total: float | None = float(expected_bytes) if expected_bytes is not None else None
            written = 0
            with console.progress(f"Fetching module {filename}", total=total) as bar:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    bar.advance(len(chunk))
            # A dropped connection at a chunk boundary yields a clean EOF, not an
            # exception, so a short read would otherwise cache a truncated zip and
            # re-serve it forever. Compare bytes-received to the advertised
            # Content-Length before publishing and reject a short read (#261).
            if expected_bytes is not None and written != expected_bytes:
                raise ModuleFetchError(
                    f"download truncated: got {written} of {expected_bytes} bytes for {url}; "
                    "the connection dropped mid-stream — retry the fetch"
                )
        tmp.replace(cache)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise ModuleFetchError(
            f"download failed: HTTP {e.code} fetching {url}; "
            "verify the URL is current (the upstream release may have moved)"
        ) from e
    except TimeoutError as e:
        tmp.unlink(missing_ok=True)
        raise ModuleFetchError(f"download timed out after {settings.http_timeout}s: {url}") from e
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        raise ModuleFetchError(f"download failed: cannot reach {url}: {e.reason}") from e
    except BaseException:
        # A mid-write crash (interrupt, disk-full) must not orphan the temp in
        # the user-global cache; the successful path already renamed it away.
        tmp.unlink(missing_ok=True)
        raise
    return cache


def verify_sha256(path: Path, expected: str) -> None:
    """
    Verify a file's SHA-256 digest against an expected hex value.

    Shared by the staging path (:func:`stage_for_instance` via
    ``_resolve``) and by :meth:`beetroot.backends.adb.AdbDevice.auto_install_modules`
    so both enforce the same case-insensitive comparison and raise the
    same ``sha256 mismatch`` message shape. The file is left untouched —
    callers that cache downloads decide whether a mismatch should also
    evict the bad artifact.

    Args:
        path: The file to hash.
        expected: The expected SHA-256 hex digest (case-insensitive).

    Raises:
        ValueError: If the actual digest differs from ``expected``.
    """
    actual = frida_download.sha256_of(path)
    if actual.lower() != expected.lower():
        raise ValueError(f"sha256 mismatch for {path.name}: expected {expected}, got {actual}")


def _resolve(module: Module, instance_root: Path) -> Path:
    if module.url:
        local = _fetch_url(module.url)
    else:
        # The Module pydantic validator already enforces "exactly one
        # of url / path"; this branch is a defensive net for mypy
        # narrowing and isn't covered.
        if module.path is None:  # pragma: no cover
            raise ValueError("module entry has neither url nor path set")
        local = (instance_root / module.path).resolve()
        # Contain RELATIVE path: entries to the instance dir. A relative
        # entry that resolves outside instance_root (``path: ../../etc/shadow``)
        # is the path-traversal analogue of the file:// URL exfiltration we
        # block on the url axis — reject it. An ABSOLUTE path the user typed
        # explicitly (``path: /tmp/mod.zip``) bypasses instance_root by
        # pathlib's ``/`` semantics and stays a supported feature.
        if not Path(module.path).is_absolute() and not _is_within(local, instance_root.resolve()):
            raise ModuleResolveError(
                f"module path {module.path!r} escapes the instance directory "
                f"(resolved to {local}). A relative path: entry must stay inside "
                f"the instance dir; use an explicit absolute path if you really "
                f"mean a file outside it."
            )
        if not local.exists():
            raise FileNotFoundError(f"module path not found: {local}")
    if module.sha256:
        try:
            verify_sha256(local, module.sha256)
        except ValueError:
            # On mismatch, only the regenerable URL cache is evicted so the
            # next call re-downloads rather than re-failing forever on a
            # poisoned entry. A path module's ``local`` is the user's own
            # source file on disk with no way to re-fetch it, so deleting it
            # would be irreversible data loss — leave it untouched and re-raise.
            if module.url:
                local.unlink(missing_ok=True)
            raise
    return local


def stage_for_instance(instance_root: Path, cfg: InstanceConfig) -> list[Path]:
    """
    Materialise all module zips into ``<instance_root>/modules/``. Idempotent.

    Wipes stale zips before staging so that removing a module from
    ``beetroot.yaml`` actually un-stages it on the next ``apply``.

    Args:
        instance_root: The instance directory (the one containing
            ``beetroot.yaml``). Relative ``path:`` entries in the config
            are resolved relative to this directory.
        cfg: The instance configuration containing the modules list.

    Returns:
        List of paths to the staged zip files inside the instance directory.
    """
    target = paths.instance_modules(instance_root)
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.glob("*.zip"):
        stale.unlink()
    staged: list[Path] = []
    used_names: set[str] = set()
    for index, module in enumerate(cfg.modules):
        src = _resolve(module, instance_root)
        dst = target / _unique_dest_name(src.name, used_names, index)
        used_names.add(dst.name)
        shutil.copyfile(src, dst)
        staged.append(dst)
    return staged


def _unique_dest_name(name: str, used: set[str], index: int) -> str:
    """
    Return ``name`` unchanged, or an index-prefixed variant if it collides.

    The common case — every module having a distinct basename — keeps the
    original filename so existing behaviour (and tests asserting on it) are
    preserved. Two modules whose URL/path end in the same filename would
    otherwise stage to the same destination and silently overwrite one
    another; on collision the second gets a short ``<index>_`` prefix so
    both are staged and flashed. Magisk module identity comes from inside
    the zip (``module.prop``), so renaming the staged file is safe.

    Args:
        name: The source basename to stage under.
        used: Destination basenames already claimed in this staging pass.
        index: The module's position in the list, used to derive a stable
            distinct name on collision.

    Returns:
        ``name`` if unused, else ``f"{index}_{name}"`` (recursing if that
        too collides).
    """
    if name not in used:
        return name
    return _unique_dest_name(f"{index}_{name}", used, index)
