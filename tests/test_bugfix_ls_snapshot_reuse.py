"""Regression tests for #230 — ls/status reuse a snapshot + memoize the index.

ls/status re-read the registry and each ``beetroot.yaml`` O(N)+ times per
command because ``Instance.index`` / ``ports`` / ``adb_address`` /
``frida_address`` each re-called ``registry.get`` (via ``_meta``) and the cli
row builders re-derived addresses already computed. The fix memoizes
``Instance.index`` as a ``cached_property`` and derives the row addresses from a
single ``well_known`` dict — so the output is byte-for-byte identical but the
redundant reads collapse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, registry


def _make(tmp_path: Path, name: str, *, frida: bool = True) -> Path:
    root = tmp_path / name
    root.mkdir()
    body = "api_version: 3\nandroid:\n  version: 14\n"
    if not frida:
        body += "ports:\n  - service: adb\n    guest: 5555\n"
    (root / "beetroot.yaml").write_text(body)
    registry.add_allocating(name, root)
    return root


def test_index_is_computed_once(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    CliRunner().invoke(cli.app, ["create", "alpha"])
    inst = api.Instance.load("alpha")

    calls = {"n": 0}
    real_get = registry.get

    def _spy_get(name: str) -> registry.InstanceMeta | None:
        calls["n"] += 1
        return real_get(name)

    # ``_meta`` reaches ``registry.get`` on the shared module object.
    monkeypatch.setattr("beetroot.registry.get", _spy_get)

    # Two accesses of the cached_property → exactly one registry lookup.
    first = inst.index
    second = inst.index

    assert first == second
    assert calls["n"] == 1


def test_meta_still_raises_when_row_disappears(cli_root: Path) -> None:
    # The cached_property must preserve the disappearance contract: a fresh
    # Instance whose registry row is gone raises on first index access.
    CliRunner().invoke(cli.app, ["create", "alpha"])
    inst = api.Instance.load("alpha")
    registry.remove("alpha")
    with pytest.raises(api.InstanceNotFoundError):
        _ = inst.index


def test_ls_json_output_unchanged_and_bounded_yaml_reads(
    cli_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make(tmp_path, "alpha", frida=True)
    _make(tmp_path, "bravo", frida=True)
    _make(tmp_path, "charlie", frida=False)

    from beetroot import config as config_mod

    load_calls = {"n": 0}
    real_load = config_mod.load_yaml

    def _spy_load(path: Path) -> config_mod.InstanceConfig:
        load_calls["n"] += 1
        return real_load(path)

    monkeypatch.setattr("beetroot.config.load_yaml", _spy_load)
    # ``registry`` binds ``load_yaml`` by name at import, so patch that surface
    # too — every parse of a ``beetroot.yaml`` must be counted.
    monkeypatch.setattr("beetroot.registry.load_yaml", _spy_load)

    result = CliRunner().invoke(cli.app, ["ls", "--json"])
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert set(rows) == {"alpha", "bravo", "charlie"}
    assert rows["charlie"]["frida"] == api.FRIDA_ADDRESS_UNSUPPORTED
    assert rows["alpha"]["frida"].startswith("localhost:")

    # Bounded: ``load_yaml`` runs a small constant number of times per instance,
    # not the unbounded O(N) blowup the pre-fix row builders caused. Allow a
    # generous ceiling well below the old per-address re-parse counts.
    assert load_calls["n"] <= 3 * len(rows)


def test_ls_json_matches_direct_row_builder(cli_root: Path, tmp_path: Path) -> None:
    # The reused-snapshot path must produce the same dict the direct row
    # builder does (behavior parity, not just line coverage).
    _make(tmp_path, "alpha", frida=True)
    inst = api.Instance.load("alpha")
    direct = cli._instance_json_row(inst)

    result = CliRunner().invoke(cli.app, ["ls", "--json"])
    assert result.exit_code == 0, result.stderr
    via_ls = json.loads(result.stdout)["alpha"]
    # ``status`` is live so compare the stable derived address/index fields.
    for key in ("adb", "frida", "adb_address", "frida_address", "index", "path"):
        assert via_ls[key] == direct[key]
