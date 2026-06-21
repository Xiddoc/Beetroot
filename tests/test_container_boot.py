"""End-to-end container boot test.

Runs ``docker/entrypoint.sh`` (and the helpers it sources) inside a
real container using a lightweight busybox base, with fake ``magisk`` and
``frida-server`` shims that record every invocation to a log file.

Skipped when no Docker **daemon** is reachable — a bare ``shutil.which``
check is not enough, because a host can have the docker CLI installed with
no running daemon (issue #59), and this test issues a real ``docker run``.
On GitHub CI (ubuntu-latest) the daemon is always present, so the test
always runs.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from docker_daemon import daemon_available

_DOCKER_DIR = Path(__file__).parent.parent / "docker"

# The fake magisk shim: logs every invocation, returns sane values for
# the two SELECT queries magisk-config.sh issues.  Matches the pattern
# from test_magisk_config_helper.py, translated to run inside a busybox
# container (busybox sh, not GNU bash).
_FAKE_MAGISK = textwrap.dedent(
    """\
    #!/bin/sh
    echo "$@" >> "$MAGISK_LOG"
    case "$2" in
        "SELECT 1") exit 0 ;;
        "SELECT value FROM settings WHERE key='zygisk';")
            echo "value=1"
            ;;
    esac
    exit 0
    """
)

# The fake frida-server: logs its invocation then exits immediately so
# entrypoint.sh's trailing `wait` returns without hanging.
_FAKE_FRIDA = textwrap.dedent(
    """\
    #!/bin/sh
    echo "frida-server invoked" >> "$MAGISK_LOG"
    exit 0
    """
)


def _build_inline_script(log_mount: str, docker_src: str, modules_dir: str) -> str:
    """Return the sh -c body that sets up and runs entrypoint.sh.

    The script:
    1. Copies the boot helpers from the bind-mount to ``/`` (where
       ``entrypoint.sh`` hard-codes ``. /magisk-config.sh`` etc.)
    2. Writes fake ``magisk`` and ``frida-server`` shims under
       ``/tmp/beet-bin/`` and prepends that directory to PATH.
    3. Creates a minimal ``/data/adb/`` tree (magisk-config.sh
       echoes the db path in its startup message; the path doesn't
       need to be a real sqlite file because our fake magisk handles
       every ``--sqlite`` call without touching disk).
    4. Sets all ``BEETROOT_*`` env vars expected by the helpers.
    5. Runs ``sh /entrypoint.sh`` and exits with its exit code.

    The log is written to ``log_mount/magisk.log`` which is a writable
    bind-mounted directory, so the host can read the file after the
    container exits.
    """
    return textwrap.dedent(
        f"""\
        set -e

        # 1. Stage the boot helpers at /
        cp {docker_src}/*.sh /

        # 2. Write fake shims — log to the bind-mounted log directory
        mkdir -p /tmp/beet-bin
        export MAGISK_LOG="{log_mount}/magisk.log"
        touch "$MAGISK_LOG"

        cat > /tmp/beet-bin/magisk << 'EOF'
{_FAKE_MAGISK}
EOF
        chmod +x /tmp/beet-bin/magisk

        cat > /tmp/beet-bin/frida-server << 'EOF'
{_FAKE_FRIDA}
EOF
        chmod +x /tmp/beet-bin/frida-server

        export PATH="/tmp/beet-bin:$PATH"

        # 3. Create DB parent dir (magisk-config.sh echoes the path)
        mkdir -p /data/adb

        # 4. BEETROOT_* env vars consumed by the helpers
        export BEETROOT_MAGISK_DB="/data/adb/magisk.db"
        export BEETROOT_DENYLIST_PACKAGES="com.google.android.gms,com.google.android.gms.unstable"
        export BEETROOT_MODULES_DIR="{modules_dir}"
        export BEETROOT_FRIDA_BIN="/tmp/beet-bin/frida-server"

        # 5. Run entrypoint.sh
        sh /entrypoint.sh
        """
    )


@pytest.mark.skipif(not daemon_available(), reason="docker daemon not available")
def test_container_boot_end_to_end(tmp_path: Path) -> None:
    # log_dir is bind-mounted rw into the container; shims write magisk.log there.
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o777)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    # A dummy zip triggers flash-modules.sh's `magisk --install-module` branch,
    # giving us a concrete assertion that the helper ran its loop body.
    (modules_dir / "test-module.zip").write_bytes(b"PK\x03\x04")

    docker_src = "/mnt/docker"
    log_mount = "/mnt/logs"
    modules_mount = "/mnt/modules"

    inline_script = _build_inline_script(
        log_mount=log_mount,
        docker_src=docker_src,
        modules_dir=modules_mount,
    )

    result = subprocess.run(  # noqa: S603  # argv is fully test-controlled; docker is the SUT here
        [  # noqa: S607  # docker resolved via PATH; test guards on availability above
            "docker",
            "run",
            "--rm",
            # Boot helpers (read-only; copied to / inside the script)
            "--volume",
            f"{_DOCKER_DIR}:{docker_src}:ro",
            # Modules directory (empty, but must exist so the for-loop branch runs)
            "--volume",
            f"{modules_dir}:{modules_mount}:ro",
            # Writable log directory: shims write here; host reads after exit
            "--volume",
            f"{log_dir}:{log_mount}:rw",
            "busybox",
            "sh",
            "-c",
            inline_script,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    combined = result.stdout + result.stderr
    log_file = log_dir / "magisk.log"

    assert result.returncode == 0, (
        f"entrypoint.sh exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    # magisk-config.sh must have issued the Zygisk+denylist REPLACE INTO writes
    log_content = log_file.read_text() if log_file.exists() else ""
    queries = [line for line in log_content.splitlines() if line]

    assert any(
        "--sqlite" in q and "REPLACE INTO settings" in q and "zygisk" in q for q in queries
    ), f"zygisk REPLACE INTO not found in magisk log.\nqueries={queries!r}\nstdout={combined!r}"

    assert any(
        "--sqlite" in q and "REPLACE INTO settings" in q and "denylist" in q for q in queries
    ), f"denylist REPLACE INTO not found.\nqueries={queries!r}"

    # Zygisk SELECT verification must have run
    assert any(
        "--sqlite" in q and "SELECT value FROM settings WHERE key='zygisk'" in q for q in queries
    ), f"zygisk post-write SELECT not found.\nqueries={queries!r}"

    # GMS packages must have been enrolled in the denylist
    assert any(
        "--sqlite" in q and "INSERT OR IGNORE INTO denylist" in q and "com.google.android.gms'" in q
        for q in queries
    ), f"GMS denylist INSERT not found.\nqueries={queries!r}"

    assert any(
        "--sqlite" in q
        and "INSERT OR IGNORE INTO denylist" in q
        and "com.google.android.gms.unstable" in q
        for q in queries
    ), f"GMS unstable denylist INSERT not found.\nqueries={queries!r}"

    # flash-modules ran — modules dir has a dummy zip, so the flashing branch runs
    assert "Flashing module" in combined, (
        f"flash-modules.sh did not flash the dummy module.\ncombined={combined!r}"
    )
    # magisk --install-module was called for the dummy zip
    assert any("--install-module" in q for q in queries), (
        f"magisk --install-module not called.\nqueries={queries!r}"
    )

    # magisk-env.sh was sourced — the busybox base has no /system/etc/init/magisk,
    # so it takes the missing-source branch and falls through (proving it is
    # wired into entrypoint.sh before flash-modules.sh).
    assert "Magisk source dir" in combined, (
        f"magisk-env.sh was not sourced by entrypoint.sh.\ncombined={combined!r}"
    )
    # activate-zygisk.sh was sourced — the fake magisk reports zygisk already 1,
    # so it takes the already-active branch (no zygote restart on a routine boot).
    assert "Zygisk already active" in combined, (
        f"activate-zygisk.sh was not sourced by entrypoint.sh.\ncombined={combined!r}"
    )

    # launch-frida ran — frida-server was executable so it was launched
    assert any("frida-server invoked" in line for line in log_content.splitlines()), (
        f"frida-server shim was never called.\nlog={log_content!r}"
    )

    # Entrypoint progress messages reached stdout
    assert "Android boot detected" in combined, (
        f"entrypoint.sh start banner missing from output.\ncombined={combined!r}"
    )
    assert "Configuration done" in combined, (
        f"entrypoint.sh completion banner missing from output.\ncombined={combined!r}"
    )
