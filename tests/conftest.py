"""Shared fixtures for the beetroot test suite."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_api_version_warning_dedup() -> Iterator[None]:
    """Reset the api_version-auto-bump dedup set between tests.

    The dedup keeps the per-process warning from spamming on every
    ``beetroot ls``, but it persists across tests in the same process.
    Without this fixture, the second test that loads a v0.2 YAML at
    the same absolute path would silently skip the warning and break
    its assertions on stderr content.
    """
    # Local import keeps the conftest free of beetroot at collection.
    from beetroot import config

    config._API_VERSION_BUMP_WARNED.clear()
    yield
    config._API_VERSION_BUMP_WARNED.clear()


@pytest.fixture
def isolated_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the XDG dirs Beetroot consumes to per-test tmp subdirs.

    The user-global registry lives under ``$XDG_CONFIG_HOME/beetroot/`` and
    download caches live under ``$XDG_CACHE_HOME/beetroot/``. Pointing both
    at ``tmp_path`` means each test gets its own empty world.

    Returns the tmp_path so tests can drop registry-adjacent fixtures in it.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture
def isolated_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Create a minimal instance dir, chdir into it, return its path.

    Writes ``tmp_path/instance/beetroot.yaml`` with the minimum valid
    schema, monkey-chdirs into it, and returns the directory.
    """
    root = tmp_path / "instance"
    root.mkdir()
    (root / "beetroot.yaml").write_text("api_version: 2\nandroid:\n  version: 14\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def cli_root(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Composite fixture: XDG isolation + stubbed externals + chdir into tmp.

    Every subprocess invocation is short-circuited at the test level, so
    ``shutil.which`` returns synthetic paths for the binaries Beetroot
    looks up (``docker``, ``adb``, ``frida``). ``frida_dl.download`` is
    no-op'd so tests don't hit the network. Tests chdir into ``tmp_path``
    so a default ``--path`` resolves under it.
    """
    import shutil

    def _which(name: str) -> str | None:
        if name in {"docker", "adb", "frida"}:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(shutil, "which", _which)

    from beetroot import frida_dl

    def _fake_download(version: str) -> Path:
        out = frida_dl.cached_binary(version)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_bytes(b"fake-frida")
            out.chmod(0o755)
        return out

    monkeypatch.setattr(frida_dl, "download", _fake_download)
    monkeypatch.chdir(tmp_path)
    return tmp_path
