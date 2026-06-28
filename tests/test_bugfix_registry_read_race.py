"""Bugfix: a losing concurrent reader must not crash on the legacy backup rename.

``_read`` performs a destructive ``path.rename(<file>.bak)`` at two sites
— when the registry JSON can't be parsed, and when its schema ``version``
doesn't match ``SCHEMA_VERSION``. But ``list_instances`` / ``get`` reach
``_read`` holding only a *shared* flock, which permits concurrent readers.
When two reader processes (e.g. two parallel ``beetroot ls``) both open the
same legacy/corrupt registry, both fail validation and both try to rename
the file; the first wins and removes the source, the second hits
``FileNotFoundError`` on the now-missing path — an unhandled crash in an
innocuous read command.

These tests simulate the losing reader deterministically (no real threads):
patch ``Path.rename`` to raise ``FileNotFoundError`` (the source vanished
between the existence probe and the rename), then assert ``_read`` returns
the same empty :class:`RegistryFile` the winning reader does, without
raising. Both trigger sites (unparseable JSON, wrong version) are covered.

Before the fix the ``FileNotFoundError`` propagates and these fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beetroot import paths, registry


def _raise_file_not_found(self: Path, target: Path) -> Path:
    # Simulate the losing reader: the source was already renamed away by
    # the winning concurrent reader, so the OS reports it missing.
    raise FileNotFoundError(self)


def test_unparseable_registry_read_tolerates_losing_the_backup_rename(
    isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = paths.user_registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json at all")
    registry._LEGACY_HINT_PRINTED = False

    monkeypatch.setattr(Path, "rename", _raise_file_not_found)

    result = registry._read(path)

    assert result == registry.RegistryFile()
    assert result.instances == {}


def test_wrong_version_registry_read_tolerates_losing_the_backup_rename(
    isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = paths.user_registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "instances": {}}))
    registry._LEGACY_HINT_PRINTED = False

    monkeypatch.setattr(Path, "rename", _raise_file_not_found)

    result = registry._read(path)

    assert result == registry.RegistryFile()
    assert result.instances == {}


def test_list_instances_returns_empty_when_losing_the_backup_rename(
    isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drive the full user-facing path (list_instances → shared lock →
    # _read) that a parallel ``beetroot ls`` runs, and assert the losing
    # reader gets an empty mapping instead of an unhandled crash.
    path = paths.user_registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "instances": {}}))
    registry._LEGACY_HINT_PRINTED = False

    monkeypatch.setattr(Path, "rename", _raise_file_not_found)

    assert registry.list_instances() == {}
