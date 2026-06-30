"""
Lint fenced bash examples inside CHANGELOG.md's ``## Unreleased`` block.

Why this exists
---------------
The v0.3 CR aggregation pass turned up a CHANGELOG entry under
``## Unreleased`` that cited ``beetroot create alpha --preset
with-frida`` — but the ``--preset`` flag had been removed earlier in
the same Unreleased block (theme T3). The bad example sailed through
ten per-theme code reviews because the failure mode ("the example
prints the wrong thing when a user runs it") is invisible without
actually parsing the fence and probing the CLI.

This linter closes that gap. It does **not** execute the cited
commands (we do not want side effects, network, or docker calls
during pre-commit); it only validates that:

1. Every ``beetroot <verb>`` invocation cited in an Unreleased
   fenced block names a verb registered in ``beetroot --help``.
2. Every ``--long-flag`` cited next to that verb is named in
   ``beetroot <verb> --help`` (short flags and positional args
   are intentionally skipped — they are too easy to false-positive
   on documentation conventions like ``-- <args>``).

If either check fails, exit 1 with a line-number-anchored error
pointing the reviewer at the offending fence. Otherwise exit 0.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

UNRELEASED_HEADING = re.compile(r"^## Unreleased\s*$")
NEXT_TOP_HEADING = re.compile(r"^## (?!Unreleased\b)")
FENCE_DELIMITER = re.compile(r"^```(?P<lang>[A-Za-z0-9_+-]*)\s*$")
SHELL_LANGS = frozenset({"", "bash", "sh", "shell", "console"})
BEETROOT_INVOCATION = re.compile(r"\bbeetroot\s+(?P<rest>[^\n#`]+)")
# Inline-code spans inside prose paragraphs, e.g. the prose sentence
# ``the new ``beetroot doctor --foo`` verb …``. v0.4 (T3) extends
# the scanner to flag invented flags / verbs cited in inline spans
# too — the v0.3 CR-CR aggregation found one such drift case
# (``--auto-install`` cited inline in a paragraph that had been
# revised after the flag was deferred).
INLINE_CODE_SPAN = re.compile(r"`([^`\n]+)`")
# Python-import statements that mention ``beetroot`` are NOT CLI
# invocations even though the regex above would otherwise treat the
# token after ``beetroot`` as a verb. The CHANGELOG often quotes
# imports like ``from beetroot import frida_download`` in rename
# entries, which would false-positive as ``beetroot import`` =
# "unknown verb 'import'". Skip any inline-code span whose stripped
# content begins with ``from `` or ``import ``.
_PYTHON_IMPORT_PREFIXES = ("from ", "import ")

# Shell command separators. A single CHANGELOG line can chain several
# ``beetroot`` invocations with ``&&``/``||``/``;``/``|`` (or an embedded
# newline). ``BEETROOT_INVOCATION``'s ``[^\n#`]+`` class greedily spans those
# separators to end-of-line, so without splitting first ``finditer`` yields one
# match per line — a bad *second* verb (or a flag mis-attributed across the
# separator) sails through (#212). Split each line on these separators and run
# the matcher per segment so every invocation is validated independently.
_SHELL_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def _slurp(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _extract_unreleased_lines(
    lines: list[str],
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """
    Return shell-fence lines and inline-code spans inside ``## Unreleased``.

    The first list is shell-fenced lines (run as a shell command would
    interpret them); the second is the contents of every inline-code
    span (single-backtick) inside prose paragraphs of the same
    section. Both lists are sources of ``beetroot <verb> [--flag]``
    invocations the linter checks; the v0.3 retro showed that
    inline-code references can also drift (an invented flag cited in
    a prose paragraph after the flag was deferred from the release).

    Args:
        lines: The full CHANGELOG content split by ``splitlines()``.

    Returns:
        A two-tuple ``(fenced_lines, inline_spans)``. Each element is
        a ``(1-based line number, text)`` pair.
    """
    in_unreleased = False
    in_fence = False
    fence_is_shell = False
    fenced: list[tuple[int, str]] = []
    inline: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        if UNRELEASED_HEADING.match(line):
            in_unreleased = True
            continue
        if in_unreleased and NEXT_TOP_HEADING.match(line):
            break
        if not in_unreleased:
            continue
        delim = FENCE_DELIMITER.match(line)
        if delim:
            if in_fence:
                in_fence = False
                fence_is_shell = False
            else:
                in_fence = True
                fence_is_shell = delim.group("lang") in SHELL_LANGS
            continue
        if in_fence:
            if fence_is_shell:
                fenced.append((idx, line))
            # Inside any fence (shell or not) the prose-inline scanner
            # is off — we don't want to interpret python-fence body
            # text as a markdown inline-code span.
            continue
        # Prose paragraph. Pick up every inline-code span on this line.
        inline.extend((idx, match.group(1)) for match in INLINE_CODE_SPAN.finditer(line))
    return fenced, inline


def _registered_verbs() -> set[str]:
    """Parse ``beetroot --help`` and return the set of registered verb names."""
    proc = subprocess.run(
        ["uv", "run", "beetroot", "--help"],  # noqa: S607  # uv resolved via PATH; argv hard-coded; beetroot is a project-internal CLI
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`beetroot --help` failed (exit {proc.returncode}): {proc.stderr}")
    return _parse_verbs(proc.stdout)


# A registered verb name: lowercase, may contain hyphens (e.g. ``frida-addr``).
# ``str.isidentifier()`` rejects hyphens, so a hyphenated verb would otherwise be
# silently dropped from the known-verb set and every reference to it in the
# CHANGELOG flagged as "unknown verb".
_VERB_NAME = re.compile(r"[a-z][a-z0-9-]*$")


def _parse_verbs(help_text: str) -> set[str]:
    """Extract registered verb names from ``beetroot --help`` output."""
    verbs: set[str] = set()
    for raw in _strip_ansi(help_text).splitlines():
        line = _strip_box_drawing(raw).strip()
        if not line:
            continue
        head = line.split(None, 1)[0]
        if _VERB_NAME.fullmatch(head):
            verbs.add(head)
    typer_meta = {"options", "commands", "arguments", "usage"}
    return verbs - typer_meta


def _verb_long_flags(verb: str) -> set[str]:
    """Parse ``beetroot <verb> --help`` and return the set of long-flag names."""
    proc = subprocess.run(  # noqa: S603  # ``verb`` is a verb name parsed from beetroot's own --help; not user input
        ["uv", "run", "beetroot", verb, "--help"],  # noqa: S607  # uv resolved via PATH; beetroot is a project-internal CLI
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`beetroot {verb} --help` failed (exit {proc.returncode}): {proc.stderr}",
        )
    return _parse_long_flags(proc.stdout)


def _parse_long_flags(help_text: str) -> set[str]:
    """Extract long-flag names from ``beetroot <verb> --help`` output."""
    flags: set[str] = set()
    for match in re.finditer(r"--[A-Za-z][A-Za-z0-9-]*", _strip_ansi(help_text)):
        flags.add(match.group(0))
    flags.update({"--help", "--install-completion", "--show-completion"})
    return flags


def _strip_box_drawing(line: str) -> str:
    """Strip Typer's Rich box-drawing characters so we can parse the inner text."""
    return re.sub(r"[│╭╮╯╰─╞╡┃┌┐└┘├┤┬┴┼━┏┓┗┛┣┫┳┻╋]", " ", line)


# Rich force-enables terminal mode under GitHub Actions (it detects the
# GITHUB_ACTIONS env var so CI logs get colour), so in CI ``beetroot
# --help`` arrives wrapped in ANSI SGR escapes: the verb column reads
# ``\x1b[1;36mmodule\x1b[0m``, the first-token ``isidentifier()`` check
# rejects every row, the known-verb set comes back empty, and every
# CHANGELOG invocation is reported as an unknown verb. Flags split the
# same way (``--auto-install`` is emitted as ``-``/``-auto``/``-install``
# fragments with escapes in between). Strip every escape sequence before
# parsing so the linter sees the same text a human does.
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences (colour/style codes) from ``text``."""
    return _ANSI_ESCAPE.sub("", text)


def _check_invocations(
    sources: list[tuple[int, str]],
    known_verbs: set[str],
    *,
    flag_cache: dict[str, set[str]] | None = None,
) -> list[str]:
    """
    Return a list of human-readable error strings (empty means all green).

    ``sources`` is a list of ``(line_no, text)`` pairs from either
    fenced shell blocks or inline-code spans inside prose. The same
    matcher works against both because every ``beetroot <verb>
    [--flag]`` invocation looks the same regardless of where it
    appears.
    """
    errors: list[str] = []
    flag_cache = flag_cache if flag_cache is not None else {}
    for line_no, raw in sources:
        if raw.lstrip().startswith(_PYTHON_IMPORT_PREFIXES):
            # Python-import statement quoted in prose, not a CLI
            # invocation — skip the whole span (see comment above).
            continue
        # Split on shell separators FIRST, then run the matcher per segment,
        # so a second ``beetroot <verb>`` after a ``&&``/``;``/``|`` is checked
        # too and a post-separator flag never leaks into the prior verb's token
        # list (#212). Each segment keeps the line's number for anchoring.
        for segment in _SHELL_SEPARATORS.split(raw):
            for match in BEETROOT_INVOCATION.finditer(segment):
                rest = match.group("rest").strip()
                tokens = rest.split()
                if not tokens:
                    continue
                verb = tokens[0]
                if verb.startswith("-"):
                    continue
                if verb not in known_verbs:
                    errors.append(
                        f"CHANGELOG.md:{line_no}: unknown beetroot verb {verb!r} "
                        f"(line: {raw.strip()!r})",
                    )
                    continue
                if verb not in flag_cache:
                    flag_cache[verb] = _verb_long_flags(verb)
                allowed = flag_cache[verb]
                for tok in tokens[1:]:
                    if tok == "--":
                        break
                    if not tok.startswith("--"):
                        continue
                    bare = tok.split("=", 1)[0]
                    if bare not in allowed:
                        errors.append(
                            f"CHANGELOG.md:{line_no}: unknown flag {bare!r} for verb "
                            f"{verb!r} (line: {raw.strip()!r})",
                        )
    return errors


def main() -> int:
    """Entry point: lint the CHANGELOG and exit 0 / 1 accordingly."""
    if not CHANGELOG.exists():
        print(f"error: {CHANGELOG} not found", file=sys.stderr)
        return 1
    if shutil.which("uv") is None:
        print("error: `uv` not on PATH — install uv to run the changelog lint", file=sys.stderr)
        return 1
    lines = _slurp(CHANGELOG)
    fence_lines, inline_spans = _extract_unreleased_lines(lines)
    if not fence_lines and not inline_spans:
        return 0
    known_verbs = _registered_verbs()
    # Share the flag cache across the two passes so each verb's
    # ``--help`` is invoked at most once per linter run.
    flag_cache: dict[str, set[str]] = {}
    errors = _check_invocations(fence_lines, known_verbs, flag_cache=flag_cache)
    errors.extend(_check_invocations(inline_spans, known_verbs, flag_cache=flag_cache))
    if errors:
        print("changelog-lint: invalid beetroot invocations under ## Unreleased:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
