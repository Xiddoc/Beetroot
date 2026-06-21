"""Behaviour tests for ``docker/activate-zygisk.sh`` (Zygisk activation).

Zygisk injects zygote at zygote start. ``magisk-config.sh`` enables it on
``sys.boot_completed=1`` — after the first zygote already started without it —
so on the first boot of a fresh instance the setting lands but Zygisk (and any
flashed Zygisk module) is not live until a zygote restart. ``activate-zygisk.sh``
performs that one-shot restart, gated to the boot that *newly* enables Zygisk so
routine restarts don't churn zygote.

The first group of tests sources ``activate-zygisk.sh`` alone with the gating
flag passed via env. The integration test sources ``magisk-config.sh`` then
``activate-zygisk.sh`` in one shell (as ``entrypoint.sh`` does), with a fake
``magisk`` whose reported *prior* zygisk value decides whether the restart
fires — the user-input → behaviour path the helpers compose to deliver.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DOCKER_DIR = Path(__file__).parent.parent / "docker"
ACTIVATE = DOCKER_DIR / "activate-zygisk.sh"
MAGISK_CONFIG = DOCKER_DIR / "magisk-config.sh"

# Fake setprop: records every invocation to $SETPROP_LOG. ``fail=1`` makes it
# exit non-zero to drive the guarded-failure branch.
_FAKE_SETPROP = """#!/bin/sh
echo "$@" >> "$SETPROP_LOG"
exit ${SETPROP_EXIT:-0}
"""


def _fake_magisk(prev_zygisk: str) -> str:
    """A stateful magisk shim.

    The settings SELECT reports ``prev_zygisk`` until the helper issues the
    ``REPLACE INTO ... 'zygisk', 1`` write, after which it reports ``1`` — so
    magisk-config.sh reads the genuine prior value first but still passes its
    own post-write verification (which re-SELECTs the same key).
    """
    return f"""#!/bin/sh
echo "$@" >> "$MAGISK_LOG"
case "$2" in
    "SELECT 1") exit 0 ;;
    "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);")
        echo 1 > "$ZYGISK_STATE" ;;
    "SELECT value FROM settings WHERE key='zygisk';")
        if [ -f "$ZYGISK_STATE" ]; then echo "value=1"; else echo "value={prev_zygisk}"; fi
        ;;
esac
exit 0
"""  # noqa: S608  # fake `sh` shim; the SELECT literal is matched by `case`, not run as SQL


def _bin_with(tmp_path: Path, **shims: str) -> Path:
    """Create a bin dir holding the named shims (name -> script body)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in shims.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return bin_dir


def _source(
    script_body: str, tmp_path: Path, env: dict[str, str], *, timeout: float = 10
) -> tuple[int, str, list[str]]:
    """Run ``script_body`` under sh; return (code, output, setprop_calls)."""
    log = tmp_path / "setprop.log"
    log.write_text("")
    full_env = {**env, "SETPROP_LOG": str(log), "PATH": f"{env['PATH']}"}
    res = subprocess.run(  # noqa: S603  # sources the shipped helpers under controlled shims
        ["sh", "-c", script_body],  # noqa: S607  # `sh` resolved via PATH, matching Android init
        check=False,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
    )
    calls = [line for line in log.read_text().splitlines() if line]
    return res.returncode, res.stdout + res.stderr, calls


def test_newly_enabled_restarts_zygote(tmp_path: Path) -> None:
    bin_dir = _bin_with(tmp_path, setprop=_FAKE_SETPROP)
    code, out, calls = _source(
        f". {ACTIVATE}; echo POST",
        tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "BEETROOT_ZYGISK_NEWLY_ENABLED": "1"},
    )
    assert code == 0, out
    assert "POST" in out, "sourced helper killed the parent shell"
    assert calls == ["ctl.restart zygote"], f"zygote not restarted: {calls!r}"


def test_not_newly_enabled_skips_restart(tmp_path: Path) -> None:
    bin_dir = _bin_with(tmp_path, setprop=_FAKE_SETPROP)
    code, out, calls = _source(
        f". {ACTIVATE}; echo POST",
        tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "BEETROOT_ZYGISK_NEWLY_ENABLED": "0"},
    )
    assert code == 0, out
    assert "POST" in out
    assert not calls, f"zygote restarted when already active: {calls!r}"
    assert "already active" in out


def test_opt_out_disables_restart(tmp_path: Path) -> None:
    bin_dir = _bin_with(tmp_path, setprop=_FAKE_SETPROP)
    code, out, calls = _source(
        f". {ACTIVATE}; echo POST",
        tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "BEETROOT_ZYGISK_NEWLY_ENABLED": "1",
            "BEETROOT_ZYGOTE_RESTART": "0",
        },
    )
    assert code == 0, out
    assert "POST" in out
    assert not calls, f"restart fired despite opt-out: {calls!r}"
    assert "disabled" in out


def test_setprop_failure_does_not_abort_boot(tmp_path: Path) -> None:
    bin_dir = _bin_with(tmp_path, setprop=_FAKE_SETPROP)
    code, out, calls = _source(
        f". {ACTIVATE}; echo POST",
        tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "BEETROOT_ZYGISK_NEWLY_ENABLED": "1",
            "SETPROP_EXIT": "1",
        },
    )
    assert code == 0, out
    assert "POST" in out, "a failed setprop killed the parent shell"
    assert calls == ["ctl.restart zygote"]
    assert "Could not restart zygote" in out


def test_integration_fresh_boot_triggers_restart(tmp_path: Path) -> None:
    # prior zygisk=0 → magisk-config flags newly-enabled → activate restarts.
    bin_dir = _bin_with(tmp_path, setprop=_FAKE_SETPROP, magisk=_fake_magisk("0"))
    (tmp_path / "magisk.log").write_text("")
    code, out, calls = _source(
        f". {MAGISK_CONFIG}; . {ACTIVATE}; echo POST",
        tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "MAGISK_LOG": str(tmp_path / "magisk.log"),
            "ZYGISK_STATE": str(tmp_path / "zygisk.state"),
            "BEETROOT_DENYLIST_PACKAGES": "",
        },
    )
    assert code == 0, out
    assert "POST" in out
    assert calls == ["ctl.restart zygote"], f"fresh boot did not restart zygote: {calls!r}"


def test_integration_steady_state_skips_restart(tmp_path: Path) -> None:
    # prior zygisk=1 → already enabled → no restart on a routine boot.
    bin_dir = _bin_with(tmp_path, setprop=_FAKE_SETPROP, magisk=_fake_magisk("1"))
    (tmp_path / "magisk.log").write_text("")
    code, out, calls = _source(
        f". {MAGISK_CONFIG}; . {ACTIVATE}; echo POST",
        tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "MAGISK_LOG": str(tmp_path / "magisk.log"),
            "ZYGISK_STATE": str(tmp_path / "zygisk.state"),
            "BEETROOT_DENYLIST_PACKAGES": "",
        },
    )
    assert code == 0, out
    assert "POST" in out
    assert not calls, f"steady-state boot needlessly restarted zygote: {calls!r}"
