"""
Fetch prebuilt ``binder: vm`` guest rootfs images from GitHub Releases.

Assembling the guest rootfs is the *other* long pole of ``beetroot build
--vm-kernel`` (the kernel compile being the first, addressed by
:mod:`beetroot.kernel_download`). The local bake stages busybox + the Docker
static bundle + socat + ``guest-init.sh`` and then **pulls + bakes a ~2 GiB
redroid image** into ``/var/lib/docker`` — which needs a running Docker daemon
and a Docker Hub round-trip. A fresh host — a CI runner, or a throwaway Claude
Code on the web sandbox — has neither cheaply.

This module lets the CLI download a **prebuilt** rootfs image (a zstd-compressed
ext4 blob) over plain HTTPS instead, so a fresh host needs no dockerd and no
Docker Hub pull. The image is published as a release asset on the Beetroot repo,
named per Android version **and** a composite fingerprint over the three inputs
that determine the baked bytes: the Android major version, the pinned Docker
static-bundle version, and the bundled ``guest-init.sh``. Change any of those and
the fingerprint changes, the prebuilt no longer matches, and the caller falls
back to a local bake — so you can never boot a stale prebuilt rootfs.

Each (version, fingerprint) pair gets its **own** release, tagged
``vm-rootfs-<version>-<fingerprint>``, with the ``.img.zst`` + ``.sha256``
attached at creation. A per-fingerprint tag (rather than one rolling
``vm-rootfs`` release that accrues assets) is what makes the publish compatible
with **immutable releases**: an immutable release freezes its assets at
creation, so you cannot append a new image to an existing one — but you can
always create a brand-new release with its asset already attached. This mirrors
:mod:`beetroot.kernel_download` one-to-one.

A ``.sha256`` sidecar is fetched alongside the **compressed** image and verified
before decompression — this guards against a truncated/corrupt download. It is
*not* a supply-chain anchor (an attacker controlling the release controls both
files); the trust model is "the release was produced by the repo's pinned CI
from pinned source", same as :mod:`beetroot.kernel_download`.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

import zstandard

from . import console
from .settings import settings

_CHUNK_SIZE = 1 << 16  # 64 KiB per read; matches kernel_download for parity
_REPO = "Xiddoc/Beetroot"
# Each rootfs gets its own immutable release tagged vm-rootfs-<version>-<fp>;
# the publishing workflow (rootfs-release.yml) creates it with the asset already
# attached. See module docstring for why this beats a rolling tag.
_RELEASE_TAG_PREFIX = "vm-rootfs"

# Suffix of the marker file the local bake writes (and the VM backend reads) to
# record the baked Android version. Kept in sync with
# builder._ROOTFS_VERSION_MARKER_SUFFIX; duplicated here rather than imported to
# avoid a builder <-> rootfs_download import cycle (builder imports this module).
_ROOTFS_VERSION_MARKER_SUFFIX = ".android-version"


class RootfsFetchError(RuntimeError):
    """
    Raised when a prebuilt guest rootfs cannot be downloaded or verified.

    Callers (notably :func:`beetroot.builder.build_vm_kernel`) catch this to
    fall back to assembling the rootfs locally.
    """


def composite_fingerprint(
    *, android_version: int, docker_version: str, guest_init_path: Path
) -> str:
    """
    Return a short, stable fingerprint of the three rootfs-determining inputs.

    Unlike the kernel fingerprint (a hash of a single config file), the baked
    rootfs bytes are a pure function of *three* inputs — the Android major
    version, the pinned Docker static-bundle version, and ``guest-init.sh`` — so
    the fingerprint must key on all three. The preimage is a newline-delimited
    ``key=value`` block with the keys ``android``, ``docker`` and
    ``guest-init`` (the last being the SHA-256 of the file), in that order, each
    followed by a newline — so the publishing workflow can reproduce it
    trivially in shell; the two must agree for a host to find its prebuilt
    rootfs.

    Args:
        android_version: The Android major version baked into the rootfs.
        docker_version: The pinned Docker static-bundle version.
        guest_init_path: Path to the bundled ``guest-init.sh`` installed as
            ``/init`` in the guest.

    Returns:
        The first 12 hex chars of the SHA-256 of the preimage.
    """
    guest_init_hash = hashlib.sha256(guest_init_path.read_bytes()).hexdigest()
    preimage = (
        f"android={android_version}\ndocker={docker_version}\nguest-init={guest_init_hash}\n"
    ).encode()
    return hashlib.sha256(preimage).hexdigest()[:12]


def asset_name(version: str, fingerprint: str) -> str:
    """
    Return the release asset filename for a (version, fingerprint) pair.

    Args:
        version: The Android major version (e.g. ``14``).
        fingerprint: The :func:`composite_fingerprint` of the rootfs inputs.

    Returns:
        The asset filename, e.g. ``rootfs-14-abc123def456.img.zst``.
    """
    return f"rootfs-{version}-{fingerprint}.img.zst"


def release_tag(version: str, fingerprint: str) -> str:
    """
    Return the release tag holding a (version, fingerprint) rootfs.

    Each rootfs lives in its own release, ``vm-rootfs-<version>-<fingerprint>``,
    so the publish stays compatible with immutable releases (the asset is
    attached at creation, never appended). The publishing workflow
    (``rootfs-release.yml``) creates exactly this tag.

    Args:
        version: The Android major version.
        fingerprint: The :func:`composite_fingerprint` of the rootfs inputs.

    Returns:
        The release tag, e.g. ``vm-rootfs-14-abc123def456``.
    """
    return f"{_RELEASE_TAG_PREFIX}-{version}-{fingerprint}"


def release_url(version: str, fingerprint: str) -> str:
    """
    Return the GitHub download URL for a prebuilt guest rootfs.

    Args:
        version: The Android major version.
        fingerprint: The composite fingerprint of the rootfs inputs.

    Returns:
        The full HTTPS URL to the ``.img.zst`` release asset.
    """
    return (
        f"https://github.com/{_REPO}/releases/download/"
        f"{release_tag(version, fingerprint)}/{asset_name(version, fingerprint)}"
    )


def _fetch_bytes(url: str, description: str) -> bytes:
    """
    Download ``url`` into memory with a progress bar, mapping errors.

    Raises:
        RootfsFetchError: On HTTP errors, timeouts, or unreachable URLs.
    """
    try:
        with urllib.request.urlopen(url, timeout=settings.http_timeout) as resp:  # noqa: S310  # URL built from a pinned GitHub release path; scheme is https
            raw_length = resp.headers.get("Content-Length")
            total: float | None = float(raw_length) if raw_length else None
            chunks: list[bytes] = []
            with console.progress(description, total=total) as bar:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    bar.advance(len(chunk))
        return b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise RootfsFetchError(f"HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        raise RootfsFetchError(f"timed out after {settings.http_timeout}s: {url}") from e
    except urllib.error.URLError as e:
        raise RootfsFetchError(f"cannot reach {url}: {e.reason}") from e


def fetch_prebuilt(
    *, android_version: int, fingerprint: str, out_image: Path, docker_version: str
) -> Path:
    """
    Download the prebuilt rootfs for (android_version, fingerprint) to ``out_image``.

    Fetches the zstd-compressed image and its ``.sha256`` sidecar, verifies the
    digest of the **compressed** bytes (matching what the publishing workflow's
    ``sha256sum <asset>`` records — so a truncated download is caught before
    decompression), decompresses, and writes the raw ext4 image atomically. Also
    writes the ``.android-version`` marker beside the image so the VM backend's
    rootfs-version skew check sees a fetched image identically to a baked one
    (issue #82).

    Args:
        android_version: The Android major version (selects the asset + marker).
        fingerprint: The :func:`composite_fingerprint` of the local rootfs inputs.
        out_image: Where to write the verified, decompressed rootfs image.
        docker_version: The pinned Docker static-bundle version (unused for the
            URL — already folded into ``fingerprint`` — but kept in the signature
            so the call shape matches what the caller threads through; documents
            that the version is part of the rootfs identity).

    Returns:
        ``out_image``, now holding the verified, decompressed rootfs image.

    Raises:
        RootfsFetchError: If the asset (or its checksum) cannot be fetched, the
            downloaded bytes do not match the published digest, or the payload is
            not a valid zstd stream.
    """
    del docker_version  # folded into ``fingerprint``; named for call-shape parity
    version = str(android_version)
    url = release_url(version, fingerprint)
    payload = _fetch_bytes(url, f"Fetching prebuilt guest rootfs (Android {version})")
    expected = _fetch_bytes(f"{url}.sha256", "Fetching rootfs checksum").decode().split()[0].strip()
    actual = hashlib.sha256(payload).hexdigest()
    if actual.lower() != expected.lower():
        raise RootfsFetchError(
            f"sha256 mismatch for {url}: expected {expected.lower()}, got {actual.lower()}"
        )
    try:
        image = zstandard.ZstdDecompressor().decompress(payload)
    except zstandard.ZstdError as e:
        raise RootfsFetchError(f"corrupt/truncated zstd payload for {url}: {e}") from e
    out_image.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_image.with_suffix(".tmp")
    tmp.write_bytes(image)
    tmp.replace(out_image)
    marker = out_image.with_name(out_image.name + _ROOTFS_VERSION_MARKER_SUFFIX)
    marker.write_text(f"{android_version}\n", encoding="utf-8")
    return out_image
