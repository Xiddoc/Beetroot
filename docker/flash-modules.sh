#!/system/bin/sh
# flash-modules.sh — install every Magisk module zip staged under the
# modules directory.
#
# The host CLI bind-mounts the user-selected zips into the container; this
# helper iterates them and calls `magisk --install-module` on each one.
#
# Env vars (all optional, defaults shown):
#   BEETROOT_MODULES_DIR=/flash_dir
#     Container path where module zips are staged. v0.4 (see
#     docs/design/stealth-posture.md §3.5) moves this to
#     /data/adb/modules_update by setting this var.
#
# Idempotent: Magisk handles re-install of an already-installed module
# gracefully, so init re-triggering this script is safe.

MODULES_DIR="${BEETROOT_MODULES_DIR:-/flash_dir}"

if [ ! -d "$MODULES_DIR" ]; then
    echo "[!] Modules directory $MODULES_DIR not present — skipping flash step."
    exit 0
fi

for zip in "$MODULES_DIR"/*.zip; do
    if [ -f "$zip" ]; then
        echo "[*] Flashing module: $zip"
        magisk --install-module "$zip"
    fi
done
