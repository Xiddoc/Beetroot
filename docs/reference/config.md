# Configuration Reference

Every instance directory has a `beetroot.yaml` that fully describes it. This file is the source of truth. Edit it, run `beetroot apply <name>`, restart — the change takes effect.

The schema is validated by Pydantic on every load. Fields you omit use the defaults shown below.

---

## Top-level structure

```yaml
api_version: 4
android: ...
display: ...
resources: ...
frida: ...   # optional / opt-in
modules: [...]
magisk: ...
ports: ...
binder: auto   # auto | host | vm
```

---

## `api_version`

Schema version this `beetroot.yaml` targets.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_version` | int | `4` | Schema version. Must match the value supported by this Beetroot release. |

```yaml
api_version: 4
```

### Versioning policy

Each Beetroot release supports **exactly one** `api_version`. The current
release supports `api_version: 4`. Loading a YAML that pins a different
value raises one of two errors:

- **Unknown / future version** (`0`, `99`, …): raises a `ValidationError`
  with a pointer to `CHANGELOG.md` for the migration steps.
- **Non-additive migration required** (e.g. `api_version: 3` with a
  `stealth:` key that was renamed to `magisk:` in v4): raises a clear
  migration error naming the changed field and pointing at `CHANGELOG.md`.

**Auto-bump (additive legacy versions):** `api_version: 1` (v0.2),
`api_version: 2` (v0.3), and `api_version: 3` (v0.4) are recognised
legacy values and auto-bumped on load with a one-line warning, because
those bumps added only new optional fields — nothing was renamed.
Persistence happens on the next `beetroot apply`.

Omitting the field is equivalent to writing the currently supported value
— existing instance YAMLs without `api_version` keep working. Pinning the
field explicitly is recommended once you're committing an instance YAML to
source control, so that a future Beetroot release with a breaking schema
change fails loud instead of silently reinterpreting your config.

All [example YAMLs](../guides/examples.md) declare `api_version: 4`
explicitly as the first field. When the schema breaks, the constant
`SUPPORTED_API_VERSION` in `src/beetroot/config.py` is bumped and a
migration entry is added to `CHANGELOG.md`.

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
| `width` | int | `540` | Framebuffer width in pixels. Must be > 0. |
| `height` | int | `960` | Framebuffer height in pixels. Must be > 0. |
| `fps` | int | `3` | Maximum framebuffer FPS. Must be > 0. 3 is enough for research; raise only if you need smooth UI. |
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
| `mem` | string | `3g` | Memory limit. Docker size format: `512m`, `1g`, `3g`, etc. Validated at load time — typos like `"3gb"` fail immediately. |
| `cpus` | float | `2.0` | CPU limit in fractional cores. |
| `shared_mem` | string | `256m` | Shared memory size (`/dev/shm`, Docker `shm_size`). Docker size format. |
| `mem_reservation` | string | none | Soft memory floor. Docker size format. Docker scheduler reserves this for the container but allows it to use more up to `mem`. |
| `memswap_limit` | string | none | Combined memory + swap cap. Docker size format. Prevents swap storms if set equal to `mem`. |
| `pids_limit` | int | `4096` | Maximum number of PIDs the container can spawn. |

```yaml
resources:
  mem: 3g
  cpus: 2.0
  shared_mem: 256m
```

!!! warning "Legacy `shm` field removed"
    The old `resources.shm` field was renamed to `resources.shared_mem` for clarity. Loading a YAML with the legacy field raises a `ValidationError` pointing at this migration — see `CHANGELOG.md`.

!!! note "Docker size format"
    All string memory fields (`mem`, `shared_mem`, `mem_reservation`, `memswap_limit`) must use Docker's size format: a number optionally followed by a single suffix — `b`, `k`, `m`, `g`, or `t` (case-insensitive). Examples: `3g`, `512m`, `1.5G`. Values like `3gb` (two-letter suffix) fail at load time with a clear error rather than being silently misinterpreted at `docker compose up`.

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
| `path` | string | Exclusive with `url` | Path to a local `.zip`. Relative paths resolve against the instance directory. |
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

## `magisk`

Magisk configuration, including the boot-time denylist. Processes listed here are denylisted in the Magisk SQLite database at boot time, before any app launches.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `denylist` | list[string] | `["com.google.android.gms", "com.google.android.gms.unstable"]` | Package names to add to Magisk's denylist. Each entry must match the Android package-id grammar (`[a-zA-Z0-9._]+`) — validated at load time as SQL-injection prophylaxis. |

```yaml
magisk:
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
    - com.google.android.gms.persistent
    - com.android.vending
```

!!! note "Denylist vs. Shamiko"
    The Magisk denylist alone makes root *inaccessible* to listed processes. With [Shamiko](../guides/modules.md) installed, the denylist is upgraded to a full allowlist-based hide — listed processes can't detect Magisk at all.

!!! warning "Migrating from `stealth:` (api_version 3)"
    The `stealth:` key was renamed to `magisk:` in api_version 4. If your `beetroot.yaml` still contains `stealth:`, Beetroot will fail at load with:

    ```
    The 'stealth:' key was removed in api_version 4.
    Move 'stealth.denylist' to 'magisk.denylist' and update
    'api_version' to 4. See CHANGELOG.md for the migration.
    ```

    Rename the key and bump `api_version` to `4` to fix it.

---

## `binder`

Selects how redroid obtains the kernel **binder** driver it needs to boot. redroid is a container, not an emulator — it runs Android's userspace against the *host* kernel — so binder must come from somewhere.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `binder` | string | `auto` | One of `auto`, `host`, `vm`. See the table below. |

| Value | Behaviour |
|-------|-----------|
| `auto` | Use the host kernel's binder. If the host can't provide it, `beetroot up` prints a one-line advisory (once) and starts anyway — the container comes up but Android may not boot. This is the historical behaviour and the default. |
| `host` | Strict: `beetroot up` **refuses to start** (exit 1) unless the host binder is ready. Prefer this in CI, where a container that silently never boots Android is worse than a fast, clear failure. |
| `vm` | Opt into running redroid inside an emulated QEMU micro-VM that ships its own binder-enabled kernel — the path for hosts with no host binder at all (hardened CI, `nomodule` cloud sandboxes). |

```yaml
binder: host
```

!!! tip "`binder: vm` boots an emulated micro-VM"
    Selecting `vm` dispatches `beetroot up` to a QEMU micro-VM that ships its own binder-enabled kernel. Build the guest artifacts once with `beetroot build --vm-kernel`, point `vm.kernel` / `vm.rootfs` at them (or set `BEETROOT_VM_KERNEL` / `BEETROOT_VM_ROOTFS`), and run `beetroot apply` then `beetroot up`. On a host with `/dev/kvm` this is near-native; without it the backend falls back to TCG (~5-20x slower — a slow first boot is expected, not a hang). The slow path is **never** engaged automatically; `binder: vm` is always an explicit opt-in. See [Binderless hosts (QEMU/TCG)](../design/binderless-hosts-qemu-tcg.md).

!!! warning "Frida is not yet supported under `binder: vm`"
    The micro-VM guest is network-isolated, so the `vm` backend is scoped to ADB forwarding (`beetroot shell`) only. `beetroot frida <vm-instance>` raises a friendly "not yet supported on the 'vm' backend" error, `beetroot doctor` omits the `frida.handshake` row, and `ls` / `status` report the Frida address as `unsupported`. Any `frida:` block in a `binder: vm` config is ignored (no `frida-server` is staged). For Frida, use `binder: auto` / `host` (redroid) or `beetroot adopt` an external rooted device. Tracked as a follow-up to [#44](https://github.com/Xiddoc/Beetroot/issues/44).

---

## `vm`

QEMU micro-VM tunables. Consulted **only** when `binder: vm`; ignored otherwise. All fields are optional — an empty `vm:` block (or none) is valid, and the kernel/rootfs paths then fall back to the `BEETROOT_VM_KERNEL` / `BEETROOT_VM_ROOTFS` environment variables.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vm.kernel` | string \| null | `null` | Host path to the guest `bzImage`. `null` defers to `BEETROOT_VM_KERNEL`. |
| `vm.rootfs` | string \| null | `null` | Host path to the guest ext4 root image. `null` defers to `BEETROOT_VM_ROOTFS`. |
| `vm.accel` | string | `auto` | QEMU accelerator: `auto` (probe `/dev/kvm`, prefer KVM, else TCG), `kvm` (force; errors if `/dev/kvm` is absent), or `tcg` (force software emulation). |
| `vm.smp` | int | `4` | Guest vCPUs (`-smp`). Must be >= 1. |
| `vm.memory_mib` | int | `8192` | Guest RAM in MiB (`-m`). Must be >= 256. |

After launching QEMU, `beetroot up` polls `adb connect` against the guest until the forwarded ADB endpoint accepts a connection — the guest restarts `adbd` to enable TCP a few seconds *after* `sys.boot_completed=1`, so a single immediate connect would race that late bind. The poll deadline is the `BEETROOT_VM_ADB_CONNECT_TIMEOUT` environment variable (seconds, default `60`); raise it for slow TCG first boots. If the guest never exposes ADB within the deadline, `up` fails with an actionable error (try `beetroot logs <name>` to watch the boot, or pin `vm.accel: kvm`) rather than a traceback.

```yaml
binder: vm
vm:
  kernel: ~/.cache/beetroot/vm/bzImage
  rootfs: ~/.cache/beetroot/vm/rootdisk.img
  accel: auto
  smp: 4
  memory_mib: 8192
```

---

## Complete example

```yaml
api_version: 4

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

magisk:
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
