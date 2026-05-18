#!/system/bin/sh
# launch-frida.sh — launch the bind-mounted frida-server in the background.
#
# Frida is bind-mounted from the host (the CLI downloads + decompresses it
# into the instance dir, then compose mounts it ro). This helper just
# checks the binary exists and is executable, and starts it as a detached
# child of the entrypoint shell so its stderr streams to `docker compose
# logs`.
#
# Env vars (all optional, defaults shown):
#   BEETROOT_FRIDA_BIN=/data/local/tmp/frida-server
#     Container path to the frida-server binary. v0.4 (§3.1 of
#     docs/design/stealth-posture.md) randomizes this to a per-build path
#     under /data/adb/modules/<random>/ without editing this script.
#
# Idempotent: launching twice would bind to the same port and fail, so we
# don't guard against that here — init only runs us once per boot. The
# helper exits 0 either way (binary missing is a warning, not an error).

FRIDA_BIN="${BEETROOT_FRIDA_BIN:-/data/local/tmp/frida-server}"

if [ -x "$FRIDA_BIN" ]; then
    echo "[*] Launching Frida from $FRIDA_BIN"
    "$FRIDA_BIN" &
else
    echo "[!] $FRIDA_BIN missing or not executable — Frida not launched."
    echo "[!] The host CLI is responsible for staging frida-server at this path."
fi
