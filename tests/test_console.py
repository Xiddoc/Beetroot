"""Tests for beetroot.console — the shared rich-based output layer.

Every public branch of console.py must be exercised here: styled helpers,
table rendering, progress advance, TTY vs. non-TTY degradation, and the
set_consoles / accessor helpers.
"""
from __future__ import annotations

import io
import types

import pytest
from rich.console import Console

from beetroot import console

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_console(*, tty: bool = False) -> tuple[Console, io.StringIO]:
    """Return a (Console, buffer) pair for test capture."""
    buf: io.StringIO = io.StringIO()
    c = Console(file=buf, force_terminal=tty, highlight=False, no_color=(not tty))
    return c, buf


# ---------------------------------------------------------------------------
# set_consoles + accessor helpers
# ---------------------------------------------------------------------------


def test_set_consoles_stdout_replaces_stdout_console() -> None:
    new_stdout, _buf = _make_console()
    old_stdout = console._stdout_console
    try:
        console.set_consoles(stdout=new_stdout)
        assert console._stdout_console is new_stdout
        assert console.stdout_console() is new_stdout
    finally:
        console.set_consoles(stdout=old_stdout)


def test_set_consoles_stderr_replaces_stderr_console() -> None:
    new_stderr, _buf = _make_console()
    old_stderr = console._stderr_console
    try:
        console.set_consoles(stderr=new_stderr)
        assert console._stderr_console is new_stderr
        assert console.stderr_console() is new_stderr
    finally:
        console.set_consoles(stderr=old_stderr)


def test_set_consoles_none_leaves_unchanged() -> None:
    old_stdout = console._stdout_console
    old_stderr = console._stderr_console
    console.set_consoles(stdout=None, stderr=None)
    assert console._stdout_console is old_stdout
    assert console._stderr_console is old_stderr


def test_stdout_console_returns_module_singleton() -> None:
    assert console.stdout_console() is console._stdout_console


def test_stderr_console_returns_module_singleton() -> None:
    assert console.stderr_console() is console._stderr_console


def test_stdout_file_returns_backing_file() -> None:
    result = console.stdout_file()
    assert result is console._stdout_console.file


# ---------------------------------------------------------------------------
# Styled helpers (error, warn, info, success) — non-TTY plain text
# ---------------------------------------------------------------------------


def test_error_writes_error_prefix_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    console.error("something broke")
    assert "error:" in buf.getvalue()
    assert "something broke" in buf.getvalue()


def test_warn_writes_warn_prefix_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    console.warn("watch out")
    assert "warn:" in buf.getvalue()
    assert "watch out" in buf.getvalue()


def test_info_writes_info_prefix_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    console.info("FYI")
    assert "info:" in buf.getvalue()
    assert "FYI" in buf.getvalue()


def test_success_writes_ok_prefix_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    console.success("all done")
    assert "ok:" in buf.getvalue()
    assert "all done" in buf.getvalue()


# ---------------------------------------------------------------------------
# TTY vs. non-TTY degradation — ANSI codes stripped when not a TTY
# ---------------------------------------------------------------------------


def test_error_no_ansi_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=False)
    monkeypatch.setattr(console, "_stderr_console", c)
    console.error("oops")
    assert "\x1b" not in buf.getvalue()


def test_error_has_ansi_when_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=True)
    monkeypatch.setattr(console, "_stderr_console", c)
    console.error("oops")
    assert "\x1b" in buf.getvalue()


def test_warn_no_ansi_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=False)
    monkeypatch.setattr(console, "_stderr_console", c)
    console.warn("careful")
    assert "\x1b" not in buf.getvalue()


def test_info_no_ansi_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=False)
    monkeypatch.setattr(console, "_stderr_console", c)
    console.info("note")
    assert "\x1b" not in buf.getvalue()


def test_success_no_ansi_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=False)
    monkeypatch.setattr(console, "_stderr_console", c)
    console.success("done")
    assert "\x1b" not in buf.getvalue()


# ---------------------------------------------------------------------------
# table() — renders to stdout
# ---------------------------------------------------------------------------


def test_table_renders_columns_and_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stdout_console", c)
    console.table(["Name", "Status"], [["alpha", "running"], ["beta", "stopped"]])
    out = buf.getvalue()
    assert "Name" in out
    assert "Status" in out
    assert "alpha" in out
    assert "running" in out
    assert "beta" in out
    assert "stopped" in out


def test_table_empty_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stdout_console", c)
    console.table(["Col1", "Col2"], [])
    out = buf.getvalue()
    assert "Col1" in out
    assert "Col2" in out


def test_table_no_ansi_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=False)
    monkeypatch.setattr(console, "_stdout_console", c)
    console.table(["A"], [["x"]])
    assert "\x1b" not in buf.getvalue()


def test_table_writes_to_stdout_not_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout_c, stdout_buf = _make_console()
    stderr_c, stderr_buf = _make_console()
    monkeypatch.setattr(console, "_stdout_console", stdout_c)
    monkeypatch.setattr(console, "_stderr_console", stderr_c)
    console.table(["Name"], [["alpha"]])
    assert "alpha" in stdout_buf.getvalue()
    assert "alpha" not in stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# ProgressContext — direct use
# ---------------------------------------------------------------------------


def test_progress_context_advance_updates_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    ctx = console.ProgressContext("Fetching", 100.0)
    with ctx:
        ctx.advance(50.0)
    out = buf.getvalue()
    assert "Fetching" in out


def test_progress_context_advance_default_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    ctx = console.ProgressContext("Work", 10.0)
    with ctx:
        ctx.advance()
    assert "Work" in buf.getvalue()


def test_progress_context_renders_to_stderr_not_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout_c, stdout_buf = _make_console()
    stderr_c, stderr_buf = _make_console()
    monkeypatch.setattr(console, "_stdout_console", stdout_c)
    monkeypatch.setattr(console, "_stderr_console", stderr_c)
    with console.ProgressContext("Job", 10.0) as ctx:
        ctx.advance(5.0)
    assert "Job" in stderr_buf.getvalue()
    assert "Job" not in stdout_buf.getvalue()


def _raise_inside_progress_context(c: Console) -> None:
    with console.ProgressContext("Exploding", 10.0) as ctx:
        ctx.advance(1.0)
        raise ValueError("boom")


def test_progress_context_exit_called_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    with pytest.raises(ValueError, match="boom"):
        _raise_inside_progress_context(c)
    assert "Exploding" in buf.getvalue()


def test_progress_context_no_ansi_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console(tty=False)
    monkeypatch.setattr(console, "_stderr_console", c)
    with console.ProgressContext("DL", 100.0) as ctx:
        ctx.advance(100.0)
    assert "\x1b" not in buf.getvalue()


# ---------------------------------------------------------------------------
# progress() context manager helper
# ---------------------------------------------------------------------------


def test_progress_helper_yields_progress_context(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    with console.progress("Syncing", total=50.0) as p:
        assert isinstance(p, console.ProgressContext)
        p.advance(25.0)
    assert "Syncing" in buf.getvalue()


def test_progress_helper_renders_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout_c, stdout_buf = _make_console()
    stderr_c, stderr_buf = _make_console()
    monkeypatch.setattr(console, "_stdout_console", stdout_c)
    monkeypatch.setattr(console, "_stderr_console", stderr_c)
    with console.progress("Uploading", total=1.0) as p:
        p.advance(1.0)
    assert "Uploading" in stderr_buf.getvalue()
    assert "Uploading" not in stdout_buf.getvalue()


def _raise_inside_progress_helper() -> None:
    with console.progress("Download", total=100.0) as p:
        p.advance(10.0)
        raise RuntimeError("network error")


def test_progress_helper_propagates_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    with pytest.raises(RuntimeError, match="network error"):
        _raise_inside_progress_helper()


# ---------------------------------------------------------------------------
# ProgressContext.__exit__ traceback argument typing
# ---------------------------------------------------------------------------


def test_progress_context_exit_with_none_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    ctx = console.ProgressContext("TB", 10.0)
    ctx.__enter__()
    ctx.__exit__(None, None, None)
    assert "TB" in buf.getvalue()


def test_progress_context_exit_with_real_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    ctx = console.ProgressContext("TB2", 10.0)
    ctx.__enter__()
    captured_tb: types.TracebackType | None = None
    try:
        raise ValueError("synthetic")
    except ValueError as exc:
        captured_tb = exc.__traceback__
        ctx.__exit__(type(exc), exc, captured_tb)
    assert "TB2" in buf.getvalue()


# ---------------------------------------------------------------------------
# Module-level singletons use sys.stdout / sys.stderr
# ---------------------------------------------------------------------------


def test_initial_stdout_console_uses_sys_stdout() -> None:
    assert isinstance(console._stdout_console, Console)
    assert hasattr(console._stdout_console.file, "write")


def test_initial_stderr_console_uses_sys_stderr() -> None:
    assert isinstance(console._stderr_console, Console)
    assert hasattr(console._stderr_console.file, "write")


# ---------------------------------------------------------------------------
# ProgressContext.advance before __enter__ — task_id is None branch
# ---------------------------------------------------------------------------


def test_progress_context_advance_before_enter_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = _make_console()
    monkeypatch.setattr(console, "_stderr_console", c)
    ctx = console.ProgressContext("Noop", 10.0)
    ctx.advance(5.0)
    assert buf.getvalue() == ""
