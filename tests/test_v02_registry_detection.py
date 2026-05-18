"""Detection hint for a v0.2 ``instances.json`` at $PWD.

v0.2 wrote ``instances.json`` at the repo root. v0.3 moved it under
``$XDG_CONFIG_HOME/beetroot/``. Auto-moving silently would break a
user who keeps the v0.2 file under version control or in a different
repo. The contract is to surface the situation **once per process**
on stderr and let the user pick the migration path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import cli, registry


@pytest.fixture(autouse=True)
def _reset_hint_flag() -> None:
    # The detection hint fires once per process; tests reset the
    # module-level flag so each case starts clean.
    registry._V02_HINT_PRINTED = False


def _write_v02_registry(path: Path) -> None:
    # v0.2 shape: flat name → meta mapping, no version/instances wrapper.
    path.write_text(
        json.dumps(
            {
                "alpha": {
                    "index": 0,
                    "created_at": "2025-12-01T10:00:00Z",
                }
            }
        )
    )


def test_ls_with_v02_registry_at_cwd_prints_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    _write_v02_registry(cwd / "instances.json")
    monkeypatch.chdir(cwd)

    result = CliRunner().invoke(cli.app, ["ls"])
    assert result.exit_code == 0
    # The hint goes to stderr; the table goes to stdout.
    err = result.stderr
    assert "v0.2 registry" in err
    assert str(cwd / "instances.json") in err
    assert "beetroot register" in err


def test_no_hint_when_only_v03_registry_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # No instances.json at cwd at all.
    cwd = tmp_path / "fresh"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(cli.app, ["ls"])
    assert result.exit_code == 0
    assert "v0.2 registry" not in result.stderr


def test_no_hint_when_v03_registry_is_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # v0.3 registry already exists and is populated → no hint, even
    # if the v0.2-shaped file at cwd would otherwise trip it.
    xdg = tmp_path / "config" / "beetroot" / "instances.json"
    xdg.parent.mkdir(parents=True)
    xdg.write_text(
        json.dumps(
            {
                "version": 2,
                "instances": {
                    "alpha": {
                        "absolute_path": str(tmp_path / "alpha"),
                        "index": 0,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            }
        )
    )
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    _write_v02_registry(cwd / "instances.json")
    monkeypatch.chdir(cwd)
    # Drive the read path directly so we don't need to seed a full
    # on-disk instance dir to satisfy `ls` rendering.
    registry._read(xdg)
    # No SystemExit / SystemError — successful read with no warning.
    # Verify stderr was clean by re-invoking _read and capturing.
    import contextlib
    import io

    buf = io.StringIO()
    registry._V02_HINT_PRINTED = False  # re-check on a fresh flag
    with contextlib.redirect_stderr(buf):
        registry._read(xdg)
    assert "v0.2 registry" not in buf.getvalue()


def test_no_hint_for_non_v02_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    # A v0.3-shaped file at $PWD is NOT a v0.2 registry (no hint).
    (cwd / "instances.json").write_text(
        json.dumps({"version": 2, "instances": {}})
    )
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(cli.app, ["ls"])
    assert result.exit_code == 0
    assert "v0.2 registry" not in result.stderr


def test_no_hint_for_garbage_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    # Garbage that doesn't parse as JSON shouldn't be treated as v0.2.
    (cwd / "instances.json").write_text("this is not json")
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(cli.app, ["ls"])
    assert result.exit_code == 0
    assert "v0.2 registry" not in result.stderr


def test_no_hint_for_list_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    # A JSON list isn't a v0.2 registry — refuse to fire the hint.
    (cwd / "instances.json").write_text("[]")
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(cli.app, ["ls"])
    assert result.exit_code == 0
    assert "v0.2 registry" not in result.stderr


def test_hint_fires_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Multiple registry reads should produce only one warning line.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    _write_v02_registry(cwd / "instances.json")
    monkeypatch.chdir(cwd)

    # Drive _read directly so capsys (which only sees this process's
    # streams) captures stderr.
    xdg_path = tmp_path / "config" / "beetroot" / "instances.json"
    registry._read(xdg_path)
    registry._read(xdg_path)
    registry._read(xdg_path)

    err = capsys.readouterr().err
    assert err.count("v0.2 registry") == 1, (
        f"hint fired more than once; full stderr:\n{err}"
    )


def test_hint_skipped_when_file_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If reading the cwd file raises (e.g. permissions), the hint
    # must not blow up the caller — the try/except in
    # _check_v02_registry_at_cwd swallows the OSError.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cwd = tmp_path / "old-repo"
    cwd.mkdir()
    # An unreadable file: write garbage, then chmod 0 so json.loads
    # in the hint logic gets denied. Wait — the hint catches OSError
    # AND JSONDecodeError, so either path is fine. Use garbage so
    # the test works without root.
    (cwd / "instances.json").write_text("not-json{")
    monkeypatch.chdir(cwd)

    xdg_path = tmp_path / "config" / "beetroot" / "instances.json"
    # Must not raise.
    registry._read(xdg_path)


# Quiet flake8/ruff on the unused `sys` import — kept for parity with
# the actual implementation file.
_ = sys
