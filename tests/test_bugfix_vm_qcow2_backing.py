"""Regression test: ``_resolve_artifact`` returns a cwd-independent absolute path.

A relative ``vm.kernel`` / ``vm.rootfs`` used to be returned unchanged after the
existence check, so it later reached ``qemu-img create -b`` as the qcow2 boot-cache
overlay's backing file. qemu records that backing reference *relative to the
overlay's directory* (the instance dir, not the process cwd), so a relative
artifact path resolved against the cwd at check time could pass validation yet
produce an overlay whose backing reference pointed at the wrong place — leaving it
unopenable on the next ``up``. ``_resolve_artifact`` now ``.resolve()``s the path
so the returned artifact is absolute regardless of cwd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beetroot.backends import vm as vm_backend


def test_resolve_artifact_returns_absolute_path_for_relative_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real artifact lives at an absolute location; the config points at it
    # via a path that is only *relative to the cwd* the CLI happens to run from.
    artifact = tmp_path / "vm" / "rootdisk.img"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"r")
    monkeypatch.chdir(tmp_path)
    relative = "vm/rootdisk.img"

    resolved = vm_backend._resolve_artifact(relative, "", "rootfs")

    # Before the fix this returned ``Path("vm/rootdisk.img")`` (relative); after,
    # it is the absolute, canonical location — stable regardless of cwd, so the
    # qcow2 backing reference qemu stores relative to the overlay dir is correct.
    assert resolved.is_absolute()
    assert resolved == artifact.resolve()


def test_resolve_artifact_still_expands_and_resolves_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    cache = home / ".cache" / "beetroot" / "vm"
    cache.mkdir(parents=True)
    bz = cache / "bzImage"
    bz.write_bytes(b"k")
    monkeypatch.setenv("HOME", str(home))

    resolved = vm_backend._resolve_artifact("~/.cache/beetroot/vm/bzImage", "", "kernel")

    assert resolved.is_absolute()
    assert resolved == bz.resolve()
