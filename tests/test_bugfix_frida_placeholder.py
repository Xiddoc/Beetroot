"""Regression tests for #165 — apply must not wipe a working frida-server.

``_stage_local`` used to write the empty placeholder unconditionally, zeroing a
real binary to 0 bytes / 0o644 *before* ``_stage_network`` re-downloaded it. On a
cache-miss re-fetch failure the instance was left with a non-executable 0-byte
frida-server that ``launch-frida.sh`` skips — Frida silently disabled. The fix
keeps a usable binary in place, and stages the fresh one atomically on success.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, frida_download, paths


def _write_frida_yaml(root: Path) -> None:
    (root / "beetroot.yaml").write_text(
        "api_version: 3\nandroid:\n  version: 14\nfrida:\n  version: '16.4.10'\n"
    )


def _stage_fake_binary(root: Path, content: bytes = b"REAL-FRIDA-BINARY") -> Path:
    frida = paths.instance_frida(root)
    frida.parent.mkdir(parents=True, exist_ok=True)
    frida.write_bytes(content)
    frida.chmod(0o755)
    return frida


def test_apply_preserves_working_binary_when_refetch_fails(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    CliRunner().invoke(cli.app, ["create", "alpha"])
    root = api.Instance.load("alpha").root
    _write_frida_yaml(root)
    frida = _stage_fake_binary(root)
    original = frida.read_bytes()

    def _boom(version: str, *, expected_sha256: str | None = None) -> Path:
        raise frida_download.FridaFetchError("simulated cache-miss re-fetch failure")

    monkeypatch.setattr(frida_download, "download", _boom)

    with pytest.raises(frida_download.FridaFetchError):
        api.Instance.load("alpha").apply()

    # The prior working binary is untouched: same bytes, non-zero, still exec.
    assert frida.read_bytes() == original
    assert frida.stat().st_size > 0
    assert frida.stat().st_mode & stat.S_IXUSR


def test_apply_with_valid_binary_skips_placeholder(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    CliRunner().invoke(cli.app, ["create", "alpha"])
    root = api.Instance.load("alpha").root
    _write_frida_yaml(root)
    _stage_fake_binary(root)

    called = {"stage_empty": False}
    real_stage_empty = frida_download.stage_empty

    def _spy(instance_root: Path) -> Path:
        called["stage_empty"] = True
        return real_stage_empty(instance_root)

    monkeypatch.setattr(frida_download, "stage_empty", _spy)
    # Let the (stubbed cli_root) download succeed so apply completes.
    api.Instance.load("alpha").apply()

    # With a configured frida AND an existing non-empty binary, the placeholder
    # branch is skipped — the real binary is never zeroed first.
    assert called["stage_empty"] is False


def test_apply_without_frida_still_writes_placeholder(cli_root: Path) -> None:
    CliRunner().invoke(cli.app, ["create", "alpha"])
    root = api.Instance.load("alpha").root
    # No frida: block → the bind-mount target must still exist as a placeholder.
    (root / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
    api.Instance.load("alpha").apply()

    frida = paths.instance_frida(root)
    assert frida.exists()
    assert frida.stat().st_size == 0
    assert not (frida.stat().st_mode & stat.S_IXUSR)


def test_stage_for_instance_swaps_atomically(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = cli_root / "alpha"
    root.mkdir()
    _stage_fake_binary(root, b"OLD-BINARY")
    frida = paths.instance_frida(root)

    # The cli_root fixture stubs download to write b"fake-frida"; staging must
    # replace the old binary with the fresh cached one via an atomic swap.
    monkeypatch.setattr(frida_download, "host_frida_tools_version", lambda: None)
    dst = frida_download.stage_for_instance(root, "16.4.10")
    assert dst == frida
    assert frida.read_bytes() == b"fake-frida"
    # No leftover temp files beside the target.
    leftovers = [p for p in root.iterdir() if p.name.startswith(".frida-server.")]
    assert leftovers == []


def test_stage_for_instance_cleans_temp_on_copy_failure(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = cli_root / "alpha"
    root.mkdir()
    _stage_fake_binary(root, b"OLD-BINARY")
    frida = paths.instance_frida(root)

    monkeypatch.setattr(frida_download, "host_frida_tools_version", lambda: None)

    def _boom_copy(src: str | Path, dst: str | Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("beetroot.frida_download.shutil.copyfile", _boom_copy)

    with pytest.raises(OSError, match="disk full"):
        frida_download.stage_for_instance(root, "16.4.10")

    # The old binary survives and no temp is orphaned.
    assert frida.read_bytes() == b"OLD-BINARY"
    leftovers = [p for p in root.iterdir() if p.name.startswith(".frida-server.")]
    assert leftovers == []
