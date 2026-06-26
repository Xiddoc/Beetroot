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
import yaml

from beetroot import builder, config

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "beetroot"
_CONFIG_PY = _SRC / "config.py"
_BUILDER_PY = _SRC / "builder.py"
_ROOTFS_RELEASE_YML = _ROOT / ".github" / "workflows" / "rootfs-release.yml"

# Note: ``\d{2}`` assumes two-digit versions (11-14 today). A future single- or
# triple-digit version would need this widened — but the constant literal would
# then also stop matching, so the ``>= 3`` floor below trips and forces the edit.
# A version enumeration: two or more 2-digit numbers (each optionally wrapped in
# RST/markdown backticks) joined by commas, with an optional trailing "or". This
# matches the constant literal ``{11, 12, 13, 14}``, the prose docstrings
# ("11, 12, 13, or 14"), and the backtick form ("``11``, ``12``, ``13``, ``14``")
# after whitespace is collapsed.
_ENUM_RE = re.compile(r"\d{2}(?:[`\s]*,[`\s]*(?:or[`\s]+)?`*\d{2})+")

# Doc pages that hand-copy the version list, paired with the phrasing style they
# use. These are guarded by a *presence* check (the canonical phrase must
# appear) rather than the generic scan, so an unrelated number list elsewhere on
# the page can't false-positive. Keep this list in sync with the AGENTS.md
# "Adding a new Android version" checklist.
_README = _ROOT / "README.md"
_CONFIG_MD = _ROOT / "docs" / "reference" / "config.md"
_CI_WORKFLOW_MD = _ROOT / "docs" / "guides" / "ci-reusable-workflow.md"
_DOC_SOURCES: list[tuple[Path, str]] = [
    (_README, "oxford"),
    (_CONFIG_MD, "backtick"),
    (_CI_WORKFLOW_MD, "backtick"),
]


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


def test_doc_version_enumerations_match_constant() -> None:
    versions = sorted(config._VALID_ANDROID_VERSIONS)
    oxford = ", ".join(str(v) for v in versions[:-1]) + f", or {versions[-1]}"
    backtick = ", ".join(f"`{v}`" for v in versions)
    phrases = {"oxford": oxford, "backtick": backtick}
    for path, style in _DOC_SOURCES:
        phrase = phrases[style]
        assert phrase in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(_ROOT)} should list the supported versions as "
            f"{phrase!r} but doesn't; update it (or the constant) to match "
            f"_VALID_ANDROID_VERSIONS={versions} (issue #98)."
        )


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
def test_base_image_tag_well_formed_for_every_version(version: int) -> None:
    tag = config.base_image_tag(config.Android(version=version))
    assert re.fullmatch(rf"redroid/redroid:{version}\.0\.0_litegapps_houdini_magisk", tag), tag


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
@pytest.mark.parametrize("gapps", ["none", "minimal", "full"])
def test_base_image_tag_starts_with_version_for_every_gapps(
    version: int, gapps: config.GappsIntent
) -> None:
    tag = config.base_image_tag(config.Android(version=version, gapps=gapps))
    assert tag.startswith(f"redroid/redroid:{version}.0.0")
    assert tag.endswith("_houdini_magisk")


@pytest.mark.parametrize("version", sorted(config._VALID_ANDROID_VERSIONS))
@pytest.mark.parametrize("vendor", sorted(config._VENDOR_SLUG))
def test_base_image_tag_well_formed_for_every_vendor(version: int, vendor: str) -> None:
    tag = config.base_image_tag(
        config.Android(version=version, gapps="full", gapps_vendor=vendor)  # type: ignore[arg-type]
    )
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


def test_rootfs_release_workflow_versions_match_constant() -> None:
    # rootfs-release.yml hard-codes the supported-version list in two places (the
    # publish matrix + the workflow_dispatch input description). Both must track
    # config._VALID_ANDROID_VERSIONS or adding Android 15 silently skips
    # publishing a 15 rootfs (issue #79, review finding #5).
    doc = yaml.safe_load(_ROOTFS_RELEASE_YML.read_text(encoding="utf-8"))
    expected = sorted(config._VALID_ANDROID_VERSIONS)

    matrix = doc["jobs"]["build-and-publish"]["strategy"]["matrix"]["android_version"]
    assert sorted(int(v) for v in matrix) == expected, (
        f"rootfs-release.yml publish matrix {matrix} disagrees with "
        f"_VALID_ANDROID_VERSIONS={expected}; update the matrix (issue #79)."
    )

    # PyYAML parses `on:` as the boolean key True, so look it up that way.
    description = doc[True]["workflow_dispatch"]["inputs"]["android_version"]["description"]
    found_in_desc = sorted(int(n) for n in re.findall(r"\d{2}", description))
    assert found_in_desc == expected, (
        f"rootfs-release.yml android_version input description {description!r} disagrees "
        f"with _VALID_ANDROID_VERSIONS={expected}; update it (issue #79)."
    )


def test_rootfs_release_workflow_docker_version_matches_builder() -> None:
    # rootfs-release.yml bakes with a hard-coded DOCKER_VERSION env that feeds the
    # composite fingerprint; it must equal builder._DEFAULT_DOCKER_VERSION or the
    # workflow publishes assets under a fingerprint the CLI never asks for — the
    # publish<->fetch contract silently breaks (issue #79, review finding #7).
    doc = yaml.safe_load(_ROOTFS_RELEASE_YML.read_text(encoding="utf-8"))
    workflow_docker_version = doc["jobs"]["build-and-publish"]["env"]["DOCKER_VERSION"]
    assert workflow_docker_version == builder._DEFAULT_DOCKER_VERSION, (
        f"rootfs-release.yml DOCKER_VERSION={workflow_docker_version!r} disagrees with "
        f"builder._DEFAULT_DOCKER_VERSION={builder._DEFAULT_DOCKER_VERSION!r}; the published "
        "rootfs fingerprint would not match what the CLI fetches (issue #79)."
    )
