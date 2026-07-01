"""Regression tests for #197 — a malformed BEETROOT_*_TIMEOUT must not brick import.

Before the fix, ``settings = Settings()`` ran eagerly at import time, so an
empty/non-numeric ``BEETROOT_HTTP_TIMEOUT`` raised a raw ``pydantic.ValidationError``
during ``import beetroot.cli`` — *before* ``cli.main()``'s error boundary — and
even ``beetroot --help`` dumped a traceback. The lazy proxy defers construction
to first attribute access (CLI runtime), so a bad var maps to the friendly
``error: ...`` + exit 1 contract, and ``--help`` (which reads no setting) still works.
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console
from typer.testing import CliRunner

from beetroot import cli, console, settings


def _run_main_with_argv(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """Drive cli.main() under a faked argv. Returns (exit_code, stderr)."""
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    console.set_consoles(stderr=Console(file=buf, force_terminal=False))
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0), buf.getvalue()
    return 0, buf.getvalue()


@pytest.mark.parametrize("var", ["BEETROOT_HTTP_TIMEOUT", "BEETROOT_VM_ADB_CONNECT_TIMEOUT"])
def test_lazy_proxy_raises_domain_error_on_access(
    var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(var, "notanumber")
    proxy = settings._LazySettings()
    with pytest.raises(settings.InvalidSettingsError):
        _ = proxy.http_timeout


def test_lazy_proxy_defers_validation_until_access(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing the proxy under a bad env must NOT raise — only reading an
    # attribute does. This is what keeps ``import beetroot.cli`` (and --help) alive.
    monkeypatch.setenv("BEETROOT_HTTP_TIMEOUT", "")
    proxy = settings._LazySettings()  # no exception here
    with pytest.raises(settings.InvalidSettingsError):
        _ = proxy.docker_bin


def test_lazy_proxy_caches_and_forwards_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEETROOT_HTTP_TIMEOUT", "45")
    proxy = settings._LazySettings()
    assert proxy.http_timeout == 45
    # Second access returns the cached Settings (no re-read).
    assert proxy._get() is proxy._get()


def _reset_module_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Force the shared module-level proxy to re-read env on next access.

    Consumers (compose, frida_download, …) bound ``settings.settings`` at import,
    so tests can't swap the object out from under them — instead clear its cache;
    ``monkeypatch.setattr`` restores the prior resolved value on teardown.
    """
    monkeypatch.setattr(settings.settings, "_resolved", None)


def test_help_still_works_with_malformed_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEETROOT_HTTP_TIMEOUT", "notanumber")
    _reset_module_proxy(monkeypatch)
    # --help reads no setting, so the bad env is never touched → exit 0, no crash.
    code, err = _run_main_with_argv(["beetroot", "--help"], monkeypatch)
    assert code == 0
    assert "Traceback" not in err


def test_bad_timeout_maps_to_friendly_error(
    cli_root: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    CliRunner().invoke(cli.app, ["create", "alpha"])
    monkeypatch.setenv("BEETROOT_HTTP_TIMEOUT", "notanumber")
    _reset_module_proxy(monkeypatch)

    # ``doctor alpha`` reads ``settings.docker_bin`` via compose while probing
    # status, forcing the lazy proxy to resolve inside main()'s boundary.
    code, err = _run_main_with_argv(["beetroot", "doctor", "alpha"], monkeypatch)
    assert code == 1
    assert "error:" in err
    assert "Traceback" not in err
    assert "BEETROOT_" in err
