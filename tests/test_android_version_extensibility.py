"""
Extensibility guards for the supported Android-version list (issue #98).

``config._VALID_ANDROID_VERSIONS`` is the single source of truth for which
Android major versions Beetroot supports. Two things keep that single-sourcing
honest:

1. The human-readable enumerations sprinkled through docstrings ("11, 12, 13,
   or 14") are hand-copied and would silently lie when a new version is added.
   ``test_source_version_enumerations_match_constant`` greps the source and
   fails CI if any enumeration disagrees with the constant.
2. Both image-tag derivations (``base_image_tag`` / ``vm_redroid_image``) are
   pure functions of ``version`` but were only ever spot-checked for a couple
   versions. The parametrized tests below exercise *every* member of the set,
   so a version whose tag wouldn't be well-formed is caught at the unit level.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from beetroot import config
from beetroot.builder import GappsVariant

_SRC = Path(__file__).resolve().parents[1] / "src" / "beetroot"
_CONFIG_PY = _SRC / "config.py"
_BUILDER_PY = _SRC / "builder.py"

# A version enumeration: two or more 2-digit numbers (each optionally wrapped in
# RST/markdown backticks) joined by commas, with an optional trailing "or". This
# matches the constant literal ``{11, 12, 13, 14}``, the prose docstrings
# ("11, 12, 13, or 14"), and the backtick form ("``11``, ``12``, ``13``, ``14``")
# after whitespace is collapsed.
_ENUM_RE = re.compile(r"\d{2}(?:[`\s]*,[`\s]*(?:or[`\s]+)?`*\d{2})+")


def _enumerations(path: Path) -> list[list[int]]:
    text = " ".join(path.read_text(encoding="utf-8").split())
    return [[int(n) for n in re.findall(r"\d{2}", m)] for m in _ENUM_RE.findall(text)]


def test_source_version_enumerations_match_constant() -> None:
    expected = sorted(config._VALID_ANDROID_VERSIONS)
    found = _enumerations(_CONFIG_PY) + _enumerations(_BUILDER_PY)
    # Guard against the regex going stale (e.g. a phrasing change that makes the
    # drift check vacuously pass): the constant literal plus the two config.py
    # docstrings plus the builder.py docstring are the known sites.
    assert len(found) >= 3, f"expected to find version enumerations; got {found}"
    for enumeration in found:
        assert sorted(enumeration) == expected, (
            f"version enumeration {enumeration} disagrees with "
            f"_VALID_ANDROID_VERSIONS={expected}; update the docstring/source "
            "(or the constant) so they stay in sync (issue #98)."
        )


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
def test_base_image_tag_well_formed_for_every_version(version: int) -> None:
    tag = config.base_image_tag(config.Android(version=version))
    assert re.fullmatch(
        rf"redroid/redroid:{version}\.0\.0_litegapps_houdini_magisk", tag
    ), tag


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
@pytest.mark.parametrize("gapps", ["none", "lite", "full", "mindthegapps"])
def test_base_image_tag_starts_with_version_for_every_gapps(
    version: int, gapps: GappsVariant
) -> None:
    tag = config.base_image_tag(config.Android(version=version, gapps=gapps))
    assert tag.startswith(f"redroid/redroid:{version}.0.0")
    assert tag.endswith("_houdini_magisk")


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
def test_vm_redroid_image_well_formed_for_every_version(version: int) -> None:
    image = config.vm_redroid_image(version)
    assert re.fullmatch(rf"redroid/redroid:{version}\.0\.0-latest", image), image


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
def test_validate_accepts_every_supported_version(version: int) -> None:
    assert config.validate_android_version(version) == version


def test_default_version_is_supported() -> None:
    assert config.DEFAULT_ANDROID_VERSION in config._VALID_ANDROID_VERSIONS
