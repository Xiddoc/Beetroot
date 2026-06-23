# Xposed hook-test module (e2e fixture)

A minimal Xposed/LSPosed module used by the **LSPosed module-hook e2e** to prove
that a real Xposed module's method hooks fire on a Beetroot instance.

When loaded into an app it is scoped to, it:

1. logs `BEETROOT_HOOK_LOADED pkg=<app>` from `handleLoadPackage` (proves the
   module's code runs inside the target process), then
2. hooks `android.app.Activity.onCreate`/`onResume` and logs
   `BEETROOT_HOOK_FIRED <where> pkg=<app>` from the hook callback (proves a real
   **method interception** fired).

All lines go through `XposedBridge.log`, so they land in LSPosed's module log
(`/data/adb/lspd/log/modules_*.log`) — the channel the e2e greps.

- `src/` — the module (`party.beetroot.hooktest.Init`).
- `stubs/` — compile-only Xposed API stubs; the framework provides the real
  (obfuscated) classes at runtime, so they're **not** packaged into the dex
  (see `build.sh`). Their signatures mirror the real API exactly.
- `assets/xposed_init` — names the module entry class (classic Xposed contract).
- `AndroidManifest.xml` — the `xposedmodule` meta-data marking it a module.
- `beetroot-hook-test.apk` — the prebuilt, signed fixture the e2e installs.
- `build.sh` — regenerates the apk from the sources (needs a JDK + Android
  build-tools + an android.jar).

**Verified** end-to-end on the `binder: vm` TCG VM: after flashing Vector
(LSPosed), installing this apk, enabling it in scope for `com.android.settings`,
and rebooting, launching Settings produced `BEETROOT_HOOK_FIRED onCreate` and
`BEETROOT_HOOK_FIRED onResume`.
