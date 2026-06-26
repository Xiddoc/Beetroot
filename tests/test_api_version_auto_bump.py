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
from pydantic import ValidationError

from beetroot import config


def test_v02_api_version_auto_bumps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 1\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 1" in err
    # The hint must mention `beetroot apply` so users know how to
    # persist the bump on disk.
    assert "apply" in err


def test_v03_api_version_auto_bumps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    capsys: pytest.CaptureFixture[str],
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

    with pytest.raises(ValidationError) as exc_info:
        config.load_yaml(yaml_path)
    msg = str(exc_info.value)
    # Must name the old key and the new key so the user knows what to change.
    assert "stealth" in msg
    assert "magisk" in msg.lower()
    # Must mention api_version so the user knows to bump it too.
    assert "api_version" in msg
    # The auto-bump "auto-upgraded … run apply" line must NOT appear — it
    # contradicts the migration error that correctly wins. The only message
    # the user should see is the migration error itself.
    err = capsys.readouterr().err
    assert "auto-upgraded" not in err


def test_v06_api_version_without_gpu_mode_auto_bumps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #106: v0.6's `api_version: 4` without a display.gpu_mode key is a clean
    # rename-free bump — auto-upgrade to SUPPORTED_API_VERSION with a warning.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 4\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 4" in err
    assert "apply" in err


def test_v06_api_version_with_gpu_mode_raises_migration_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #106: a YAML that used display.gpu_mode must fail with a clear migration
    # error — the field was renamed to display.rendering in api_version 5.
    # This path cannot silently auto-bump because the field was renamed.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 4\nandroid:\n  version: 14\ndisplay:\n  gpu_mode: host\n")

    with pytest.raises(ValidationError) as exc_info:
        config.load_yaml(yaml_path)
    msg = str(exc_info.value)
    # Must name the old key and the new key so the user knows what to change.
    assert "gpu_mode" in msg
    assert "rendering" in msg
    assert "api_version" in msg
    # The auto-bump line must NOT appear — the migration error wins cleanly.
    err = capsys.readouterr().err
    assert "auto-upgraded" not in err


def test_v07_api_version_auto_bumps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # #124: the `lifecycle` field bumped SUPPORTED 5 → 6 additively, so a
    # YAML pinned at 5 auto-upgrades with a warning (no renamed key).
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 5\nandroid:\n  version: 14\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    # An auto-bumped, lifecycle-less YAML defaults to the durable contract.
    assert cfg.lifecycle == "durable"
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 5" in err
    assert "apply" in err


def test_v06_api_version_without_legacy_gapps_auto_bumps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #107: the gapps split bumped SUPPORTED 6 → 7. A YAML pinned at 6 with a
    # still-valid gapps intent (or none) is a clean rename-free bump.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 6\nandroid:\n  version: 14\n  gapps: full\n")

    cfg = config.load_yaml(yaml_path)

    assert cfg.api_version == config.SUPPORTED_API_VERSION
    assert cfg.android.gapps == "full"
    err = capsys.readouterr().err
    assert "auto-upgraded api_version 6" in err
    assert "apply" in err


def test_v06_api_version_with_legacy_gapps_value_raises_migration_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #107: a YAML that wrote the now-vendor value `gapps: lite` must fail with
    # a clear migration error naming the intent + gapps_vendor replacement —
    # this path cannot silently auto-bump because the value was split out.
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text("api_version: 6\nandroid:\n  version: 14\n  gapps: lite\n")

    with pytest.raises(ValidationError) as exc_info:
        config.load_yaml(yaml_path)
    msg = str(exc_info.value)
    # Must name the legacy value and the new intent + vendor replacement.
    assert "lite" in msg
    assert "minimal" in msg
    assert "gapps_vendor: litegapps" in msg
    assert "api_version" in msg
    # The auto-bump line must NOT appear — the migration error wins cleanly.
    err = capsys.readouterr().err
    assert "auto-upgraded" not in err


def test_explicit_current_api_version_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = tmp_path / "beetroot.yaml"
    yaml_path.write_text(f"api_version: {config.SUPPORTED_API_VERSION}\nandroid:\n  version: 14\n")

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
    yaml_path.write_text('api_version: 1\nandroid:\n  version: 14\nfrida:\n  version: "16.4.10"\n')

    cfg = config.load_yaml(yaml_path)
    assert cfg.api_version == config.SUPPORTED_API_VERSION
    assert cfg.frida is not None
    assert cfg.frida.version == "16.4.10"


def test_auto_bump_warning_deduplicates_per_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
