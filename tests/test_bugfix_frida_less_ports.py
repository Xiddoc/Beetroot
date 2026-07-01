"""Regression tests for #158 — a Frida-less ports config must not crash ls/status.

A valid adb-only ``ports:`` config (no ``frida``/``frida_control`` service, no
``frida:`` block) produces a ``ports.well_known`` dict with no ``frida`` key.
Before the fix, ``Instance.frida_address`` / ``health()`` / ``_instance_json_row``
indexed ``wk['frida']`` eagerly and raised ``KeyError: 'frida'``, taking down the
whole-fleet ``ls`` because every row is built in one comprehension. Now the
redroid backend returns the ``unsupported`` sentinel like the vm backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, compose, config, registry
from beetroot.config import PortMapping


def _adb_only_cfg() -> config.InstanceConfig:
    """Build a validator-accepted adb-only config (no Frida service at all)."""
    return config.InstanceConfig(
        frida=None,
        ports=[PortMapping(service="adb", guest=5555)],
    )


def test_frida_address_returns_sentinel_for_adb_only(cli_root: Path) -> None:
    registry.add_allocating("alpha", cli_root / "alpha")
    inst = api.Instance(name="alpha", root=cli_root / "alpha", cfg=_adb_only_cfg())
    assert inst.frida_address == api.FRIDA_ADDRESS_UNSUPPORTED
    # adb still resolves normally — only frida is absent.
    assert inst.adb_address == "localhost:5555"


def test_health_does_not_crash_without_frida_service(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.add_allocating("alpha", cli_root / "alpha")
    inst = api.Instance(name="alpha", root=cli_root / "alpha", cfg=_adb_only_cfg())
    # ``health()`` used to KeyError on ``wk['frida']`` before probing anything;
    # stub the compose/adb/binder probes so the test isolates that fix.
    monkeypatch.setattr(compose, "ps_status", lambda name, root: "running")
    monkeypatch.setattr("shutil.which", lambda _: None)
    with patch("subprocess.run") as run:
        run.return_value = None
        checks = inst.health()
    assert "frida.handshake" in checks
    # frida disabled (cfg.frida is None) → the socket check is skipped, not run.
    assert checks["frida.handshake"].status == "skip"


def test_instance_json_row_survives_frida_less_config(cli_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    (root / "beetroot.yaml").write_text(
        "api_version: 3\nandroid:\n  version: 14\nports:\n"
        "  - service: adb\n    guest: 5555\n"
    )
    registry.add_allocating("alpha", root)
    inst = api.Instance.load("alpha")
    row = cli._instance_json_row(inst)
    assert row["frida"] == api.FRIDA_ADDRESS_UNSUPPORTED
    assert row["frida_address"] == api.FRIDA_ADDRESS_UNSUPPORTED
    assert row["adb"] == "localhost:5555"


def _write_instance(tmp_path: Path, name: str, *, frida: bool) -> Path:
    root = tmp_path / name
    root.mkdir()
    body = "api_version: 3\nandroid:\n  version: 14\n"
    if frida:
        body += "frida:\n  version: '16.4.10'\n"
    else:
        body += "ports:\n  - service: adb\n    guest: 5555\n"
    (root / "beetroot.yaml").write_text(body)
    registry.add_allocating(name, root)
    return root


def test_fleet_ls_renders_both_frida_full_and_frida_less(
    isolated_registry: Path, tmp_path: Path
) -> None:
    _write_instance(tmp_path, "withfrida", frida=True)
    _write_instance(tmp_path, "nofrida", frida=False)

    result = CliRunner().invoke(cli.app, ["ls"])
    assert result.exit_code == 0, result.stderr
    # The whole fleet renders — neither instance aborts the comprehension.
    assert "withfrida" in result.stdout
    assert "nofrida" in result.stdout
    assert api.FRIDA_ADDRESS_UNSUPPORTED in result.stdout


def test_fleet_ls_json_renders_both_rows(isolated_registry: Path, tmp_path: Path) -> None:
    _write_instance(tmp_path, "withfrida", frida=True)
    _write_instance(tmp_path, "nofrida", frida=False)

    result = CliRunner().invoke(cli.app, ["ls", "--json"])
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert rows["nofrida"]["frida"] == api.FRIDA_ADDRESS_UNSUPPORTED
    assert rows["withfrida"]["frida"].startswith("localhost:")
