#!/system/bin/sh
# activate-zygisk.sh — make a freshly-enabled Zygisk actually take effect.
#
# Zygisk injects itself into **zygote at zygote start**. magisk-config.sh
# enables Zygisk by writing zygisk=1 to Magisk's settings DB, but that runs on
# sys.boot_completed=1 — long after the first zygote has already started
# *without* Zygisk. So on the very first boot of a fresh instance the setting
# lands but Zygisk (and any Zygisk module just flashed, e.g. LSPosed) is not
# live until the next zygote restart. This helper performs that one-shot
# restart so a declarative `up` → module-flashed → active flow works without
# the user having to `beetroot restart`.
#
# Gated to run ONLY when Zygisk was *newly* enabled this boot
# (BEETROOT_ZYGISK_NEWLY_ENABLED=1, set by magisk-config.sh when the prior
# DB value was not already 1). On every later boot zygisk is already 1, so
# magiskd injects the first zygote and no restart is needed — this avoids
# churning zygote on routine restarts. Opt out entirely with
# BEETROOT_ZYGOTE_RESTART=0 (e.g. a backend where a mid-boot zygote restart is
# undesirable).
#
# Best-effort: sourced by entrypoint.sh under `set -e`, so it must never abort
# the boot — every branch falls through and setprop is guarded.
set -eu # fail fast on undefined vars and unhandled errors (T3).

RESTART_ENABLED="${BEETROOT_ZYGOTE_RESTART:-1}"
NEWLY_ENABLED="${BEETROOT_ZYGISK_NEWLY_ENABLED:-0}"

if [ "$RESTART_ENABLED" != "1" ]; then
    echo "[*] Zygote restart disabled (BEETROOT_ZYGOTE_RESTART=$RESTART_ENABLED) — skipping Zygisk activation."
elif [ "$NEWLY_ENABLED" != "1" ]; then
    echo "[*] Zygisk already active from boot — no zygote restart needed."
else
    echo "[*] Zygisk newly enabled — restarting zygote so it injects and Zygisk modules load."
    setprop ctl.restart zygote 2>/dev/null ||
        echo "[!] Could not restart zygote — Zygisk modules may need a 'beetroot restart' to activate."
fi
