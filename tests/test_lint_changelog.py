"""
Unit tests for ``scripts/lint_changelog.py``.

These tests don't shell out to ``uv run beetroot --help`` — they
exercise the pure-Python markdown / inline-code extraction by
calling the helpers directly with synthetic CHANGELOG content.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "lint_changelog.py"
_SPEC = importlib.util.spec_from_file_location("lint_changelog", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
lint_changelog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lint_changelog)


def test_inline_code_span_picks_up_beetroot_invocation_in_prose() -> None:
    raw = """\
## Unreleased

The new `beetroot doctor --foo` verb is gone.

## v0.3.0
"""
    fenced, inline = lint_changelog._extract_unreleased_lines(raw.splitlines())
    assert fenced == []
    # The inline-code span must include "beetroot doctor --foo".
    span_texts = [text for _, text in inline]
    assert any("beetroot doctor --foo" in span for span in span_texts), span_texts


def test_inline_code_span_inside_fence_is_skipped() -> None:
    raw = """\
## Unreleased

```python
# This `beetroot doctor` reference inside a python fence is NOT inline.
```

## v0.3.0
"""
    fenced, inline = lint_changelog._extract_unreleased_lines(raw.splitlines())
    # python fence is not shell-flavoured so fenced is empty.
    assert fenced == []
    # The "inline-looking" backticks inside the fence body must NOT
    # be captured as inline spans.
    span_texts = [text for _, text in inline]
    assert all("beetroot doctor" not in span for span in span_texts), span_texts


def test_shell_fence_lines_captured() -> None:
    raw = """\
## Unreleased

```bash
beetroot create alpha
beetroot up alpha
```

## v0.3.0
"""
    fenced, _inline = lint_changelog._extract_unreleased_lines(raw.splitlines())
    fenced_text = [text for _, text in fenced]
    assert "beetroot create alpha" in fenced_text
    assert "beetroot up alpha" in fenced_text


def test_check_invocations_rejects_unknown_verb() -> None:
    sources = [(5, "beetroot doctor --foo")]
    known = {"create", "up", "down"}
    errors = lint_changelog._check_invocations(
        sources, known, flag_cache={"create": {"--help"}},
    )
    assert len(errors) == 1
    assert "unknown beetroot verb" in errors[0]
    assert "doctor" in errors[0]


def test_check_invocations_accepts_known_verb_and_flag() -> None:
    sources = [(5, "beetroot create alpha --path /tmp/alpha")]
    known = {"create"}
    errors = lint_changelog._check_invocations(
        sources, known, flag_cache={"create": {"--help", "--path"}},
    )
    assert errors == []


def test_check_invocations_rejects_invented_inline_flag() -> None:
    """The bug class T3 added inline-span scanning for.

    A prose paragraph cites `--foo` inline (after the flag was deferred
    from the release); the linter must reject it.
    """
    sources = [(5, "beetroot create --foo")]
    known = {"create"}
    errors = lint_changelog._check_invocations(
        sources, known, flag_cache={"create": {"--help", "--path"}},
    )
    assert len(errors) == 1
    assert "unknown flag '--foo'" in errors[0]


def test_python_import_in_inline_span_is_not_a_cli_invocation() -> None:
    """Regression: ``from beetroot import X`` is Python, not a CLI verb.

    T3's expanded inline-code scanner false-positived on T2's CHANGELOG
    block where the rename announcement said ``from beetroot import
    frida_dl`` — the scanner parsed it as ``beetroot import`` =
    "unknown verb 'import'". The fix skips any inline span whose
    stripped content begins with ``from `` or ``import ``.
    """
    sources = [
        (5, "from beetroot import frida_dl"),
        (6, "from beetroot import frida_download"),
        (7, "import beetroot"),
        (8, "import beetroot.api"),
    ]
    known = {"create", "up", "down"}
    errors = lint_changelog._check_invocations(
        sources, known, flag_cache={"create": {"--help"}},
    )
    assert errors == []


def test_python_import_skip_does_not_swallow_real_invocation_in_same_paragraph() -> None:
    """A real ``beetroot <verb>`` later in prose is still caught.

    The skip is per-inline-span (each span is its own ``(line_no,
    text)`` source), so a paragraph containing both an import line and
    a CLI invocation gets the invocation scanned normally.
    """
    sources = [
        (5, "from beetroot import frida_download"),  # skipped
        (5, "beetroot doctor --foo"),                 # scanned
    ]
    known = {"create"}
    errors = lint_changelog._check_invocations(
        sources, known, flag_cache={"create": {"--help"}},
    )
    assert len(errors) == 1
    assert "unknown beetroot verb 'doctor'" in errors[0]


_PLAIN_HELP = """\
 Usage: beetroot [OPTIONS] COMMAND [ARGS]...

 Beetroot — multi-instance Magisk-Android research lab CLI.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help                        Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ create    Create a new instance directory and stage its files.               │
│ module    Install a Magisk module — append + re-stage (redroid), push (adb), │
│           or root --auto-install.                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
"""

# The same help as Rich renders it under GitHub Actions (terminal mode
# forced by the GITHUB_ACTIONS env var): verb names wrapped in SGR
# escapes, box-drawing characters dimmed.
_ANSI_HELP = _PLAIN_HELP.replace(
    "│ create ", "\x1b[2m│\x1b[0m \x1b[1;36mcreate\x1b[0m ",
).replace(
    "│ module ", "\x1b[2m│\x1b[0m \x1b[1;36mmodule\x1b[0m ",
).replace(
    "--auto-install", "\x1b[1;36m-\x1b[0m\x1b[1;36m-auto\x1b[0m\x1b[1;36m-install\x1b[0m",
)


def test_parse_verbs_plain_help_finds_verbs_and_drops_typer_meta() -> None:
    verbs = lint_changelog._parse_verbs(_PLAIN_HELP)
    assert "create" in verbs
    assert "module" in verbs
    # Section headings and usage boilerplate must not register as verbs.
    assert {"options", "commands", "usage"}.isdisjoint(verbs)


def test_parse_verbs_strips_ansi_escapes_from_github_actions_help() -> None:
    """Regression: PR #37 CI failure ("unknown beetroot verb 'module'").

    Rich force-enables terminal mode under GitHub Actions, so
    ``beetroot --help`` arrives wrapped in ANSI SGR escapes there. The
    first token of each command row was ``\\x1b[1;36mmodule`` — not an
    identifier — so the parsed verb set came back EMPTY in CI (while
    passing locally), and every CHANGELOG invocation was reported as an
    unknown verb. The parser must yield the same set either way.
    """
    assert lint_changelog._parse_verbs(_ANSI_HELP) == lint_changelog._parse_verbs(_PLAIN_HELP)
    assert "module" in lint_changelog._parse_verbs(_ANSI_HELP)


def test_parse_verbs_does_not_invent_verbs_absent_from_help() -> None:
    """ANSI stripping must not loosen the gate: a verb the CLI does not
    register is still unknown, colour codes or not."""
    assert "doctor" not in lint_changelog._parse_verbs(_ANSI_HELP)
    assert "doctor" not in lint_changelog._parse_verbs(_PLAIN_HELP)


def test_parse_long_flags_reassembles_ansi_split_flag() -> None:
    """Rich emits ``--auto-install`` as ``-``/``-auto``/``-install``
    fragments with escapes in between; stripping must reassemble it."""
    flags = lint_changelog._parse_long_flags(_ANSI_HELP)
    assert "--auto-install" in flags
    assert flags == lint_changelog._parse_long_flags(_PLAIN_HELP)


def test_parse_long_flags_does_not_invent_flags() -> None:
    assert "--no-such-flag" not in lint_changelog._parse_long_flags(_ANSI_HELP)


def test_empty_unreleased_returns_no_lines() -> None:
    raw = """\
## Unreleased

(intentionally empty)

## v0.3.0
"""
    fenced, inline = lint_changelog._extract_unreleased_lines(raw.splitlines())
    assert fenced == []
    assert inline == []
