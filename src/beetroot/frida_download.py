"""
Download and stage frida-server binaries on the host.

Frida releases are fetched from
``github.com/frida/frida/releases/download/<version>/
frida-server-<version>-android-x86_64.xz``. The decompressed binary is
cached under ``$XDG_CACHE_HOME/beetroot/frida/`` (default
``~/.cache/beetroot/frida/``) and copied per-instance on apply, shared
across all instances on the host.
"""
from __future__ import annotations

import hashlib
import lzma
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from . import paths
from .settings import settings


def release_url(version: str) -> str:
    """
    Return the GitHub download URL for a frida-server release.

    Args:
        version: The frida release tag (e.g. ``16.4.10``).

    Returns:
        The full HTTPS URL to the ``.xz`` compressed binary.
    """
    return (
        f"https://github.com/frida/frida/releases/download/{version}/"
        f"frida-server-{version}-{settings.frida_arch}.xz"
    )


def frida_cache_dir() -> Path:
    """Return the user-global Frida binary cache directory."""
    return paths.user_cache_dir("frida")


def cached_binary(version: str) -> Path:
    """
    Return the cache path for a decompressed frida-server binary.

    Args:
        version: The frida release tag.

    Returns:
        Path under the user-global Frida cache where the binary lives.
    """
    return frida_cache_dir() / f"frida-server-{version}-{settings.frida_arch}"


def download(version: str, *, expected_sha256: str | None = None) -> Path:
    """
    Fetch and decompress frida-server into the host cache. Idempotent.

    If the binary already exists in the cache with non-zero size, the
    download is skipped. If ``expected_sha256`` is set, the cached
    (or freshly-downloaded) binary's digest is compared against it
    and a ``ValueError`` is raised on mismatch — guards against a
    hostile mirror substituting the upstream release.

    Args:
        version: The frida release tag to download.
        expected_sha256: Optional hex digest of the decompressed
            frida-server binary. Comparison is case-insensitive.

    Returns:
        Path to the cached (decompressed, executable) binary.

    Raises:
        RuntimeError: On HTTP errors, network timeouts, or URL errors.
        ValueError: If ``expected_sha256`` is set and doesn't match
            the binary's actual digest.
    """
    out = cached_binary(version)
    if out.exists() and out.stat().st_size > 0:
        _check_sha256(out, expected_sha256)
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    url = release_url(version)
    print(f"[beetroot] fetching {url}")  # noqa: T201  # researcher-facing stdout; replacing with logging would change UX
    try:
        with urllib.request.urlopen(url, timeout=settings.http_timeout) as resp:  # noqa: S310  # URL built from a pinned GitHub release path; scheme is https
            compressed = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"download failed: HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        raise RuntimeError(
            f"download timed out after {settings.http_timeout}s: {url}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"download failed: cannot reach {url}: {e.reason}") from e
    decompressed = lzma.decompress(compressed)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(decompressed)
    tmp.chmod(0o755)
    tmp.replace(out)
    _check_sha256(out, expected_sha256)
    return out


def _check_sha256(path: Path, expected: str | None) -> None:
    """Raise ``ValueError`` if ``expected`` is set and doesn't match ``path``.

    The bad file is deleted before raising so the next call re-downloads
    rather than treating the corrupt artifact as a warm cache hit.
    """
    if expected is None:
        return
    actual = sha256_of(path)
    if actual.lower() != expected.lower():
        path.unlink(missing_ok=True)
        raise ValueError(
            f"sha256 mismatch for frida-server at {path}: "
            f"expected {expected.lower()}, got {actual.lower()}"
        )


def stage_for_instance(
    instance_root: Path,
    version: str,
    *,
    expected_sha256: str | None = None,
) -> Path:
    """
    Copy the cached frida-server binary into the instance's directory.

    Args:
        instance_root: The instance directory (the one containing
            ``beetroot.yaml``). The binary is written to
            ``<instance_root>/frida-server``.
        version: Frida release tag.
        expected_sha256: Optional hex digest forwarded to
            :func:`download` for integrity verification. Comparison
            is case-insensitive.

    Returns:
        Path to the staged binary inside the instance directory.
    """
    src = download(version, expected_sha256=expected_sha256)
    dst = paths.instance_frida(instance_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o755)
    return dst


def stage_empty(instance_root: Path) -> Path:
    """
    Place a zero-byte non-executable placeholder for instances with no Frida.

    The compose bind mount is unconditional, so the file must exist.
    ``entrypoint.sh`` checks for the executable bit and skips launching
    when it's not set.

    Args:
        instance_root: The instance directory.

    Returns:
        Path to the placeholder file inside the instance directory.
    """
    dst = paths.instance_frida(instance_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"")
    dst.chmod(0o644)
    return dst


def sha256_of(path: Path) -> str:
    """
    Return the lowercase hex SHA-256 digest of a file.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hex digest string.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
