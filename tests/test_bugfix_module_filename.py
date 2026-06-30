"""Regression tests for #168 — module URL query/fragment poisons the staged basename.

A module URL carrying a ``?query`` or ``#fragment`` used to stage a filename
that retained the suffix (e.g. ``m.zip?v=2``), which the ``*.zip`` flash glob in
``flash-modules.sh`` never matched, so the module was silently skipped. The
basename is now derived from the URL *path* only.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import modules_download, paths
from beetroot.config import InstanceConfig, Module

FAKE_ZIP_CONTENT = b"PK\x03\x04 fake zip content"


def _make_url_resp() -> MagicMock:
    resp = MagicMock()
    resp.read.side_effect = [FAKE_ZIP_CONTENT, b""]
    resp.headers.get.side_effect = lambda key, *args: None
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    root = tmp_path / "alpha"
    root.mkdir()
    return root


class TestFilenameFromUrl:
    def test_query_and_fragment_are_stripped(self) -> None:
        assert modules_download._filename_from_url("https://h/m.zip?token=abc#frag") == "m.zip"

    def test_query_only_is_stripped(self) -> None:
        assert modules_download._filename_from_url("https://h/path/m.zip?v=2") == "m.zip"

    def test_empty_path_falls_back(self) -> None:
        assert modules_download._filename_from_url("https://h") == "module.zip"

    def test_trailing_slash_falls_back(self) -> None:
        assert modules_download._filename_from_url("https://h/dir/") == "module.zip"


class TestStagedNameMatchesFlashGlob:
    def test_query_string_url_stages_clean_zip(self, instance_root: Path) -> None:
        cfg = InstanceConfig(modules=[Module(url="https://example.com/magisk-mod.zip?v=2")])
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            staged = modules_download.stage_for_instance(instance_root, cfg)
        assert len(staged) == 1
        assert staged[0].parent == paths.instance_modules(instance_root)
        assert staged[0].name.endswith(".zip")
        # The redroid boot helper globs ``*.zip``; the staged name must match it.
        assert fnmatch.fnmatch(staged[0].name, "*.zip")
        assert "?" not in staged[0].name
