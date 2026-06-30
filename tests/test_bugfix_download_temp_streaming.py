"""Regression tests for #185, #227, #228 — download staging, streaming, and bounds.

#185: frida/module/kernel downloads stage into a *process-unique* temp file (via
``tempfile.mkstemp``) before the atomic rename, so two concurrent fetches of the
same artifact can't write a shared fixed ``<dest>.tmp`` and publish a corrupt,
cross-contaminated file into the user-global cache.

#227: frida and module downloads stream chunk-by-chunk to the open temp handle
(frida decompresses incrementally) instead of buffering the whole payload in RAM.

#228: frida ``.xz`` decompression is bounded by a generous output ceiling and
raises ``FridaFetchError`` instead of OOM-ing on a corrupt / zip-bomb payload.
"""

from __future__ import annotations

import hashlib
import lzma
import stat
import tempfile
import urllib.error
from pathlib import Path
from typing import Protocol
from unittest.mock import MagicMock, patch

import pytest

from beetroot import frida_download, kernel_download, modules_download
from beetroot.config import InstanceConfig, Module

# Capture the real mkstemp before any test patches the name, so the spy below
# delegates to the genuine function instead of recursing into itself.
_REAL_MKSTEMP = tempfile.mkstemp


class _MkstempSpy(Protocol):
    def __call__(self, *, dir: Path, suffix: str) -> tuple[int, str]:  # noqa: A002
        ...


def _mkstemp_spy(seen: list[str]) -> _MkstempSpy:
    """Wrap ``tempfile.mkstemp`` to record the temp names it hands out."""

    def _spy(*, dir: Path, suffix: str) -> tuple[int, str]:  # noqa: A002  # mirrors mkstemp's kw
        fd, name = _REAL_MKSTEMP(dir=dir, suffix=suffix)
        seen.append(name)
        return fd, name

    return _spy

VERSION = "16.4.10"
FAKE_BINARY = b"ELF\x7f fake frida binary content" * 64
FAKE_COMPRESSED = lzma.compress(FAKE_BINARY)
FAKE_ZIP = b"PK\x03\x04 fake module zip payload" * 64


def _chunked_resp(*chunks: bytes) -> MagicMock:
    """A mock urlopen response yielding each chunk in turn, then EOF."""
    resp = MagicMock()
    resp.read.side_effect = [*chunks, b""]
    resp.headers.get.side_effect = lambda key, *args: None
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    root = tmp_path / "alpha"
    root.mkdir()
    return root


def _split(data: bytes, n: int) -> list[bytes]:
    step = max(1, len(data) // n)
    return [data[i : i + step] for i in range(0, len(data), step)]


class TestFridaProcessUniqueTemp:
    def test_does_not_use_fixed_dotted_tmp(self, isolated_registry: Path) -> None:
        # The staging path must be a mkstemp name, never the deterministic
        # ``<dest>.tmp`` that two processes would collide on.
        out = frida_download.cached_binary(VERSION)
        seen: list[str] = []
        with (
            patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)),
            patch("beetroot.frida_download.tempfile.mkstemp", side_effect=_mkstemp_spy(seen)),
        ):
            frida_download.download(VERSION)
        assert seen, "download must stage via tempfile.mkstemp"
        assert seen[0] != str(out.with_suffix(".tmp"))
        assert Path(seen[0]).parent == out.parent

    def test_staged_binary_keeps_executable_bit(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)):
            out = frida_download.download(VERSION)
        assert out.stat().st_mode & stat.S_IXUSR


class TestFridaIncrementalStreaming:
    def test_multi_chunk_decompresses_correctly(self, isolated_registry: Path) -> None:
        # Multiple chunks exercise the incremental decompressor's
        # intermediate-vs-final output; the file must be byte-identical.
        chunks = _split(FAKE_COMPRESSED, 5)
        with patch("urllib.request.urlopen", return_value=_chunked_resp(*chunks)):
            out = frida_download.download(VERSION)
        assert out.read_bytes() == FAKE_BINARY

    def test_corrupt_xz_raises_frida_fetch_error(self, isolated_registry: Path) -> None:
        chunks = _split(b"this is not valid lzma data at all" * 8, 4)
        with patch("urllib.request.urlopen", return_value=_chunked_resp(*chunks)):
            with pytest.raises(frida_download.FridaFetchError, match="decompression failed"):
                frida_download.download(VERSION)
        assert not frida_download.cached_binary(VERSION).exists()

    def test_truncated_xz_raises_and_leaves_no_output(self, isolated_registry: Path) -> None:
        # A valid .xz that ends before its LZMA end-of-stream marker (the
        # network died mid-download) must be rejected, not silently accepted
        # as a complete binary — the regression the eof check restores (#228).
        truncated = FAKE_COMPRESSED[:-1]
        with patch("urllib.request.urlopen", return_value=_chunked_resp(truncated)):
            with pytest.raises(frida_download.FridaFetchError, match="ended mid-stream"):
                frida_download.download(VERSION)
        assert not frida_download.cached_binary(VERSION).exists()

    def test_bounded_drain_across_buffered_output(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A tiny chunk size forces the bounded decompressor to drain its
        # internal buffer over several ``b""`` steps (the needs_input=False
        # path that caps single-chunk expansion, #228); the binary must still
        # come out byte-identical.
        monkeypatch.setattr(frida_download, "_CHUNK_SIZE", 8)
        with patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)):
            out = frida_download.download(VERSION)
        assert out.read_bytes() == FAKE_BINARY


class TestFridaDecompressionCeiling:
    def test_exceeding_ceiling_raises_and_leaves_no_output(
        self, isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shrink the ceiling so the (small) happy-path payload trips it,
        # proving the bound is enforced without needing a real zip bomb.
        monkeypatch.setattr(frida_download, "_MAX_DECOMPRESSED_BYTES", 8)
        with patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)):
            with pytest.raises(frida_download.FridaFetchError, match="ceiling"):
                frida_download.download(VERSION)
        assert not frida_download.cached_binary(VERSION).exists()

    def test_under_ceiling_succeeds(self, isolated_registry: Path) -> None:
        with patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)):
            out = frida_download.download(VERSION)
        assert out.read_bytes() == FAKE_BINARY


class TestFridaConcurrentDoesNotPoison:
    def test_interleaved_writers_publish_one_complete_payload(
        self, isolated_registry: Path
    ) -> None:
        # Two back-to-back downloads to the same cache target (the second a cache
        # hit) must publish exactly one complete, valid binary — never a
        # concatenation of two writers' bytes.
        with patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)):
            first = frida_download.download(VERSION)
        with patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_COMPRESSED)):
            second = frida_download.download(VERSION)
        assert first == second
        assert first.read_bytes() == FAKE_BINARY


class TestModuleProcessUniqueTempAndStreaming:
    def test_does_not_use_fixed_dotted_tmp(self, instance_root: Path) -> None:
        url = "https://example.com/mod.zip"
        cache = modules_download._cache_path_for_url(url)
        cfg = InstanceConfig(modules=[Module(url=url)])
        seen: list[str] = []
        with (
            patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_ZIP)),
            patch("beetroot.modules_download.tempfile.mkstemp", side_effect=_mkstemp_spy(seen)),
        ):
            modules_download.stage_for_instance(instance_root, cfg)
        assert seen
        assert seen[0] != str(cache.with_suffix(".tmp"))
        assert Path(seen[0]).parent == cache.parent

    def test_multi_chunk_streams_to_disk(self, instance_root: Path) -> None:
        chunks = _split(FAKE_ZIP, 4)
        url = "https://example.com/mod.zip"
        cfg = InstanceConfig(modules=[Module(url=url)])
        with patch("urllib.request.urlopen", return_value=_chunked_resp(*chunks)):
            staged = modules_download.stage_for_instance(instance_root, cfg)
        assert staged[0].read_bytes() == FAKE_ZIP

    def test_http_error_leaves_no_temp_behind(self, instance_root: Path) -> None:
        url = "https://example.com/mod.zip"
        cfg = InstanceConfig(modules=[Module(url=url)])

        def _raise(*args: object, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(modules_download.ModuleFetchError, match="HTTP 404"):
                modules_download.stage_for_instance(instance_root, cfg)
        cache_dir = modules_download._cache_path_for_url(url).parent
        leftovers = list(cache_dir.glob("*.tmp")) if cache_dir.exists() else []
        assert leftovers == []

    def test_unmapped_crash_cleans_up_temp(self, instance_root: Path) -> None:
        # A failure that isn't one of the mapped HTTP/timeout/URL errors (e.g. a
        # KeyboardInterrupt mid-write) must still unlink the staged temp so it
        # doesn't orphan in the user-global cache.
        url = "https://example.com/mod.zip"
        cfg = InstanceConfig(modules=[Module(url=url)])

        def _close_and_crash(fd: int, mode: str) -> object:
            import os as _os

            _os.close(fd)  # avoid leaking the mkstemp fd / a ResourceWarning
            raise KeyboardInterrupt

        with (
            patch("urllib.request.urlopen", return_value=_chunked_resp(FAKE_ZIP)),
            patch("beetroot.modules_download.os.fdopen", side_effect=_close_and_crash),
        ):
            with pytest.raises(KeyboardInterrupt):
                modules_download.stage_for_instance(instance_root, cfg)
        cache_dir = modules_download._cache_path_for_url(url).parent
        leftovers = list(cache_dir.glob("*.tmp")) if cache_dir.exists() else []
        assert leftovers == []


class TestKernelProcessUniqueTemp:
    def test_does_not_use_fixed_dotted_tmp(self, isolated_registry: Path, tmp_path: Path) -> None:
        out = tmp_path / "kernels" / "bzImage"
        digest = hashlib.sha256(b"kernel-bytes").hexdigest()

        def _fake_fetch(url: str, description: str) -> bytes:
            return digest.encode() if url.endswith(".sha256") else b"kernel-bytes"

        seen: list[str] = []
        with (
            patch("beetroot.kernel_download._fetch_bytes", side_effect=_fake_fetch),
            patch("beetroot.kernel_download.tempfile.mkstemp", side_effect=_mkstemp_spy(seen)),
        ):
            result = kernel_download.fetch_prebuilt(
                version="6.12.9", fingerprint="abc123def456", out_path=out
            )
        assert result == out
        assert out.read_bytes() == b"kernel-bytes"
        assert seen
        assert seen[0] != str(out.with_suffix(".tmp"))
        assert Path(seen[0]).parent == out.parent

    def test_unmapped_crash_cleans_up_temp(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "kernels" / "bzImage"
        digest = hashlib.sha256(b"kernel-bytes").hexdigest()

        def _fake_fetch(url: str, description: str) -> bytes:
            return digest.encode() if url.endswith(".sha256") else b"kernel-bytes"

        def _close_and_crash(fd: int, mode: str) -> object:
            import os as _os

            _os.close(fd)
            raise KeyboardInterrupt

        with (
            patch("beetroot.kernel_download._fetch_bytes", side_effect=_fake_fetch),
            patch("beetroot.kernel_download.os.fdopen", side_effect=_close_and_crash),
        ):
            with pytest.raises(KeyboardInterrupt):
                kernel_download.fetch_prebuilt(
                    version="6.12.9", fingerprint="abc123def456", out_path=out
                )
        leftovers = list(out.parent.glob("*.tmp")) if out.parent.exists() else []
        assert leftovers == []
        assert not out.exists()
