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
#     see Magisk._check_packages in src/beetroot/config.py). Empty by
#     default — the helper SQL'es nothing extra.
#   BEETROOT_MAGISK_WAIT_SECS=120
#     Upper bound (in 1-second probe attempts) on the Magisk daemon wait.
#     Conservative because a first boot of redroid+Magisk can legitimately
#     take a while. Not passed through compose — a test / escape-hatch knob.
#
# Idempotent: REPLACE INTO and INSERT OR IGNORE both no-op on re-run.
set -eu # fail fast on undefined vars and unhandled errors (T3).

MAGISK_DB="${BEETROOT_MAGISK_DB:-/data/adb/magisk.db}"
DENYLIST_PACKAGES="${BEETROOT_DENYLIST_PACKAGES:-}"
MAGISK_WAIT_SECS="${BEETROOT_MAGISK_WAIT_SECS:-120}"

echo "[*] Waiting for Magisk daemon (db: $MAGISK_DB)..."
waited=0
while ! magisk --sqlite "SELECT 1" >/dev/null 2>&1; do
    waited=$((waited + 1))
    if [ "$waited" -ge "$MAGISK_WAIT_SECS" ]; then
        echo "[!] Magisk daemon unreachable after ${MAGISK_WAIT_SECS} attempts (~${MAGISK_WAIT_SECS}s) — Magisk is broken or missing. Aborting boot configuration." >&2
        exit 1
    fi
    sleep 1
done

echo "[*] Enabling Zygisk + denylist"
# Capture the prior zygisk value so activate-zygisk.sh knows whether this boot
# is the one that flips it on. Zygisk only injects zygote at zygote start, so a
# 0/missing → 1 transition this boot means the running zygote predates Zygisk
# and needs a one-shot restart to activate it (and any flashed Zygisk module).
# ``magisk --sqlite`` prints each row as ``column=value``; we want the value.
# The prior ``magisk ... | awk -F= '{print $NF}'`` did the right extraction but
# put magisk *inside a pipeline*, so under ``set -eu`` the substitution's exit
# status was awk's (always 0) — a magisk failure after the liveness probe was
# silently masked, leaving PREV_ZYGISK empty and spuriously flagging Zygisk as
# newly enabled. Capture magisk's raw output in its own command substitution
# (so a magisk failure aborts the script), then strip the ``value=`` prefix
# with shell parameter expansion — no pipe (issue #239).
PREV_ZYGISK_ROW="$(magisk --sqlite "SELECT value FROM settings WHERE key='zygisk';")"
PREV_ZYGISK="${PREV_ZYGISK_ROW##*=}"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);"
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('denylist', 1);"
if [ "$PREV_ZYGISK" != "1" ]; then
    # Read by activate-zygisk.sh (sourced into the same entrypoint shell).
    BEETROOT_ZYGISK_NEWLY_ENABLED=1
    export BEETROOT_ZYGISK_NEWLY_ENABLED
fi

# Verify Zygisk actually landed in the settings table. A silent
# regression here (e.g. Magisk renames its settings schema, or the
# helper races the daemon's own first-boot write) would leave the
# container with denylist=1 but zygisk=0 and no root hiding — a
# user-visible behaviour change that v0.3 had no detection for.
# (T2 Agent 1 / Agent 2 F-9 / Agent 3 1.2.)
# Same structure as PREV_ZYGISK above: read into its own command substitution
# (so a magisk failure aborts under ``set -eu`` rather than being masked into
# an empty value that misreports as "setting did not take"), then strip the
# ``value=`` prefix without a pipe (issue #239).
ZYGISK_ROW="$(magisk --sqlite "SELECT value FROM settings WHERE key='zygisk';")"
ZYGISK_VALUE="${ZYGISK_ROW##*=}"
if [ "$ZYGISK_VALUE" != "1" ]; then
    echo "[!] Magisk reports zygisk='$ZYGISK_VALUE' after the REPLACE INTO (expected '1'); the setting did not persist. Aborting." >&2
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
    # Whitespace to strip from each field: a literal space and a literal
    # tab. Built via printf so no raw tab has to live in the source (which
    # would trip shfmt) — toybox printf honours \t.
    trim_ws="$(printf ' \t')"
    for pkg in "$@"; do
        # Trim leading / trailing space and tab so a spaced CSV like
        # ``com.foo, com.bar`` enrols ``com.bar`` and not `` com.bar`` — a
        # leading space never matches the real package, silently defeating
        # the denylist (issue #263). Toybox sh has no ``${var//}`` or a
        # coreutils ``xargs``, so strip one char at a time with POSIX
        # ``case`` + parameter expansion.
        while :; do
            case "$pkg" in
            [$trim_ws]*) pkg="${pkg#?}" ;;
            *) break ;;
            esac
        done
        while :; do
            case "$pkg" in
            *[$trim_ws]) pkg="${pkg%?}" ;;
            *) break ;;
            esac
        done
        # Skip empty fields produced by trailing / leading commas, or a
        # field that was pure whitespace and trimmed away to nothing. The
        # pydantic regex in T1 already rejected empty entries at config-load
        # time, but defend at the boundary too in case a hand-crafted .env
        # arrives via the raw compose escape hatch.
        if [ -z "$pkg" ]; then
            continue
        fi
        magisk --sqlite "INSERT OR IGNORE INTO denylist (package_name, process) VALUES ('$pkg', '$pkg');"
    done
fi
