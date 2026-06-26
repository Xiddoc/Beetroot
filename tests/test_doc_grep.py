"""Pin the doc surface against drift from already-removed v0.2 identifiers.

This test file complements the live-code tests by asserting that obsolete
spellings never reappear in user-facing prose. CHANGELOG.md is allow-listed
because it preserves history under previous theme blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The migration guide deliberately mentions removed verbs/identifiers in
# past tense ("`--preset` is gone", "paths.repo_root() referenced in custom
# tooling") — that's its job. Excluding it from the grep keeps the gate
# strict for every other page.
ALLOWLISTED_PATHS: frozenset[str] = frozenset(
    {
        "CHANGELOG.md",
        "migration-v0.2-to-v0.3.md",
    }
)

DOC_FILES: list[Path] = [
    *(REPO_ROOT / "docs").rglob("*.md"),
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
]

# Each entry is (forbidden_pattern, human_explanation).
# Patterns are interpreted as plain substrings unless they look like regex
# (start with a backslash or contain a metachar that we explicitly handle).
FORBIDDEN_LITERAL: tuple[tuple[str, str], ...] = (
    ("cmd_setup", "T4 removed the cmd_setup argparse handler"),
    ("cmd_create", "T4 removed the cmd_create argparse handler"),
    ("setup_runner", "T5 renamed setup_runner.py to builder.py"),
    ("bootstrap_base_image", "T5 renamed bootstrap_base_image to build_image"),
    ("paths.repo_root", "T1 deleted paths.repo_root"),
    ("paths.compose_file", "T1 deleted paths.compose_file"),
    ("paths.presets_dir", "T1 deleted paths.presets_dir"),
    ("beetroot setup ", "T5 renamed `beetroot setup` to `beetroot build`"),
)

# Regex-based forbidden patterns where a substring match is too coarse.
# Invented flags are anchored so we catch them on every page including the
# migration guide — they are CR #1 finding 1 regressions in disguise, and
# the guide is allowed to discuss removed verbs but not to cite invented
# new ones. ``ALLOWLISTED_PATHS`` therefore doesn't help here; the regex
# tests below skip the migration guide for the same reason the literal
# tests do, but the migration guide IS hand-checked for these strings via
# the dedicated assertion at the bottom of the file.
FORBIDDEN_REGEX: tuple[tuple[str, str], ...] = (
    (r"\B--preset\b", "T3 removed the --preset flag"),
    # CR #1 finding 1: the migration guide invented these v0.2-shape
    # flags that ``beetroot destroy`` never accepted. Pin them on
    # every page including the migration guide via the dedicated
    # assertion below; the test_no_forbidden_regex_in_user_docs
    # still excludes the guide for fairness with past-tense
    # discussion of removed verbs.
    (r"\bdestroy --no-rm\b", "CR #1: --no-rm is not a real `destroy` flag"),
    (r"\bdestroy --no-data\b", "CR #1: --no-data is not a real `destroy` flag"),
    (
        r"\bdestroy --no-deregister\b",
        "CR #1: --no-deregister is not a real `destroy` flag",
    ),
)


def _iter_lines(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, text) pairs for the file, ignoring empties cheaply."""
    return list(enumerate(path.read_text().splitlines(), start=1))


def test_no_forbidden_literals_in_user_docs() -> None:
    failures: list[str] = []
    for path in DOC_FILES:
        if path.name in ALLOWLISTED_PATHS:
            continue
        for lineno, line in _iter_lines(path):
            for needle, why in FORBIDDEN_LITERAL:
                if needle in line:
                    failures.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                        f"forbidden token {needle!r} present ({why}): {line.strip()!r}"
                    )
    assert not failures, "Doc-grep regressions:\n  " + "\n  ".join(failures)


def test_no_forbidden_regex_in_user_docs() -> None:
    failures: list[str] = []
    compiled = [(re.compile(pat), why) for pat, why in FORBIDDEN_REGEX]
    for path in DOC_FILES:
        if path.name in ALLOWLISTED_PATHS:
            continue
        for lineno, line in _iter_lines(path):
            for rx, why in compiled:
                if rx.search(line):
                    failures.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                        f"forbidden pattern /{rx.pattern}/ ({why}): {line.strip()!r}"
                    )
    assert not failures, "Doc-grep regressions:\n  " + "\n  ".join(failures)


# CR #1 finding 1: the migration guide invented `beetroot destroy --no-rm`,
# `--no-data`, `--no-deregister` — flags that never existed. The
# allowlisted-path exemption keeps the other regex/literal tests
# permissive for the migration guide's past-tense discussion of removed
# verbs, but invented future-flags must NEVER appear there either.
_INVENTED_DESTROY_FLAGS: tuple[str, ...] = (
    "destroy --no-rm",
    "destroy --no-data",
    "destroy --no-deregister",
)


def test_migration_guide_does_not_invent_destroy_flags() -> None:
    guide = REPO_ROOT / "docs" / "guides" / "migration-v0.2-to-v0.3.md"
    text = guide.read_text()
    failures = [flag for flag in _INVENTED_DESTROY_FLAGS if flag in text]
    assert not failures, (
        f"Migration guide cites flags that don't exist on `beetroot "
        f"destroy`: {failures}. The real cleanup verb for an orphan is "
        f"`beetroot destroy <name> -y`."
    )
