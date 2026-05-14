#!/system/bin/sh

# No need to start /init here; it's already running as PID 1
echo "[*] Android boot detected. Applying Stealth Configuration..."

# Wait for the Magisk daemon to be ready (its sqlite is what we use below).
while ! magisk --sqlite "SELECT 1" >/dev/null 2>&1; do sleep 1; done

echo "[*] Applying Stealth Configuration..."
# 1 = Enable, 0 = Disable
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('denylist', 1);"

# Silence GMS components
magisk --sqlite "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('com.google.android.gms', 'com.google.android.gms');"
magisk --sqlite "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('com.google.android.gms.unstable', 'com.google.android.gms.unstable');"

# Install Research Modules (Shamiko, etc.)
MODULE_DIR="/flash_dir"
if [ -d "$MODULE_DIR" ]; then
    for zip in "$MODULE_DIR"/*.zip; do
        if [ -f "$zip" ]; then
            echo "[*] Flashing: $zip"
            magisk --install-module "$zip"
        fi
    done
fi

FRIDA_BIN=/data/local/tmp/frida-server
if [ -x "$FRIDA_BIN" ]; then
    echo "[*] Launching Frida from $FRIDA_BIN..."
    "$FRIDA_BIN" &
else
    echo "[!] $FRIDA_BIN missing or not executable — Frida not launched."
    echo "[!] The host CLI is responsible for staging frida-server at this path."
fi

echo "[*] Waiting, if you need to inspect logs"
wait

