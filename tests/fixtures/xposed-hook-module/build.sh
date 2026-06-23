#!/usr/bin/env bash
# Rebuild beetroot-hook-test.apk — the minimal Xposed/LSPosed module used by the
# LSPosed module-hook e2e (docs/guides/lsposed.md). The module hooks
# android.app.Activity onCreate/onResume in any app it's scoped to and writes
# BEETROOT_HOOK_FIRED to LSPosed's module log, so the e2e can assert that a real
# method hook fired.
#
# The committed .apk is the artifact the e2e installs; this script documents how
# it was produced and lets you regenerate it. Requires a JDK and Android
# build-tools (aapt2, d8, zipalign, apksigner) + an android.jar platform.
#
#   ANDROID_BT=/path/to/build-tools  ANDROID_JAR=/path/to/android.jar  ./build.sh
#
# Notes:
#   * The de.robv.android.xposed.* classes under stubs/ are compile-only — the
#     LSPosed/Vector framework provides the real (obfuscated) implementations at
#     runtime, so they are passed to d8 as --lib (NOT packaged into the dex).
#     Their signatures must match the real API exactly: in particular
#     XposedHelpers.findAndHookMethod returns XC_MethodHook.Unhook (getting the
#     return type wrong yields a runtime NoSuchMethodError because Vector matches
#     the full method descriptor after remapping de.robv.* to its obfuscated
#     names).
#   * --release 8 keeps the bytecode at a version d8 accepts.
set -euo pipefail
cd "$(dirname "$0")"

BT="${ANDROID_BT:?set ANDROID_BT to an Android build-tools dir (aapt2, d8, zipalign, apksigner)}"
AJ="${ANDROID_JAR:?set ANDROID_JAR to an android.jar platform}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# shellcheck disable=SC2046  # intentional word-split: pass every .java to javac
javac --release 8 -cp "$AJ" -d "$work/classes" $(find src stubs -name '*.java')
(cd "$work/classes" && jar cf "$work/module.jar" party && jar cf "$work/stubs.jar" de)
"$BT/d8" --min-api 21 --lib "$AJ" --lib "$work/stubs.jar" --output "$work" "$work/module.jar"
"$BT/aapt2" link -I "$AJ" --manifest AndroidManifest.xml -o "$work/base.apk"
mkdir -p "$work/assets" && cp assets/xposed_init "$work/assets/xposed_init"
(cd "$work" && zip -q base.apk classes.dex assets/xposed_init)
"$BT/zipalign" -p -f 4 "$work/base.apk" "$work/aligned.apk"
if [ ! -f debug.ks ]; then
    keytool -genkeypair -keystore debug.ks -storepass android -keypass android \
        -alias d -dname "CN=Beetroot" -keyalg RSA -validity 10000
fi
"$BT/apksigner" sign --ks debug.ks --ks-pass pass:android --key-pass pass:android \
    --out beetroot-hook-test.apk "$work/aligned.apk"
"$BT/apksigner" verify beetroot-hook-test.apk
echo "ok: beetroot-hook-test.apk ($(wc -c <beetroot-hook-test.apk) bytes)"
