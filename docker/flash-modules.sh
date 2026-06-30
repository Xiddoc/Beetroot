#!/system/bin/sh
# flash-modules.sh — install every Magisk module zip staged under the
# modules directory.
#
# The host CLI bind-mounts the user-selected zips into the container; this
# helper iterates them and calls `magisk --install-module` on each one.
#
# Env vars (all optional, defaults shown):
#   BEETROOT_MODULES_DIR=/data/adb/modules_update
#     Container path where module zips are staged. v0.4 T4 (see
#     docs/design/stealth-posture.md §3.5) replaces the
#     Beetroot-invented /flash_dir with Magisk's well-known staging
#     directory. The POSIX ${VAR:-default} fallback below matches what
#     render_env emits so a bare ``docker run`` without a
#     Beetroot-rendered .env still lands on the same path.
#
# Idempotent: Magisk handles re-install of an already-installed module
# gracefully, so init re-triggering this script is safe.
set -eu # fail fast on undefined vars and unhandled errors (T3).

MODULES_DIR="${BEETROOT_MODULES_DIR:-/data/adb/modules_update}"

# This script is sourced by entrypoint.sh (`. /flash-modules.sh`), so
# any `exit` here would terminate the sourcing parent shell and skip
# every helper that runs after us (currently `launch-frida.sh` and the
# trailing `wait`). Fall through with an `if [ -d ]` guard instead.
if [ -d "$MODULES_DIR" ]; then
    for zip in "$MODULES_DIR"/*.zip; do
        if [ -f "$zip" ]; then
            echo "[*] Flashing module: $zip"
            # `|| echo` keeps a bad module from aborting boot: this file
            # is sourced under the entrypoint's `set -e`, so a bare
            # non-zero exit here would terminate the sourcing parent shell
            # and skip launch-frida.sh and the trailing `wait`.
            magisk --install-module "$zip" ||
                echo "[!] Module $zip failed to install — continuing."
        fi
    done
else
    echo "[!] Modules directory $MODULES_DIR not present — skipping flash step."
fi
