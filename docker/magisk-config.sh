#!/system/bin/sh
# magisk-config.sh — write Beetroot's stealth posture into Magisk's sqlite DB.
#
# Waits (bounded) for the Magisk daemon to be reachable, then enables Zygisk
# and the denylist, and enrols the configured packages so root is hidden from
# them. If the daemon never answers within the wait budget, the helper exits 1
# — and because entrypoint.sh sources this file under `set -eu`, that aborts
# the whole boot configuration loudly instead of hanging forever in a
# container that looks "up" (issue #14).
#
# Container paths are read from env vars with safe defaults so v0.4 stealth
# work (see docs/design/stealth-posture.md) can randomize them without
# touching this script.
#
# Env vars (all optional, defaults shown):
#   BEETROOT_MAGISK_DB=/data/adb/magisk.db
#     Informational only — `magisk --sqlite` always targets this path
#     internally. Echoed in the waiting log so the user knows which DB
#     we're targeting when v0.4 stealth-posture work randomises it.
#   BEETROOT_DENYLIST_PACKAGES=
#     Comma-separated list of Android package ids to enrol in Magisk's
#     denylist (per-package SQL-injection prophylaxis lives in pydantic;
#     see Stealth._check_packages in src/beetroot/config.py). Empty by
#     default — the helper SQL'es nothing extra.
#   BEETROOT_MAGISK_WAIT_SECS=120
#     Upper bound (in 1-second probe attempts) on the Magisk daemon wait.
#     Conservative because a first boot of redroid+Magisk can legitimately
#     take a while. Not passed through compose — a test / escape-hatch knob.
#
# Idempotent: REPLACE INTO and INSERT OR IGNORE both no-op on re-run.
set -eu  # fail fast on undefined vars and unhandled errors (T3).

MAGISK_DB="${BEETROOT_MAGISK_DB:-/data/adb/magisk.db}"
DENYLIST_PACKAGES="${BEETROOT_DENYLIST_PACKAGES:-}"
MAGISK_WAIT_SECS="${BEETROOT_MAGISK_WAIT_SECS:-120}"

echo "[*] Waiting for Magisk daemon (db: $MAGISK_DB)..."
waited=0
while ! magisk --sqlite "SELECT 1" >/dev/null 2>&1; do
    waited=$((waited + 1))
    if [ "$waited" -ge "$MAGISK_WAIT_SECS" ]; then
        echo "[!] Magisk daemon unreachable after ${MAGISK_WAIT_SECS}s — Magisk is broken or missing. Aborting boot configuration." >&2
        exit 1
    fi
    sleep 1
done

echo "[*] Enabling Zygisk + denylist"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('denylist', 1);"

# Verify Zygisk actually landed in the settings table. A silent
# regression here (e.g. Magisk renames its settings schema, or the
# helper races the daemon's own first-boot write) would leave the
# container with denylist=1 but zygisk=0 and no root hiding — a
# user-visible behaviour change that v0.3 had no detection for.
# (T2 Agent 1 / Agent 2 F-9 / Agent 3 1.2.)
ZYGISK_VALUE="$(magisk --sqlite "SELECT value FROM settings WHERE key='zygisk';" | awk -F'=' '{print $NF}')"
if [ "$ZYGISK_VALUE" != "1" ]; then
    echo "[!] Magisk Zygisk setting did not take (got: '$ZYGISK_VALUE'). Aborting."
    exit 1
fi

# Iterate $DENYLIST_PACKAGES (comma-separated). Toybox sh has no array
# support; ``IFS=,`` + ``set --`` is the portable way to walk the
# list. Defend against the empty string explicitly — under ``set -u``
# (T3) a bare ``set -- $VAR`` on an empty value blows up with an
# unbound-variable error, and the loop body would otherwise run once
# with ``$pkg=""`` which would SQL-inject an empty package row.
if [ -n "$DENYLIST_PACKAGES" ]; then
    echo "[*] Adding denylist packages: $DENYLIST_PACKAGES"
    OLD_IFS="$IFS"
    IFS=,
    # shellcheck disable=SC2086  # word-splitting on IFS=, is the point.
    set -- $DENYLIST_PACKAGES
    IFS="$OLD_IFS"
    for pkg in "$@"; do
        # Skip empty fields produced by trailing / leading commas.
        # The pydantic regex in T1 already rejected empty entries at
        # config-load time, but defend at the boundary too in case a
        # hand-crafted .env arrives via the raw compose escape hatch.
        if [ -z "$pkg" ]; then
            continue
        fi
        magisk --sqlite "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('$pkg', '$pkg');"
    done
fi
