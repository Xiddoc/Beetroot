#!/system/bin/sh
# entrypoint.sh — boot-time glue triggered by stealth.rc on
# sys.boot_completed=1. Sources the helpers in order, then `wait`s. Container
# lifetime is owned by /init (PID 1), not this entrypoint; the trailing `wait`
# only blocks while Frida — the sole backgrounded child (launch-frida.sh) — is
# running, and returns immediately when no Frida server was started. See
# docs/how-it-works/boot-scripts.md for each helper's contract.
# shellcheck disable=SC1091  # helpers live at container root, not this tree.
set -eu # fail fast on undefined vars and unhandled errors (T3).
echo "[*] Android boot detected. Applying Beetroot configuration..."
. /magisk-path.sh
. /magisk-config.sh
. /magisk-env.sh
. /flash-modules.sh
. /activate-zygisk.sh
. /launch-frida.sh
echo "[*] Configuration done; waiting on child processes."
wait
