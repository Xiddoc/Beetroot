"""v0.2 YAMLs with ``api_version: 1`` auto-bump to v2 with a warning.

v0.2 pinned ``api_version: 1``. v0.3 raised :data:`SUPPORTED_API_VERSION`
to ``2`` — strictly additive, no fields renamed. Hard-failing on a v0.2
YAML would force every researcher to hand-edit every ``beetroot.yaml``
before running anything; the CR asked for an auto-bump + one-line
stderr warning instead. Persistence happens organically on the next
``beetroot apply``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import config


def test_v02_api_version_auto_bumps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 1\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 1" in err
    # The hint must mention `beetroot apply` so users know how to
    # persist the bump on disk.
    assert "apply" in err


def test_explicit_v2_api_version_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 2\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == 2
    err = capsys.readouterr().err
    assert "auto-upgraded" not in err


def test_omitted_api_version_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("android:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    err = capsys.readouterr().err
    assert "auto-upgraded" not in err


def test_unsupported_api_version_still_raises(tmp_path: Path) -> None:
    # Future-versions (99) and other non-1 mismatches still hard-fail,
    # the auto-bump is targeted at v0.2 → v0.3 only.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 99\nandroid:\n  version: 14\n")

    with pytest.raises(Exception, match="api_version"):
        config.load_yaml(yaml_path)


def test_v02_yaml_with_frida_block_round_trips(tmp_path: Path) -> None:
    # A v0.2 YAML that carries the frida block (v0.2's default
    # behavior) should auto-bump cleanly and preserve the block.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text(
        "api_version: 1\n"
        "android:\n  version: 14\n"
        'frida:\n  version: "16.4.10"\n'
    )

    cfg = config.load_yaml(yaml_path)
    assert cfg.api_version == 2
    assert cfg.frida is not None
    assert cfg.frida.version == "16.4.10"
