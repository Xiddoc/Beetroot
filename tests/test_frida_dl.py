"""Tests for frida_dl.py — download, cache, stage frida-server."""
from __future__ import annotations

import hashlib
import lzma
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class TestRelaseUrl:
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
    def test_path_under_frida_cache(self, isolated_root: Path) -> None:
        p = frida_dl.cached_binary(VERSION)
        assert p.is_relative_to(paths.frida_cache_dir())

    def test_path_contains_version(self, isolated_root: Path) -> None:
        p = frida_dl.cached_binary(VERSION)
        assert VERSION in p.name


class TestDownload:
    def test_downloads_and_decompresses(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.download(VERSION)
        assert result.exists()
        assert result.read_bytes() == FAKE_BINARY

    def test_cached_file_is_executable(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.download(VERSION)
        mode = result.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_idempotent_second_call_skips_fetch(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen) as mock_open:
            frida_dl.download(VERSION)
            frida_dl.download(VERSION)
        assert mock_open.call_count == 1

    def test_returns_path_to_cached_binary(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.download(VERSION)
        assert result == frida_dl.cached_binary(VERSION)

    def test_cache_dir_created_automatically(self, isolated_root: Path) -> None:
        assert not paths.frida_cache_dir().exists()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            frida_dl.download(VERSION)
        assert paths.frida_cache_dir().exists()


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
    def test_creates_zero_byte_file(self, isolated_root: Path) -> None:
        result = frida_dl.stage_empty("alpha")
        assert result.exists()
        assert result.stat().st_size == 0

    def test_not_executable(self, isolated_root: Path) -> None:
        result = frida_dl.stage_empty("alpha")
        mode = result.stat().st_mode
        assert not (mode & stat.S_IXUSR)

    def test_path_matches_instance_frida(self, isolated_root: Path) -> None:
        result = frida_dl.stage_empty("alpha")
        assert result == paths.instance_frida("alpha")

    def test_creates_parent_dirs(self, isolated_root: Path) -> None:
        assert not paths.instance_dir("alpha").exists()
        frida_dl.stage_empty("alpha")
        assert paths.instance_dir("alpha").exists()


class TestStageForInstance:
    def test_copies_binary_to_instance(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.stage_for_instance("alpha", VERSION)
        assert result.read_bytes() == FAKE_BINARY

    def test_staged_file_is_executable(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.stage_for_instance("alpha", VERSION)
        mode = result.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_staged_path_matches_instance_frida(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = frida_dl.stage_for_instance("alpha", VERSION)
        assert result == paths.instance_frida("alpha")

    def test_different_instances_get_own_copy(self, isolated_root: Path) -> None:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            frida_dl.stage_for_instance("alpha", VERSION)
            frida_dl.stage_for_instance("bravo", VERSION)
        assert paths.instance_frida("alpha") != paths.instance_frida("bravo")
        assert paths.instance_frida("alpha").exists()
        assert paths.instance_frida("bravo").exists()
