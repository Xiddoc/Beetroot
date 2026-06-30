"""Regression tests for module staging integrity bugfixes.

Two real bugs in ``modules_download``:

1. A sha256 mismatch on a *host-path* module used to delete the user's own
   source file (irreversible data loss); only the regenerable URL cache may
   be evicted.
2. Two modules sharing a basename used to overwrite each other in staging,
   so only one was ever flashed; both must now be staged as distinct files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beetroot import modules_download, paths
from beetroot.config import InstanceConfig, Module

FAKE_ZIP_CONTENT = b"PK\x03\x04 fake zip content"
OTHER_ZIP_CONTENT = b"PK\x03\x04 a different module entirely"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_url_resp(data: bytes = FAKE_ZIP_CONTENT) -> MagicMock:
    """Return a mock HTTP response yielding ``data`` in one chunk then EOF."""
    resp = MagicMock()
    resp.read.side_effect = [data, b""]
    resp.headers.get.return_value = None
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    """An empty instance directory under the isolated XDG tree."""
    root = tmp_path / "alpha"
    root.mkdir()
    return root


class TestSha256MismatchDoesNotDeleteUserSourceFile:
    def test_path_module_mismatch_keeps_user_file(
        self, instance_root: Path, tmp_path: Path
    ) -> None:
        # A host-path module with a wrong sha256 must raise, but the user's
        # original file on disk must SURVIVE — there is no way to re-fetch it.
        src = tmp_path / "external" / "local-mod.zip"
        src.parent.mkdir()
        src.write_bytes(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(modules=[Module(path=str(src), sha256="d" * 64)])

        with pytest.raises(ValueError, match="sha256 mismatch"):
            modules_download.stage_for_instance(instance_root, cfg)

        assert src.exists(), "the user's own module source file must not be deleted"
        assert src.read_bytes() == FAKE_ZIP_CONTENT

    def test_url_module_mismatch_still_evicts_cache(self, instance_root: Path) -> None:
        # The intentional behaviour for URL modules is preserved: a mismatch
        # evicts the regenerable cache so the next call re-downloads.
        url = "https://example.com/mod.zip"
        cache_path = modules_download._cache_path_for_url(url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(FAKE_ZIP_CONTENT)

        cfg = InstanceConfig(modules=[Module(url=url, sha256="d" * 64)])
        with patch("urllib.request.urlopen", return_value=_make_url_resp()):
            with pytest.raises(ValueError, match="sha256 mismatch"):
                modules_download.stage_for_instance(instance_root, cfg)

        assert not cache_path.exists(), "the regenerable URL cache must be evicted on mismatch"


class TestSameBasenameModulesStageDistinctly:
    def test_two_host_paths_same_basename_both_staged(
        self, instance_root: Path, tmp_path: Path
    ) -> None:
        # Two distinct host-path modules whose paths end in the same filename
        # must BOTH stage as distinct files with their own (distinct) content.
        src_a = tmp_path / "a" / "mod.zip"
        src_a.parent.mkdir()
        src_a.write_bytes(FAKE_ZIP_CONTENT)
        src_b = tmp_path / "b" / "mod.zip"
        src_b.parent.mkdir()
        src_b.write_bytes(OTHER_ZIP_CONTENT)

        cfg = InstanceConfig(modules=[Module(path=str(src_a)), Module(path=str(src_b))])
        staged = modules_download.stage_for_instance(instance_root, cfg)

        assert len(staged) == 2
        assert len(set(staged)) == 2, "staged paths must be distinct"
        assert all(p.exists() for p in staged)
        contents = {p.read_bytes() for p in staged}
        assert contents == {FAKE_ZIP_CONTENT, OTHER_ZIP_CONTENT}

    def test_two_urls_same_basename_both_staged(self, instance_root: Path) -> None:
        # Two URLs ending in the same basename but from different domains must
        # BOTH stage distinctly (the cache already disambiguates by URL hash).
        url_a = "https://example.com/mod.zip"
        url_b = "https://other.org/mod.zip"
        cfg = InstanceConfig(modules=[Module(url=url_a), Module(url=url_b)])

        responses = {
            url_a: _make_url_resp(FAKE_ZIP_CONTENT),
            url_b: _make_url_resp(OTHER_ZIP_CONTENT),
        }

        def _dispatch(url: str, **kwargs: object) -> MagicMock:
            return responses[url]

        with patch("urllib.request.urlopen", side_effect=_dispatch):
            staged = modules_download.stage_for_instance(instance_root, cfg)

        assert len(staged) == 2
        assert len(set(staged)) == 2, "staged paths must be distinct"
        contents = {p.read_bytes() for p in staged}
        assert contents == {FAKE_ZIP_CONTENT, OTHER_ZIP_CONTENT}

    def test_single_module_keeps_original_basename(
        self, instance_root: Path, tmp_path: Path
    ) -> None:
        # The common unique-basename case is unchanged: the staged file keeps
        # its original name (existing callers/tests rely on this).
        src = tmp_path / "external" / "module.zip"
        src.parent.mkdir()
        src.write_bytes(FAKE_ZIP_CONTENT)
        cfg = InstanceConfig(modules=[Module(path=str(src))])

        staged = modules_download.stage_for_instance(instance_root, cfg)

        assert len(staged) == 1
        assert staged[0].name == "module.zip"
        assert staged[0].parent == paths.instance_modules(instance_root)
