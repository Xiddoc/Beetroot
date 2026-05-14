"""
Download and stage frida-server binaries on the host.

Frida releases are fetched from
``github.com/frida/frida/releases/download/<version>/
frida-server-<version>-android-x86_64.xz``. The decompressed binary is
cached under ``.cache/frida/<version>`` and copied per-instance on apply.
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


def cached_binary(version: str) -> Path:
    """
    Return the cache path for a decompressed frida-server binary.

    Args:
        version: The frida release tag.

    Returns:
        Path under ``.cache/frida/`` where the binary will be stored.
    """
    return paths.frida_cache_dir() / f"frida-server-{version}-{settings.frida_arch}"


def download(version: str) -> Path:
    """
    Fetch and decompress frida-server into the host cache. Idempotent.

    If the binary already exists in the cache with non-zero size, the
    download is skipped.

    Args:
        version: The frida release tag to download.

    Returns:
        Path to the cached (decompressed, executable) binary.

    Raises:
        RuntimeError: On HTTP errors, network timeouts, or URL errors.
    """
    out = cached_binary(version)
    if out.exists() and out.stat().st_size > 0:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    url = release_url(version)
    print(f"[beetroot] fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=settings.http_timeout) as resp:
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
    return out


def stage_for_instance(name: str, version: str) -> Path:
    """
    Copy the cached frida-server binary into ``instances/<name>/frida-server``.

    Args:
        name: Instance name.
        version: Frida release tag.

    Returns:
        Path to the staged binary inside the instance directory.
    """
    src = download(version)
    dst = paths.instance_frida(name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o755)
    return dst


def stage_empty(name: str) -> Path:
    """
    Place a zero-byte non-executable placeholder for instances with no Frida.

    The compose bind mount is unconditional, so the file must exist.
    ``entrypoint.sh`` checks for the executable bit and skips launching
    when it's not set.

    Args:
        name: Instance name.

    Returns:
        Path to the placeholder file inside the instance directory.
    """
    dst = paths.instance_frida(name)
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
