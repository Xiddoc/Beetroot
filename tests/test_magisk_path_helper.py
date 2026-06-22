"""Behaviour tests for ``docker/magisk-path.sh``.

``entrypoint.sh`` is launched by Android init (``stealth.rc``'s
``exec_background``), which inherits init's default service PATH
(``/system/bin:/system/xbin:/vendor/bin:…``) — *without* the directory Magisk
installs its ``magisk`` binary into (``/sbin/magisk`` on the redroid Magisk
image). Every helper calls bare ``magisk``, so without a PATH fix
``magisk-config.sh``'s daemon wait spins on ``magisk --sqlite "SELECT 1"`` until
it times out and aborts the whole boot before Zygisk/denylist/MAGISKBIN/modules
are configured. ``magisk-path.sh`` (sourced first) prepends the directory that
actually holds ``magisk``.

These tests source the helper from ``sh`` with a pinned PATH and a fake
``magisk`` placed in a candidate directory named via ``BEETROOT_MAGISK_DIRS``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

HELPER = Path(__file__).parent.parent / "docker" / "magisk-path.sh"


def _make_magisk(directory: Path) -> Path:
    """Create an executable fake ``magisk`` in ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "magisk"
    binary.write_text("#!/bin/sh\n# fake magisk\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _source(env: dict[str, str], *, timeout: float = 10) -> tuple[int, str]:
    """Source the helper, then echo PATH + ``command -v magisk``; return (code, output)."""
    # The trailing probes run in the same shell, so they observe the PATH the
    # helper exported.
    script = f'. {HELPER}; echo "PATH=$PATH"; echo "MAGISK=$(command -v magisk || echo NONE)"'
    res = subprocess.run(  # noqa: S603  # runs the shipped helper under a pinned PATH + fake magisk
        ["sh", "-c", script],  # noqa: S607  # `sh` is universal POSIX, matching how Android init invokes the helper
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return res.returncode, res.stdout + res.stderr


def test_resolves_magisk_from_candidate_dir(tmp_path: Path) -> None:
    sbin = tmp_path / "sbin"
    magisk = _make_magisk(sbin)
    code, out = _source(
        {
            "PATH": "/usr/bin:/bin",  # deliberately excludes the fake magisk dir
            "BEETROOT_MAGISK_DIRS": str(sbin),
        }
    )
    assert code == 0, out
    assert "Resolved magisk" in out
    assert f"MAGISK={magisk}" in out, out


def test_picks_first_candidate_that_exists(tmp_path: Path) -> None:
    missing = tmp_path / "debug_ramdisk"  # never created
    sbin = tmp_path / "sbin"
    magisk = _make_magisk(sbin)
    code, out = _source(
        {
            "PATH": "/usr/bin:/bin",
            # First candidate is absent; the helper must skip to the second.
            "BEETROOT_MAGISK_DIRS": f"{missing}:{sbin}",
        }
    )
    assert code == 0, out
    assert f"MAGISK={magisk}" in out, out


def test_noop_when_magisk_already_on_path(tmp_path: Path) -> None:
    # magisk already resolvable on PATH → the helper must NOT prepend a
    # different candidate (the on-PATH binary keeps winning).
    onpath = tmp_path / "onpath"
    onpath_magisk = _make_magisk(onpath)
    other = tmp_path / "sbin"
    _make_magisk(other)
    code, out = _source(
        {
            "PATH": f"{onpath}:/usr/bin:/bin",
            "BEETROOT_MAGISK_DIRS": str(other),
        }
    )
    assert code == 0, out
    assert "Resolved magisk" not in out, out
    assert f"MAGISK={onpath_magisk}" in out, out


def test_falls_through_when_magisk_nowhere(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = _source(
        {
            "PATH": "/usr/bin:/bin",
            "BEETROOT_MAGISK_DIRS": str(empty),
        }
    )
    assert code == 0, out
    assert "Resolved magisk" not in out
    assert "MAGISK=NONE" in out, out
