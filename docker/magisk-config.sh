#!/system/bin/sh
# magisk-config.sh — write Beetroot's stealth posture into Magisk's sqlite DB.
#
# Waits for the Magisk daemon to be reachable, then enables Zygisk and the
# denylist, and enrols the GMS packages so root is hidden from them.
#
# Container paths are read from env vars with safe defaults so v0.4 stealth
# work (see docs/design/stealth-posture.md) can randomize them without
# touching this script.
#
# Env vars (all optional, defaults shown):
#   BEETROOT_MAGISK_DB=/data/adb/magisk.db
#     Informational only — `magisk --sqlite` always targets this path
#     internally. Exported here so v0.4 can flag-gate alternate DB paths.
#
# Idempotent: REPLACE INTO and INSERT OR IGNORE both no-op on re-run.
set -eu  # fail fast on undefined vars and unhandled errors (T3).

MAGISK_DB="${BEETROOT_MAGISK_DB:-/data/adb/magisk.db}"

echo "[*] Waiting for Magisk daemon (db: $MAGISK_DB)..."
while ! magisk --sqlite "SELECT 1" >/dev/null 2>&1; do
    sleep 1
done

echo "[*] Enabling Zygisk + denylist"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('denylist', 1);"

echo "[*] Adding GMS packages to denylist"
magisk --sqlite "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('com.google.android.gms', 'com.google.android.gms');"
magisk --sqlite "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('com.google.android.gms.unstable', 'com.google.android.gms.unstable');"
