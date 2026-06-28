"""
Fetch prebuilt ``binder: vm`` guest kernels from GitHub Releases.

Compiling the binder-enabled guest kernel from source is the long pole of
``beetroot build --vm-kernel`` (~7 min cold, even after the config trim, and
ccache only helps *re*builds). A fresh host — a CI runner, or a throwaway
Claude Code on the web sandbox — is always cold, so it pays the full price
every time.

This module lets the CLI download a **prebuilt** ``bzImage`` (~12 MiB) instead.
The kernel is published as a release asset on the Beetroot repo, named by the
pinned kernel version **and** a fingerprint of the bundled ``kernel.config`` —
so a host only ever fetches a kernel built from *exactly* the config it ships.
Edit the config (or bump the version) and the fingerprint changes,
the prebuilt no longer matches, and the caller falls back to a source compile.
That keeps the vendored config the single source of truth: you can never boot a
stale prebuilt kernel.

Each (version, fingerprint) pair gets its **own** release, tagged
``vm-kernel-<version>-<fingerprint>``, with the ``bzImage`` + ``.sha256``
attached at creation. A per-fingerprint tag (rather than one rolling
``vm-kernel`` release that accrues assets) is what makes the publish compatible
with **immutable releases**: an immutable release freezes its assets at
creation, so you cannot append a new kernel to an existing one — but you can
always create a brand-new release with its asset already attached.

A ``.sha256`` sidecar is fetched alongside the binary and verified — this
guards against a truncated/corrupt download. It is *not* a supply-chain anchor
(an attacker controlling the release controls both files); the trust model is
"the release was produced by the repo's pinned CI from pinned source", same as
:mod:`beetroot.frida_download` fetching frida-server.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

from . import console
from .settings import settings

_CHUNK_SIZE = 1 << 16  # 64 KiB per read; matches frida_download for parity
_REPO = "Xiddoc/Beetroot"
# Each kernel gets its own immutable release tagged vm-kernel-<version>-<fp>;
# the publishing workflow (vm-kernel-release.yml) creates it with the asset
# already attached. See module docstring for why this beats a rolling tag.
_RELEASE_TAG_PREFIX = "vm-kernel"


class KernelFetchError(RuntimeError):
    """
    Raised when a prebuilt guest kernel cannot be downloaded or verified.

    Callers (notably :func:`beetroot.builder.build_vm_kernel`) catch this to
    fall back to compiling the kernel from source.
    """


def config_fingerprint(config_path: Path) -> str:
    """
    Return a short, stable fingerprint of the kernel config fragment.

    The first 12 hex chars of the SHA-256 of the file's bytes. The publishing
    workflow computes the same value with ``sha256sum
    src/beetroot/templates/vm/kernel.config | cut -c1-12`` — the two must agree
    for a host to find its prebuilt kernel.

    Args:
        config_path: Path to the bundled ``kernel.config`` fragment.

    Returns:
        The 12-character hex fingerprint.
    """
    return hashlib.sha256(config_path.read_bytes()).hexdigest()[:12]


def asset_name(version: str, fingerprint: str) -> str:
    """
    Return the release asset filename for a (version, fingerprint) pair.

    Args:
        version: The pinned kernel version (e.g. ``6.12.9``).
        fingerprint: The :func:`config_fingerprint` of the config fragment.

    Returns:
        The asset filename, e.g. ``bzImage-6.12.9-abc123def456``.
    """
    return f"bzImage-{version}-{fingerprint}"


def release_tag(version: str, fingerprint: str) -> str:
    """
    Return the release tag holding a (version, fingerprint) kernel.

    Each kernel lives in its own release, ``vm-kernel-<version>-<fingerprint>``,
    so the publish stays compatible with immutable releases (the asset is
    attached at creation, never appended). The publishing workflow
    (``vm-kernel-release.yml``) creates exactly this tag.

    Args:
        version: The pinned kernel version.
        fingerprint: The :func:`config_fingerprint` of the config fragment.

    Returns:
        The release tag, e.g. ``vm-kernel-6.12.9-abc123def456``.
    """
    return f"{_RELEASE_TAG_PREFIX}-{version}-{fingerprint}"


def release_url(version: str, fingerprint: str) -> str:
    """
    Return the GitHub download URL for a prebuilt guest kernel.

    Args:
        version: The pinned kernel version.
        fingerprint: The config fragment fingerprint.

    Returns:
        The full HTTPS URL to the ``bzImage`` release asset.
    """
    return (
        f"https://github.com/{_REPO}/releases/download/"
        f"{release_tag(version, fingerprint)}/{asset_name(version, fingerprint)}"
    )


def _fetch_bytes(url: str, description: str) -> bytes:
    """
    Download ``url`` into memory with a progress bar, mapping errors.

    Raises:
        KernelFetchError: On HTTP errors, timeouts, or unreachable URLs.
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
        raise KernelFetchError(f"HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        raise KernelFetchError(f"timed out after {settings.http_timeout}s: {url}") from e
    except urllib.error.URLError as e:
        raise KernelFetchError(f"cannot reach {url}: {e.reason}") from e


def fetch_prebuilt(*, version: str, fingerprint: str, out_path: Path) -> Path:
    """
    Download the prebuilt ``bzImage`` for (version, fingerprint) to ``out_path``.

    Fetches the binary and its ``.sha256`` sidecar, verifies the digest, and
    writes the kernel atomically to ``out_path``.

    Args:
        version: The pinned kernel version.
        fingerprint: The :func:`config_fingerprint` of the local config.
        out_path: Where to write the verified ``bzImage``.

    Returns:
        ``out_path``, now holding the verified prebuilt kernel.

    Raises:
        KernelFetchError: If the asset (or its checksum) cannot be fetched, or
            the downloaded bytes do not match the published digest.
    """
    url = release_url(version, fingerprint)
    payload = _fetch_bytes(url, f"Fetching prebuilt guest kernel {version}")
    sidecar = _fetch_bytes(f"{url}.sha256", "Fetching kernel checksum")
    try:
        expected = sidecar.decode().split()[0].strip()
    except (UnicodeDecodeError, IndexError) as e:
        # A 200-OK but empty (``.split()`` → ``[]``) or non-UTF-8 sidecar must
        # fall back to a source compile like any other fetch failure, not crash
        # ``build_vm_kernel`` (which only catches ``KernelFetchError``).
        raise KernelFetchError(f"malformed/empty checksum sidecar {url}.sha256: {e}") from e
    actual = hashlib.sha256(payload).hexdigest()
    if actual.lower() != expected.lower():
        raise KernelFetchError(
            f"sha256 mismatch for {url}: expected {expected.lower()}, got {actual.lower()}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_bytes(payload)
    tmp.replace(out_path)
    return out_path
