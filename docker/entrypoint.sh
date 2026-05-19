#!/system/bin/sh
# entrypoint.sh — boot-time glue triggered by stealth.rc on
# sys.boot_completed=1. Sources three helpers in order, then blocks on
# `wait` so logs stream to `docker compose logs`. See
# docs/how-it-works/boot-scripts.md for each helper's contract.
# shellcheck disable=SC1091  # helpers live at container root, not this tree.
set -eu  # fail fast on undefined vars and unhandled errors (T3).
echo "[*] Android boot detected. Applying Beetroot configuration..."
. /magisk-config.sh
. /flash-modules.sh
. /launch-frida.sh
echo "[*] Configuration done; waiting on child processes."
wait
