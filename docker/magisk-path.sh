#!/system/bin/sh
# magisk-path.sh — make Magisk's `magisk` binary resolvable on PATH before
# any other helper runs.
#
# entrypoint.sh is launched by Android init (stealth.rc's exec_background),
# which inherits init's *default* service PATH —
# /system/bin:/system/xbin:/vendor/bin:/product/bin:... — and that does NOT
# include the directory Magisk installs its `magisk` binary into. On the
# redroid Magisk image that binary lives at /sbin/magisk (verified against a
# real booted image; the data-side MAGISKTMP mirror is /debug_ramdisk). Every
# helper calls bare `magisk` (magisk-config.sh's daemon wait, flash-modules.sh's
# `magisk --install-module`, …), so without this each `magisk` invocation fails
# with "not found": magisk-config.sh then spins its `magisk --sqlite "SELECT 1"`
# wait until it times out and exits 1, which aborts the whole entrypoint
# *before* Zygisk/denylist/MAGISKBIN/modules are ever configured. (Unit tests
# put a fake `magisk` on PATH, so this was invisible until a real boot.)
#
# Prepends the first candidate dir that actually holds an executable `magisk`
# (and is a no-op when `magisk` already resolves, e.g. a future image that puts
# it on PATH, or the test harness's fake on PATH). Candidate dirs come from
# BEETROOT_MAGISK_DIRS (colon-separated; default `/sbin:/debug_ramdisk`).
#
# Sourced FIRST by entrypoint.sh so every later helper inherits PATH. Like the
# other helpers it is sourced under `set -e` and must never `exit`.
set -eu # fail fast on undefined vars and unhandled errors (T3).

if ! command -v magisk >/dev/null 2>&1; then
    _old_ifs="$IFS"
    IFS=:
    # shellcheck disable=SC2086  # intentional word-split of the colon list.
    set -- ${BEETROOT_MAGISK_DIRS:-/sbin:/debug_ramdisk}
    IFS="$_old_ifs"
    for _dir in "$@"; do
        if [ -x "$_dir/magisk" ]; then
            PATH="$_dir:$PATH"
            export PATH
            echo "[*] Resolved magisk at $_dir/magisk; prepended to PATH."
            break
        fi
    done
fi
