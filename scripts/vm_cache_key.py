#!/usr/bin/env python3
"""
Cache-key helper for the ``binder: vm`` savevm boot-cache (issue #49).

Booting redroid in the VM backend under TCG costs ~100 s (see
``docs/design/vm-rnd-log.md``). CI jobs that need a *booted* device but don't
measure boot time (functional e2e, post-boot perf) can skip that cost by
restoring a previously checkpointed, already-booted VM. The cache that holds
that checkpoint must be **keyed by the exact artifacts that produced it** — a
stale snapshot booted against a newer kernel or rootfs is worse than a cold
boot, so the key must change the instant any input changes.

This module computes that key: a stable digest over the named input files
(their basenames + streamed SHA-256), independent of argument order. Point it
at the built ``bzImage`` + ``rootdisk.img`` (the guest binaries) and/or the
guest-defining sources (``kernel.config``, ``build-rootfs.sh``,
``guest-init.sh``) — whichever set the cache should invalidate on.

Kept dependency-free and unit-tested (``tests/test_vm_cache_key.py``) so the
design doc's "keyed by the kernel+rootfs hash" claim is enforced, not assumed.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

# Prepended to the digest so the cache key reads as what it is in the Actions
# cache UI. ``actions/cache`` keys are opaque strings; a prefix keeps them
# greppable.
DEFAULT_PREFIX = "vm-snapshot"

# How much of the combined digest to keep. 16 hex chars (64 bits) is far past
# collision risk for a per-repo cache namespace and keeps the key readable.
_KEY_HEX_LEN = 16

# Streaming read size for hashing the (multi-GB) rootfs without loading it all.
_CHUNK = 1024 * 1024


def hash_file(path: Path) -> str:
    """
    Return the streamed SHA-256 of a file's contents.

    Args:
        path: The file to hash. Read in chunks so a multi-GB rootfs image is
            never loaded into memory at once.

    Returns:
        The hex digest of the file's contents.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def compute_cache_key(paths: list[Path], *, prefix: str = DEFAULT_PREFIX) -> str:
    """
    Compute a stable cache key over a set of input files.

    The key folds each file's *basename* and content hash into one digest,
    sorted by ``(basename, content-hash)`` so the result is independent of the
    order the paths are passed — even when two inputs share a basename (issue
    #235). Including the basename means renaming an input (e.g. swapping which
    rootfs is staged) changes the key even if two files share content.

    Args:
        paths: The input files the cached snapshot depends on (kernel, rootfs,
            and/or the guest-defining sources).
        prefix: A human-readable prefix for the returned key.

    Returns:
        A key of the form ``<prefix>-<16-hex>``.

    Raises:
        ValueError: If ``paths`` is empty (a key over nothing is meaningless).
        FileNotFoundError: If any path does not exist.
    """
    if not paths:
        raise ValueError("compute_cache_key needs at least one input path")
    # Hash each input exactly once (a rootfs is multi-GB; hashing it inside the
    # sort key AND again in the fold would double its cost). Precompute the
    # {path: digest} map, then sort + fold off it.
    digests = {path: hash_file(path) for path in paths}
    combined = hashlib.sha256()
    for path in sorted(digests, key=lambda p: (p.name, digests[p])):
        combined.update(path.name.encode())
        combined.update(b"\0")
        combined.update(digests[path].encode())
        combined.update(b"\0")
    return f"{prefix}-{combined.hexdigest()[:_KEY_HEX_LEN]}"


def main(argv: list[str] | None = None) -> int:
    """
    Print the cache key for the given input files.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success.
    """
    parser = argparse.ArgumentParser(description="Compute the binder:vm savevm cache key")
    parser.add_argument("paths", nargs="+", type=Path, help="input files (kernel, rootfs, sources)")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="cache-key prefix")
    args = parser.parse_args(argv)
    print(compute_cache_key(args.paths, prefix=args.prefix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
