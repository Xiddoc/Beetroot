"""Legacy YAMLs auto-bump ``api_version`` to :data:`SUPPORTED_API_VERSION`.

v0.6 (D3) extended the auto-bump to also cover v0.4-pinned
``api_version: 3``. The 1→2→3 bumps were strictly additive (no fields
renamed); the 3→4 bump moves ``stealth.denylist`` to ``magisk.denylist``.
A v0.4 YAML that did NOT use ``stealth:`` at all bumps silently; one that
DID use ``stealth:`` gets a clear migration error (D1/D3).
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


def test_v03_api_version_auto_bumps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # T1's schema bump: v0.3's `api_version: 2` auto-upgrades to the
    # current default. Same dedup, same one-shot warning, same call to
    # action.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 2\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 2" in err
    assert "apply" in err


def test_v04_api_version_without_stealth_auto_bumps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # D3: v0.4's `api_version: 3` without a stealth: key is a valid
    # additive bump — auto-upgrade to SUPPORTED_API_VERSION with a warning.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 3\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 3" in err
    assert "apply" in err


def test_v04_api_version_with_stealth_raises_migration_error(
    tmp_path: Path,
) -> None:
    # D1/D3: a v0.4 YAML that used stealth.denylist must fail with a
    # clear, actionable migration error — the stealth: key was renamed
    # to magisk: in api_version 4. This path cannot silently auto-bump
    # because the data lives under a different key.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text(
        "api_version: 3\n"
        "android:\n  version: 14\n"
        "stealth:\n"
        "  denylist:\n"
        "    - com.google.android.gms\n"
    )

    with pytest.raises(Exception) as exc_info:
        config.load_yaml(yaml_path)
    msg = str(exc_info.value)
    # Must name the old key and the new key so the user knows what to change.
    assert "stealth" in msg
    assert "magisk" in msg.lower()
    # Must mention api_version so the user knows to bump it too.
    assert "api_version" in msg


def test_explicit_current_api_version_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text(
        f"api_version: {config.SUPPORTED_API_VERSION}\n"
        "android:\n  version: 14\n"
    )

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
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
    # Future-versions (99) and other unrecognised mismatches still
    # hard-fail; the auto-bump is targeted at known-additive legacy
    # versions only.
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
    assert cfg.api_version == config.SUPPORTED_API_VERSION
    assert cfg.frida is not None
    assert cfg.frida.version == "16.4.10"


def test_auto_bump_warning_deduplicates_per_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # CR #2 finding A2: the warning used to fire on every load.
    # `beetroot ls` over 5 v0.2 instances → 5+ warning lines; a
    # single `register bravo` triple-prints because of cascading
    # `all_resolved_ports` calls. The fix is a module-level set
    # of paths we've already warned about in this process. The
    # conftest's autouse fixture clears it between tests so this
    # test starts from a known-empty state.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 1\nandroid:\n  version: 14\n")

    config.load_yaml(yaml_path)
    err_first = capsys.readouterr().err
    assert err_first.count("auto-upgraded") == 1

    config.load_yaml(yaml_path)
    err_second = capsys.readouterr().err
    assert err_second.count("auto-upgraded") == 0

    # Third load via a sibling Path() pointing at the same file
    # must also be deduped (the dedup key is the resolved path).
    config.load_yaml(yaml_path.resolve())
    err_third = capsys.readouterr().err
    assert err_third.count("auto-upgraded") == 0


def test_auto_bump_warning_fires_per_distinct_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Dedup is keyed by path — two different YAML files each get
    # their own one-shot warning.
    alpha_yaml = tmp_path / "alpha" / "beetroot.yaml"
    alpha_yaml.parent.mkdir()
    alpha_yaml.write_text("api_version: 1\nandroid:\n  version: 14\n")
    bravo_yaml = tmp_path / "bravo" / "beetroot.yaml"
    bravo_yaml.parent.mkdir()
    bravo_yaml.write_text("api_version: 1\nandroid:\n  version: 14\n")

    config.load_yaml(alpha_yaml)
    config.load_yaml(bravo_yaml)
    err = capsys.readouterr().err
    # One warning per distinct path.
    assert err.count("auto-upgraded") == 2
    assert str(alpha_yaml) in err
    assert str(bravo_yaml) in err
