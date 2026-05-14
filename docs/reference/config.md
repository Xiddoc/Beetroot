# Configuration Reference

Every instance has an `instances/<name>/beetroot.yaml` that fully describes it. This file is the source of truth. Edit it, run `beetroot apply <name>`, restart — the change takes effect.

The schema is validated by Pydantic on every load. Fields you omit use the defaults shown below.

---

## Top-level structure

```yaml
android: ...
display: ...
resources: ...
frida: ...
modules: [...]
stealth: ...
```

---

## `android`

Android version + image-tag derivation. Beetroot computes the redroid base image tag from these fields via `config.base_image_tag()` — you don't write the long tag yourself.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | int | `14` | Android version. Valid: `11`, `12`, `13`, `14`. |

```yaml
android:
  version: 14
```

!!! warning "Legacy `base_image` field removed"
    The old `android.base_image: redroid/redroid:14.0.0_...` field was replaced by `android.version` in the current schema. Loading a YAML with the legacy field raises a `ValidationError` pointing at this migration — see `CHANGELOG.md`.

---

## `display`

Virtual display configuration for the Android framebuffer.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `width` | int | `540` | Framebuffer width in pixels |
| `height` | int | `960` | Framebuffer height in pixels |
| `fps` | int | `3` | Maximum framebuffer FPS. 3 is enough for research; raise only if you need smooth UI. |
| `gpu_mode` | string | `host` | GPU passthrough mode. `host` uses the host GPU (recommended). `guest` renders in software (slow). |

```yaml
display:
  width: 540
  height: 960
  fps: 3
  gpu_mode: host
```

!!! tip "Low FPS saves resources"
    The default 3 FPS is intentional. redroid's GPU passthrough still costs CPU even at low FPS. For headless research (no UI interaction needed), you can reduce to 1.

---

## `resources`

Docker resource limits for the container.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mem` | string | `3g` | Memory limit. Docker format: `512m`, `1g`, `3g`, etc. |
| `cpus` | float | `2.0` | CPU limit in fractional cores. |
| `shm` | string | `256m` | Shared memory size (`/dev/shm`). redroid uses this for GPU passthrough. |

```yaml
resources:
  mem: 3g
  cpus: 2.0
  shm: 256m
```

!!! note "pids_limit is fixed"
    `pids_limit: 4096` is hardcoded in `compose.yaml` — Android forks aggressively and hitting the default 1024 limit causes random crashes. It's not configurable in `beetroot.yaml`.

---

## `frida`

Frida server configuration. Set to `null` (`~` in YAML) to disable Frida entirely.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | string | `"16.4.10"` | Frida server version to download from GitHub releases. Must match your host-side `frida-tools` major + minor version. |

```yaml
frida:
  version: "16.4.10"

# To disable:
# frida: ~
```

The binary is downloaded from `github.com/frida/frida/releases`, decompressed (`.xz`), and staged at `instances/<name>/frida-server`. It's bind-mounted into the container at `/data/local/tmp/frida-server`.

---

## `modules`

A list of Magisk module zips to flash on the next boot. Each entry is an object with exactly one of `url` or `path`, and an optional `sha256`.

```yaml
modules:
  - url: https://example.com/Module.zip
    sha256: abc123...   # optional but recommended

  - path: ./local-modules/MyHook.zip
    # sha256: optional even for local files
```

### Module entry fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Exclusive with `path` | HTTPS URL to a `.zip`. Downloaded and cached by the CLI. |
| `path` | string | Exclusive with `url` | Path to a local `.zip`. Relative to the project root. |
| `sha256` | string | No | Expected SHA-256 hex digest. If provided, the CLI verifies the downloaded/local file before staging. |

!!! warning "Exactly one of `url` or `path`"
    Setting both or neither raises a Pydantic validation error at load time.

---

## `stealth`

Magisk denylist configuration. Processes listed here are denylisted in the Magisk SQLite database at boot time, before any app launches.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `denylist` | list[string] | `[]` | Package names and process names to add to Magisk's denylist. |

```yaml
stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
    - com.google.android.gms.persistent
    - com.android.vending
```

!!! note "Denylist vs. Shamiko"
    The Magisk denylist alone makes root *inaccessible* to listed processes. With [Shamiko](../guides/modules.md) installed, the denylist is upgraded to a full allowlist-based hide — listed processes can't detect Magisk at all.

---

## Complete example

```yaml
android:
  version: 14

display:
  width: 1080
  height: 1920
  fps: 10
  gpu_mode: host

resources:
  mem: 4g
  cpus: 3.0
  shm: 512m

frida:
  version: "16.5.0"

modules:
  - url: https://github.com/LSPosed/LSPosed.github.io/releases/download/shamiko-426/Shamiko-v0.7.4-426-release.zip
    sha256: <your-hash-here>
  - path: ./local-modules/CustomHook.zip

stealth:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
    - com.google.android.gms.persistent
    - com.android.vending
    - com.target.app
```
