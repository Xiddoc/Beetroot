"""Tests guarding README.md against drift from the actual CLI verb set."""

from __future__ import annotations

import re
from pathlib import Path

from beetroot import cli

README = Path(__file__).resolve().parents[1] / "README.md"

# Verbs that were once advertised in the README but never (or no longer)
# existed as CLI commands. Keep this list in sync with the README; the test
# both asserts that none of these are real verbs *and* that the README does
# not introduce them as plain English words that look like verb names.
GHOST_VERBS = ("attach", "list")


def _registered_verbs() -> set[str]:
    """Return every verb name registered on the Typer app."""
    out: set[str] = set()
    for command in cli.app.registered_commands:
        # Typer's CommandInfo.name is None when no explicit name was set; in
        # that case the verb name comes from the function's own __name__.
        name = command.name or (command.callback.__name__ if command.callback else None)
        assert name is not None, f"unnamed Typer command: {command!r}"
        out.add(name)
    return out


def test_ghost_verbs_are_not_registered() -> None:
    registered = _registered_verbs()
    for verb in GHOST_VERBS:
        assert verb not in registered, (
            f"Ghost verb {verb!r} is now actually registered — update GHOST_VERBS in this test."
        )


def test_readme_does_not_advertise_ghost_verbs() -> None:
    text = README.read_text()
    tokens = set(re.findall(r"[A-Za-z]+", text.lower()))
    for verb in GHOST_VERBS:
        assert verb not in tokens, f"README mentions ghost verb {verb!r}; remove it or rephrase."


def test_readme_backticked_verbs_are_registered() -> None:
    text = README.read_text()
    registered = _registered_verbs()
    backticked = set(re.findall(r"`([a-z]+)`", text))
    candidates = backticked & (registered | set(GHOST_VERBS))
    for token in candidates:
        assert token in registered, (
            f"README has `{token}` backticked as a CLI verb, but it is not registered."
        )
