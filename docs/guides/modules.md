# Magisk Modules

Beetroot supports flashing Magisk modules into instances declaratively — declare them in `beetroot.yaml` and they're automatically downloaded, staged, and installed on the next boot.

## How module flashing works

At boot, `entrypoint.sh` (running inside the Android container under the `u:r:magisk:s0` SELinux context) iterates every `.zip` in `/data/adb/modules_update/` and calls `magisk --install-module <zip>` for each one. A module that fails to install is logged with a `[!]` warning and skipped — the remaining modules still flash and boot continues (Frida still launches). Check `beetroot logs <name>` for the warning. That container path is a read-only bind-mount of the instance directory's `modules/` subdirectory on the host — Magisk's well-known staging directory, so the modules are visible to the daemon the way a manually-side-loaded one would be.

(v0.3 used the Beetroot-invented `/flash_dir` mount target; v0.4 T4 moved the default to `/data/adb/modules_update` to drop the indicator from `/proc/mounts`. The `BEETROOT_MODULES_DIR` env var overrides the path if you need a different one.)

Beetroot's CLI mirrors the `modules:` list from `beetroot.yaml` into the instance directory's `modules/` when you run `beetroot create` or `beetroot apply`. URL-sourced modules are downloaded and verified; path-sourced modules are copied from the local filesystem.

## Declaring modules in YAML

```yaml
modules:
  - url: https://github.com/LSPosed/.../Shamiko-v0.7.4-426-release.zip
    sha256: abc123...  # optional but recommended

  - path: ./local-modules/MyResearchHook.zip
    # sha256: optional even for local files
```

- Use `url` for modules hosted remotely (GitHub releases, etc.).
- Use `path` for local zips. **Relative** paths are resolved against the instance directory itself and must stay inside it — a relative path that escapes the instance dir (e.g. `../../secret.zip`) is rejected. An **absolute** path is read as-is.
- `sha256` is optional but strongly recommended for URL modules: if the remote file changes (or the URL is hijacked), `beetroot apply` will refuse to stage a mismatched zip.

!!! warning "URL or path, not both"
    Each module entry must set exactly one of `url` or `path`. Setting both raises a validation error.

## Adding a module on the fly

```bash
beetroot module alpha https://github.com/LSPosed/LSPosed.github.io/releases/download/shamiko-426/Shamiko-v0.7.4-426-release.zip
```

or for a local zip:

```bash
beetroot module alpha ./local-modules/MyHook.zip
```

`beetroot module` appends the entry to the instance's `beetroot.yaml` and immediately re-stages the instance's `modules/` directory. You must restart for it to flash:

```bash
beetroot down alpha && beetroot up alpha
```

## Modules on adb-adopted devices

Instances registered with `beetroot adopt` have no `beetroot.yaml` and no boot-time staging — the `module` verb talks to the device directly over `adb`, in one of two modes.

### Safe default: push to Downloads

```bash
beetroot module phone ./MyHook.zip
```

The zip is pushed to `/sdcard/Download/MyHook.zip` and Beetroot prints a one-line instruction: install it from the Magisk app's **Modules** tab (*Install from storage*). Nothing on the device is modified beyond the copied file, and `--sha256` is ignored on this path — verify the hash yourself before invoking.

### `--auto-install`: root-driven install

```bash
beetroot module phone ./MyHook.zip --auto-install
```

For rooted devices where you don't want manual Magisk-app interaction. Each zip is pushed to a synthesized temp name under `/data/local/tmp/` (`beetroot-module-<N>.zip`, numbered by batch position — the local filename never reaches the device shell) and installed with `su -c magisk --install-module <zip>` — Magisk's own non-interactive install primitive (the same one Beetroot's redroid boot scripts use), which stages the module into `/data/adb/modules_update/<id>/` for the next reboot. The pushed temp zip is removed afterwards, even if the install step fails.

Several modules can be installed in one invocation, and `--sha256` is **enforced** here — a zip whose digest doesn't match is never pushed:

```bash
beetroot module phone ./Shamiko.zip ./MyHook.zip --auto-install \
    --sha256 <shamiko-hex> --sha256 <myhook-hex>
```

When pinning digests, repeat `--sha256` once per source, in the same order. Every module gets its own report line — `ok:` on stdout, `failed:` (with the reason) on stderr — and a failing module never aborts the rest of the batch:

```
[beetroot] failed: ./Shamiko.zip — sha256 mismatch for Shamiko.zip: expected ..., got ...
[beetroot] ok: ./MyHook.zip — installed via `su -c 'magisk --install-module /data/local/tmp/beetroot-module-1.zip'`
```

The verb exits `0` only if every module installed; any failure exits `1`, so scripted flows can gate on `$?`. Reboot the device for the staged modules to take effect.

Whole-device problems are diagnosed up front instead of drowning you in identical failed rows: before anything is pushed, a pre-flight probe checks that the device is reachable, that `su` works, and that the `magisk` binary is on the root PATH. A failed probe prints a single friendly `error: ...` line (e.g. ``error: device 'emulator-5554' is offline or not connected (reconnect it, accept its USB-debugging authorization prompt if one is shown, and check `adb devices`)``) and exits `1` without pushing a thing — an unauthorized device (one that hasn't accepted the USB-debugging prompt) is reported as offline/not-connected too, since it's equally unreachable until you approve it. Connectivity is always decided by re-running `adb devices` for the device's serial, never by matching the probe's error text, so a hostile module's stderr can't masquerade as a connectivity failure. A device that genuinely drops offline mid-batch aborts the remaining modules with the same offline diagnosis (the message names how many modules were skipped) — modules already installed keep their `ok:` rows. Host-side validation failures (a missing/non-zip path, or a `--sha256` mismatch) always stay per-module `failed:` rows and never abort the batch: they can't mean the device is offline.

!!! note "Redroid instances don't take `--auto-install`"
    Container instances flash their staged `modules/` directory at boot — there is nothing to auto-install at runtime, so the flag exits with code `2` (`BackendCapabilityError`) for them. Use the declarative `beetroot.yaml` flow above instead.

## Shamiko walk-through

[Shamiko](https://github.com/LSPosed/LSPosed.github.io) is the most commonly needed module — it upgrades Magisk's denylist from a "hide root access" mode to a full allowlist-based hide where denylisted processes can't detect Magisk at all.

1. Add Shamiko to your instance:

    ```bash
    beetroot module alpha \
        https://github.com/LSPosed/LSPosed.github.io/releases/download/shamiko-426/Shamiko-v0.7.4-426-release.zip
    ```

2. Pin the sha256 (recommended):

    ```bash
    sha256sum "$(beetroot ls --json | jq -r .alpha.path)/modules/Shamiko-v0.7.4-426-release.zip"
    ```

    Edit the instance's `beetroot.yaml` and add the hash to the module entry:

    ```yaml
    modules:
      - url: https://github.com/LSPosed/.../Shamiko-v0.7.4-426-release.zip
        sha256: <paste-hash-here>
    ```

    Then:

    ```bash
    beetroot apply alpha
    ```

3. Restart:

    ```bash
    beetroot down alpha && beetroot up alpha
    ```

4. Verify the module flashed:

    Open the Magisk app on the device and check the **Modules** tab —
    Shamiko should be listed and enabled. From a shell you can confirm
    the staged module directory exists:

    ```bash
    beetroot shell alpha
    # In the shell:
    ls /data/adb/modules/
    # A directory for the installed module (e.g. `zygisk_shamiko`)
    # should appear once it has flashed.
    ```

## Modules only flash once per boot

`entrypoint.sh` only iterates the modules-staging directory **once**, at boot time. If you add a module after a boot, you must restart:

```bash
beetroot down alpha && beetroot up alpha
```

This is by design — re-flashing on a live system would require Android to restart the Zygote, which is equivalent to a reboot anyway.

## Module verification

If you provide `sha256`, Beetroot verifies the downloaded zip before staging it. A mismatch causes `beetroot apply` (and `beetroot create`) to fail with an error:

```
error: module sha256 mismatch for Shamiko-v0.7.4-426-release.zip
  expected: abc123...
  got:      def456...
```

This protects against accidental URL drift and supply-chain tampering.
