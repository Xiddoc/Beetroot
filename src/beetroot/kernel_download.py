"""
Download and cache the binder-enabled micro-VM guest kernel on the host.

The ``binder: vm`` backend boots redroid inside a QEMU micro-VM against a
guest kernel built with the binder / binderfs / PSI deltas vendored in
``docker/vm/kernel.config``. Compiling that kernel from source takes ~20
minutes (and needs a full kernel toolchain); instead the CLI fetches a
**prebuilt** ``bzImage`` from the project's GitHub releases — mirroring
:mod:`beetroot.frida_download` — and caches it under
``$XDG_CACHE_HOME/beetroot/vm/`` (default ``~/.cache/beetroot/vm/``), shared
across every instance on the host.

The release is pinned by :data:`KERNEL_VERSION` and the downloaded image is
verified against :data:`KERNEL_SHA256`, so a hostile mirror can't substitute
a tampered kernel. Bumping the kernel means publishing a new
``vm-kernel-<version>`` release (the source recipe lives in
``.github/workflows/e2e.yml`` and ``docs/design/vm-rnd-log.md``) and updating
both constants here.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

from . import console, paths
from .settings import settings

_CHUNK_SIZE = 1 << 16  # 64 KiB per read; balances memory and progress granularity

# The prebuilt guest kernel pinned for the binder: vm backend. Keep in lockstep
# with the KERNEL_VERSION in .github/workflows/e2e.yml and the recipe in
# docs/design/vm-rnd-log.md.
KERNEL_VERSION: Final = "6.12.9"

# SHA-256 of the published bzImage-<version>-x86_64 release asset. Guards
# against a tampered or truncated download.
KERNEL_SHA256: Final = "88abab8832307a951bc5636663a5361baf511a580396183a3cc03b98d2021bab"

# The repository whose Releases host the prebuilt kernel.
_RELEASE_REPO: Final = "Xiddoc/Beetroot"

# The micro-VM backend is x86_64-only (QEMU TCG/KVM guest), so the asset arch
# is fixed rather than settings-driven like frida_download's frida_arch.
_KERNEL_ARCH: Final = "x86_64"


class KernelFetchError(RuntimeError):
    """
    Raised when the prebuilt guest kernel cannot be downloaded.

    Mirrors :class:`~beetroot.frida_download.FridaFetchError` so callers can
    catch a single named domain exception rather than the raw
    :class:`urllib.error.URLError` that surfaces underneath.
    """


def release_url(version: str) -> str:
    """
    Return the GitHub download URL for a prebuilt guest-kernel release.

    Args:
        version: The kernel release version (e.g. ``6.12.9``), matching the
            ``vm-kernel-<version>`` release tag.

    Returns:
        The full HTTPS URL to the ``bzImage`` release asset.
    """
    return (
        f"https://github.com/{_RELEASE_REPO}/releases/download/"
        f"vm-kernel-{version}/bzImage-{version}-{_KERNEL_ARCH}"
    )


def kernel_cache_dir() -> Path:
    """
    Return the user-global micro-VM artifact cache directory.
    """
    return paths.user_cache_dir("vm")


def cached_kernel(version: str = KERNEL_VERSION) -> Path:
    """
    Return the cache path for a downloaded guest ``bzImage``.

    Args:
        version: The kernel release version.

    Returns:
        Path under the user-global VM cache where the image lives.
    """
    return kernel_cache_dir() / f"bzImage-{version}-{_KERNEL_ARCH}"


def download(version: str = KERNEL_VERSION, *, expected_sha256: str | None = KERNEL_SHA256) -> Path:
    """
    Fetch the prebuilt guest kernel into the host cache. Idempotent.

    If the image already exists in the cache with non-zero size, the download
    is skipped. When ``expected_sha256`` is set, the cached (or freshly
    downloaded) image's digest is compared against it and a ``ValueError`` is
    raised on mismatch — the default pins to :data:`KERNEL_SHA256` so a
    tampered mirror is rejected.

    Args:
        version: The kernel release version to download.
        expected_sha256: Hex digest of the ``bzImage`` (default:
            :data:`KERNEL_SHA256`). Pass ``None`` to skip verification.
            Comparison is case-insensitive.

    Returns:
        Path to the cached guest ``bzImage``.

    Raises:
        KernelFetchError: On HTTP errors, network timeouts, or URL errors.
        ValueError: If ``expected_sha256`` is set and doesn't match the
            downloaded image's actual digest.
    """
    out = cached_kernel(version)
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
            with console.progress(f"Fetching micro-VM guest kernel {version}", total=total) as bar:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    bar.advance(len(chunk))
        payload = b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise KernelFetchError(f"download failed: HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        raise KernelFetchError(f"download timed out after {settings.http_timeout}s: {url}") from e
    except urllib.error.URLError as e:
        raise KernelFetchError(f"download failed: cannot reach {url}: {e.reason}") from e

    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(payload)
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
            f"sha256 mismatch for guest kernel at {path}: "
            f"expected {expected.lower()}, got {actual.lower()}"
        )


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
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()
