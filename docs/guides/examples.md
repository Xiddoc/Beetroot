# Examples

Beetroot ships a handful of starter `beetroot.yaml` files under the [`examples/`](https://github.com/Xiddoc/Beetroot/tree/main/examples) directory of the repository. They are **documentation only** — the CLI does not load or reference them. Each file is a hand-readable, copy-pasteable snippet you drop over a fresh `beetroot.yaml` when you want that configuration as your starting point.

`beetroot create <name>` always writes a minimal `beetroot.yaml`:

```yaml
api_version: 5
android:
  version: 14
```

That's the full file. Every other field falls back to schema defaults (see the [config reference](../reference/config.md)). To start from a richer baseline, copy one of the examples below over the generated YAML and re-run `beetroot apply <name>`.

## Available examples

### `default.yaml`

The lightweight baseline — GMS is denylisted from Magisk root, but no additional stealth modules are installed.

```yaml title="examples/default.yaml"
api_version: 5

android:
  version: 14

magisk:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

Use this when you're testing something that doesn't perform anti-root checks, or when you want the lightest-weight setup with sensible defaults.

### `stealth.yaml`

A wider Magisk denylist suitable for use with a root-hider like [Shamiko](https://github.com/LSPosed/LSPosed.github.io). Shamiko turns Magisk's denylist mode into a true allowlist-based hide — processes on the denylist can't detect Magisk at all. The denylist below covers all GMS variants and the Play Store.

```yaml title="examples/stealth.yaml"
api_version: 5

android:
  version: 14

# To add a root-hider module, look up a current release URL (e.g. for
# Shamiko: https://github.com/LSPosed/LSPosed.github.io/releases) and
# fill the block below in. The sha256 is optional but recommended —
# beetroot will verify it on every stage.
#
# modules:
#   - url: https://github.com/.../<release>/<asset>.zip
#     sha256: <hex-digest>

magisk:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
    - com.google.android.gms.persistent
    - com.android.vending
```

!!! tip "Pin the sha256"
    The `modules:` block is commented out by default because the upstream Shamiko release URL changes whenever LSPosed cuts a new tag — shipping a hard-coded URL here means this example goes stale silently the moment that release is retired. Pick a current release, paste its URL into `url:`, and (recommended) pre-compute the sha256 with `curl -sL <url> | sha256sum` so Beetroot can verify on every stage.

### `no-gapps.yaml`

Same as `default.yaml` but with `android.gapps: none`. Use this if you want a stripped-down Android without Google Mobile Services — fewer running processes, smaller `/data`, no GMS-specific anti-emulator checks. Requires `beetroot build none` to have produced a matching base image.

```yaml title="examples/no-gapps.yaml"
api_version: 5

android:
  version: 14
  gapps: none
```

### `with-frida.yaml`

The baseline plus an explicit, version-pinned `frida-server`. Copy this over a freshly-generated `beetroot.yaml` whenever you want Frida on for that instance — the version pin must match your host-side `frida-tools` on major + minor. (Prefer `version: auto`, the default, to track your host `frida-tools` automatically; pin only when you need a reproducible server build. See the [`frida` config reference](../reference/config.md#frida).)

```yaml title="examples/with-frida.yaml"
api_version: 5

android:
  version: 14

frida:
  version: "16.4.10"

magisk:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
```

Drop the `frida:` block (or copy `examples/default.yaml`) to turn Frida back off.

### `adb-device.yaml`

Documentation only — unlike the others, this file is **not** a copy-pasteable `beetroot.yaml`. adb-backed instances (created via `beetroot adopt <serial>`) have no instance directory and no `beetroot.yaml`; they're managed outside Beetroot (a real phone, a third-party emulator, or an `adb connect`-ed network device). The file documents the *conceptual* shape of the registry row `beetroot adopt` writes — the actual storage is JSON in `$XDG_CONFIG_HOME/beetroot/instances.json` under the `InstanceMeta` schema.

```yaml title="examples/adb-device.yaml (conceptual registry row)"
name: phone

backend:
  kind: adb
  serial: emulator-5554   # passed verbatim to `adb -s <serial> ...`

index: 0
created_at: "2026-05-19T00:00:00+00:00"
```

Produce it with `beetroot adopt emulator-5554 --name phone`. adb-backed devices have no `android:`, `frida:`, `modules:`, `magisk:`, or resource blocks — manage Zygisk, the denylist, and modules via the Magisk app on the device itself.

### `vm.yaml`

Runs redroid inside an emulated QEMU micro-VM that ships its own binder-enabled kernel — for hosts with no kernel binder driver (hardened CI, `nomodule` cloud sandboxes). Build the guest artifacts once with `beetroot build --vm-kernel`, then `beetroot apply` + `beetroot up`.

```yaml title="examples/vm.yaml"
api_version: 5

binder: vm

vm:
  kernel: ~/.cache/beetroot/vm/bzImage
  rootfs: ~/.cache/beetroot/vm/rootdisk.img
  accel: auto
  smp: auto
  memory_mib: 8192
```

`smp: auto` pins `-smp` to the host's **physical** core count (HT siblings collapsed, capped by CPU affinity) — the measured optimum for the redroid boot (it scales with vCPUs up to the host core count, then regresses past it, so a logical-CPU count would oversubscribe on a hyperthreaded host). `accel: auto` prefers KVM when `/dev/kvm` is available (near-native) and falls back to TCG (software emulation, ~5–20× slower) otherwise. A slow first boot under TCG is expected, not a hang.

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
api_version: 5
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
