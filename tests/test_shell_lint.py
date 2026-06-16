"""Lint guardrails for the project's shell scripts.

The in-container boot helpers (``docker/*.sh``) and the micro-VM guest init
(``docker/vm/*.sh``) are POSIX ``sh`` (toybox / busybox, not bash). CI gates
them with ``shellcheck -S style -s sh`` and ``shfmt -i 4``; these tests run the
same checks locally so a regression is caught before the push, and skip
cleanly when the tools are not installed (mirroring ``test_container_boot``'s
docker-availability skip).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_SHELL_SCRIPTS = sorted(_REPO_ROOT.glob("docker/*.sh")) + sorted(_REPO_ROOT.glob("docker/vm/*.sh"))


def test_shell_scripts_are_discovered() -> None:
    # Guard against a glob that silently matches nothing (which would make the
    # lint tests vacuously pass). The four boot helpers + guest-init.sh.
    names = {p.name for p in _SHELL_SCRIPTS}
    assert {"entrypoint.sh", "magisk-config.sh", "guest-init.sh"} <= names


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", _SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_shellcheck_clean_at_style_severity(script: Path) -> None:
    result = subprocess.run(  # noqa: S603  # argv is test-controlled; script paths come from a repo-local glob
        ["shellcheck", "-S", "style", "-s", "sh", str(script)],  # noqa: S607  # shellcheck resolved via PATH; skipif guards availability
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("shfmt") is None, reason="shfmt not installed")
@pytest.mark.parametrize("script", _SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_shfmt_formatted(script: Path) -> None:
    result = subprocess.run(  # noqa: S603  # argv is test-controlled; script paths come from a repo-local glob
        ["shfmt", "-i", "4", "-d", str(script)],  # noqa: S607  # shfmt resolved via PATH; skipif guards availability
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
