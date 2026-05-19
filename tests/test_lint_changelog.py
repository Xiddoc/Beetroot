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


def test_empty_unreleased_returns_no_lines() -> None:
    raw = """\
## Unreleased

(intentionally empty)

## v0.3.0
"""
    fenced, inline = lint_changelog._extract_unreleased_lines(raw.splitlines())
    assert fenced == []
    assert inline == []
