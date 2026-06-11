"""Shared fixtures for the beetroot test suite."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _snapshot_backend_registry() -> Iterator[None]:
    """Snapshot and restore the in-process backend registry between tests.

    T5 registers ``AdbDevice`` at import time via
    ``beetroot.backends.adb``'s module-level ``register_backend`` call.
    Existing tests in ``tests/test_backend_registry.py`` ``pop("adb",
    None)`` to remove that entry as part of their setup — without
    restoration, the entry would be permanently gone for every
    subsequent test in the same process (test order dependent), and
    the synthetic third-backend test would see a different starting
    state than a fresh import.

    The autouse fixture is the simplest fix: snapshot the dict before
    each test, restore it after. ``register_backend`` raises on
    duplicate, so this is also defensively safer than the
    ``pop`` + ``register_backend`` pattern in the legacy tests.
    """
    from beetroot import backends

    snapshot = dict(backends._BACKEND_REGISTRY)
    try:
        yield
    finally:
        backends._BACKEND_REGISTRY.clear()
        backends._BACKEND_REGISTRY.update(snapshot)


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


@pytest.fixture(autouse=True)
def _reset_registry_hint_flags() -> Iterator[None]:
    """Reset the registry module's print-once hint flags between tests.

    ``_LEGACY_HINT_PRINTED`` and ``_V02_HINT_PRINTED`` dedup per-process
    stderr hints, so whichever test triggers a hint first silently
    swallows it for every later test in the same process.  Under
    pytest-randomly's order shuffling (issue #21) that made
    hint-asserting tests fail on some seeds.  Same pattern as
    ``_reset_api_version_warning_dedup`` above.
    """
    from beetroot import registry

    registry._LEGACY_HINT_PRINTED = False
    registry._V02_HINT_PRINTED = False
    yield
    registry._LEGACY_HINT_PRINTED = False
    registry._V02_HINT_PRINTED = False


@pytest.fixture(autouse=True)
def _reset_consoles() -> Iterator[None]:
    """Snapshot and restore the console module's stdout/stderr singletons.

    ``console.set_consoles(...)`` mutates two module-level globals.  Without
    restoration, any test that calls ``set_consoles`` (or any future wave that
    injects test consoles without a manual teardown) leaks its replacement
    into every subsequent test in the same process.  The snapshot/restore here
    mirrors the pattern used by ``_snapshot_backend_registry`` and
    ``_reset_api_version_warning_dedup`` above.
    """
    from beetroot import console

    saved_stdout = console._stdout_console
    saved_stderr = console._stderr_console
    try:
        yield
    finally:
        console._stdout_console = saved_stdout
        console._stderr_console = saved_stderr


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
    (root / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def cli_root(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Composite fixture: XDG isolation + stubbed externals + chdir into tmp.

    Every subprocess invocation is short-circuited at the test level, so
    ``shutil.which`` returns synthetic paths for the binaries Beetroot
    looks up (``docker``, ``adb``, ``frida``). ``frida_download.download`` is
    no-op'd so tests don't hit the network. Tests chdir into ``tmp_path``
    so a default ``--path`` resolves under it.
    """
    import shutil

    def _which(name: str) -> str | None:
        if name in {"docker", "adb", "frida"}:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(shutil, "which", _which)

    from beetroot import frida_download

    def _fake_download(
        version: str, *, expected_sha256: str | None = None,
    ) -> Path:
        out = frida_download.cached_binary(version)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_bytes(b"fake-frida")
            out.chmod(0o755)
        # Honour the digest contract — tests that pass an
        # ``expected_sha256`` mismatched against ``b"fake-frida"``
        # must see the same ValueError they would in production.
        if expected_sha256 is not None:
            import hashlib
            actual = hashlib.sha256(out.read_bytes()).hexdigest()
            if actual.lower() != expected_sha256.lower():
                raise ValueError(
                    f"sha256 mismatch for frida-server at {out}: "
                    f"expected {expected_sha256.lower()}, got {actual.lower()}"
                )
        return out

    monkeypatch.setattr(frida_download, "download", _fake_download)
    monkeypatch.chdir(tmp_path)
    return tmp_path
