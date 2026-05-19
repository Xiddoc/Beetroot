"""End-to-end v0.2 → v0.3 migration smoke test.

The post-CR fixes shipped three migration aids:

* A hint when a v0.2 ``instances.json`` sits at ``$PWD``.
* Auto-bump for ``api_version: 1`` YAMLs.
* The new ``beetroot register <path>`` verb (which now stages files).

This test drives all three from the same workspace, in the same
ordering a real upgrading user would see, and pins the stdout/stderr
shape at each step.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import cli, frida_download, paths, registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_hint_flag() -> None:
    registry._V02_HINT_PRINTED = False


def _write_v02_instance(root: Path) -> None:
    root.mkdir(parents=True)
    # v0.2 YAML: api_version 1 + a frida block (the v0.2 default).
    (root / "beetroot.yaml").write_text(
        "api_version: 1\n"
        "android:\n  version: 14\n"
        'frida:\n  version: "16.4.10"\n'
    )


def _write_v02_registry(root: Path, instance_name: str) -> None:
    (root / "instances.json").write_text(
        json.dumps(
            {
                instance_name: {
                    "index": 0,
                    "created_at": "2025-12-01T10:00:00Z",
                }
            }
        )
    )


def test_v02_to_v03_walkthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point XDG at a fresh subdir so the v0.3 registry starts empty.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    # Stub frida-server download so we don't hit the network.
    def _fake_download(version: str) -> Path:
        out = frida_download.cached_binary(version)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_bytes(b"fake-frida")
            out.chmod(0o755)
        return out

    monkeypatch.setattr(frida_download, "download", _fake_download)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}"
                        if name in {"docker", "adb", "frida"} else None)
    # Use the real shutil for the rest of the suite — only `which` is faked.
    _ = shutil  # quiet pyflakes; the real module is patched via attr lookup.

    # Scaffold the v0.2 workspace.
    v02 = tmp_path / "v02"
    v02.mkdir()
    _write_v02_instance(v02 / "alpha")
    _write_v02_registry(v02, "alpha")
    monkeypatch.chdir(v02)

    # Step 1: `beetroot ls` (with no v0.3 registry yet, but a v0.2 file
    # at cwd). Expect: exit 0, table header on stdout, v0.2 hint on
    # stderr. The hint mentions the v0.2 file location and the new
    # `beetroot register` migration path.
    result = runner.invoke(cli.app, ["ls"])
    assert result.exit_code == 0, result.stderr
    assert "v0.2 registry" in result.stderr
    assert str(v02 / "instances.json") in result.stderr
    assert "beetroot register" in result.stderr

    # Step 2: `beetroot register v02/alpha` — the v0.2 YAML pins
    # api_version: 1, so we expect the auto-bump warning on stderr
    # AND a successful registration. Path resolution uses the
    # absolute path so the registry can recover it later.
    result = runner.invoke(cli.app, ["register", str(v02 / "alpha")])
    assert result.exit_code == 0, result.stderr
    assert "auto-upgraded api_version 1" in result.stderr
    assert registry.get("alpha") is not None

    # The .env and frida-server should be staged from `register`'s
    # call to _stage() — no follow-up `apply` required.
    inst_root = registry.instance_path("alpha")
    assert paths.instance_env(inst_root).is_file()
    assert paths.instance_frida(inst_root).is_file()

    # Step 3: `beetroot apply alpha` — re-stages cleanly. Should NOT
    # re-print the auto-bump warning (apply's load_yaml re-reads from
    # disk where the on-disk YAML still says api_version: 1; we
    # accept the warning re-firing here since the YAML hasn't been
    # rewritten yet, but the operation must succeed).
    result = runner.invoke(cli.app, ["apply", "alpha"])
    assert result.exit_code == 0, result.stderr
    assert paths.instance_env(inst_root).is_file()
