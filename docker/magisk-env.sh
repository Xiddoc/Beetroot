#!/system/bin/sh
# magisk-env.sh — populate Magisk's binary directory (MAGISKBIN) so that
# `magisk --install-module` works headlessly.
#
# The redroid-script Magisk image bakes the Magisk binaries into
# MAGISK_SRC_DIR (/system/etc/init/magisk) and its bootanim.rc only `mkdir`s
# MAGISKBIN (/data/adb/magisk) *empty* at boot. On a real phone the Magisk
# **app** finishes the install the first time a human opens it — it copies the
# binaries and extracts the per-install shell scripts (util_functions.sh,
# module_installer.sh, …) out of magisk.apk into MAGISKBIN. Headless redroid
# never runs that app flow, so MAGISKBIN stays empty and every
# `magisk --install-module` aborts with "Incomplete Magisk install" (the
# installer needs /data/adb/magisk/util_functions.sh). This helper replicates
# the app's environment-fix headlessly so flash-modules.sh can install
# modules. entrypoint.sh sources it BEFORE flash-modules.sh for that reason.
#
# Env vars (all optional, defaults shown):
#   BEETROOT_MAGISK_BIN_DIR=/data/adb/magisk
#     MAGISKBIN — the data-side Magisk install the module installer reads.
#   BEETROOT_MAGISK_SRC_DIR=/system/etc/init/magisk
#     Where redroid-script baked the Magisk binaries + magisk.apk at build
#     time (MAGISKSYSTEMDIR in its bootanim.rc).
#
# Idempotent: skips when MAGISKBIN already holds util_functions.sh (the exact
# file module_installer.sh checks). Sourced by entrypoint.sh under `set -e`,
# so — like the other helpers — it must never `exit`: an `exit` here would
# terminate the sourcing parent shell and skip every later helper
# (flash-modules.sh, launch-frida.sh, and the trailing `wait`). Every branch
# falls through and external commands that could fail are guarded.
set -eu # fail fast on undefined vars and unhandled errors (T3).

MAGISK_BIN_DIR="${BEETROOT_MAGISK_BIN_DIR:-/data/adb/magisk}"
MAGISK_SRC_DIR="${BEETROOT_MAGISK_SRC_DIR:-/system/etc/init/magisk}"

if [ -f "$MAGISK_BIN_DIR/util_functions.sh" ]; then
    echo "[*] Magisk env already populated at $MAGISK_BIN_DIR — skipping."
elif [ ! -d "$MAGISK_SRC_DIR" ]; then
    echo "[!] Magisk source dir $MAGISK_SRC_DIR missing — cannot populate $MAGISK_BIN_DIR; module install will fail."
else
    echo "[*] Populating Magisk env at $MAGISK_BIN_DIR from $MAGISK_SRC_DIR"
    mkdir -p "$MAGISK_BIN_DIR"

    # Copy the Magisk binaries redroid-script staged in MAGISK_SRC_DIR.
    # busybox is the hard requirement (the module installer runs install.sh in
    # busybox's standalone ash); the rest are copied best-effort so a future
    # fork that ships magisk32/magisk64 splits still lands a complete install.
    for bin in busybox magisk magiskboot magiskpolicy magiskinit init-ld magisk32 magisk64; do
        if [ -f "$MAGISK_SRC_DIR/$bin" ]; then
            cp -f "$MAGISK_SRC_DIR/$bin" "$MAGISK_BIN_DIR/$bin" &&
                chmod 755 "$MAGISK_BIN_DIR/$bin" ||
                echo "[!] Failed to stage $bin into $MAGISK_BIN_DIR."
        fi
    done

    # The per-install shell scripts live ONLY inside magisk.apk's assets/.
    # busybox unzip extracts them; copy the flat scripts into MAGISKBIN where
    # module_installer.sh expects them (it sources $MAGISKBIN/util_functions.sh).
    apk="$MAGISK_SRC_DIR/magisk.apk"
    tmp="$MAGISK_BIN_DIR/.apk_extract"
    if [ -f "$apk" ] && [ -x "$MAGISK_BIN_DIR/busybox" ]; then
        rm -rf "$tmp"
        mkdir -p "$tmp"
        if "$MAGISK_BIN_DIR/busybox" unzip -o "$apk" 'assets/*' -d "$tmp" >/dev/null 2>&1; then
            # Explicit `if` (not `[ -f ] && cp`): a non-matching glob would
            # leave the literal pattern and make `[ ... ] && cp` return
            # non-zero, which under the entrypoint's `set -e` would abort the
            # whole boot. The `if` form returns 0 when the glob matches nothing.
            for script in "$tmp"/assets/*.sh; do
                if [ -f "$script" ]; then
                    cp -f "$script" "$MAGISK_BIN_DIR/"
                fi
            done
        else
            echo "[!] Failed to extract Magisk scripts from $apk — module install may fail."
        fi
        rm -rf "$tmp"
    else
        echo "[!] $apk or busybox missing — cannot stage Magisk scripts; module install may fail."
    fi

    if [ -f "$MAGISK_BIN_DIR/util_functions.sh" ]; then
        echo "[*] Magisk env ready at $MAGISK_BIN_DIR."
    else
        echo "[!] Magisk env incomplete — $MAGISK_BIN_DIR/util_functions.sh not present after staging; module install will fail."
    fi
fi
