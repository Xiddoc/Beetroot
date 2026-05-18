"""Lint fenced bash examples inside CHANGELOG.md's ``## Unreleased`` block.

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


def _slurp(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _extract_unreleased_fences(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(1-based line number, line text)`` pairs inside Unreleased shell fences.

    Only lines inside shell-flavoured fenced code blocks (no language
    specifier, or ``bash`` / ``sh`` / ``shell`` / ``console``) inside the
    ``## Unreleased`` block are returned. yaml/python/etc. fences are
    tracked-but-skipped so we don't leak fence state into the matcher.
    Inline code spans (single-backtick) are ignored — they document
    shapes, not runnable invocations.
    """
    in_unreleased = False
    in_fence = False
    fence_is_shell = False
    out: list[tuple[int, str]] = []
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
        if in_fence and fence_is_shell:
            out.append((idx, line))
    return out


def _registered_verbs() -> set[str]:
    """Parse ``beetroot --help`` and return the set of registered verb names."""
    proc = subprocess.run(  # noqa: S603 — beetroot is a project-internal CLI
        ["uv", "run", "beetroot", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`beetroot --help` failed (exit {proc.returncode}): {proc.stderr}")
    verbs: set[str] = set()
    for raw in proc.stdout.splitlines():
        line = _strip_box_drawing(raw).strip()
        if not line:
            continue
        head = line.split(None, 1)[0]
        if head.isidentifier() and head.islower():
            verbs.add(head)
    typer_meta = {"options", "commands", "arguments", "usage"}
    return verbs - typer_meta


def _verb_long_flags(verb: str) -> set[str]:
    """Parse ``beetroot <verb> --help`` and return the set of long-flag names."""
    proc = subprocess.run(  # noqa: S603 — beetroot is a project-internal CLI
        ["uv", "run", "beetroot", verb, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`beetroot {verb} --help` failed (exit {proc.returncode}): {proc.stderr}",
        )
    flags: set[str] = set()
    for match in re.finditer(r"--[A-Za-z][A-Za-z0-9-]*", proc.stdout):
        flags.add(match.group(0))
    flags.update({"--help", "--install-completion", "--show-completion"})
    return flags


def _strip_box_drawing(line: str) -> str:
    """Strip Typer's Rich box-drawing characters so we can parse the inner text."""
    return re.sub(r"[│╭╮╯╰─╞╡┃┌┐└┘├┤┬┴┼━┏┓┗┛┣┫┳┻╋]", " ", line)


def _check_invocations(
    fence_lines: list[tuple[int, str]],
    known_verbs: set[str],
) -> list[str]:
    """Return a list of human-readable error strings (empty means all green)."""
    errors: list[str] = []
    flag_cache: dict[str, set[str]] = {}
    for line_no, raw in fence_lines:
        for match in BEETROOT_INVOCATION.finditer(raw):
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
    if not CHANGELOG.exists():
        print(f"error: {CHANGELOG} not found", file=sys.stderr)
        return 1
    if shutil.which("uv") is None:
        print("error: `uv` not on PATH — install uv to run the changelog lint", file=sys.stderr)
        return 1
    lines = _slurp(CHANGELOG)
    fence_lines = _extract_unreleased_fences(lines)
    if not fence_lines:
        return 0
    known_verbs = _registered_verbs()
    errors = _check_invocations(fence_lines, known_verbs)
    if errors:
        print("changelog-lint: invalid beetroot invocations under ## Unreleased:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
