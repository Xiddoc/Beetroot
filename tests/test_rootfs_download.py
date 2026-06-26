"""Tests for prebuilt guest-rootfs fetching (``beetroot.rootfs_download``)."""

from __future__ import annotations

import hashlib
import urllib.error
from pathlib import Path

import pytest
import zstandard

from beetroot import rootfs_download


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


def _write_guest_init(tmp_path: Path, body: bytes = b"#!/bin/sh\n") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    gi = tmp_path / "guest-init.sh"
    gi.write_bytes(body)
    return gi


def test_composite_fingerprint_is_short_stable_sha(tmp_path: Path) -> None:
    gi = _write_guest_init(tmp_path)
    fp = rootfs_download.composite_fingerprint(
        android_version=14, docker_version="27.5.1", guest_init_path=gi
    )
    gi_hash = hashlib.sha256(gi.read_bytes()).hexdigest()
    preimage = f"android=14\ndocker=27.5.1\nguest-init={gi_hash}\n".encode()
    assert fp == hashlib.sha256(preimage).hexdigest()[:12]
    assert len(fp) == 12


def test_composite_fingerprint_keys_on_every_input(tmp_path: Path) -> None:
    gi = _write_guest_init(tmp_path)
    base = rootfs_download.composite_fingerprint(
        android_version=14, docker_version="27.5.1", guest_init_path=gi
    )
    # Changing the Android version changes the fingerprint.
    assert (
        rootfs_download.composite_fingerprint(
            android_version=13, docker_version="27.5.1", guest_init_path=gi
        )
        != base
    )
    # Changing the Docker bundle version changes the fingerprint.
    assert (
        rootfs_download.composite_fingerprint(
            android_version=14, docker_version="27.5.0", guest_init_path=gi
        )
        != base
    )
    # Changing guest-init.sh changes the fingerprint.
    gi2 = _write_guest_init(tmp_path / "alt", body=b"#!/bin/sh\nexit 0\n")
    assert (
        rootfs_download.composite_fingerprint(
            android_version=14, docker_version="27.5.1", guest_init_path=gi2
        )
        != base
    )


def test_asset_name_release_tag_release_url() -> None:
    assert rootfs_download.asset_name("14", "abc123def456") == "rootfs-14-abc123def456.img.zst"
    assert rootfs_download.release_tag("14", "abc123def456") == "vm-rootfs-14-abc123def456"
    url = rootfs_download.release_url("14", "abc123def456")
    assert url.endswith(
        "/releases/download/vm-rootfs-14-abc123def456/rootfs-14-abc123def456.img.zst"
    )
    assert url.startswith("https://github.com/Xiddoc/Beetroot/")


def test_fetch_prebuilt_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = b"ext4-image-bytes"
    payload = zstandard.ZstdCompressor().compress(image)
    digest = hashlib.sha256(payload).hexdigest()
    url = rootfs_download.release_url("14", "abc123def456")

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(payload)  # with Content-Length
        # sidecar over the compressed bytes; exercise the no-Content-Length path.
        return _FakeResp(
            f"{digest}  rootfs-14-abc123def456.img.zst\n".encode(), content_length=False
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "nested" / "rootdisk.img"
    result = rootfs_download.fetch_prebuilt(
        android_version=14, fingerprint="abc123def456", out_image=out, docker_version="27.5.1"
    )
    assert result == out
    assert out.read_bytes() == image
    marker = out.with_name(out.name + ".android-version")
    assert marker.read_text(encoding="utf-8") == "14\n"


def test_fetch_prebuilt_sha_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = rootfs_download.release_url("14", "abc123def456")
    out = tmp_path / "rootdisk.img"

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(zstandard.ZstdCompressor().compress(b"ext4"))
        return _FakeResp(b"0000  rootfs-14-abc123def456.img.zst\n")  # wrong digest

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match="sha256 mismatch"):
        rootfs_download.fetch_prebuilt(
            android_version=14, fingerprint="abc123def456", out_image=out, docker_version="27.5.1"
        )
    assert not out.exists()
    assert not out.with_name(out.name + ".android-version").exists()


def test_fetch_prebuilt_corrupt_zstd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = rootfs_download.release_url("14", "abc123def456")
    payload = b"not-a-valid-zstd-stream"
    digest = hashlib.sha256(payload).hexdigest()
    out = tmp_path / "rootdisk.img"

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(payload)
        return _FakeResp(f"{digest}  rootfs-14-abc123def456.img.zst\n".encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match="corrupt/truncated"):
        rootfs_download.fetch_prebuilt(
            android_version=14, fingerprint="abc123def456", out_image=out, docker_version="27.5.1"
        )
    assert not out.exists()


def test_fetch_prebuilt_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        raise urllib.error.HTTPError(req_url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match="HTTP 404"):
        rootfs_download.fetch_prebuilt(
            android_version=14,
            fingerprint="abc123def456",
            out_image=tmp_path / "rootdisk.img",
            docker_version="27.5.1",
        )


def test_fetch_prebuilt_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        raise TimeoutError

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match="timed out"):
        rootfs_download.fetch_prebuilt(
            android_version=14,
            fingerprint="abc123def456",
            out_image=tmp_path / "rootdisk.img",
            docker_version="27.5.1",
        )


def test_fetch_prebuilt_url_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match="cannot reach"):
        rootfs_download.fetch_prebuilt(
            android_version=14,
            fingerprint="abc123def456",
            out_image=tmp_path / "rootdisk.img",
            docker_version="27.5.1",
        )


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (urllib.error.HTTPError("u", 404, "nf", hdrs=None, fp=None), "HTTP 404"),  # type: ignore[arg-type]
        (TimeoutError(), "timed out"),
        (urllib.error.URLError("down"), "cannot reach"),
    ],
)
def test_fetch_prebuilt_sidecar_fetch_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception, match: str
) -> None:
    # The image streams fine but the .sha256 sidecar fetch fails — exercises the
    # error mapping in _fetch_bytes (used only for the sidecar after the move to
    # a streamed image download, issue #79).
    url = rootfs_download.release_url("14", "abc123def456")
    payload = zstandard.ZstdCompressor().compress(b"ext4")

    def fake_urlopen(req_url: str, timeout: float) -> _FakeResp:
        if req_url == url:
            return _FakeResp(payload)
        raise exc

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(rootfs_download.RootfsFetchError, match=match):
        rootfs_download.fetch_prebuilt(
            android_version=14,
            fingerprint="abc123def456",
            out_image=tmp_path / "rootdisk.img",
            docker_version="27.5.1",
        )
