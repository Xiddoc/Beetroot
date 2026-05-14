"""Shared fixtures for the beetroot test suite."""
from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import paths


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect paths.repo_root() to a fresh tmp directory.

    Every accessor in paths.py derives from repo_root(), so this one patch
    isolates all filesystem activity from the real repo layout.
    """
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path)
    return tmp_path
