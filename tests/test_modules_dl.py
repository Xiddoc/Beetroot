"""Tests for modules_dl.py — stage Magisk module zips per instance."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import modules_dl, paths
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


class TestStageForInstanceUrlModule:
    def test_happy_path_url_module(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/magisk-mod.zip")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_dl.stage_for_instance("alpha", cfg)
        assert len(staged) == 1
        assert staged[0].exists()
        assert staged[0].read_bytes() == FAKE_ZIP_CONTENT

    def test_staged_file_lands_in_instance_modules(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/magisk-mod.zip")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_dl.stage_for_instance("alpha", cfg)
        assert staged[0].parent == paths.instance_modules("alpha")

    def test_url_module_with_correct_sha256(self, isolated_root: Path) -> None:
        sha = _sha256(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/mod.zip", sha256=sha)]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_dl.stage_for_instance("alpha", cfg)
        assert len(staged) == 1
        assert staged[0].exists()

    def test_sha256_mismatch_raises_value_error(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/mod.zip", sha256="deadbeef")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                modules_dl.stage_for_instance("alpha", cfg)

    def test_sha256_mismatch_error_contains_both_hashes(self, isolated_root: Path) -> None:
        expected = "deadbeef"
        actual = _sha256(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/mod.zip", sha256=expected)]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            with pytest.raises(ValueError, match="sha256 mismatch") as exc_info:
                modules_dl.stage_for_instance("alpha", cfg)
        msg = str(exc_info.value)
        assert expected in msg
        assert actual in msg


class TestUrlModuleCache:
    def test_second_call_reuses_cached_zip(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(
            modules=[Module(url="https://example.com/magisk-mod.zip")]
        )
        with patch("urllib.request.urlopen", return_value=_make_url_resp()) as mock_open:
            modules_dl.stage_for_instance("alpha", cfg)
            # Second stage wipes the instance dir but the .cache/ copy remains
            modules_dl.stage_for_instance("alpha", cfg)
        assert mock_open.call_count == 1


class TestStageForInstancePathModule:
    def test_path_module_copies_file(self, isolated_root: Path, tmp_path: Path) -> None:
        src = tmp_path / "local-mod.zip"
        src.write_bytes(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(modules=[Module(path=str(src))])
        staged = modules_dl.stage_for_instance("alpha", cfg)
        assert len(staged) == 1
        assert staged[0].read_bytes() == FAKE_ZIP_CONTENT

    def test_path_module_missing_raises(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(path="/nonexistent/mod.zip")])
        with pytest.raises(FileNotFoundError):
            modules_dl.stage_for_instance("alpha", cfg)


class TestStaleZipWiping:
    def test_stale_zips_are_removed_on_re_stage(self, isolated_root: Path) -> None:
        modules_dir = paths.instance_modules("alpha")
        modules_dir.mkdir(parents=True)
        stale = modules_dir / "old-module.zip"
        stale.write_bytes(b"stale")

        cfg = InstanceConfig(modules=[])
        modules_dl.stage_for_instance("alpha", cfg)
        assert not stale.exists()

    def test_only_stale_zips_are_wiped_not_all_files(self, isolated_root: Path) -> None:
        modules_dir = paths.instance_modules("alpha")
        modules_dir.mkdir(parents=True)
        stale_zip = modules_dir / "old.zip"
        stale_zip.write_bytes(b"stale zip")
        other_file = modules_dir / "readme.txt"
        other_file.write_bytes(b"readme")

        cfg = InstanceConfig(modules=[])
        modules_dl.stage_for_instance("alpha", cfg)
        assert not stale_zip.exists()
        assert other_file.exists()


class TestEmptyModuleList:
    def test_empty_modules_list_creates_dir(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(modules=[])
        modules_dl.stage_for_instance("alpha", cfg)
        assert paths.instance_modules("alpha").exists()

    def test_empty_modules_list_returns_empty_staged(self, isolated_root: Path) -> None:
        cfg = InstanceConfig(modules=[])
        staged = modules_dl.stage_for_instance("alpha", cfg)
        assert staged == []
