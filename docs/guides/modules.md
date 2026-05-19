# Magisk Modules

Beetroot supports flashing Magisk modules into instances declaratively — declare them in `beetroot.yaml` and they're automatically downloaded, staged, and installed on the next boot.

## How module flashing works

At boot, `entrypoint.sh` (running inside the Android container under the `u:r:magisk:s0` SELinux context) iterates every `.zip` in `/data/adb/modules_update/` and calls `magisk --install-module <zip>` for each one. That container path is a read-only bind-mount of the instance directory's `modules/` subdirectory on the host — Magisk's well-known staging directory, so the modules are visible to the daemon the way a manually-side-loaded one would be.

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
- Use `path` for local zips. Relative paths are resolved against the instance directory itself.
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

4. Verify in ADB:

    ```bash
    beetroot shell alpha
    # In the shell:
    magisk --list
    # Shamiko should appear in the list
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
