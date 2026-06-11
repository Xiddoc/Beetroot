"""Behaviour tests for ``docker/magisk-config.sh``.

T2 (Agent 2 B-1, Agent 3 1.3) wired ``stealth.denylist`` through
``render_env`` → ``.env`` → compose ``environment:`` → the helper. The
helper now iterates ``BEETROOT_DENYLIST_PACKAGES`` (comma-separated)
and SQL'es each package into Magisk's denylist table. The pre-T2
hard-coded GMS pair has moved into ``Stealth``'s default.

T2 (Agent 1, Agent 2 F-9, Agent 3 1.2) added a post-write Zygisk
verification — the helper SELECTs ``zygisk`` back from the settings
table and exits non-zero if Magisk didn't accept the write.

Issue #14 bounded the daemon wait — the helper now gives up after
``BEETROOT_MAGISK_WAIT_SECS`` (default 120) one-second probe attempts
and exits 1 with a clear error instead of looping forever when Magisk
is broken or missing.

These tests source ``magisk-config.sh`` from ``sh`` with a fake
``magisk`` binary on PATH. The fake records every ``--sqlite`` query
into a log file so the test can assert on the exact statements that
were issued (and the order).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).parent.parent / "docker" / "magisk-config.sh"


def _fake_magisk(
    zygisk_select_value: str = "1", *, daemon_reachable: bool = True
) -> str:
    """Return a sh script that records every ``magisk --sqlite`` invocation.

    Args:
        zygisk_select_value: The value the fake reports back to the
            ``SELECT value FROM settings WHERE key='zygisk'`` query.
            Default ``1`` (the success path); pass ``0`` to drive the
            verification-fail branch.
        daemon_reachable: When ``False``, the fake fails the
            ``SELECT 1`` liveness probe forever, driving the
            bounded-wait timeout branch (issue #14).
    """
    probe_exit = 0 if daemon_reachable else 1
    return f"""#!/bin/sh
# Fake magisk shim — logs every invocation to $MAGISK_LOG.
# Replies to the zygisk SELECT with ``value={zygisk_select_value}``.
echo "$@" >> "$MAGISK_LOG"
case "$2" in
    "SELECT 1") exit {probe_exit} ;;
    "SELECT value FROM settings WHERE key='zygisk';")
        echo "value={zygisk_select_value}"
        ;;
esac
exit 0
"""  # noqa: S608  # fake `sh` script for a test shim; the "SELECT ..." literal is matched by `case`, not executed as SQL


def _run_helper(
    tmp_path: Path,
    env: dict[str, str],
    *,
    zygisk_value: str = "1",
    daemon_reachable: bool = True,
) -> tuple[int, str, list[str]]:
    """Source ``magisk-config.sh`` with a fake magisk on PATH.

    Returns ``(exit_code, stdout, sqlite_queries)``.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    magisk = fake_bin / "magisk"
    magisk.write_text(_fake_magisk(zygisk_value, daemon_reachable=daemon_reachable))
    magisk.chmod(0o755)
    log = tmp_path / "magisk.log"
    log.write_text("")

    full_env = {
        **env,
        "MAGISK_LOG": str(log),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    res = subprocess.run(  # noqa: S603  # test invokes the shipped magisk-config.sh under a controlled fake-magisk shim; argv is fixed
        ["sh", str(HELPER)],  # noqa: S607  # `sh` is universal POSIX; resolving an absolute path here would diverge from how Android init invokes the helper
        check=False,
        capture_output=True,
        text=True,
        env=full_env,
    )
    queries = [line for line in log.read_text().splitlines() if line]
    return res.returncode, res.stdout + res.stderr, queries


def test_default_denylist_enrols_gms_pair(tmp_path: Path) -> None:
    code, _out, queries = _run_helper(
        tmp_path,
        env={
            "BEETROOT_DENYLIST_PACKAGES": (
                "com.google.android.gms,com.google.android.gms.unstable"
            )
        },
    )
    assert code == 0
    gms_inserts = [q for q in queries if "INSERT OR IGNORE INTO denylist" in q]
    assert any("com.google.android.gms'" in q for q in gms_inserts)
    assert any("com.google.android.gms.unstable" in q for q in gms_inserts)


def test_custom_denylist_csv_parsed(tmp_path: Path) -> None:
    code, _out, queries = _run_helper(
        tmp_path,
        env={"BEETROOT_DENYLIST_PACKAGES": "com.app.one,com.app.two,com.x.y"},
    )
    assert code == 0
    inserts = [q for q in queries if "INSERT OR IGNORE INTO denylist" in q]
    assert sum(1 for q in inserts if "com.app.one" in q) == 1
    assert sum(1 for q in inserts if "com.app.two" in q) == 1
    assert sum(1 for q in inserts if "com.x.y" in q) == 1


def test_empty_denylist_skips_inserts(tmp_path: Path) -> None:
    # When the env var is unset / empty, the helper must NOT issue any
    # INSERT — and especially must not SQL'inject an empty
    # ('', '') row (the pre-T2 draft had a missing -n guard that did
    # exactly this once the GMS pair was removed from the helper).
    code, _out, queries = _run_helper(
        tmp_path,
        env={"BEETROOT_DENYLIST_PACKAGES": ""},
    )
    assert code == 0
    inserts = [q for q in queries if "INSERT OR IGNORE INTO denylist" in q]
    assert not inserts, f"empty denylist still wrote rows: {inserts!r}"


def test_zygisk_post_write_verification_succeeds(tmp_path: Path) -> None:
    code, _out, queries = _run_helper(
        tmp_path,
        env={"BEETROOT_DENYLIST_PACKAGES": ""},
        zygisk_value="1",
    )
    assert code == 0
    selects = [
        q for q in queries
        if "SELECT value FROM settings WHERE key='zygisk'" in q
    ]
    assert selects, "helper did not verify zygisk landed in the DB"


def test_zygisk_post_write_verification_fails_loudly(tmp_path: Path) -> None:
    # If Magisk reports ``zygisk`` is not ``1`` after the REPLACE INTO,
    # the helper must exit non-zero and the user must see a clear
    # message on stderr. v0.3 silently believed the REPLACE INTO.
    code, out, _ = _run_helper(
        tmp_path,
        env={"BEETROOT_DENYLIST_PACKAGES": ""},
        zygisk_value="0",
    )
    assert code != 0, "helper accepted a botched zygisk write silently"
    assert "Zygisk" in out


def test_daemon_wait_times_out_and_fails_loudly(tmp_path: Path) -> None:
    # If the Magisk daemon never answers (broken / missing Magisk), the
    # helper must give up after $BEETROOT_MAGISK_WAIT_SECS attempts and
    # exit non-zero with a clear error — not loop forever leaving a
    # container that looks "up" but never configures (issue #14).
    code, out, queries = _run_helper(
        tmp_path,
        env={
            "BEETROOT_DENYLIST_PACKAGES": "",
            "BEETROOT_MAGISK_WAIT_SECS": "2",
        },
        daemon_reachable=False,
    )
    assert code != 0, "helper hung past the wait budget or exited 0"
    assert "Magisk daemon unreachable" in out
    writes = [q for q in queries if "REPLACE INTO settings" in q]
    assert not writes, f"helper wrote settings despite unreachable daemon: {writes!r}"


def test_daemon_wait_succeeds_within_budget(tmp_path: Path) -> None:
    # A reachable daemon must still configure normally with the bound
    # in place — the timeout is an upper bound, not a fixed delay.
    code, _out, queries = _run_helper(
        tmp_path,
        env={
            "BEETROOT_DENYLIST_PACKAGES": "",
            "BEETROOT_MAGISK_WAIT_SECS": "2",
        },
    )
    assert code == 0
    assert any("REPLACE INTO settings" in q for q in queries)


@pytest.mark.parametrize("packages", [",,", ",com.app,", " "])
def test_malformed_csv_does_not_inject_empty_rows(
    tmp_path: Path, packages: str
) -> None:
    # CSVs with extra commas (``,,``, leading/trailing) must not produce
    # empty package rows in the denylist table — those would be a
    # quiet correctness bug and a tiny SQL footprint widening.
    code, _out, queries = _run_helper(
        tmp_path,
        env={"BEETROOT_DENYLIST_PACKAGES": packages},
    )
    assert code == 0
    inserts = [q for q in queries if "INSERT OR IGNORE INTO denylist" in q]
    for q in inserts:
        assert "('', '')" not in q, f"empty package row written: {q!r}"
