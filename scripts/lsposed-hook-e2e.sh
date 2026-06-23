#!/usr/bin/env bash
# LSPosed module-hook e2e driver.
#
# Proves an Xposed/LSPosed module's method hooks fire on a Beetroot instance:
# installs the hook-test module as an app, enables it in LSPosed's scope for a
# target package, and (after a reboot) asserts BEETROOT_HOOK_FIRED appears in
# LSPosed's module log when the target launches.
#
# Prerequisites: the instance is booted with the Vector/LSPosed framework active
# (lspd running, Zygisk injected). A Zygisk module goes live on the *second*
# boot, so the caller is expected to have flashed Vector and rebooted once
# already (see docs/guides/lsposed.md).
#
# Usage — two phases around a reboot the caller performs:
#   lsposed-hook-e2e.sh setup <adb-serial> <module.apk> <target-pkg>
#   # ... caller reboots the instance (e.g. `beetroot restart <name>`) ...
#   lsposed-hook-e2e.sh check <adb-serial> <target-pkg>
#
# `check` exits 0 iff BEETROOT_HOOK_FIRED <…> pkg=<target> is found.
#
# Root: the driver runs `adb root` first (redroid is debuggable), so the adb
# shell is already root — commands are passed as a SINGLE string to
# `adb shell` (adb joins argv with spaces and drops quoting, so `sh -c "…"`
# would break).
set -euo pipefail

_sh() { adb -s "$SERIAL" shell "$1"; }

MODULE_PKG="party.beetroot.hooktest"
LSPD_LOG='/data/adb/lspd/log/modules_*.log'

cmd_setup() {
    local apk="$1" target="$2"
    echo "[*] installing hook module: $apk"
    adb -s "$SERIAL" install -r "$apk"
    echo "[*] enabling '$MODULE_PKG' in LSPosed scope for '$target'"
    # LSPosed stores enabled modules + scope in this SQLite DB; sqlite3 ships in
    # the redroid system image.
    _sh "DB=/data/adb/lspd/config/modules_config.db; \
         APK=\$(pm path $MODULE_PKG | sed 's/package://'); \
         sqlite3 \"\$DB\" \"INSERT OR REPLACE INTO modules(module_pkg_name, apk_path, enabled, auto_include) VALUES('$MODULE_PKG', '\$APK', 1, 0);\"; \
         sqlite3 \"\$DB\" \"INSERT OR IGNORE INTO scope(mid, app_pkg_name, user_id) SELECT mid, '$target', 0 FROM modules WHERE module_pkg_name='$MODULE_PKG';\"; \
         echo 'enabled modules:'; sqlite3 \"\$DB\" 'SELECT module_pkg_name FROM modules WHERE enabled=1;'; \
         echo 'scope:'; sqlite3 \"\$DB\" 'SELECT app_pkg_name FROM scope;'"
    echo "[*] setup done — reboot the instance, then run: $0 check $SERIAL $target"
}

cmd_check() {
    local target="$1"
    echo "[*] launching $target to trigger the hook"
    _sh "am force-stop $target" || true
    _sh "am start -n $target/.Settings >/dev/null 2>&1 || monkey -p $target -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true"
    # Give the emulated guest time to start the activity (slow under TCG).
    local deadline=$(($(date +%s) + 90))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if _sh "cat $LSPD_LOG 2>/dev/null" | grep -q "BEETROOT_HOOK_FIRED .* pkg=$target"; then
            echo "[+] PASS — module hook fired:"
            _sh "cat $LSPD_LOG 2>/dev/null" | grep "BEETROOT_HOOK" | tail -5
            return 0
        fi
        sleep 5
    done
    echo "[!] FAIL — BEETROOT_HOOK_FIRED not found for $target. Module log tail:"
    _sh "cat $LSPD_LOG 2>/dev/null" | grep -i "BEETROOT_HOOK\|hooktest" | tail -10 || true
    return 1
}

main() {
    local sub="${1:?usage: $0 setup|check <serial> ...}"
    SERIAL="${2:?adb serial required (e.g. localhost:5555)}"
    adb connect "$SERIAL" >/dev/null 2>&1 || true
    adb -s "$SERIAL" root >/dev/null 2>&1 || true
    sleep 2
    adb connect "$SERIAL" >/dev/null 2>&1 || true
    case "$sub" in
    setup) cmd_setup "${3:?module apk}" "${4:?target pkg}" ;;
    check) cmd_check "${3:?target pkg}" ;;
    *)
        echo "unknown subcommand: $sub" >&2
        exit 2
        ;;
    esac
}

main "$@"
