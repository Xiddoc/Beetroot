"""Tests for frida_download.py — download, cache, stage frida-server."""
from __future__ import annotations

import hashlib
import lzma
import stat
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import frida_download, paths
from beetroot.settings import settings

FAKE_BINARY = b"ELF\x7f fake frida binary content"
FAKE_COMPRESSED = lzma.compress(FAKE_BINARY)
VERSION = "16.4.10"


def _make_resp(data: bytes, *, content_length: int | None = None) -> MagicMock:
    """Return a mock HTTP response that yields ``data`` in a single chunk then EOF.

    ``resp.read(n)`` returns ``data`` on the first call, then ``b""`` on all
    subsequent calls — matching the chunked-read loop in ``download()``.
    ``resp.headers.get("Content-Length")`` returns a string value when
    ``content_length`` is set, or ``None`` when it is not.
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
    return _make_resp(FAKE_COMPRESSED, content_length=len(FAKE_COMPRESSED))


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    """An empty instance directory under the isolated XDG tree."""
    root = tmp_path / "alpha"
    root.mkdir()
    return root


class TestReleaseUrl:
    def test_contains_version(self) -> None:
        url = frida_download.release_url(VERSION)
        assert VERSION in url

    def test_contains_arch(self) -> None:
        url = frida_download.release_url(VERSION)
        assert settings.frida_arch in url

    def test_is_https(self) -> None:
        url = frida_download.release_url(VERSION)
        assert url.startswith("https://")


class TestCachedBinary:
    def test_path_under_frida_cache(self, isolated_registry: Path) -> None:
        p = frida_download.cached_binary(VERSION)
        assert p.is_relative_to(frida_download.frida_cache_dir())

    def test_path_contains_version(self, isolated_registry: Path) -> None:
        p = frida_download.cached_binary(VERSION)
        assert VERSION in p.name

    def test_cache_dir_under_user_cache(self, isolated_registry: Path) -> None:
        assert frida_download.frida_cache_dir() == paths.user_cache_dir("frida")


class TestDownload:
    def test_downloads_and_decompresses(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.download(VERSION)
        assert result.exists()
        assert result.read_bytes() == FAKE_BINARY

    def test_cached_file_is_executable(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.download(VERSION)
        mode = result.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_idempotent_second_call_skips_fetch(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen) as mock_open:
            frida_download.download(VERSION)
            frida_download.download(VERSION)
        assert mock_open.call_count == 1

    def test_returns_path_to_cached_binary(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.download(VERSION)
        assert result == frida_download.cached_binary(VERSION)

    def test_cache_dir_created_automatically(self, isolated_registry: Path) -> None:
        assert not frida_download.frida_cache_dir().exists()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            frida_download.download(VERSION)
        assert frida_download.frida_cache_dir().exists()


class TestDownloadErrors:
    def test_http_error_raises_frida_fetch_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(frida_download.FridaFetchError, match="HTTP 404"):
                frida_download.download(VERSION)

    def test_timeout_raises_frida_fetch_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(frida_download.FridaFetchError, match="timed out"):
                frida_download.download(VERSION)

    def test_url_error_raises_frida_fetch_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.URLError("no route to host")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(frida_download.FridaFetchError, match="cannot reach"):
                frida_download.download(VERSION)

    def test_frida_fetch_error_is_runtime_error_subclass(self) -> None:
        # Existing callers that catch RuntimeError continue to work.
        assert issubclass(frida_download.FridaFetchError, RuntimeError)

    def test_lzma_error_raises_frida_fetch_error(self, isolated_registry: Path) -> None:
        # A corrupt or truncated .xz payload must surface as FridaFetchError,
        # not as a raw lzma.LZMAError that reveals internal implementation
        # details to the caller.
        def _bad_resp(url: str, **kwargs: object) -> MagicMock:
            return _make_resp(b"not valid lzma data")

        with patch("urllib.request.urlopen", side_effect=_bad_resp):
            with pytest.raises(frida_download.FridaFetchError, match="decompression failed"):
                frida_download.download(VERSION)


class TestSha256Of:
    def test_known_input(self, tmp_path: Path) -> None:
        data = b"hello beetroot"
        p = tmp_path / "file.bin"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert frida_download.sha256_of(p) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert frida_download.sha256_of(p) == hashlib.sha256(b"").hexdigest()


class TestStageEmpty:
    def test_creates_zero_byte_file(self, instance_root: Path) -> None:
        result = frida_download.stage_empty(instance_root)
        assert result.exists()
        assert result.stat().st_size == 0

    def test_not_executable(self, instance_root: Path) -> None:
        result = frida_download.stage_empty(instance_root)
        mode = result.stat().st_mode
        assert not (mode & stat.S_IXUSR)

    def test_path_matches_instance_frida(self, instance_root: Path) -> None:
        result = frida_download.stage_empty(instance_root)
        assert result == paths.instance_frida(instance_root)

    def test_creates_parent_dirs(self, isolated_registry: Path, tmp_path: Path) -> None:
        root = tmp_path / "deep" / "path" / "alpha"
        assert not root.exists()
        frida_download.stage_empty(root)
        assert root.exists()


class TestStageForInstance:
    def test_copies_binary_to_instance(self, instance_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.stage_for_instance(instance_root, VERSION)
        assert result.read_bytes() == FAKE_BINARY

    def test_staged_file_is_executable(self, instance_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.stage_for_instance(instance_root, VERSION)
        mode = result.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_staged_path_matches_instance_frida(self, instance_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.stage_for_instance(instance_root, VERSION)
        assert result == paths.instance_frida(instance_root)

    def test_different_instances_get_own_copy(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        alpha = tmp_path / "alpha"
        bravo = tmp_path / "bravo"
        alpha.mkdir()
        bravo.mkdir()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            frida_download.stage_for_instance(alpha, VERSION)
            frida_download.stage_for_instance(bravo, VERSION)
        assert paths.instance_frida(alpha) != paths.instance_frida(bravo)
        assert paths.instance_frida(alpha).exists()
        assert paths.instance_frida(bravo).exists()


class TestDownloadSha256:
    """T2 Agent 1: optional sha256 verification on the cached binary."""

    def test_matching_sha256_succeeds(
        self, isolated_registry: Path
    ) -> None:
        expected = hashlib.sha256(FAKE_BINARY).hexdigest()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.download(
                VERSION, expected_sha256=expected,
            )
        assert result.exists()

    def test_mismatching_sha256_raises(
        self, isolated_registry: Path
    ) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                frida_download.download(
                    VERSION, expected_sha256="0" * 64,
                )

    def test_sha256_case_insensitive(
        self, isolated_registry: Path
    ) -> None:
        expected = hashlib.sha256(FAKE_BINARY).hexdigest().upper()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.download(
                VERSION, expected_sha256=expected,
            )
        assert result.exists()

    def test_none_sha256_skips_check(
        self, isolated_registry: Path
    ) -> None:
        # The default ``expected_sha256=None`` must NOT do any
        # digest comparison — preserves the v0.3 no-sha behaviour.
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_download.download(VERSION)
        assert result.exists()

    def test_cached_binary_sha256_verified_too(
        self, isolated_registry: Path
    ) -> None:
        # Prime the cache with a non-matching binary, then call
        # download() with an expected_sha256 — verification must
        # fire on the cached file, NOT skip-because-cached.
        cache = frida_download.cached_binary(VERSION)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"some other content")
        with pytest.raises(ValueError, match="sha256 mismatch"):
            frida_download.download(
                VERSION,
                expected_sha256=hashlib.sha256(FAKE_BINARY).hexdigest(),
            )

    def test_sha256_mismatch_on_cached_file_deletes_cache(
        self, isolated_registry: Path
    ) -> None:
        # A sha256 mismatch on a cached file must delete the bad artifact
        # so the next download() call re-fetches rather than re-failing
        # forever on a poisoned cache entry.
        cache = frida_download.cached_binary(VERSION)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"bad content")
        with pytest.raises(ValueError, match="sha256 mismatch"):
            frida_download.download(VERSION, expected_sha256="0" * 64)
        assert not cache.exists(), "bad cached file should be deleted after mismatch"

    def test_sha256_mismatch_on_fresh_download_deletes_tmp_and_out(
        self, isolated_registry: Path
    ) -> None:
        # A sha256 mismatch on a freshly downloaded binary must also delete
        # the output file so the cache can't be poisoned by a bad download.
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                frida_download.download(VERSION, expected_sha256="0" * 64)
        out = frida_download.cached_binary(VERSION)
        assert not out.exists(), "output file should be deleted after mismatch on fresh download"

    def test_stage_for_instance_forwards_sha256(
        self, instance_root: Path
    ) -> None:
        # T2 Agent 1: ``stage_for_instance`` must forward the digest
        # to ``download`` so a Frida(version=..., sha256=...) block
        # in beetroot.yaml fires the verification at apply time.
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                frida_download.stage_for_instance(
                    instance_root, VERSION,
                    expected_sha256="b" * 64,
                )


class TestDownloadProgress:
    """Progress bar behaviour during frida-server downloads."""

    def test_content_length_header_produces_determinate_bar(
        self, isolated_registry: Path
    ) -> None:
        # When the response includes a Content-Length header the progress bar
        # receives a non-None total so percentage and ETA columns are shown.
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
                    FAKE_COMPRESSED, content_length=len(FAKE_COMPRESSED)
                ),
            ):
                frida_download.download(VERSION)

        assert len(captured_totals) == 1
        assert captured_totals[0] == float(len(FAKE_COMPRESSED))

    def test_missing_content_length_produces_indeterminate_bar(
        self, isolated_registry: Path
    ) -> None:
        # When Content-Length is absent the progress bar total must be None so
        # an indeterminate / pulse bar is rendered instead of a broken 0%.
        captured_totals: list[float | None] = []

        from beetroot import console as cons

        class _RecordingProgress(cons.ProgressContext):
            def __init__(self, description: str, total: float | None = None) -> None:
                captured_totals.append(total)
                super().__init__(description, total)

        with patch("beetroot.console.ProgressContext", _RecordingProgress):
            with patch(
                "urllib.request.urlopen",
                # No content_length → headers.get returns None
                side_effect=lambda url, **kw: _make_resp(FAKE_COMPRESSED),
            ):
                frida_download.download(VERSION)

        assert len(captured_totals) == 1
        assert captured_totals[0] is None

    def test_chunked_read_produces_correct_binary(self, isolated_registry: Path) -> None:
        # End-to-end: drive download() with a multi-chunk response and assert
        # that the decompressed binary on disk is byte-for-byte correct.
        half = len(FAKE_COMPRESSED) // 2
        chunk_a = FAKE_COMPRESSED[:half]
        chunk_b = FAKE_COMPRESSED[half:]

        def _multi_chunk_resp(url: str, **kw: object) -> MagicMock:
            resp = MagicMock()
            resp.read.side_effect = [chunk_a, chunk_b, b""]
            resp.headers.get.return_value = None
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_multi_chunk_resp):
            result = frida_download.download(VERSION)

        assert result.read_bytes() == FAKE_BINARY
