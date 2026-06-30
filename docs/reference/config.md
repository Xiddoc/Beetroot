# Configuration Reference

Every instance directory has a `beetroot.yaml` that fully describes it. This file is the source of truth. Edit it, run `beetroot apply <name>`, restart — the change takes effect.

The schema is validated by Pydantic on every load. Fields you omit use the defaults shown below.

---

## Top-level structure

```yaml
api_version: 8
lifecycle: durable   # durable | ephemeral (optional; default durable)
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
| `api_version` | int | `8` | Schema version. Must match the value supported by this Beetroot release. |

```yaml
api_version: 8
```

### Versioning policy

Each Beetroot release supports **exactly one** `api_version`. The current
release supports `api_version: 8`. Loading a YAML that pins a different
value raises one of two errors:

- **Unknown / future version** (`0`, `99`, …): raises a `ValidationError`
  with a pointer to `CHANGELOG.md` for the migration steps.
- **Non-additive migration required** (e.g. `api_version: 3` with a
  `stealth:` key renamed to `magisk:` in v4, `display.gpu_mode` renamed
  to `display.rendering` in v5, a vendor-named `android.gapps` in v7, or
  a `ports:` mapping with a non-well-known key in v8): raises a clear
  migration error naming the changed field and pointing at `CHANGELOG.md`.

**Auto-bump (legacy versions):** `api_version: 1` (v0.2) through `7` are
recognised legacy values and auto-bumped on load with a one-line warning —
*unless* the YAML still uses a key that a non-additive bump renamed
(`stealth:` for 3→4, `display.gpu_mode` for 4→5, a vendor-named
`android.gapps` for 6→7), in which case the migration error fires instead.
The additive bumps (the `lifecycle` field for 5→6, the lossless `ports`
mapping → list translation for 7→8) are silent. Persistence happens on the
next `beetroot apply`.

Omitting the field is equivalent to writing the currently supported value
— existing instance YAMLs without `api_version` keep working. Pinning the
field explicitly is recommended once you're committing an instance YAML to
source control, so that a future Beetroot release with a breaking schema
change fails loud instead of silently reinterpreting your config.

All [example YAMLs](../guides/examples.md) declare `api_version: 8`
explicitly as the first field. When the schema breaks, the constant
`SUPPORTED_API_VERSION` in `src/beetroot/config.py` is bumped and a
migration entry is added to `CHANGELOG.md`.

---

## `lifecycle`

Whether this instance's `/data` is meant to **survive** or is **throwaway**.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `lifecycle` | `durable` \| `ephemeral` | `durable` | Persistence *intent* — a label + guardrails, **not** a runtime persistence switch. |

```yaml
lifecycle: durable
```

- `durable` (default) — a long-lived "research phone" whose `/data` must
  survive (the product's namesake guarantee). Preserves today's behaviour
  exactly. `beetroot destroy` escalates its confirmation copy for these.
- `ephemeral` — a throwaway instance (CI/E2E, comparative fleets, reset
  between runs). Combined with `vm.boot_cache: true` it opts into the
  warm-resume `/data` revert **quietly** (the runtime advisory is
  suppressed — a reset each boot is exactly what `ephemeral` asked for).

!!! warning "Label, not a switch"
    `lifecycle` records intent and tunes guardrails; it never changes when
    `/data` is wiped. `beetroot down` **never** wipes `/data` for either
    value — only `destroy` / `reset` (and a `vm.boot_cache` warm resume) do.
    Set it at create time with `beetroot create --lifecycle ephemeral|durable`,
    or add the key to `beetroot.yaml` and `beetroot apply`.

---

## `android`

Android version + image-tag derivation. Beetroot computes the redroid base image tag from these fields via `config.base_image_tag()` — you don't write the long tag yourself.

GApps is split across two axes (issue #107): an **intent** that says *what you get*, and an optional **vendor** escape hatch that says *which distribution bakes it* — for the rare app that detects or prefers a specific GApps build. Most configs only set `gapps`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | int | `14` | Android version. Valid: `11`, `12`, `13`, `14`. |
| `gapps` | enum | `minimal` | GApps **intent**: `none` (no Play Services), `minimal` (a slim Play Services), or `full` (the full suite). |
| `gapps_vendor` | enum | _(unset)_ | Optional **vendor** override: `litegapps`, `opengapps`, or `mindthegapps`. Unset lets the intent pick the vendor (`minimal` → LiteGApps, `full` → OpenGApps). Cannot be combined with `gapps: none`. |

```yaml
android:
  version: 14
  gapps: minimal          # what you get; vendor is picked for you

# Pin a specific distribution for app compatibility:
android:
  version: 14
  gapps: full
  gapps_vendor: mindthegapps
```

!!! warning "Legacy `gapps: lite` / `gapps: mindthegapps` removed"
    Before api_version 7, `gapps` fused intent and vendor into one enum (`none`/`lite`/`full`/`mindthegapps`). `none` and `full` are unchanged; the vendor-named values were split out. Replace `gapps: lite` with `gapps: minimal` and `gapps: mindthegapps` with `gapps: full` + `gapps_vendor: mindthegapps` (the base image is identical). Loading a YAML with the old value raises a `ValidationError` pointing at `CHANGELOG.md`.

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
| `rendering` | enum | `auto` | How redroid renders — the speed-vs-portability axis. `gpu` renders via the host GPU (fast, needs a GPU-capable host); `software` uses SwiftShader (always works, slower); `auto` (default) probes the host for a DRM render node (`/dev/dri/renderD*`) and picks `gpu` when present, else `software`. A typo (e.g. `rendering: gpuu`) fails at load. |

```yaml
display:
  width: 540
  height: 960
  fps: 3
  rendering: auto
```

!!! tip "Low FPS saves resources"
    The default 3 FPS is intentional. redroid's GPU passthrough still costs CPU even at low FPS. For headless research (no UI interaction needed), you can reduce to 1.

!!! warning "Legacy `gpu_mode` field renamed"
    `display.gpu_mode` (redroid's `host`/`guest` vocabulary) was renamed to `display.rendering` (intent: `gpu`/`software`/`auto`) in `api_version: 5`. Loading a YAML with the old field raises a `ValidationError` with the mapping (`gpu_mode: host` → `rendering: gpu`, `gpu_mode: guest` → `rendering: software`) — see `CHANGELOG.md`. The default also changed from the aggressive `host` (which assumed a host GPU) to `auto`, so a headless box renders in software instead of misbehaving.

---

## `resources`

Docker resource limits for the container.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mem` | string | `3g` | Memory limit. Docker size format: `512m`, `1g`, `3g`, etc. Validated at load time — typos like `"3gb"` fail immediately. |
| `cpus` | float | `2.0` | CPU limit in fractional cores. |
| `shared_mem` | string | `256m` | Shared memory size (`/dev/shm`, Docker `shm_size`). Docker size format. |
| `mem_reservation` | string | none | Soft memory floor. Docker size format. Docker scheduler reserves this for the container but allows it to use more up to `mem`. |
| `memswap_limit` | string | none | Combined memory + swap cap. Docker size format. Unset → Docker's normal swap allowance applies; set it equal to `mem` to disable swap entirely and prevent swap storms. |
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

!!! note "`resources.mem` vs `vm.memory_mib`"
    `resources.mem` is the Docker container memory cap, **authoritative for `binder: auto` / `host`** (redroid). For `binder: vm` the guest RAM is `vm.memory_mib` (the QEMU `-m`). Both knobs are deliberately kept — collapsing them into one field is deferred. Decision recorded in [#104](https://github.com/Xiddoc/Beetroot/issues/104).

---

## `frida`

Frida server configuration. **Opt-in starting in v0.3** — omit the block entirely (or set it explicitly to `null` / `~`) to disable Frida. Declare the block to opt in.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | string | `"auto"` | Which `frida-server` release to stage. One of: **`auto`** (default) — match your host's installed `frida-tools` version so the staged server and the client you attach with agree on major+minor, falling back to `latest` when `frida-tools` isn't installed; **`latest`** — the current upstream release, resolved at download time; or a pinned **`major.minor.patch`** tag (e.g. `"16.4.10"`) for reproducibility. `auto` / `latest` are resolved to a concrete tag at staging time; a malformed pinned tag fails at config-load. |
| `sha256` | string \| null | `null` | Optional expected hex digest of the decompressed `frida-server` binary. When set, it's verified against the downloaded binary at download time (case-insensitive); a mismatch raises an error rather than staging a tampered binary. **Requires a pinned `version`** — a digest can't match the moving target `auto` / `latest` resolve to, so that combination is rejected at load. |

```yaml
# Opt in, tracking your host frida-tools (recommended):
frida:
  version: auto

# Or pin a specific server (reproducible; required if you set sha256):
frida:
  version: "16.4.10"

# Default (omit the block entirely, or:):
# frida: ~
```

When opted in, the binary is downloaded from `github.com/frida/frida/releases`, decompressed (`.xz`), and cached at `~/.cache/beetroot/frida/` (respects `$XDG_CACHE_HOME`). The CLI then copies it into the instance directory at `frida-server`, which is bind-mounted into the container at `/data/local/tmp/frida-server`. When opted out, that same path is a 0-byte non-executable placeholder and `entrypoint.sh` skips the launch.

!!! tip "Client / server version skew"
    Frida requires the client and server to agree on **major + minor**. `version: auto` keeps them in lock-step automatically. If you pin a `version` (or use `latest`) that diverges from your host `frida-tools`, `beetroot apply` prints a one-line warning, because the `frida` client would otherwise fail to attach.

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
| `url` | string | Exclusive with `path` | HTTP or HTTPS URL to a `.zip`. Downloaded and cached by the CLI. |
| `path` | string | Exclusive with `url` | Path to a local `.zip`. **Relative** paths resolve against (and are contained to) the instance directory; one that escapes it is rejected. **Absolute** paths are read as-is. |
| `sha256` | string | No | Expected SHA-256 hex digest. If provided, the CLI verifies the downloaded/local file before staging. |

!!! warning "Exactly one of `url` or `path`"
    Setting both or neither raises a Pydantic validation error at load time.

---

## `ports`

A **list** of guest→host port mappings (since `api_version: 8`, issue #108). Each entry is a `{service, guest, host}` mapping; the list defaults to the three well-known services with auto-allocated host ports.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `service` | string \| null | `null` | Optional label. `adb` / `frida` / `frida_control` are the **well-known** names the [stride allocator](./ports.md) and the `adb_address` / `frida_address` accessors key off; any other string is a free-form label for an arbitrary mapping. |
| `guest` | int | *(required)* | The container-side (guest) port this mapping exposes (1..65535). |
| `host` | int \| null | `null` (auto-allocate) | The host-side port. `null` auto-allocates — a [stride base](./ports.md) for a well-known service, or a dedicated extra-pool slot (`40000 + index*10 + slot`) for an arbitrary one. An integer pins the host port. |

```yaml
# Default (omit the block): the three well-known services, stride-allocated.
ports:
  - {service: adb, guest: 5555}
  - {service: frida, guest: 27042}
  - {service: frida_control, guest: 27043}

# Pin ADB to a stable host port; forward an arbitrary in-guest service.
ports:
  - {service: adb, guest: 5555, host: 9000}   # explicit host port
  - {service: frida, guest: 27042}            # host unset → stride default
  - {service: frida_control, guest: 27043}
  - {guest: 8080, host: 9090}                 # arbitrary, explicit host
  - {service: metrics, guest: 9100}           # arbitrary, auto-allocated host
```

If you omit the block entirely (the default), the three well-known services are stride-allocated. The list is validated: duplicate `service` names, duplicate `guest` ports, and duplicate explicit `host` ports are all rejected at load time. If a resolved host port collides with another instance, `beetroot create` and `beetroot apply` exit with a clear error before staging:

```
error: port 5555 (adb) collides with instance 'alpha' (which also uses 5555). Pin or remove one.
```

The variable-length port list is written to a per-instance `compose.override.yaml` (regenerated on every `apply`, like `.env`) which the CLI layers on top of the bundled compose template — a flat `.env` can't expand into a YAML list.

!!! warning "Migrating from the old `ports` mapping (api_version 7)"
    The old fixed mapping (`ports: {adb: 9000, frida: ..., frida_control: ...}`) was replaced by the list form. A YAML still carrying the old mapping with only well-known keys is migrated losslessly on load (one-line note) and auto-bumps to `8`; a mapping with any other key raises a `ValidationError` naming the list shape. Rewrite the block as a list and set `api_version: 8`.

!!! note "Arbitrary mappings under `binder: vm`"
    The `binder: vm` backend forwards only adb to the guest; arbitrary mappings beyond the well-known services are ignored (`beetroot apply` warns, mirroring the gapps/frida vm-inert advisories).

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
    Move 'stealth.denylist' to 'magisk.denylist' and set
    'api_version' to 8. See CHANGELOG.md for the migration.
    ```

    Rename the key and bump `api_version` to the current value (`8`) to fix it.

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
    The micro-VM guest is network-isolated, so the `vm` backend is scoped to ADB forwarding (`beetroot shell`) only. `beetroot frida-addr <vm-instance>` raises a friendly "not yet supported on the 'vm' backend" error (exit 2) instead of emitting an address, `beetroot doctor` omits the `frida.handshake` row, and `ls` / `status` report the Frida address as `unsupported`. Any `frida:` block in a `binder: vm` config is ignored (no `frida-server` is staged). For Frida, use `binder: auto` / `host` (redroid) or `beetroot adopt` an external rooted device. Tracked as a follow-up to [#44](https://github.com/Xiddoc/Beetroot/issues/44).

!!! warning "`binder: vm` boots **plain** redroid — GApps, Magisk, and Houdini are inert"
    Unlike `binder: auto` / `host` (which boot the layered image `beetroot build` produces: redroid **+ Magisk + optional GApps + Houdini**), the `binder: vm` guest boots an *unmodified upstream* redroid image. So `android.gapps` and `magisk.denylist` have **no effect** under `binder: vm` — there are no Play Services and no Magisk in the guest. `beetroot apply` prints a one-line advisory naming any such setting it had to ignore (e.g. a `gapps: full` you'd expect to give you the Play Store) — once, at config/apply time, not on every boot. Set `android.gapps: none` (as `examples/vm.yaml` does) to make the config match what the VM actually runs. If you need rooted Magisk **and** GApps, use a `binder: auto` / `host` instance or `beetroot adopt` an external rooted device. The field→backend applicability matrix is expressed structurally in code (`config.inert_fields`), so the advisory is built from a single source of truth. Tracked as [#96](https://github.com/Xiddoc/Beetroot/issues/96) and [#104](https://github.com/Xiddoc/Beetroot/issues/104).

---

## `vm`

QEMU micro-VM tunables. Consulted **only** when `binder: vm`; ignored otherwise. All fields are optional — an empty `vm:` block (or none) is valid, and the kernel/rootfs paths then fall back to the `BEETROOT_VM_KERNEL` / `BEETROOT_VM_ROOTFS` environment variables.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vm.kernel` | string \| null | `null` | Host path to the guest `bzImage`. `null` defers to `BEETROOT_VM_KERNEL`. |
| `vm.rootfs` | string \| null | `null` | Host path to the guest ext4 root image. `null` defers to `BEETROOT_VM_ROOTFS`. |
| `vm.accel` | string | `auto` | QEMU accelerator: `auto` (probe `/dev/kvm`, prefer KVM, else TCG), `kvm` (force; errors if `/dev/kvm` is absent), or `tcg` (force software emulation). |
| `vm.smp` | int \| `auto` | `auto` | Guest vCPUs (`-smp`). `auto` pins `-smp` to the host's **physical** core count (HyperThread siblings collapsed, capped by CPU affinity so a cgroup-limited CI runner is respected) — the vm-rnd-log §B.5 measured optimum, since more vCPUs than physical cores oversubscribe the emulator. An explicit integer (>= 1) pins it. |
| `vm.memory_mib` | int | `8192` | Guest RAM in MiB (`-m`). Must be >= 256. Authoritative for `binder: vm`; `resources.mem` is the Docker cap used by `binder: auto` / `host`. Both knobs kept by decision in [#104](https://github.com/Xiddoc/Beetroot/issues/104). |
| `vm.boot_cache` | bool | `false` | Warm-start boot cache. When `true`, the first `up` cold-boots through a qcow2 overlay and checkpoints the running machine state with QEMU `savevm`; every later `up` *resumes* that checkpoint (`-loadvm`) instead of cold-booting — **~10 s vs ~3-4 min under TCG**. Resume reverts the guest to the checkpoint each time (a fast *known-good boot*, not persistence) — so `/data` writes made after the first boot (installed apps, logins, flashed-module state) are discarded on every warm `up`, and Beetroot prints a runtime warning on each resume so that reset is never silent. Set `vm.boot_cache: false` if you need `/data` to persist across restarts. The checkpoint lives at `<instance>/vm-overlay.qcow2` (~2 GiB) and auto-invalidates when the kernel/rootfs changes (a digest is recorded beside it); delete it by hand to force a reset otherwise. Requires `qemu-img`. See [Warm-start boot cache](#warm-start-boot-cache-vmboot_cache). |

After launching QEMU, `beetroot up` polls `adb connect` against the guest until the forwarded ADB endpoint accepts a connection — the guest restarts `adbd` to enable TCP a few seconds *after* `sys.boot_completed=1`, so a single immediate connect would race that late bind. The poll deadline is the `BEETROOT_VM_ADB_CONNECT_TIMEOUT` environment variable (seconds, default `60`); raise it for slow TCG first boots. If the guest never exposes ADB within the deadline, `up` fails with an actionable error (try `beetroot logs <name>` to watch the boot, or pin `vm.accel: kvm`) rather than a traceback.

```yaml
binder: vm
vm:
  kernel: ~/.cache/beetroot/vm/bzImage
  rootfs: ~/.cache/beetroot/vm/rootdisk.img
  accel: auto
  smp: auto
  memory_mib: 8192
  boot_cache: false
```

### Warm-start boot cache (`vm.boot_cache`)

Booting redroid in the micro-VM under TCG is **CPU-bound** — emulating ART / Zygote / `system_server` to `sys.boot_completed` costs ~3-4 min on a 4-core host (Android 14). Entropy and disk-cache tweaks do not move it (see [vm-rnd-log Stage E](../design/vm-rnd-log.md)). The one lever that does is to **not boot at all** on repeat starts:

```yaml
binder: vm
vm:
  kernel: ~/.cache/beetroot/vm/bzImage
  rootfs: ~/.cache/beetroot/vm/rootdisk.img
  boot_cache: true     # checkpoint once, resume in ~10 s thereafter
```

```bash
beetroot apply alpha
beetroot up alpha      # FIRST up: cold-boot (~3-4 min under TCG), then checkpoint
beetroot down alpha
beetroot up alpha      # LATER up: resume the checkpoint — ~10 s to a live device
```

Measured on a binderless, KVM-less host (Android 14, pure TCG): cold first boot to first host ADB **~222 s**; warm resume **~10 s** — a **~22x** speedup. How it works:

- The first `up` boots through a **qcow2 overlay** over the (untouched) raw rootfs, with an HMP monitor socket. Once ADB is reachable, Beetroot issues `savevm` to checkpoint the *running machine state* (RAM + device + disk) inside the overlay.
- Every later `up` detects the checkpoint and launches QEMU with `-loadvm`, resuming the already-booted guest in seconds.

Caveats:

- **Resume is ephemeral.** Each warm `up` reverts the guest to the checkpoint moment — it is a fast known-good boot, not a way to persist changes across restarts. (Persist research state with `beetroot snapshot` instead.)
- **The checkpoint auto-invalidates when the kernel or rootfs changes.** Beetroot records a digest of the `vm.kernel` + `vm.rootfs` the overlay was built from (`<instance>/vm-overlay.cache-key`); if either changes (e.g. you re-run `beetroot build --vm-kernel`), the next `up` discards the stale checkpoint and cold-boots once to re-cache — no manual cleanup needed. You can still delete `<instance>/vm-overlay.qcow2` by hand to force a fresh cold boot.
- Requires the `qemu-img` binary (Debian/Ubuntu: `qemu-utils`); override with `BEETROOT_QEMU_IMG_BIN`.

---

## Complete example

```yaml
api_version: 8

android:
  version: 14

display:
  width: 1080
  height: 1920
  fps: 10
  rendering: gpu

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
  - {service: adb, guest: 5555, host: 9000}
  - {service: frida, guest: 27042, host: 9001}
  - {service: frida_control, guest: 27043, host: 9002}
  - {service: app-debug, guest: 8080, host: 9090}
```
