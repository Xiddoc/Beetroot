"""Tests for kernel_download.py — fetch, cache, and verify the guest bzImage."""

from __future__ import annotations

import hashlib
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import kernel_download, paths

FAKE_KERNEL = b"\x1f\x8b fake bzImage payload"
FAKE_SHA256 = hashlib.sha256(FAKE_KERNEL).hexdigest()
VERSION = "6.12.9"


def _make_resp(data: bytes, *, content_length: int | None = None) -> MagicMock:
    """Return a mock HTTP response yielding ``data`` once then EOF.

    Mirrors the chunked-read loop in ``download()``: ``resp.read(n)`` returns
    ``data`` on the first call and ``b""`` after.
    """
    resp = MagicMock()
    resp.read.side_effect = [data, b""]
    resp.headers.get.side_effect = lambda key, *args: (
        str(content_length) if key == "Content-Length" and content_length is not None else None
    )
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _fake_urlopen(url: str, **kwargs: object) -> MagicMock:
    return _make_resp(FAKE_KERNEL, content_length=len(FAKE_KERNEL))


class TestReleaseUrl:
    def test_contains_version(self) -> None:
        assert VERSION in kernel_download.release_url(VERSION)

    def test_contains_vm_kernel_tag(self) -> None:
        assert f"vm-kernel-{VERSION}" in kernel_download.release_url(VERSION)

    def test_points_at_project_repo(self) -> None:
        assert "Xiddoc/Beetroot" in kernel_download.release_url(VERSION)

    def test_is_https(self) -> None:
        assert kernel_download.release_url(VERSION).startswith("https://")


class TestCachedKernel:
    def test_path_under_vm_cache(self, isolated_registry: Path) -> None:
        p = kernel_download.cached_kernel(VERSION)
        assert p.is_relative_to(kernel_download.kernel_cache_dir())

    def test_path_contains_version(self, isolated_registry: Path) -> None:
        assert VERSION in kernel_download.cached_kernel(VERSION).name

    def test_cache_dir_under_user_cache(self, isolated_registry: Path) -> None:
        assert kernel_download.kernel_cache_dir() == paths.user_cache_dir("vm")

    def test_defaults_to_pinned_version(self, isolated_registry: Path) -> None:
        assert kernel_download.cached_kernel() == kernel_download.cached_kernel(
            kernel_download.KERNEL_VERSION
        )


class TestDownload:
    def test_downloads_raw_payload(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
        assert result.exists()
        assert result.read_bytes() == FAKE_KERNEL

    def test_idempotent_second_call_skips_fetch(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen) as mock_open:
            kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
            kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
        assert mock_open.call_count == 1

    def test_returns_path_to_cached_kernel(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
        assert result == kernel_download.cached_kernel(VERSION)

    def test_cache_dir_created_automatically(self, isolated_registry: Path) -> None:
        assert not kernel_download.kernel_cache_dir().exists()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
        assert kernel_download.kernel_cache_dir().exists()

    def test_multi_chunk_payload_reassembled(self, isolated_registry: Path) -> None:
        half = len(FAKE_KERNEL) // 2

        def _multi_chunk_resp(url: str, **kw: object) -> MagicMock:
            resp = MagicMock()
            resp.read.side_effect = [FAKE_KERNEL[:half], FAKE_KERNEL[half:], b""]
            resp.headers.get.return_value = None
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_multi_chunk_resp):
            result = kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
        assert result.read_bytes() == FAKE_KERNEL


class TestDownloadErrors:
    def test_http_error_raises_kernel_fetch_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(kernel_download.KernelFetchError, match="HTTP 404"):
                kernel_download.download(VERSION)

    def test_timeout_raises_kernel_fetch_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(kernel_download.KernelFetchError, match="timed out"):
                kernel_download.download(VERSION)

    def test_url_error_raises_kernel_fetch_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.URLError("no route to host")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(kernel_download.KernelFetchError, match="cannot reach"):
                kernel_download.download(VERSION)

    def test_kernel_fetch_error_is_runtime_error_subclass(self) -> None:
        assert issubclass(kernel_download.KernelFetchError, RuntimeError)


class TestDownloadSha256:
    def test_default_sha_matches_pinned_constant(self) -> None:
        # The shipped pin: build_vm_kernel relies on download() defaulting to
        # KERNEL_SHA256, so a fetched-but-tampered kernel is rejected without
        # the caller passing anything.
        import inspect

        sig = inspect.signature(kernel_download.download)
        assert sig.parameters["expected_sha256"].default == kernel_download.KERNEL_SHA256

    def test_matching_sha256_succeeds(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)
        assert result.exists()

    def test_mismatching_sha256_raises(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                kernel_download.download(VERSION, expected_sha256="0" * 64)

    def test_sha256_case_insensitive(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = kernel_download.download(VERSION, expected_sha256=FAKE_SHA256.upper())
        assert result.exists()

    def test_none_sha256_skips_check(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = kernel_download.download(VERSION, expected_sha256=None)
        assert result.exists()

    def test_cached_kernel_sha256_verified_too(self, isolated_registry: Path) -> None:
        # Prime the cache with a non-matching image, then download() with a
        # mismatching expected digest must fire verification on the cached file.
        cache = kernel_download.cached_kernel(VERSION)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"some other content")
        with pytest.raises(ValueError, match="sha256 mismatch"):
            kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)

    def test_sha256_mismatch_on_cached_file_deletes_cache(self, isolated_registry: Path) -> None:
        cache = kernel_download.cached_kernel(VERSION)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"bad content")
        with pytest.raises(ValueError, match="sha256 mismatch"):
            kernel_download.download(VERSION, expected_sha256="0" * 64)
        assert not cache.exists(), "bad cached file should be deleted after mismatch"

    def test_sha256_mismatch_on_fresh_download_deletes_out(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                kernel_download.download(VERSION, expected_sha256="0" * 64)
        assert not kernel_download.cached_kernel(VERSION).exists()


class TestSha256Of:
    def test_known_input(self, tmp_path: Path) -> None:
        data = b"hello kernel"
        p = tmp_path / "file.bin"
        p.write_bytes(data)
        assert kernel_download.sha256_of(p) == hashlib.sha256(data).hexdigest()

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert kernel_download.sha256_of(p) == hashlib.sha256(b"").hexdigest()


class TestDownloadProgress:
    def test_content_length_header_produces_determinate_bar(
        self, isolated_registry: Path
    ) -> None:
        captured_totals: list[float | None] = []

        from beetroot import console as cons

        class _RecordingProgress(cons.ProgressContext):
            def __init__(self, description: str, total: float | None = None) -> None:
                captured_totals.append(total)
                super().__init__(description, total)

        with patch("beetroot.console.ProgressContext", _RecordingProgress):
            with patch(
                "urllib.request.urlopen",
                side_effect=lambda url, **kw: _make_resp(
                    FAKE_KERNEL, content_length=len(FAKE_KERNEL)
                ),
            ):
                kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)

        assert captured_totals == [float(len(FAKE_KERNEL))]

    def test_missing_content_length_produces_indeterminate_bar(
        self, isolated_registry: Path
    ) -> None:
        captured_totals: list[float | None] = []

        from beetroot import console as cons

        class _RecordingProgress(cons.ProgressContext):
            def __init__(self, description: str, total: float | None = None) -> None:
                captured_totals.append(total)
                super().__init__(description, total)

        with patch("beetroot.console.ProgressContext", _RecordingProgress):
            with patch(
                "urllib.request.urlopen",
                side_effect=lambda url, **kw: _make_resp(FAKE_KERNEL),
            ):
                kernel_download.download(VERSION, expected_sha256=FAKE_SHA256)

        assert captured_totals == [None]
