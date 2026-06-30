"""Shell-verb exit-code normalization (#217).

``beetroot shell`` forwards the underlying ``adb shell`` exit code so
research scripts that check ``$?`` see the real status. But a
signal-killed subprocess returns a *negative* code (``-N``), and routing
that through typer.Exit / ``os._exit`` masks it with ``&0xFF`` — turning
SIGINT (-2) into 254 and SIGTERM (-15) into 241 instead of the POSIX
``128+N`` convention. The verb normalizes the negative case at the CLI
boundary; these tests pin every branch of that conditional.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, registry

runner = CliRunner()


def _stub_backend_returning(rc: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Manager.resolve(...).shell(...)`` return ``rc`` for any instance."""
    backend = MagicMock()
    backend.shell.return_value = rc
    monkeypatch.setattr(api.Manager, "resolve", lambda name: cast(api.DeviceBackend, backend))


@pytest.mark.parametrize(
    ("returncode", "expected_exit"),
    [
        (-2, 130),  # SIGINT  → 128 + 2
        (-15, 143),  # SIGTERM → 128 + 15
        (1, 1),  # ordinary non-zero passes through unchanged
    ],
)
def test_shell_normalizes_exit_code(
    cli_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected_exit: int,
) -> None:
    registry.add_allocating("alpha", cli_root / "alpha")
    _stub_backend_returning(returncode, monkeypatch)
    result = runner.invoke(cli.app, ["shell", "alpha"])
    assert result.exit_code == expected_exit


def test_shell_zero_does_not_exit(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # rc == 0 → the verb returns without raising typer.Exit (exit 0).
    registry.add_allocating("alpha", cli_root / "alpha")
    _stub_backend_returning(0, monkeypatch)
    result = runner.invoke(cli.app, ["shell", "alpha"])
    assert result.exit_code == 0


def test_shell_extreme_negative_clamped_to_255(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pathological very-negative returncode would exceed the 8-bit exit
    # range; min(..., 255) clamps it so typer.Exit stays in [0, 255]
    # (128 - (-200) == 328 → 255).
    registry.add_allocating("alpha", cli_root / "alpha")
    _stub_backend_returning(-200, monkeypatch)
    result = runner.invoke(cli.app, ["shell", "alpha"])
    assert result.exit_code == 255
