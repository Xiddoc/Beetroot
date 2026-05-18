# Presets

Presets are starter `beetroot.yaml` configs that ship **inside the `beetroot` wheel** (under `beetroot.templates.presets`). They let you say `beetroot create <name> --preset stealth` instead of manually editing YAML before your first boot. Because they're bundled with the package, the same preset names resolve identically from `uv tool install` and `uv sync` checkouts.

## Available presets

### `default`

The cheap baseline — low FPS, small framebuffer, host GPU passthrough. GMS is denylisted so it can't see Magisk root, but no additional stealth modules are installed. **Frida is opt-in** (v0.3+) — the preset deliberately omits the `frida:` block, so no `frida-server` is downloaded or launched.

```yaml title="default.yaml"
android:
  version: 14

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

That's the entire preset. Every other field (display, resources, modules) defaults to a sensible value — see the [config reference](../reference/config.md). This preset is intentionally minimal so the file shows *only* what the researcher cares about: which Android version, and which packages to hide root from.

Use this when you're testing something that doesn't use anti-root checks, or when you want the lightest-weight setup. For a Frida-enabled baseline, use [`with-frida`](#with-frida).

### `with-frida`

Same as `default` but with an explicit `frida:` block. Use this when you want `frida-server` downloaded, staged, and auto-launched inside the container at boot.

```yaml title="with-frida.yaml"
android:
  version: 14

frida:
  version: "16.4.10"

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

Pin `frida.version` to match the host-side `frida-tools` major + minor version you're using (see [Frida](frida.md)).

### `stealth`

Adds [Shamiko](https://github.com/LSPosed/LSPosed.github.io) on top of `default`. Shamiko turns Magisk's denylist mode into a true allowlist-based hide — processes on the denylist can't detect Magisk at all. The denylist is also wider to cover all GMS variants and the Play Store.

```yaml title="stealth.yaml"
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

### `no-gapps`

Same as `default` but with `android.gapps: none`. Use this if you want a stripped-down Android without Google Mobile Services — fewer running processes, smaller `/data`, no GMS-specific anti-emulator checks.

## Using presets

```bash
beetroot create research-clean            # default preset (no frida)
beetroot create research-hooked --preset with-frida
beetroot create research-hidden --preset stealth
```

The preset is only used at creation time. The resulting `<instance>/beetroot.yaml` is a standalone file — it doesn't reference or import the preset. You can freely edit it afterward.

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

## Writing your own preset

The set of bundled presets is fixed per Beetroot release (they ship inside the wheel). To use a one-off custom starting point, write a `beetroot.yaml` directly into a new instance directory and adopt it:

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
