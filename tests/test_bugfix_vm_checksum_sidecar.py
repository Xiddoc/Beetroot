"""Regression tests: a malformed checksum sidecar must raise the module's own
*FetchError (so ``build_vm_kernel`` falls back to a local build) rather than a
raw ``IndexError`` / ``UnicodeDecodeError``.

The sidecar fetch used to parse ``.decode().split()[0]`` unguarded: a 200-OK but
empty body makes ``.split()`` return ``[]`` (``[0]`` → ``IndexError``) and a
non-UTF-8 body makes ``.decode()`` raise ``UnicodeDecodeError``. Neither
subclasses ``KernelFetchError`` / ``RootfsFetchError``, so the prebuilt-with-
local-fallback path (#79) crashed instead of compiling/baking from source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import zstandard

from beetroot import kernel_download, rootfs_download


class _FakeResp:
    """Minimal urlopen() context-manager stand-in serving fixed bytes."""

    def __init__(self, data: bytes, *, content_length: bool = True) -> None:
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(len(data))} if content_length else {}

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


@pytest.mark.parametrize("sidecar_body", [b"", b"\xff\xfe"], ids=["empty", "non-utf8"])
def test_kernel_fetch_prebuilt_malformed_sidecar_raises_kernel_fetch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar_body: bytes
) -> None:
    # The payload fetch succeeds; only the sidecar is malformed, so this isolates
    # the decode/split path.
    payload = b"kernel-bytes"
    url = kernel_download.release_url("6.12.9", "abc123def456")

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(payload)
        return _FakeResp(sidecar_body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(kernel_download.KernelFetchError, match="malformed/empty checksum sidecar"):
        kernel_download.fetch_prebuilt(
            version="6.12.9", fingerprint="abc123def456", out_path=tmp_path / "bzImage"
        )


@pytest.mark.parametrize("sidecar_body", [b"", b"\xff\xfe"], ids=["empty", "non-utf8"])
def test_rootfs_fetch_prebuilt_malformed_sidecar_raises_rootfs_fetch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar_body: bytes
) -> None:
    # The compressed image streams fine; only the sidecar is malformed.
    payload = zstandard.ZstdCompressor().compress(b"ext4-image-bytes")
    url = rootfs_download.release_url("14", "abc123def456")
    out = tmp_path / "rootdisk.img"

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(payload)
        return _FakeResp(sidecar_body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match="malformed/empty checksum sidecar"):
        rootfs_download.fetch_prebuilt(
            android_version=14, fingerprint="abc123def456", out_image=out, docker_version="27.5.1"
        )
    assert not out.exists()
    assert not out.with_name(out.name + ".android-version").exists()


def test_kernel_malformed_sidecar_distinct_from_raw_exceptions() -> None:
    # Guard the type contract the bug violated: the module's error is NOT a raw
    # IndexError / UnicodeDecodeError, so build_vm_kernel's except clause catches it.
    assert not issubclass(kernel_download.KernelFetchError, (IndexError, UnicodeDecodeError))
    assert not issubclass(rootfs_download.RootfsFetchError, (IndexError, UnicodeDecodeError))
