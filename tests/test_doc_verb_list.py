"""Pin the CLI reference's verb list against the live Typer app.

A future verb add/rename should land in the same PR as its `## <verb>`
heading in `docs/reference/cli.md` — these tests fail in both directions
to catch one-sided drift.
"""
from __future__ import annotations

import re
from pathlib import Path

from beetroot import cli

CLI_REF = Path(__file__).resolve().parents[1] / "docs" / "reference" / "cli.md"

# Verb names may contain hyphens (e.g. ``frida-addr``), so allow them in the
# heading capture — not just ``[a-z]+``.
HEADING_RX = re.compile(r"^##\s+`([a-z][a-z-]*)`\s*$", re.MULTILINE)


def _registered_verbs() -> set[str]:
    """Return every non-hidden verb name registered on the Typer app.

    Hidden commands (``hidden=True``) are migration shims for renamed/removed
    verbs — they intentionally don't appear in ``--help`` or in the reference
    docs, so they should not be required to have a doc section.
    """
    out: set[str] = set()
    for command in cli.app.registered_commands:
        if command.hidden:
            continue
        name = command.name or (command.callback.__name__ if command.callback else None)
        assert name is not None, f"unnamed Typer command: {command!r}"
        out.add(name)
    return out


def _documented_verbs() -> set[str]:
    """Return every verb that has a ``## `<verb>` `` heading in cli.md."""
    return set(HEADING_RX.findall(CLI_REF.read_text()))


def test_every_registered_verb_has_a_doc_heading() -> None:
    registered = _registered_verbs()
    documented = _documented_verbs()
    missing = sorted(registered - documented)
    assert not missing, (
        f"Registered verbs without a `## <verb>` section in {CLI_REF}: {missing}"
    )


def test_every_documented_verb_is_registered() -> None:
    registered = _registered_verbs()
    documented = _documented_verbs()
    stale = sorted(documented - registered)
    assert not stale, (
        f"`## <verb>` sections in {CLI_REF} without a registered command: {stale}"
    )
