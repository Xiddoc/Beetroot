"""Behaviour tests for ``docker/magisk-env.sh``.

The redroid-script Magisk image bakes the Magisk binaries into
``/system/etc/init/magisk`` and only ``mkdir``s ``/data/adb/magisk`` (MAGISKBIN)
*empty* at boot — the per-install shell scripts (``util_functions.sh``,
``module_installer.sh``, …) live only inside ``magisk.apk`` and are normally
extracted by the Magisk app the first time a human opens it. Headless redroid
never runs that, so MAGISKBIN stays empty and ``magisk --install-module`` aborts
with "Incomplete Magisk install". ``magisk-env.sh`` replicates the app's
environment-fix headlessly so ``flash-modules.sh`` can install modules.

These tests source the helper from ``sh`` with a fake MAGISK_SRC_DIR. The fake
``busybox`` shim implements just enough of the ``unzip`` applet to simulate
extracting ``assets/*.sh`` out of ``magisk.apk`` into MAGISKBIN.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

HELPER = Path(__file__).parent.parent / "docker" / "magisk-env.sh"

# A fake busybox whose only implemented applet is ``unzip``: it ignores the apk
# contents and materialises the asset scripts the real busybox would extract,
# into the ``-d`` directory under ``assets/`` (matching the real layout the
# helper then copies flat).
_FAKE_BUSYBOX = """#!/bin/sh
if [ "$1" = "unzip" ]; then
    shift
    dir=.
    while [ $# -gt 0 ]; do
        case "$1" in
            -d) dir="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    mkdir -p "$dir/assets"
    printf 'MAGISK_VER_CODE=30700\\ninstall_module(){ :; }\\n' > "$dir/assets/util_functions.sh"
    : > "$dir/assets/module_installer.sh"
    exit 0
fi
exit 0
"""


def _make_src(
    tmp_path: Path,
    *,
    binaries: tuple[str, ...] = ("busybox", "magisk", "magiskboot", "magiskpolicy", "magiskinit"),
    with_apk: bool = True,
) -> Path:
    """Build a fake MAGISK_SRC_DIR with staged binaries and (optionally) an apk."""
    src = tmp_path / "src"
    src.mkdir()
    for name in binaries:
        binary = src / name
        if name == "busybox":
            binary.write_text(_FAKE_BUSYBOX)
        else:
            binary.write_text(f"#!/bin/sh\n# fake {name}\n")
        binary.chmod(0o755)
    if with_apk:
        # Contents are irrelevant — the fake busybox unzip ignores them.
        (src / "magisk.apk").write_bytes(b"PK\x03\x04 fake apk")
    return src


def _run_helper(*, src: Path | None, bin_dir: Path, timeout: float = 10) -> tuple[int, str]:
    """Source ``magisk-env.sh`` with the given SRC/BIN dirs; return (code, output)."""
    env = {
        "BEETROOT_MAGISK_BIN_DIR": str(bin_dir),
        "PATH": "/usr/bin:/bin",
    }
    if src is not None:
        env["BEETROOT_MAGISK_SRC_DIR"] = str(src)
    res = subprocess.run(  # noqa: S603  # runs the shipped helper under a controlled fake-busybox shim
        ["sh", str(HELPER)],  # noqa: S607  # `sh` is universal POSIX, matching how Android init invokes the helper
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return res.returncode, res.stdout + res.stderr


def test_populates_magiskbin_with_binaries_and_scripts(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    bin_dir = tmp_path / "magiskbin"
    code, out = _run_helper(src=src, bin_dir=bin_dir)
    assert code == 0, out
    # The module installer's exact requirement: $MAGISKBIN/util_functions.sh.
    assert (bin_dir / "util_functions.sh").is_file(), out
    # busybox + the magisk binary must be staged too.
    assert (bin_dir / "busybox").is_file()
    assert (bin_dir / "magisk").is_file()
    # The throwaway extraction dir must be cleaned up.
    assert not (bin_dir / ".apk_extract").exists()
    assert "Magisk env ready" in out


def test_idempotent_skip_when_already_populated(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    bin_dir = tmp_path / "magiskbin"
    bin_dir.mkdir()
    sentinel = bin_dir / "util_functions.sh"
    sentinel.write_text("PRE-EXISTING")
    code, out = _run_helper(src=src, bin_dir=bin_dir)
    assert code == 0, out
    assert "already populated" in out
    # Must not overwrite an existing install.
    assert sentinel.read_text() == "PRE-EXISTING"
    assert not (bin_dir / "busybox").exists(), "helper re-staged despite the skip"


def test_missing_source_dir_warns_and_exits_clean(tmp_path: Path) -> None:
    bin_dir = tmp_path / "magiskbin"
    code, out = _run_helper(src=tmp_path / "nope", bin_dir=bin_dir)
    assert code == 0, out
    assert "Magisk source dir" in out
    assert not (bin_dir / "util_functions.sh").exists()


def test_missing_apk_leaves_env_incomplete_without_aborting(tmp_path: Path) -> None:
    src = _make_src(tmp_path, with_apk=False)
    bin_dir = tmp_path / "magiskbin"
    code, out = _run_helper(src=src, bin_dir=bin_dir)
    assert code == 0, out
    # Binaries copied, but no util_functions.sh (it only lives in the apk).
    assert (bin_dir / "busybox").is_file()
    assert not (bin_dir / "util_functions.sh").exists()
    assert "incomplete" in out.lower()
