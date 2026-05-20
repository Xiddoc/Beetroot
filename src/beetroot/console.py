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
not a TTY (e.g. a pipe), ``Console`` strips ANSI codes and renders plain text
so log files and CI output remain readable. Progress bars still render a single
summary line in non-TTY mode — they do not spam the log with carriage-return
sequences because rich uses a ``RichHandler``-style line-based renderer when it
detects a non-interactive stream.
"""
from __future__ import annotations

import contextlib
import sys
import types
from collections.abc import Generator, Sequence
from typing import IO

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeRemainingColumn
from rich.table import Table

# ---------------------------------------------------------------------------
# Module-level console singletons.  Tests replace these via set_consoles().
# ---------------------------------------------------------------------------

_stdout_console: Console = Console(
    file=sys.stdout,
    highlight=False,
)
_stderr_console: Console = Console(
    file=sys.stderr,
    highlight=False,
)


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


def error(msg: str) -> None:
    """
    Print a styled ``error: <msg>`` line to stderr.

    Matches the ``error: ...`` convention already used throughout ``cli.py``.
    Rich strips ANSI codes automatically when stderr is not a TTY.

    Args:
        msg: The error message text (no trailing newline needed).
    """
    _stderr_console.print(f"[bold red]error:[/bold red] {msg}")


def warn(msg: str) -> None:
    """
    Print a styled ``warn: <msg>`` line to stderr.

    Args:
        msg: The warning message text.
    """
    _stderr_console.print(f"[bold yellow]warn:[/bold yellow] {msg}")


def info(msg: str) -> None:
    """
    Print a styled ``info: <msg>`` line to stderr.

    Args:
        msg: The informational message text.
    """
    _stderr_console.print(f"[bold cyan]info:[/bold cyan] {msg}")


def success(msg: str) -> None:
    """
    Print a styled ``ok: <msg>`` line to stderr.

    Args:
        msg: The success message text.
    """
    _stderr_console.print(f"[bold green]ok:[/bold green] {msg}")


# ---------------------------------------------------------------------------
# Table helper — renders to stdout so it forms part of the primary result.
# ---------------------------------------------------------------------------


def table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """
    Render a rich ``Table`` with the given column headers and rows to stdout.

    Primary results (like ``beetroot ls`` or ``beetroot status``) go to
    stdout via the stdout console so they can be captured by shell pipelines.
    Rich strips decoration automatically when stdout is not a TTY.

    Args:
        columns: Sequence of column header strings.
        rows: Sequence of rows; each row is a sequence of cell strings whose
            length must match ``columns``.
    """
    t = Table(*columns)
    for row in rows:
        t.add_row(*row)
    _stdout_console.print(t)


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

    Example::

        with console.progress("Downloading frida-server", total=1024) as p:
            p.advance(512)
            p.advance(512)
    """

    def __init__(self, description: str, total: float) -> None:
        """
        Initialise the context with a task description and total work units.

        Args:
            description: Short label displayed beside the progress bar.
            total: Total number of work units (passed to ``Progress.add_task``).
        """
        self._description = description
        self._total = total
        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=_stderr_console,
        )
        self._task_id: TaskID | None = None

    def __enter__(self) -> ProgressContext:
        """Start the progress display and register the initial task."""
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
        """Stop the progress display."""
        self._progress.__exit__(exc_type, exc_val, exc_tb)


@contextlib.contextmanager
def progress(description: str, *, total: float) -> Generator[ProgressContext]:
    """
    Context manager wrapping ``rich.progress.Progress`` for long operations.

    Renders to stderr so progress bars never pollute stdout pipes.  Rich
    degrades gracefully on non-TTY streams (no carriage-return spam).

    Example::

        with console.progress("Fetching release", total=file_size) as p:
            for chunk in stream:
                process(chunk)
                p.advance(len(chunk))

    Args:
        description: Short label displayed next to the progress bar.
        total: Total work units; sets the 100% mark.

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
