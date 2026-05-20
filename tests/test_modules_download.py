"""Tests for modules_download.py — stage Magisk module zips per instance."""
from __future__ import annotations

import hashlib
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import modules_download, paths
from beetroot.config import InstanceConfig, Module

FAKE_ZIP_CONTENT = b"PK\x03\x04 fake zip content"


def _make_url_resp(data: bytes = FAKE_ZIP_CONTENT) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = data
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    """An empty instance directory under the isolated XDG tree."""
    root = tmp_path / "alpha"
    root.mkdir()
    return root


class TestStageForInstanceUrlModule:
    def test_happy_path_url_module(self, instance_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/magisk-mod.zip")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_download.stage_for_instance(instance_root, cfg)
        assert len(staged) == 1
        assert staged[0].exists()
        assert staged[0].read_bytes() == FAKE_ZIP_CONTENT

    def test_staged_file_lands_in_instance_modules(self, instance_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/magisk-mod.zip")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_download.stage_for_instance(instance_root, cfg)
        assert staged[0].parent == paths.instance_modules(instance_root)

    def test_url_module_with_correct_sha256(self, instance_root: Path) -> None:
        sha = _sha256(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/mod.zip", sha256=sha)]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_download.stage_for_instance(instance_root, cfg)
        assert len(staged) == 1
        assert staged[0].exists()

    def test_sha256_mismatch_raises_value_error(self, instance_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/mod.zip", sha256="deadbeef")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                modules_download.stage_for_instance(instance_root, cfg)

    def test_sha256_mismatch_error_contains_both_hashes(self, instance_root: Path) -> None:
        expected = "deadbeef"
        actual = _sha256(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/mod.zip", sha256=expected)]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            with pytest.raises(ValueError, match="sha256 mismatch") as exc_info:
                modules_download.stage_for_instance(instance_root, cfg)
        msg = str(exc_info.value)
        assert expected in msg
        assert actual in msg


class TestUrlModuleCache:
    def test_second_call_reuses_cached_zip(self, instance_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/magisk-mod.zip")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()) as mock_open:
            modules_download.stage_for_instance(instance_root, cfg)
            modules_download.stage_for_instance(instance_root, cfg)
        assert mock_open.call_count == 1

    def test_cache_under_url_hash_subdirectory(self, instance_root: Path) -> None:
        # The cache path must be under a URL-hash subdirectory, not just the
        # basename — so modules from different domains don't collide.
        url = "https://example.com/magisk-mod.zip"
        cfg = InstanceConfig(modules=[Module(url=url)])
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            modules_download.stage_for_instance(instance_root, cfg)
        expected = modules_download._cache_path_for_url(url)
        assert expected.exists()

    def test_same_basename_different_domains_do_not_collide(
        self, instance_root: Path
    ) -> None:
        # Two URLs with the same basename but different domains must produce
        # different cache paths so they never overwrite each other.
        url_a = "https://example.com/mod.zip"
        url_b = "https://other.org/mod.zip"
        cache_a = modules_download._cache_path_for_url(url_a)
        cache_b = modules_download._cache_path_for_url(url_b)
        assert cache_a != cache_b, (
            "same-basename different-domain URLs must not share a cache path"
        )

    def test_sha256_mismatch_deletes_cached_file(self, instance_root: Path) -> None:
        # A sha256 mismatch on a URL module must delete the bad cached artifact
        # so the next call re-downloads rather than re-failing forever.
        url = "https://example.com/mod.zip"
        cache_path = modules_download._cache_path_for_url(url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(FAKE_ZIP_CONTENT)

        cfg = InstanceConfig(modules=[Module(url=url, sha256="deadbeef")])
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                modules_download.stage_for_instance(instance_root, cfg)
        assert not cache_path.exists(), "bad cached file should be deleted after sha256 mismatch"


class TestFetchUrlErrors:
    def test_http_error_raises_module_fetch_error(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(url="https://example.com/mod.zip")])

        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)  # type: ignore[arg-type]

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(modules_download.ModuleFetchError, match="HTTP 500"):
                modules_download.stage_for_instance(instance_root, cfg)

    def test_timeout_raises_module_fetch_error(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(url="https://example.com/mod.zip")])

        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(modules_download.ModuleFetchError, match="timed out"):
                modules_download.stage_for_instance(instance_root, cfg)

    def test_url_error_raises_module_fetch_error(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(url="https://example.com/mod.zip")])

        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.URLError("no route to host")

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(modules_download.ModuleFetchError, match="cannot reach"):
                modules_download.stage_for_instance(instance_root, cfg)

    def test_module_fetch_error_is_runtime_error_subclass(self) -> None:
        # Existing callers that catch `RuntimeError` continue to work.
        assert issubclass(modules_download.ModuleFetchError, RuntimeError)

    def test_filename_from_empty_url_defaults_to_module_zip(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(url="https://example.com/")])
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_download.stage_for_instance(instance_root, cfg)
        assert staged[0].name == "module.zip"


class TestStageForInstancePathModule:
    def test_absolute_path_module_copies_file(
        self, instance_root: Path, tmp_path: Path
    ) -> None:
        src = tmp_path / "external" / "local-mod.zip"
        src.parent.mkdir()
        src.write_bytes(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(modules=[Module(path=str(src))])
        staged = modules_download.stage_for_instance(instance_root, cfg)
        assert len(staged) == 1
        assert staged[0].read_bytes() == FAKE_ZIP_CONTENT

    def test_relative_path_resolves_against_instance_root(
        self, instance_root: Path
    ) -> None:
        # A relative ``path:`` entry must resolve to <instance_root>/<path>.
        local = instance_root / "local-mod.zip"
        local.write_bytes(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(modules=[Module(path="local-mod.zip")])
        staged = modules_download.stage_for_instance(instance_root, cfg)
        assert len(staged) == 1
        assert staged[0].read_bytes() == FAKE_ZIP_CONTENT

    def test_path_module_missing_raises(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(path="/nonexistent/mod.zip")])
        with pytest.raises(FileNotFoundError):
            modules_download.stage_for_instance(instance_root, cfg)


class TestStaleZipWiping:
    def test_stale_zips_are_removed_on_re_stage(self, instance_root: Path) -> None:
        modules_dir = paths.instance_modules(instance_root)
        modules_dir.mkdir(parents=True)
        stale = modules_dir / "old-module.zip"
        stale.write_bytes(b"stale")

        cfg = InstanceConfig(modules=[])
        modules_download.stage_for_instance(instance_root, cfg)
        assert not stale.exists()

    def test_only_stale_zips_are_wiped_not_all_files(self, instance_root: Path) -> None:
        modules_dir = paths.instance_modules(instance_root)
        modules_dir.mkdir(parents=True)
        stale_zip = modules_dir / "old.zip"
        stale_zip.write_bytes(b"stale zip")
        other_file = modules_dir / "readme.txt"
        other_file.write_bytes(b"readme")

        cfg = InstanceConfig(modules=[])
        modules_download.stage_for_instance(instance_root, cfg)
        assert not stale_zip.exists()
        assert other_file.exists()


class TestEmptyModuleList:
    def test_empty_modules_list_creates_dir(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[])
        modules_download.stage_for_instance(instance_root, cfg)
        assert paths.instance_modules(instance_root).exists()

    def test_empty_modules_list_returns_empty_staged(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[])
        staged = modules_download.stage_for_instance(instance_root, cfg)
        assert staged == []
