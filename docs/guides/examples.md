# Examples

Beetroot ships a handful of starter `beetroot.yaml` files under the [`examples/`](https://github.com/Xiddoc/Beetroot/tree/main/examples) directory of the repository. They are **documentation only** — the CLI does not load or reference them. Each file is a hand-readable, copy-pasteable snippet you drop over a fresh `beetroot.yaml` when you want that configuration as your starting point.

`beetroot create <name>` always writes a minimal `beetroot.yaml`:

```yaml
api_version: 2
android:
  version: 14
```

That's the full file. Every other field falls back to schema defaults (see the [config reference](../reference/config.md)). To start from a richer baseline, copy one of the examples below over the generated YAML and re-run `beetroot apply <name>`.

## Available examples

### `default.yaml`

The lightweight baseline — GMS is denylisted from Magisk root, but no additional stealth modules are installed.

```yaml title="examples/default.yaml"
api_version: 2

android:
  version: 14

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

Use this when you're testing something that doesn't perform anti-root checks, or when you want the lightest-weight setup with sensible defaults.

### `stealth.yaml`

Adds [Shamiko](https://github.com/LSPosed/LSPosed.github.io) on top of `default`. Shamiko turns Magisk's denylist mode into a true allowlist-based hide — processes on the denylist can't detect Magisk at all. The denylist is also wider to cover all GMS variants and the Play Store.

```yaml title="examples/stealth.yaml"
api_version: 2

android:
  version: 14

modules:
  - url: https://github.com/LSPosed/LSPosed.github.io/releases/download/shamiko-426/Shamiko-v0.7.4-426-release.zip
    # sha256: <fill-in-after-first-download>

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
    - com.google.android.gms.persistent
    - com.android.vending
```

!!! tip "Pin the sha256"
    After Beetroot downloads Shamiko for the first time, run `sha256sum` on the staged zip (look up the instance path with `beetroot ls --json | jq -r .<name>.path`), paste the hash into the instance's `beetroot.yaml` under the module's `sha256:` field, and run `beetroot apply <name>`. Future downloads are verified against this hash — if the remote zip is tampered with or the URL redirects somewhere unexpected, the apply fails loudly.

### `no-gapps.yaml`

Same as `default.yaml` but with `android.gapps: none`. Use this if you want a stripped-down Android without Google Mobile Services — fewer running processes, smaller `/data`, no GMS-specific anti-emulator checks. Requires `beetroot build none` to have produced a matching base image.

```yaml title="examples/no-gapps.yaml"
api_version: 2

android:
  version: 14
  gapps: none
```

### `with-frida.yaml`

The baseline plus an explicit, version-pinned `frida-server`. Copy this over a freshly-generated `beetroot.yaml` whenever you want Frida on for that instance — the version pin must match your host-side `frida-tools` on major + minor.

```yaml title="examples/with-frida.yaml"
api_version: 2

android:
  version: 14

frida:
  version: "16.4.10"

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

Drop the `frida:` block (or copy `examples/default.yaml`) to turn Frida back off.

## Using an example

```bash
beetroot create research-clean
cp examples/stealth.yaml research-clean/beetroot.yaml
beetroot apply research-clean
beetroot up research-clean
```

The `examples/` directory is a sibling of `docs/` in the [Beetroot repo](https://github.com/Xiddoc/Beetroot/tree/main/examples). If you installed via `uv tool install` and don't have a checkout handy, copy the YAML from this page directly into your instance's `beetroot.yaml`.

## Modifying your config

Edit the instance's `beetroot.yaml` directly, then apply:

```bash
# Example: bump Frida version
vim "$(beetroot ls --json | jq -r .alpha.path)/beetroot.yaml"
# change frida.version to "16.5.0"

beetroot apply alpha
beetroot down alpha && beetroot up alpha
```

`beetroot apply` is idempotent. Run it after any YAML edit; it only re-downloads things that changed.

## Writing your own starter config

There is no plugin or extension hook for adding new examples — they're just documentation. For a custom starting point, hand-write a `beetroot.yaml` in a new directory and adopt it:

```bash
mkdir my-custom-instance
cat > my-custom-instance/beetroot.yaml <<'YAML'
api_version: 2
android:
  version: 14
  gapps: none
display:
  width: 1080
  height: 1920
  fps: 30
YAML
beetroot register ./my-custom-instance --name my-custom
beetroot apply my-custom
beetroot up my-custom
```
