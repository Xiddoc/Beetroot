#!/system/bin/sh

# No need to start /init here; it's already running as PID 1
echo "[*] Android boot detected. Applying Stealth Configuration..."

DB_PATH="/data/adb/magisk.db"
# Wait specifically for the database to exist
while [ ! -f "$DB_PATH" ]; do sleep 1; done

echo "[*] Applying Stealth Configuration..."
# 1 = Enable, 0 = Disable
sqlite3 $DB_PATH "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);"
sqlite3 $DB_PATH "REPLACE INTO settings (key, value) VALUES ('denylist', 1);"

# Silence GMS components
sqlite3 $DB_PATH "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('com.google.android.gms', 'com.google.android.gms');"
sqlite3 $DB_PATH "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('com.google.android.gms.unstable', 'com.google.android.gms.unstable');"

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

echo "[*] Launching Frida..."
/system/bin/frida-server &

echo "[*] Waiting, if you need to inspect logs"
wait

