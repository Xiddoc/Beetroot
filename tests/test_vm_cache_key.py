"""
Unit tests for ``scripts/vm_cache_key.py`` (issue #49).

The cache key must be deterministic, order-independent, and change the instant
any input file's name or content changes — that is the whole correctness
contract of the savevm boot-cache (a stale snapshot is worse than a cold boot).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vm_cache_key.py"
_SPEC = importlib.util.spec_from_file_location("vm_cache_key", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
vm_cache_key: ModuleType = importlib.util.module_from_spec(_SPEC)
sys.modules["vm_cache_key"] = vm_cache_key
_SPEC.loader.exec_module(vm_cache_key)


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_key_is_deterministic(tmp_path: Path) -> None:
    a = _write(tmp_path / "bzImage", b"kernel")
    b = _write(tmp_path / "rootdisk.img", b"rootfs")
    k1 = vm_cache_key.compute_cache_key([a, b])
    k2 = vm_cache_key.compute_cache_key([a, b])
    assert k1 == k2
    assert k1.startswith("vm-snapshot-")


def test_key_is_order_independent(tmp_path: Path) -> None:
    a = _write(tmp_path / "bzImage", b"kernel")
    b = _write(tmp_path / "rootdisk.img", b"rootfs")
    assert vm_cache_key.compute_cache_key([a, b]) == vm_cache_key.compute_cache_key([b, a])


def test_key_changes_when_content_changes(tmp_path: Path) -> None:
    a = _write(tmp_path / "bzImage", b"kernel")
    b = _write(tmp_path / "rootdisk.img", b"rootfs")
    before = vm_cache_key.compute_cache_key([a, b])
    _write(a, b"kernel-v2")
    assert vm_cache_key.compute_cache_key([a, b]) != before


def test_key_changes_when_filename_changes(tmp_path: Path) -> None:
    a = _write(tmp_path / "bzImage", b"kernel")
    renamed = _write(tmp_path / "bzImage-new", b"kernel")  # same content, new name
    assert vm_cache_key.compute_cache_key([a]) != vm_cache_key.compute_cache_key([renamed])


def test_prefix_is_honored(tmp_path: Path) -> None:
    a = _write(tmp_path / "bzImage", b"kernel")
    assert vm_cache_key.compute_cache_key([a], prefix="boot").startswith("boot-")


def test_empty_paths_rejected() -> None:
    with pytest.raises(ValueError, match="at least one input path"):
        vm_cache_key.compute_cache_key([])


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        vm_cache_key.compute_cache_key([tmp_path / "nope"])


def test_hash_file_streams_large_content(tmp_path: Path) -> None:
    big = _write(tmp_path / "big", b"x" * (3 * 1024 * 1024 + 7))  # spans >3 chunks
    import hashlib

    assert vm_cache_key.hash_file(big) == hashlib.sha256(b"x" * (3 * 1024 * 1024 + 7)).hexdigest()


def test_cli_prints_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = _write(tmp_path / "bzImage", b"kernel")
    b = _write(tmp_path / "rootdisk.img", b"rootfs")
    rc = vm_cache_key.main([str(a), str(b)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == vm_cache_key.compute_cache_key([a, b])
