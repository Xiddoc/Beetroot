"""Tests for frida_dl.py — download, cache, stage frida-server."""
from __future__ import annotations

import hashlib
import lzma
import stat
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import frida_dl, paths
from beetroot.settings import settings

FAKE_BINARY = b"ELF\x7f fake frida binary content"
FAKE_COMPRESSED = lzma.compress(FAKE_BINARY)
VERSION = "16.4.10"


def _fake_urlopen(url: str, **kwargs: object) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = FAKE_COMPRESSED
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    """An empty instance directory under the isolated XDG tree."""
    root = tmp_path / "alpha"
    root.mkdir()
    return root


class TestReleaseUrl:
    def test_contains_version(self) -> None:
        url = frida_dl.release_url(VERSION)
        assert VERSION in url

    def test_contains_arch(self) -> None:
        url = frida_dl.release_url(VERSION)
        assert settings.frida_arch in url

    def test_is_https(self) -> None:
        url = frida_dl.release_url(VERSION)
        assert url.startswith("https://")


class TestCachedBinary:
    def test_path_under_frida_cache(self, isolated_registry: Path) -> None:
        p = frida_dl.cached_binary(VERSION)
        assert p.is_relative_to(frida_dl.frida_cache_dir())

    def test_path_contains_version(self, isolated_registry: Path) -> None:
        p = frida_dl.cached_binary(VERSION)
        assert VERSION in p.name

    def test_cache_dir_under_user_cache(self, isolated_registry: Path) -> None:
        assert frida_dl.frida_cache_dir() == paths.user_cache_dir("frida")


class TestDownload:
    def test_downloads_and_decompresses(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.download(VERSION)
        assert result.exists()
        assert result.read_bytes() == FAKE_BINARY

    def test_cached_file_is_executable(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.download(VERSION)
        mode = result.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_idempotent_second_call_skips_fetch(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen) as mock_open:
            frida_dl.download(VERSION)
            frida_dl.download(VERSION)
        assert mock_open.call_count == 1

    def test_returns_path_to_cached_binary(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.download(VERSION)
        assert result == frida_dl.cached_binary(VERSION)

    def test_cache_dir_created_automatically(self, isolated_registry: Path) -> None:
        assert not frida_dl.frida_cache_dir().exists()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            frida_dl.download(VERSION)
        assert frida_dl.frida_cache_dir().exists()


class TestDownloadErrors:
    def test_http_error_raises_runtime_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                frida_dl.download(VERSION)

    def test_timeout_raises_runtime_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(RuntimeError, match="timed out"):
                frida_dl.download(VERSION)

    def test_url_error_raises_runtime_error(self, isolated_registry: Path) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.URLError("no route to host")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(RuntimeError, match="cannot reach"):
                frida_dl.download(VERSION)


class TestSha256Of:
    def test_known_input(self, tmp_path: Path) -> None:
        data = b"hello beetroot"
        p = tmp_path / "file.bin"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert frida_dl.sha256_of(p) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert frida_dl.sha256_of(p) == hashlib.sha256(b"").hexdigest()


class TestStageEmpty:
    def test_creates_zero_byte_file(self, instance_root: Path) -> None:
        result = frida_dl.stage_empty(instance_root)
        assert result.exists()
        assert result.stat().st_size == 0

    def test_not_executable(self, instance_root: Path) -> None:
        result = frida_dl.stage_empty(instance_root)
        mode = result.stat().st_mode
        assert not (mode & stat.S_IXUSR)

    def test_path_matches_instance_frida(self, instance_root: Path) -> None:
        result = frida_dl.stage_empty(instance_root)
        assert result == paths.instance_frida(instance_root)

    def test_creates_parent_dirs(self, isolated_registry: Path, tmp_path: Path) -> None:
        root = tmp_path / "deep" / "path" / "alpha"
        assert not root.exists()
        frida_dl.stage_empty(root)
        assert root.exists()


class TestStageForInstance:
    def test_copies_binary_to_instance(self, instance_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.stage_for_instance(instance_root, VERSION)
        assert result.read_bytes() == FAKE_BINARY

    def test_staged_file_is_executable(self, instance_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.stage_for_instance(instance_root, VERSION)
        mode = result.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_staged_path_matches_instance_frida(self, instance_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.stage_for_instance(instance_root, VERSION)
        assert result == paths.instance_frida(instance_root)

    def test_different_instances_get_own_copy(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        alpha = tmp_path / "alpha"
        bravo = tmp_path / "bravo"
        alpha.mkdir()
        bravo.mkdir()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            frida_dl.stage_for_instance(alpha, VERSION)
            frida_dl.stage_for_instance(bravo, VERSION)
        assert paths.instance_frida(alpha) != paths.instance_frida(bravo)
        assert paths.instance_frida(alpha).exists()
        assert paths.instance_frida(bravo).exists()
