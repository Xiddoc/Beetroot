# LSPosed / Vector (Xposed framework)

[LSPosed](https://github.com/JingMatrix/LSPosed) is a Zygisk-based implementation of the
Xposed framework — it injects into Zygote so Xposed/LSPosed modules can hook any app's
Java/Kotlin (and, via their bundled native libs, ART) at runtime. **Vector** is JingMatrix's
actively-maintained successor (the project rebranded LSPosed → Vector at v2.0); it ships the
same `org.lsposed.lspd` daemon and is what this guide pins.

Because Beetroot already flashes Magisk/Zygisk modules declaratively and activates Zygisk on
boot (see [Magisk Modules](modules.md) and [Boot Flow](../how-it-works/boot-flow.md)), Vector
is "just another module" — there's no manual Magisk-app tap-through to install the framework.
What this guide adds is the other half of a *real* Xposed test: installing an Xposed module as
a normal **app** (so the package manager populates its `nativeLibraryDir` — the thing
LSPatch-embedded patching can't do, see [rosetta-xposed#25](https://github.com/Xiddoc/rosetta-xposed/issues/25)),
and enabling it in LSPosed's **scope** for a target app — non-interactively, so it's automatable.

!!! info "Verified on the `binder: vm` TCG VM"
    The flash → reboot → activate flow below was verified end-to-end on the QEMU micro-VM
    backend (`beetroot modes` → `binder: vm, TCG`): the `lspd` daemon comes up, Zygisk is
    injected into `zygote64` and `system_server`, and the scope DB schema below is the live
    one (`modules_config.db` v4).

## 1. Flash the framework

Add Vector to your instance's `modules:` (or copy [`examples/lsposed.yaml`](https://github.com/Xiddoc/Beetroot/blob/master/examples/lsposed.yaml)
over a fresh `beetroot.yaml`):

```yaml
api_version: 5

android:
  version: 14

modules:
  - url: https://github.com/JingMatrix/Vector/releases/download/v2.0/Vector-v2.0-3021-Release.zip
    sha256: d5e39669c02c2c699ab948eb8f3639b348eefb7749553224a9c62fa4a2f2dc18

magisk:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

When a newer Vector is cut, look it up at
<https://github.com/JingMatrix/Vector/releases>, swap the URL, and recompute the digest with
`curl -sL <url> | sha256sum`.

```bash
beetroot apply <name>     # downloads + sha256-verifies the module into the instance
beetroot up <name>
```

## 2. Activate Zygisk (the second-boot rule)

Zygisk injects Zygote **at Zygote start**, which happens early in boot — before the framework
can be flashed/enabled on the very first boot. So a freshly-flashed Zygisk module (LSPosed
included) goes live on the **next** boot, when magiskd reads the persisted `zygisk=1` at
`post-fs-data` and injects Zygote at its first start. In practice:

```bash
beetroot restart <name>   # second boot → Zygisk active, lspd daemon starts
```

Confirm the framework is up:

```bash
beetroot shell <name> -c 'logcat -d | grep -iE "lspd|Vector daemon"'
# I LSPosedService: Vector daemon started: lateInject: false
# I LSPosedService: version 2.0 (3021)
beetroot shell <name> -c 'ps -A | grep lspd'   # the daemon process
```

!!! warning "`binder: vm` needs persistent `/data`"
    The second-boot activation requires `/data` to survive a reboot (so `zygisk=1` and the
    flashed module persist). The redroid `binder: host`/`auto` backend bind-mounts the
    instance's `data/` to `/data`, so this is automatic. The `binder: vm` micro-VM persists
    redroid's `/data` on the guest rootfs (a directory bind-mounted into the redroid
    container; override with `BEETROOT_GUEST_DATA_DIR`) — also automatic, but note the
    `vm.boot_cache` warm-start *reverts* to its checkpoint each resume (discarding the flashed
    module, `zygisk=1`, and `modules_config.db` written after that checkpoint), so leave
    `boot_cache: false` while iterating on a persistent LSPosed setup. Beetroot prints a runtime
    warning on every warm resume to flag the `/data` reset.

## 3. Install an Xposed module as an app

This is the step that makes the module's native libraries loadable: installing the module's
APK through the package manager populates its `nativeLibraryDir`, so `System.loadLibrary(...)`
inside the module resolves on ART (exactly what an LSPatch-embedded module cannot do).

```bash
adb connect localhost:5555            # or: beetroot shell <name>
adb -s localhost:5555 install /path/to/your-xposed-module.apk
```

## 4. Enable the module in scope (non-interactive)

LSPosed stores enabled modules and their per-app scope in a SQLite database at
`/data/adb/lspd/config/modules_config.db`. Two tables matter:

```sql
-- which modules are enabled
modules(mid INTEGER PK, module_pkg_name TEXT UNIQUE, apk_path TEXT, enabled 0|1, auto_include 0|1)
-- which target apps each enabled module hooks
scope(mid INTEGER, app_pkg_name TEXT, user_id INTEGER)   -- PK(mid, app_pkg_name, user_id)
```

To enable module `com.example.module` for target app `com.example.target` (user 0),
non-interactively:

```bash
beetroot shell <name> -c 'su 0 sh -c "
  DB=/data/adb/lspd/config/modules_config.db
  APK=\$(pm path com.example.module | sed s/package://)
  sqlite3 \$DB \"INSERT OR REPLACE INTO modules(module_pkg_name, apk_path, enabled, auto_include)
                 VALUES (\\\"com.example.module\\\", \\\"\$APK\\\", 1, 0);\"
  MID=\$(sqlite3 \$DB \"SELECT mid FROM modules WHERE module_pkg_name=\\\"com.example.module\\\";\")
  sqlite3 \$DB \"INSERT OR IGNORE INTO scope(mid, app_pkg_name, user_id)
                 VALUES (\$MID, \\\"com.example.target\\\", 0);\"
"'
```

!!! note "Magisk ships its own sqlite"
    If `sqlite3` isn't on the device, use `magisk --sqlite "<statement>"` against the same
    file path, or pre-seed `modules_config.db` on the host and push it into place. LSPosed reads
    its config at daemon start, so apply scope changes **before** the second boot, or
    `beetroot restart <name>` after writing them.

## 5. Launch the target and observe hooks

```bash
beetroot shell <name> -c 'monkey -p com.example.target -c android.intent.category.LAUNCHER 1'
beetroot shell <name> -c 'logcat -d | grep -iE "LSPosed|your-module-tag"'
```

When the module's hooks fire you'll see its log lines and the LSPosed "loaded module" entries
under `/data/adb/lspd/log/modules_*.log`. Note that a module's `XposedBridge.log(...)` output
lands in that LSPosed module log (not `adb logcat`), which is the reliable channel to assert
on headlessly.

## Automated end-to-end test

Beetroot ships a self-contained module-hook e2e so you can prove the whole pipeline works on a
fresh instance:

- **`tests/fixtures/xposed-hook-module/`** — a minimal prebuilt Xposed module
  (`party.beetroot.hooktest`) that hooks `android.app.Activity` `onCreate`/`onResume` in any app
  it's scoped to and logs `BEETROOT_HOOK_FIRED`. Sources + `build.sh` are included so you can
  rebuild it.
- **`scripts/lsposed-hook-e2e.sh`** — drives the flow against a booted, Vector-active instance:

  ```bash
  # 1) flash Vector (examples/lsposed.yaml), `beetroot up`, `beetroot restart` (2nd boot → active)
  scripts/lsposed-hook-e2e.sh setup localhost:5555 \
      tests/fixtures/xposed-hook-module/beetroot-hook-test.apk com.android.settings
  beetroot restart <name>                       # lspd loads the newly-scoped module
  scripts/lsposed-hook-e2e.sh check localhost:5555 com.android.settings
  # [+] PASS — module hook fired: BEETROOT_HOOK_FIRED onResume pkg=com.android.settings
  ```

This is **verified on the `binder: vm` TCG VM**: launching Settings produced
`BEETROOT_HOOK_FIRED onCreate` and `onResume`, proving a real Xposed method hook fired on a
Beetroot instance end-to-end. The `e2e.yml` `tier-lsposed-hook` job runs the same flow in CI.

!!! tip "Building a module against the Vector/LSPosed API"
    Vector obfuscates the `de.robv.android.xposed.*` API and remaps your module's references to
    its internal classes at load. Compile against the API as `compileOnly` (don't package it),
    and match the **exact** signatures — e.g. `XposedHelpers.findAndHookMethod` returns
    `XC_MethodHook.Unhook`. A wrong return type compiles fine but throws `NoSuchMethodError` at
    runtime because the full (remapped) method descriptor no longer matches. See the fixture's
    `stubs/`.

## Why a Beetroot rooted phone, not LSPatch on a vanilla AVD

`System.loadLibrary("dexkit")` (and any module that ships a native `.so`) needs the library
extracted into a real `nativeLibraryDir`. LSPatch-embedded patching doesn't populate that, so
the native load fails (see [rosetta-xposed#25](https://github.com/Xiddoc/rosetta-xposed/issues/25)).
A Beetroot instance installs the module as a normal app, so the package manager populates
`nativeLibraryDir` and the native side loads on ART — turning that issue's hardest approach
("real LSPosed install") into the easy default.
