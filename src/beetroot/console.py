"""
Shared rich-based output layer for Beetroot's human-facing terminal messages.

All styled output (errors, warnings, info, success, tables, progress) goes
through this module. The two ``Console`` instances — one on stdout for primary
results, one on stderr for status and ephemeral progress — are module-level
singletons initialised at import time. Tests inject replacements via
:func:`set_consoles`.

**Machine-readable output (JSON) must NOT go through rich.** Callers that emit
JSON print plain text to stdout directly; rich markup would corrupt the stream
and break downstream parsers.

TTY degradation is handled automatically by rich: when the underlying file is
not a TTY (e.g. a pipe), ``Console`` strips ANSI color codes. Stripping color
is *all* it does by default — box-drawing borders are plain UTF-8 (not ANSI)
and rich still clips wide table cells to its 80-column default width — so
piped output is not automatically machine-parseable. Callers that need a stable,
parseable stream should use a verb's ``--json`` mode, not scrape the human
table. The :func:`table` helper does render losslessly off-TTY (no borders, no
cell truncation), but that is a courtesy for log readability, not a contract.
Progress bars still render a single summary line in non-TTY mode — they do not
spam the log with carriage-return sequences because rich's ``Progress``/
``Console`` detects a non-interactive (non-TTY) console and disables the
``Live`` refresh loop, emitting plain line output instead.
"""

from __future__ import annotations

import contextlib
import types
from collections.abc import Generator, Sequence
from typing import IO

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

# ---------------------------------------------------------------------------
# Module-level console singletons.  Tests replace these via set_consoles().
#
# Both consoles are constructed *without* an explicit ``file=`` so rich
# resolves ``sys.stdout`` / ``sys.stderr`` lazily on every write. That makes
# the helpers track the *current* streams — Typer's ``CliRunner`` (tests) and
# ``capsys`` both swap ``sys.stdout``/``sys.stderr`` per invocation, so binding
# the file at import time would send all output to the wrong place. The
# ``stderr=True`` flag tells rich to resolve ``sys.stderr`` instead of stdout.
# ---------------------------------------------------------------------------

_stdout_console: Console = Console(highlight=False)
_stderr_console: Console = Console(stderr=True, highlight=False)

# Styled brand tag rendered at the head of every primary status line. The
# leading backslash escapes the ``[`` so rich emits a literal ``[beetroot]``
# (off-TTY the markup is stripped, leaving the plain ``[beetroot]`` prefix
# that scripts and the test-suite already match on).
_BRAND = r"\[beetroot]"


def set_consoles(
    stdout: Console | None = None,
    stderr: Console | None = None,
) -> None:
    """
    Replace the module-level consoles.  Intended for tests only.

    Passing ``None`` for either argument leaves that console unchanged.

    Example::

        import io
        from rich.console import Console
        from beetroot import console

        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=False)
        console.set_consoles(stderr=test_console)
        console.error("boom")
        assert "error: boom" in buf.getvalue()

    Args:
        stdout: Replacement for the primary (stdout) console, or ``None``
            to leave it unchanged.
        stderr: Replacement for the status/error (stderr) console, or
            ``None`` to leave it unchanged.
    """
    global _stdout_console, _stderr_console  # noqa: PLW0603  # intentional module-level mutation for test injection
    if stdout is not None:
        _stdout_console = stdout
    if stderr is not None:
        _stderr_console = stderr


# ---------------------------------------------------------------------------
# Styled helpers — all output goes to stderr so they never pollute pipes.
# ---------------------------------------------------------------------------


def _emit(console: Console, markup: str) -> None:
    """
    Print one fully-formed markup line with wrapping disabled.

    ``soft_wrap=True`` stops rich from hard-wrapping a long line at the
    console width (80 columns when the stream is not a TTY, which would inject
    spurious newlines into a status line and break both shell pipelines and
    substring assertions). The terminal itself still soft-wraps on display.
    """
    console.print(markup, soft_wrap=True)


def error(msg: str) -> None:
    """
    Print a styled ``error: <msg>`` line to stderr.

    Matches the ``error: ...`` convention already used throughout ``cli.py``.
    Rich strips ANSI codes automatically when stderr is not a TTY. The message
    is markup-escaped so a stray ``[`` in an exception string can never be
    misparsed as a rich tag (which would crash the error path itself).

    Args:
        msg: The error message text (no trailing newline needed).
    """
    _emit(_stderr_console, f"[bold red]error:[/bold red] {escape(msg)}")


def warn(msg: str) -> None:
    """
    Print a styled ``warn: <msg>`` line to stderr.

    Args:
        msg: The warning message text.
    """
    _emit(_stderr_console, f"[bold yellow]warn:[/bold yellow] {escape(msg)}")


def info(msg: str) -> None:
    """
    Print a styled ``info: <msg>`` line to stderr.

    Args:
        msg: The informational message text.
    """
    _emit(_stderr_console, f"[bold cyan]info:[/bold cyan] {escape(msg)}")


def success(msg: str) -> None:
    """
    Print a styled ``ok: <msg>`` line to stderr.

    Args:
        msg: The success message text.
    """
    _emit(_stderr_console, f"[bold green]ok:[/bold green] {escape(msg)}")


# ---------------------------------------------------------------------------
# Branded helpers — the primary CLI voice.  ``status`` / ``step`` / ``hint``
# render to stdout (the command's readable output); ``note`` renders to stderr
# (out-of-band advisories).  All keep the ``[beetroot]`` brand so existing
# scripts and tests that match on it keep working.
# ---------------------------------------------------------------------------


def status(msg: str) -> None:
    """
    Print a branded ``[beetroot] <msg>`` outcome line to stdout.

    The primary CLI voice for the result of a verb (created, started,
    destroyed, …). The brand is tinted cyan on a TTY; off-TTY rich strips the
    markup, leaving the plain ``[beetroot] <msg>`` line.

    Args:
        msg: The status message text.
    """
    _emit(_stdout_console, f"[bold cyan]{_BRAND}[/bold cyan] {escape(msg)}")


def note(msg: str) -> None:
    """
    Print a branded ``[beetroot] <msg>`` advisory line to stderr.

    Used for out-of-band advisories (binder/VM banners, best-effort cleanup
    warnings) that must not pollute a piped stdout. The brand is tinted yellow
    on a TTY.

    Args:
        msg: The advisory message text.
    """
    _emit(_stderr_console, f"[bold yellow]{_BRAND}[/bold yellow] {escape(msg)}")


def step(msg: str) -> None:
    """
    Print a dimmed ``→ <msg>`` narration line to stdout.

    The verbose "what I'm about to do" voice that precedes a slow action so
    the user sees forward motion. Dimmed so it recedes behind the eventual
    :func:`status` outcome.

    Args:
        msg: The narration text.
    """
    _emit(_stdout_console, f"[dim]→ {escape(msg)}[/dim]")


def hint(msg: str) -> None:
    """
    Print a dimmed next-step suggestion line to stdout.

    Used for the "next: …" follow-up suggestions after a verb completes.

    Args:
        msg: The suggestion text (e.g. ``next: beetroot up alpha``).
    """
    _emit(_stdout_console, f"[dim]{escape(msg)}[/dim]")


def out(msg: str, *, style: str = "") -> None:
    """
    Print a plain (un-branded) line to stdout, optionally styled.

    For machine-parseable result lines (e.g. ``beetroot doctor``'s
    ``<check>: <status>`` rows) that want a TTY tint but must stay verbatim
    when piped — rich strips the style off-TTY, leaving the exact text.

    Args:
        msg: The line text.
        style: An optional rich style (e.g. ``"green"``, ``"red"``) applied to
            the whole line; empty for the terminal's default colour.
    """
    text = escape(msg)
    _emit(_stdout_console, f"[{style}]{text}[/{style}]" if style else text)


# ---------------------------------------------------------------------------
# Table helper — renders to stdout so it forms part of the primary result.
# ---------------------------------------------------------------------------


def table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """
    Render a rich ``Table`` with the given column headers and rows to stdout.

    Primary results (like ``beetroot ls`` or ``beetroot modes``) go to stdout
    via the stdout console. On a TTY the table is fully decorated (borders,
    color). Off a TTY, rich's defaults would clip wide cells to 80 columns and
    still draw UTF-8 box borders, so a piped ``ls`` could silently truncate an
    ADB endpoint or an instance path. To keep piped output lossless this branch
    drops the borders, folds (never truncates) cells, and prints at a width wide
    enough for the longest line — so every cell survives verbatim. This is a
    readability courtesy, not a parsing contract: machine consumers should use a
    verb's ``--json`` mode.

    Args:
        columns: Sequence of column header strings.
        rows: Sequence of rows; each row is a sequence of cell strings whose
            length must match ``columns``.
    """
    if _stdout_console.is_terminal:
        t = Table(*columns)
        for row in rows:
            t.add_row(*row)
        _stdout_console.print(t)
        return

    t = Table(box=None, pad_edge=False)
    for col in columns:
        t.add_column(col, overflow="fold", no_wrap=False)
    for row in rows:
        t.add_row(*row)
    # Width the render to the longest content line so rich never falls back to
    # its 80-column default and clips a cell with an ellipsis.
    max_cell = max(
        (len(cell) for line in (columns, *rows) for cell in line),
        default=0,
    )
    width = max(max_cell * len(columns) + len(columns), 80)
    # A ``print(width=…)`` request is clamped to the Console's own width (80 on
    # a non-TTY), which folds a wide load-bearing cell across lines instead of
    # honouring the requested width. Set the width on the console for the render
    # (restored after) so every cell stays on one line, lossless (#204).
    saved_width = _stdout_console.width
    try:
        _stdout_console.width = width
        _stdout_console.print(t, crop=False)
    finally:
        _stdout_console.width = saved_width


# ---------------------------------------------------------------------------
# Progress context manager — renders to stderr; no-op friendly on non-TTY.
# ---------------------------------------------------------------------------


class ProgressContext:
    """
    Thin wrapper around ``rich.progress.Progress`` for downloads and long ops.

    Renders to stderr so progress bars never pollute captured stdout.  Rich
    degrades gracefully on non-TTY streams: it omits carriage-return rewrites
    and renders a single summary line per task completion instead of an
    animated bar, so CI logs stay readable.

    Use via :func:`progress` rather than constructing directly.

    Pass ``total=None`` for indeterminate ("pulse") mode when the total work
    units are not known in advance (e.g. a download with no ``Content-Length``
    header).  In that mode the percentage and time-remaining columns are
    omitted because they are meaningless without a known total.

    Example::

        with console.progress("Downloading frida-server", total=1024) as p:
            p.advance(512)
            p.advance(512)

        with console.progress("Downloading frida-server", total=None) as p:
            p.advance(512)   # just pulses; no percentage shown
    """

    def __init__(self, description: str, total: float | None = None) -> None:
        """
        Initialise the context with a task description and total work units.

        Pass ``total=None`` for an indeterminate (pulse) bar when the total is
        not known.  In that case the percentage and time-remaining columns are
        omitted because they carry no meaning.

        Args:
            description: Short label displayed beside the progress bar.
            total: Total number of work units (passed to ``Progress.add_task``),
                or ``None`` for indeterminate / pulse mode.
        """
        self._description = description
        self._total = total
        columns: list[ProgressColumn] = [
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
        ]
        if total is not None:
            columns += [
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
            ]
        self._progress = Progress(*columns, console=_stderr_console)
        self._task_id: TaskID | None = None

    def __enter__(self) -> ProgressContext:
        """
        Start the progress display and register the initial task.
        """
        self._progress.__enter__()
        self._task_id = self._progress.add_task(self._description, total=self._total)
        return self

    def advance(self, amount: float = 1.0) -> None:
        """
        Advance the progress bar by ``amount`` work units.

        Args:
            amount: Number of work units completed since the last call.
                Defaults to ``1.0``.
        """
        if self._task_id is not None:
            self._progress.advance(self._task_id, amount)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """
        Stop the progress display.
        """
        self._progress.__exit__(exc_type, exc_val, exc_tb)


@contextlib.contextmanager
def progress(description: str, *, total: float | None = None) -> Generator[ProgressContext]:
    """
    Context manager wrapping ``rich.progress.Progress`` for long operations.

    Renders to stderr so progress bars never pollute stdout pipes.  Rich
    degrades gracefully on non-TTY streams (no carriage-return spam).

    Pass ``total=None`` (the default) for indeterminate ("pulse") mode when the
    total is not known in advance — e.g. a download with no ``Content-Length``
    header.  Pass a positive float when the total is known so percentage and
    time-remaining columns are shown.

    Example::

        with console.progress("Fetching release", total=file_size) as p:
            for chunk in stream:
                process(chunk)
                p.advance(len(chunk))

        with console.progress("Fetching release") as p:   # indeterminate
            for chunk in stream:
                process(chunk)
                p.advance(len(chunk))

    Args:
        description: Short label displayed next to the progress bar.
        total: Total work units (sets the 100% mark), or ``None`` (default)
            for indeterminate pulse mode.

    Yields:
        A :class:`ProgressContext` with an :meth:`~ProgressContext.advance`
        method.
    """
    ctx = ProgressContext(description, total)
    with ctx:
        yield ctx


# ---------------------------------------------------------------------------
# stdout_console / stderr_console accessors for callers that need raw Console.
# ---------------------------------------------------------------------------


def stdout_console() -> Console:
    """
    Return the shared stdout ``Console`` for direct rich rendering.

    Prefer the typed helpers (:func:`table`, etc.) where they fit.  This
    accessor exists for callers that need features not wrapped here (e.g.
    ``Syntax`` highlighting, ``Markdown`` blocks).

    Returns:
        The module-level stdout ``Console`` instance.
    """
    return _stdout_console


def stderr_console() -> Console:
    """
    Return the shared stderr ``Console`` for direct rich rendering.

    Prefer the typed helpers (:func:`error`, :func:`warn`, :func:`info`,
    :func:`success`, :func:`progress`) where they fit.

    Returns:
        The module-level stderr ``Console`` instance.
    """
    return _stderr_console


# ---------------------------------------------------------------------------
# IO shims — expose the underlying file objects for callers that need them.
# ---------------------------------------------------------------------------


def stdout_file() -> IO[str]:
    """
    Return the file object backing the stdout console.

    Intended for the rare caller that must write raw text (e.g. plain JSON)
    to the same file descriptor as rich's stdout console, so file-descriptor
    ordering is preserved. Machine-readable output (JSON) must NOT go through
    rich — use this or ``sys.stdout`` directly.

    Returns:
        The ``IO[str]`` file backing ``_stdout_console``.
    """
    return _stdout_console.file
