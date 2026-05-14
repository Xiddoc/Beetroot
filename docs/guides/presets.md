# Presets

Presets are starter `beetroot.yaml` configs checked into `presets/`. They let you say `beetroot create <name> --preset stealth` instead of manually editing YAML before your first boot.

## Available presets

### `default`

The cheap baseline — low FPS, small framebuffer, host GPU passthrough. GMS is denylisted so it can't see Magisk root, but no additional stealth modules are installed.

```yaml title="presets/default.yaml"
android:
  version: 14

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

That's the entire preset. Every other field (display, resources, frida, modules) defaults to a sensible value — see the [config reference](../reference/config.md). This preset is intentionally minimal so the file shows *only* what the researcher cares about: which Android version, and which packages to hide root from.

Use this when you're testing something that doesn't use anti-root checks, or when you want the lightest-weight setup.

### `stealth`

Adds [Shamiko](https://github.com/LSPosed/LSPosed.github.io) on top of `default`. Shamiko turns Magisk's denylist mode into a true allowlist-based hide — processes on the denylist can't detect Magisk at all (rather than just being told "no root for you"). The denylist is also wider to cover all GMS variants and the Play Store.

```yaml title="presets/stealth.yaml"
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
    After Beetroot downloads Shamiko for the first time, run `sha256sum instances/<name>/modules/*.zip`, paste the hash into `beetroot.yaml` under the module's `sha256:` field, and run `beetroot apply <name>`. Future downloads are verified against this hash — if the remote zip is tampered with or the URL redirects somewhere unexpected, the apply fails loudly.

## Using presets

```bash
beetroot create research-clean            # default preset
beetroot create research-hidden --preset stealth
```

The preset is only used at creation time. The resulting `instances/<name>/beetroot.yaml` is a standalone file — it doesn't reference or import the preset. You can freely edit it afterward.

## Modifying your config

Edit `instances/<name>/beetroot.yaml` directly, then apply:

```bash
# Example: bump Frida version
vim instances/alpha/beetroot.yaml
# change frida.version to "16.5.0"

beetroot apply alpha
# Re-downloads Frida binary, re-renders .env, re-stages modules.

beetroot down alpha && beetroot up alpha
# Restart to pick up the new Frida binary.
```

`beetroot apply` is idempotent. Run it after any YAML edit; it only re-downloads things that changed.

## Writing your own preset

Add a `presets/mypreset.yaml` file following the same schema as the built-in ones. Any field you omit inherits the Pydantic model default (see [Configuration reference](../reference/config.md)). Then use it with:

```bash
beetroot create target-env --preset mypreset
```

!!! note "Where the CLI looks"
    `beetroot create` resolves presets relative to the `presets/` directory inside the Beetroot project root (the directory containing `compose.yaml`). Run the CLI from the project root, or install it editably with `uv sync`.
