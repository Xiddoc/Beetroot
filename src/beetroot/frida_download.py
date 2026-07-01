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
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path
from typing import Final

from . import config, console, paths
from .settings import settings

# The frida-server architecture ``download`` should fetch, set by
# ``stage_for_instance`` to the backend-resolved arch around its call (#189).
# Kept as a context var rather than a ``download`` parameter so the public
# ``download`` signature stays stable for existing test doubles; ``None`` means
# "use the ``settings.frida_arch`` default" (resolved in ``cached_binary`` /
# ``release_url``).
_active_arch: ContextVar[str | None] = ContextVar("_active_arch", default=None)

# Host machine strings (``platform.machine()``) mapped to the frida-server
# architecture suffix redroid expects for a ``binder: host|auto`` instance,
# which runs the Android userspace directly against the host kernel (#189).
_MACHINE_TO_FRIDA_ARCH: Final[dict[str, str]] = {
    "aarch64": "android-arm64",
    "arm64": "android-arm64",
    "x86_64": "android-x86_64",
    "amd64": "android-x86_64",
}

# The ``binder: vm`` guest is an x86_64 micro-VM by design, so its frida-server
# is always the x86_64 build regardless of the host machine.
_VM_FRIDA_ARCH: Final = "android-x86_64"

# GitHub's per-repo "latest release" endpoint 302-redirects to the concrete
# ``.../releases/tag/<version>`` URL, so following the redirect and reading the
# final tag resolves ``latest`` without hitting the rate-limited JSON API or
# needing an auth token / User-Agent (issue #105).
_LATEST_RELEASE_URL = "https://github.com/frida/frida/releases/latest"

_CHUNK_SIZE = 1 << 16  # 64 KiB per read; balances memory and progress granularity

# Ceiling on the *decompressed* frida-server output. A real frida-server is
# tens of MB; this generous 512 MiB cap turns a corrupt or zip-bomb ``.xz`` into
# a clean ``FridaFetchError`` instead of an OOM kill of the whole process (#228).
_MAX_DECOMPRESSED_BYTES: Final[int] = 512 * 1024 * 1024


class FridaFetchError(RuntimeError):
    """
    Raised when frida-server cannot be downloaded or decompressed.

    Mirrors :class:`~beetroot.modules_download.ModuleFetchError` so callers
    can catch a single, named domain exception rather than the raw
    :class:`lzma.LZMAError` or :class:`urllib.error.URLError` that
    surfaced before this class existed.
    """


def resolve_frida_arch(binder: str) -> str:
    """
    Resolve the frida-server architecture suffix for a given backend.

    An explicit ``BEETROOT_FRIDA_ARCH`` always wins (the researcher pinned a
    cross-arch build on purpose). Otherwise the arch is backend-aware: a
    ``binder: vm`` instance always uses the x86_64 build (its guest is an
    x86_64 micro-VM), while a ``binder: host|auto`` instance runs Android
    directly against the host kernel, so the arch is detected from
    :func:`platform.machine` — an aarch64 host stages ``android-arm64`` rather
    than an x86_64 ELF that never launches on ARM (#189). An unrecognized host
    machine falls back to the ``settings.frida_arch`` default so behaviour is
    never worse than before.

    Args:
        binder: The instance's ``binder`` mode (``auto`` / ``host`` / ``vm``).

    Returns:
        The frida-server architecture suffix (e.g. ``android-arm64``).
    """
    if os.environ.get("BEETROOT_FRIDA_ARCH"):
        return settings.frida_arch
    if binder == "vm":
        return _VM_FRIDA_ARCH
    return _MACHINE_TO_FRIDA_ARCH.get(platform.machine().lower(), settings.frida_arch)


def release_url(version: str, *, arch: str | None = None) -> str:
    """
    Return the GitHub download URL for a frida-server release.

    Args:
        version: The frida release tag (e.g. ``16.4.10``).
        arch: The frida-server architecture suffix; defaults to
            ``settings.frida_arch`` when not supplied by a backend-aware caller.

    Returns:
        The full HTTPS URL to the ``.xz`` compressed binary.
    """
    arch = arch if arch is not None else settings.frida_arch
    return (
        f"https://github.com/frida/frida/releases/download/{version}/"
        f"frida-server-{version}-{arch}.xz"
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


def resolve_version(version: str, *, host_version: str | None = None) -> str:
    """
    Resolve a (possibly symbolic) ``frida.version`` to a concrete release tag.

    - a pinned ``major.minor.patch`` is returned unchanged (reproducible);
    - ``auto`` resolves to the host ``frida-tools`` version when installed (so
      the staged server matches the client you'll attach with), else ``latest``;
    - ``latest`` resolves to the current upstream release.

    Args:
        version: The configured version (``auto`` / ``latest`` / a pinned tag).
        host_version: An already-resolved host ``frida-tools`` version, if the
            caller has one (e.g. :func:`stage_for_instance` computes it once and
            reuses it for the skew check). ``None`` means "fetch it here" — used
            only on the ``auto`` path, so a pinned/``latest`` resolve never
            spawns ``frida --version``.

    Returns:
        A concrete ``major.minor.patch`` tag.

    Raises:
        FridaFetchError: On a network failure resolving ``auto`` / ``latest``,
            or an unrecognized symbolic value.
    """
    if config.is_pinned_frida_version(version):
        return version
    if version == config.FRIDA_AUTO:
        host = host_version if host_version is not None else host_frida_tools_version()
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


def cached_binary(version: str, *, arch: str | None = None) -> Path:
    """
    Return the cache path for a decompressed frida-server binary.

    Args:
        version: The frida release tag.
        arch: The frida-server architecture suffix; defaults to
            ``settings.frida_arch``. Included in the filename so an aarch64
            and an x86_64 build of the same version cache side by side (#189).

    Returns:
        Path under the user-global Frida cache where the binary lives.
    """
    arch = arch if arch is not None else settings.frida_arch
    return frida_cache_dir() / f"frida-server-{version}-{arch}"


def download(version: str, *, expected_sha256: str | None = None) -> Path:
    """
    Fetch and decompress frida-server into the host cache. Idempotent.

    If the binary already exists in the cache with non-zero size, the
    download is skipped. If ``expected_sha256`` is set, the cached
    (or freshly-downloaded) binary's digest is compared against it
    and a ``ValueError`` is raised on mismatch — guards against a
    hostile mirror substituting the upstream release.

    The architecture suffix is read from the ``_active_arch`` context var
    (default ``settings.frida_arch``), which :func:`stage_for_instance` sets to
    the backend-resolved arch around its call (#189). Keeping it out of the
    signature preserves ``download``'s public shape so existing test doubles
    stay assignment-compatible.

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
    arch = _active_arch.get()
    out = cached_binary(version, arch=arch)
    if out.exists() and out.stat().st_size > 0:
        _check_sha256(out, expected_sha256)
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    url = release_url(version, arch=arch)
    # Stage into a process-unique temp on the cache filesystem so two concurrent
    # fetches of the same version can't write a shared fixed ``.tmp`` and publish
    # a cross-contaminated binary via the atomic rename (#185). The compressed
    # payload is fed chunk-by-chunk into an incremental LZMA decompressor whose
    # output streams straight to the temp file — neither the whole compressed nor
    # the whole decompressed payload is ever resident (#227) — and the running
    # decompressed total is capped to guard against a zip-bomb ``.xz`` (#228).
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        decompressor = lzma.LZMADecompressor()
        decompressed_total = 0
        with (
            os.fdopen(fd, "wb") as handle,
            urllib.request.urlopen(url, timeout=settings.http_timeout) as resp,  # noqa: S310  # URL built from a pinned GitHub release path; scheme is https
        ):
            raw_length = resp.headers.get("Content-Length")
            total: float | None = float(raw_length) if raw_length else None
            with console.progress(f"Fetching frida-server {version}", total=total) as bar:
                while not decompressor.eof:
                    to_feed = b""
                    if decompressor.needs_input:
                        to_feed = resp.read(_CHUNK_SIZE)
                        if not to_feed:
                            # Compressed input ran out before the LZMA
                            # end-of-stream marker: a truncated/incomplete .xz
                            # that the one-shot ``lzma.decompress`` this loop
                            # replaced would have rejected (#228).
                            raise FridaFetchError(
                                f"frida-server {url} ended mid-stream (truncated or "
                                "incomplete .xz) — delete the partial cache and retry"
                            )
                        bar.advance(len(to_feed))
                    # Bound the per-call output so a single compressed chunk
                    # can't expand without limit before the ceiling check
                    # (#228); the decompressor buffers any unconsumed input for
                    # the next ``b""`` drain step (#227).
                    piece = decompressor.decompress(to_feed, max_length=_CHUNK_SIZE)
                    decompressed_total += len(piece)
                    if decompressed_total > _MAX_DECOMPRESSED_BYTES:
                        raise FridaFetchError(
                            f"frida-server {url} decompressed past the "
                            f"{_MAX_DECOMPRESSED_BYTES}-byte ceiling — the download may be "
                            "corrupt or a zip bomb; refusing to continue"
                        )
                    handle.write(piece)
        tmp.chmod(0o755)
        tmp.replace(out)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise FridaFetchError(f"download failed: HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        tmp.unlink(missing_ok=True)
        raise FridaFetchError(f"download timed out after {settings.http_timeout}s: {url}") from e
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        raise FridaFetchError(f"download failed: cannot reach {url}: {e.reason}") from e
    except lzma.LZMAError as e:
        tmp.unlink(missing_ok=True)
        raise FridaFetchError(
            f"decompression failed for frida-server {url}: the download may be "
            "corrupt or truncated — delete the partial cache and retry"
        ) from e
    except BaseException:
        # Any other failure (the ceiling guard above, an interrupt, a disk-full
        # write) must not orphan the temp in the user-global cache.
        tmp.unlink(missing_ok=True)
        raise
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


def _warn_on_client_skew(server_version: str, *, host_version: str | None = None) -> None:
    """
    Warn if the host ``frida-tools`` major+minor won't match ``server_version``.

    Frida requires the client and server to agree on major+minor, so a server
    that diverges from the host client breaks ``frida`` at *attach* time.
    Surfaced as an actionable warning here, at staging time (issue #105).
    No-op when ``frida-tools`` isn't installed (nothing to attach with yet).

    Args:
        server_version: The concrete tag being staged.
        host_version: An already-resolved host ``frida-tools`` version; ``None``
            fetches it. :func:`stage_for_instance` passes the value it already
            computed so the host version is read at most once per stage.
    """
    host = host_version if host_version is not None else host_frida_tools_version()
    if host is None:
        return
    if host.split(".")[:2] != server_version.split(".")[:2]:
        console.warn(
            f"staged frida-server {server_version} but the host frida-tools is "
            f"{host}; Frida requires the client and server major+minor to match, "
            "so `frida` will fail to attach. Set frida.version to "
            f"'{config.FRIDA_AUTO}' (or pin {host}), or upgrade frida-tools."
        )


def stage_for_instance(
    instance_root: Path,
    version: str,
    *,
    expected_sha256: str | None = None,
    binder: str = "auto",
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
        binder: The instance's ``binder`` mode, used to resolve the
            host-matching frida-server architecture (#189).

    Returns:
        Path to the staged binary inside the instance directory.
    """
    # Read the host frida-tools version once and reuse it for both resolution
    # (the ``auto`` path) and the skew check, so staging never spawns
    # ``frida --version`` more than once.
    host_version = host_frida_tools_version()
    resolved = resolve_version(version, host_version=host_version)
    if resolved != version:
        console.note(f"frida version {version!r} resolved to {resolved}")
    _warn_on_client_skew(resolved, host_version=host_version)
    # Publish the backend-resolved arch (e.g. an aarch64 ``host`` instance →
    # arm64) for ``download`` to read, restoring the prior value afterwards so
    # nested/sequential stages don't leak arch state.
    arch = resolve_frida_arch(binder)
    token = _active_arch.set(arch)
    try:
        src = download(resolved, expected_sha256=expected_sha256)
    finally:
        _active_arch.reset(token)
    dst = paths.instance_frida(instance_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Stage via a process-unique temp beside the target and ``os.replace`` on
    # success, so a download failure on a *later* re-apply can never truncate a
    # prior working binary mid-copy (#165). The download itself already
    # succeeded above, so the copy+swap here is the only remaining window.
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=".frida-server.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        tmp.chmod(0o755)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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
