"""Tests for prebuilt guest-kernel fetching (``beetroot.kernel_download``)."""

from __future__ import annotations

import hashlib
import urllib.error
from pathlib import Path
from typing import override

import pytest

from beetroot import kernel_download


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


def test_config_fingerprint_is_short_stable_sha(tmp_path: Path) -> None:
    cfg = tmp_path / "kernel.config"
    cfg.write_bytes(b"CONFIG_FOO=y\n")
    fp = kernel_download.config_fingerprint(cfg)
    assert fp == hashlib.sha256(b"CONFIG_FOO=y\n").hexdigest()[:12]
    assert len(fp) == 12


def test_asset_name_and_release_url() -> None:
    assert kernel_download.asset_name("6.12.9", "abc123def456") == "bzImage-6.12.9-abc123def456"
    assert kernel_download.release_tag("6.12.9", "abc123def456") == "vm-kernel-6.12.9-abc123def456"
    url = kernel_download.release_url("6.12.9", "abc123def456")
    # Per-fingerprint tag (immutable-release compatible): the asset lives in its
    # own vm-kernel-<version>-<fingerprint> release, not a rolling 'vm-kernel'.
    assert url.endswith(
        "/releases/download/vm-kernel-6.12.9-abc123def456/bzImage-6.12.9-abc123def456"
    )
    assert url.startswith("https://github.com/Xiddoc/Beetroot/")


def test_fetch_prebuilt_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"kernel-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    url = kernel_download.release_url("6.12.9", "abc123def456")

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(payload)  # with Content-Length
        # sidecar: "<hash>  <filename>", and exercise the no-Content-Length path
        return _FakeResp(f"{digest}  bzImage-6.12.9-abc123def456\n".encode(), content_length=False)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "nested" / "bzImage"
    result = kernel_download.fetch_prebuilt(
        version="6.12.9", fingerprint="abc123def456", out_path=out
    )
    assert result == out
    assert out.read_bytes() == payload


def test_fetch_prebuilt_streams_payload_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # issue #246: the bzImage is streamed to the temp file chunk-by-chunk (hashed
    # as it writes), never buffered whole. Drive a multi-chunk payload through a
    # tiny chunk size and assert the reassembled file is byte-identical and the
    # incrementally-computed digest matched (no mismatch raised).
    payload = b"".join(bytes([i % 256]) for i in range(600))
    digest = hashlib.sha256(payload).hexdigest()
    url = kernel_download.release_url("6.12.9", "abc123def456")
    monkeypatch.setattr(kernel_download, "_CHUNK_SIZE", 7)

    reads: list[int] = []

    class _RecordingResp(_FakeResp):
        @override
        def read(self, n: int) -> bytes:
            chunk = super().read(n)
            reads.append(len(chunk))
            return chunk

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _RecordingResp(payload)
        return _FakeResp(f"{digest}  bzImage\n".encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "bzImage"
    result = kernel_download.fetch_prebuilt(
        version="6.12.9", fingerprint="abc123def456", out_path=out
    )
    assert result == out
    assert out.read_bytes() == payload
    # More than one non-empty read proves the payload arrived (and was written)
    # in pieces rather than as a single buffered blob.
    assert len([n for n in reads if n]) > 1
    assert max(n for n in reads if n) <= 7


def test_fetch_prebuilt_sha_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = kernel_download.release_url("6.12.9", "abc123def456")

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(b"kernel-bytes")
        return _FakeResp(b"0000  bzImage\n")  # wrong digest

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(kernel_download.KernelFetchError, match="sha256 mismatch"):
        kernel_download.fetch_prebuilt(
            version="6.12.9", fingerprint="abc123def456", out_path=tmp_path / "bzImage"
        )


def test_fetch_prebuilt_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        raise urllib.error.HTTPError(req_url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(kernel_download.KernelFetchError, match="HTTP 404"):
        kernel_download.fetch_prebuilt(
            version="6.12.9", fingerprint="abc123def456", out_path=tmp_path / "bzImage"
        )


def test_fetch_prebuilt_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        raise TimeoutError

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(kernel_download.KernelFetchError, match="timed out"):
        kernel_download.fetch_prebuilt(
            version="6.12.9", fingerprint="abc123def456", out_path=tmp_path / "bzImage"
        )


def test_fetch_prebuilt_url_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(kernel_download.KernelFetchError, match="cannot reach"):
        kernel_download.fetch_prebuilt(
            version="6.12.9", fingerprint="abc123def456", out_path=tmp_path / "bzImage"
        )


@pytest.mark.parametrize(
    ("raiser", "match"),
    [
        (lambda u: urllib.error.HTTPError(u, 500, "Server Error", None, None), "HTTP 500"),  # type: ignore[arg-type]
        (lambda u: TimeoutError(), "timed out"),
        (lambda u: urllib.error.URLError("down"), "cannot reach"),
    ],
    ids=["http", "timeout", "url"],
)
def test_fetch_prebuilt_sidecar_fetch_errors_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raiser: object,
    match: str,
) -> None:
    # The payload streams fine; only the sidecar fetch fails. This exercises the
    # _fetch_bytes error mapping (still used for the sidecar after #246 moved the
    # payload onto the streaming path) — a fetch failure the caller falls back on.
    url = kernel_download.release_url("6.12.9", "abc123def456")

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(b"kernel-bytes")
        raise raiser(req_url)  # type: ignore[operator]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "bzImage"
    with pytest.raises(kernel_download.KernelFetchError, match=match):
        kernel_download.fetch_prebuilt(version="6.12.9", fingerprint="abc123def456", out_path=out)
    # A failed sidecar fetch must not leave the partial payload behind.
    assert not out.exists()
    assert list(tmp_path.glob("*.tmp")) == []
