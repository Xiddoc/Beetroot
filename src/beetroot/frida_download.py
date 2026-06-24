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
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import config, console, paths
from .settings import settings

# GitHub's per-repo "latest release" endpoint 302-redirects to the concrete
# ``.../releases/tag/<version>`` URL, so following the redirect and reading the
# final tag resolves ``latest`` without hitting the rate-limited JSON API or
# needing an auth token / User-Agent (issue #105).
_LATEST_RELEASE_URL = "https://github.com/frida/frida/releases/latest"

_CHUNK_SIZE = 1 << 16  # 64 KiB per read; balances memory and progress granularity


class FridaFetchError(RuntimeError):
    """
    Raised when frida-server cannot be downloaded or decompressed.

    Mirrors :class:`~beetroot.modules_download.ModuleFetchError` so callers
    can catch a single, named domain exception rather than the raw
    :class:`lzma.LZMAError` or :class:`urllib.error.URLError` that
    surfaced before this class existed.
    """


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


def host_frida_tools_version() -> str | None:
    """
    Return the host ``frida-tools`` version (from ``frida --version``), or ``None``.

    ``None`` means there's no usable host client version to match against — the
    ``frida`` CLI isn't on PATH (the optional ``[frida]`` extra) or its output
    isn't a concrete ``major.minor.patch`` tag — so callers treat "no host
    version" uniformly.

    Returns:
        The host client version (e.g. ``16.4.10``), or ``None``.
    """
    if shutil.which("frida") is None:
        return None
    try:
        result = subprocess.run(
            ["frida", "--version"],  # noqa: S607  # `frida` resolved via PATH; fixed argv
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.http_timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    version = result.stdout.strip()
    return version if config.is_pinned_frida_version(version) else None


def latest_release_tag() -> str:
    """
    Resolve the current frida release tag via the GitHub latest-release redirect.

    Returns:
        The concrete tag the ``latest`` release points at (e.g. ``16.7.19``).

    Raises:
        FridaFetchError: If the redirect can't be reached, or the resolved URL
            doesn't end in a recognizable ``major.minor.patch`` tag.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310  # pinned https GitHub URL
            _LATEST_RELEASE_URL, timeout=settings.http_timeout
        ) as resp:
            # urlopen follows the 302; geturl() is the resolved tag URL. Typed
            # explicitly because urlopen's return type erases to ``Any``.
            final_url: str = resp.geturl()
    except (urllib.error.URLError, TimeoutError) as e:
        raise FridaFetchError(f"could not resolve frida 'latest' release: {e}") from e
    tag = final_url.rstrip("/").rsplit("/", 1)[-1]
    if not config.is_pinned_frida_version(tag):
        raise FridaFetchError(
            f"frida 'latest' resolved to an unexpected tag {tag!r} (from {final_url})"
        )
    return tag


def resolve_version(version: str) -> str:
    """
    Resolve a (possibly symbolic) ``frida.version`` to a concrete release tag.

    - a pinned ``major.minor.patch`` is returned unchanged (reproducible);
    - ``auto`` resolves to the host ``frida-tools`` version when installed (so
      the staged server matches the client you'll attach with), else ``latest``;
    - ``latest`` resolves to the current upstream release.

    Args:
        version: The configured version (``auto`` / ``latest`` / a pinned tag).

    Returns:
        A concrete ``major.minor.patch`` tag.

    Raises:
        FridaFetchError: On a network failure resolving ``auto`` / ``latest``,
            or an unrecognized symbolic value.
    """
    if config.is_pinned_frida_version(version):
        return version
    if version == config.FRIDA_AUTO:
        host = host_frida_tools_version()
        return host if host is not None else latest_release_tag()
    if version == config.FRIDA_LATEST:
        return latest_release_tag()
    raise FridaFetchError(
        f"unrecognized frida version {version!r} "
        f"(expected '{config.FRIDA_AUTO}', '{config.FRIDA_LATEST}', or major.minor.patch)"
    )


def frida_cache_dir() -> Path:
    """
    Return the user-global Frida binary cache directory.
    """
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
        FridaFetchError: On HTTP errors, network timeouts, URL errors, or
            a corrupt/truncated ``.xz`` payload that cannot be decompressed.
        ValueError: If ``expected_sha256`` is set and doesn't match
            the binary's actual digest.
    """
    out = cached_binary(version)
    if out.exists() and out.stat().st_size > 0:
        _check_sha256(out, expected_sha256)
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    url = release_url(version)
    try:
        with urllib.request.urlopen(url, timeout=settings.http_timeout) as resp:  # noqa: S310  # URL built from a pinned GitHub release path; scheme is https
            raw_length = resp.headers.get("Content-Length")
            total: float | None = float(raw_length) if raw_length else None
            chunks: list[bytes] = []
            with console.progress(f"Fetching frida-server {version}", total=total) as bar:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    bar.advance(len(chunk))
        compressed = b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise FridaFetchError(f"download failed: HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        raise FridaFetchError(f"download timed out after {settings.http_timeout}s: {url}") from e
    except urllib.error.URLError as e:
        raise FridaFetchError(f"download failed: cannot reach {url}: {e.reason}") from e
    try:
        decompressed = lzma.decompress(compressed)
    except lzma.LZMAError as e:
        raise FridaFetchError(
            f"decompression failed for frida-server {url}: the download may be "
            "corrupt or truncated — delete the partial cache and retry"
        ) from e
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(decompressed)
    tmp.chmod(0o755)
    tmp.replace(out)
    _check_sha256(out, expected_sha256)
    return out


def _check_sha256(path: Path, expected: str | None) -> None:
    """
    Raise ``ValueError`` if ``expected`` is set and doesn't match ``path``.

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


def _warn_on_client_skew(server_version: str) -> None:
    """
    Warn if the host ``frida-tools`` major+minor won't match ``server_version``.

    Frida requires the client and server to agree on major+minor, so a server
    that diverges from the host client breaks ``beetroot frida`` at *attach*
    time. Surfaced as an actionable warning here, at staging time (issue #105).
    No-op when ``frida-tools`` isn't installed (nothing to attach with yet).
    """
    host = host_frida_tools_version()
    if host is None:
        return
    if host.split(".")[:2] != server_version.split(".")[:2]:
        console.warn(
            f"staged frida-server {server_version} but the host frida-tools is "
            f"{host}; Frida requires the client and server major+minor to match, "
            "so `beetroot frida` will fail to attach. Set frida.version to "
            f"'{config.FRIDA_AUTO}' (or pin {host}), or upgrade frida-tools."
        )


def stage_for_instance(
    instance_root: Path,
    version: str,
    *,
    expected_sha256: str | None = None,
) -> Path:
    """
    Copy the cached frida-server binary into the instance's directory.

    The ``version`` is resolved first (``auto`` / ``latest`` → a concrete tag,
    see :func:`resolve_version`), then a host-client/server skew warning is
    surfaced before the binary is staged.

    Args:
        instance_root: The instance directory (the one containing
            ``beetroot.yaml``). The binary is written to
            ``<instance_root>/frida-server``.
        version: Frida version selector — ``auto`` / ``latest`` / a pinned tag.
        expected_sha256: Optional hex digest forwarded to
            :func:`download` for integrity verification. Comparison
            is case-insensitive. Only valid with a pinned ``version``
            (enforced by :class:`beetroot.config.Frida`).

    Returns:
        Path to the staged binary inside the instance directory.
    """
    resolved = resolve_version(version)
    if resolved != version:
        console.note(f"frida version {version!r} resolved to {resolved}")
    _warn_on_client_skew(resolved)
    src = download(resolved, expected_sha256=expected_sha256)
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
