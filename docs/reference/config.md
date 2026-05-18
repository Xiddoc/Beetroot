# Configuration Reference

Every instance directory has a `beetroot.yaml` that fully describes it. This file is the source of truth. Edit it, run `beetroot apply <name>`, restart — the change takes effect.

The schema is validated by Pydantic on every load. Fields you omit use the defaults shown below.

---

## Top-level structure

```yaml
api_version: 2
android: ...
display: ...
resources: ...
frida: ...   # optional / opt-in
modules: [...]
stealth: ...
ports: ...
```

---

## `api_version`

Schema version this `beetroot.yaml` targets.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_version` | int | `2` | Schema version. Must match the value supported by this Beetroot release. |

```yaml
api_version: 2
```

### Versioning policy

Each Beetroot release supports **exactly one** `api_version`. The current
release supports `api_version: 2`. Loading a YAML that pins a different
value (`0`, `1`, `99`, …) raises a `ValidationError` with a pointer to
`CHANGELOG.md` for the migration steps.

Omitting the field is equivalent to writing the currently supported value
— existing instance YAMLs without `api_version` keep working. Pinning the
field explicitly is recommended once you're committing an instance YAML to
source control, so that a future Beetroot release with a breaking schema
change fails loud instead of silently reinterpreting your config.

All shipped presets declare `api_version: 2` explicitly as the first
field. When the schema breaks, the constant `SUPPORTED_API_VERSION` in
`src/beetroot/config.py` is bumped and a migration entry is added to
`CHANGELOG.md`.

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
| `shared_mem` | string | `256m` | Shared memory size (`/dev/shm`, Docker `shm_size`). redroid uses this for GPU passthrough. |

```yaml
resources:
  mem: 3g
  cpus: 2.0
  shared_mem: 256m
```

!!! warning "Legacy `shm` field removed"
    The old `resources.shm` field was renamed to `resources.shared_mem` for clarity. Loading a YAML with the legacy field raises a `ValidationError` pointing at this migration — see `CHANGELOG.md`.

!!! note "pids_limit is fixed"
    `pids_limit: 4096` is hardcoded in `compose.yaml` — Android forks aggressively and hitting the default 1024 limit causes random crashes. It's not configurable in `beetroot.yaml`.

---

## `frida`

Frida server configuration. **Opt-in starting in v0.3** — omit the block entirely (or set it explicitly to `null` / `~`) to disable Frida. Declare the block to opt in.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | string | `"16.4.10"` | Frida server version to download from GitHub releases. Applies when the `frida:` block IS present. Must match your host-side `frida-tools` major + minor version. |

```yaml
# Opt in:
frida:
  version: "16.4.10"

# Default (omit the block entirely, or:):
# frida: ~
```

When opted in, the binary is downloaded from `github.com/frida/frida/releases`, decompressed (`.xz`), and cached at `~/.cache/beetroot/frida/` (respects `$XDG_CACHE_HOME`). The CLI then copies it into the instance directory at `frida-server`, which is bind-mounted into the container at `/data/local/tmp/frida-server`. When opted out, that same path is a 0-byte non-executable placeholder and `entrypoint.sh` skips the launch.

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

## `ports`

Optional per-instance port overrides. Each field is independently optional — set only the ones you want to pin; the rest fall back to the [stride-of-10 allocator](./ports.md) on the instance's index.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `adb` | int | stride (`5555 + index*10`) | Host port mapped to the container's ADB (5555). |
| `frida` | int | stride (`27042 + index*10`) | Host port mapped to the container's Frida data port (27042). |
| `frida_control` | int | stride (`27043 + index*10`) | Host port mapped to the container's Frida control port (27043). |

```yaml
# Pin only ADB, let Frida take the stride defaults
ports:
  adb: 9000

# Pin everything
ports:
  adb: 9000
  frida: 9001
  frida_control: 9002
```

If you omit the block entirely (the default), every port is allocated by the stride scheme. If you pin a port that another instance already uses, `beetroot create` and `beetroot apply` exit with a clear error before staging:

```
error: port 5555 (adb) collides with instance 'alpha' (which also uses 5555). Pin or remove one.
```

!!! tip "Why pin a port?"
    The most common reason is to keep a stable, memorable port across destroy/recreate cycles, or to coordinate with external tools (a CI pipeline, a fixed firewall rule, an IDE's run config) that already point at a specific host port. The stride allocator's index can shift if instances at lower indices are destroyed and recreated.

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
api_version: 2

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
  shared_mem: 512m

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

ports:
  adb: 9000
  frida: 9001
  frida_control: 9002
```
