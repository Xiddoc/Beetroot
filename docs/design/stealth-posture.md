# Stealth Posture: Threat Model and Design Doc

!!! info "Status: design only"
    This is a v0.3 design document. The mitigations described here are scheduled
    for v0.4 — no code in this repo currently implements them. See [v0.4
    implementation roadmap](#7-v04-implementation-roadmap) for the ordered list
    of PRs that will execute against this spec.

This doc lands the threat model, current exposure inventory, mitigation
playbook, and v0.4 implementation roadmap for Beetroot's stealth posture.
The goal is to give v0.4 implementers a written spec to build against, and
to stop ongoing themes ([T6 snapshots](../guides/snapshots.md),
[T7 helper scripts](../how-it-works/boot-flow.md#helper-scripts)) from
baking in new hardcoded paths that v0.4 will have to undo.

## 1. Threat model

Beetroot exists so a researcher can attach Frida to an app that suspects
it might be on a rooted device. That suspicion is driven by code shipped
in **GMS (Google Mobile Services)**, the **Play Integrity API** (which
replaced SafetyNet Attestation in 2024), and the **DroidGuard** VM that
backs both. The same fingerprinting code is also distributed in many
banking, gaming, and anti-cheat SDKs that re-use Google's attestation
results or run parallel checks of their own.

The detection surface they sweep is well documented. The categories
relevant to Beetroot's container are:

### 1.1 Frida-server fingerprints

- **The binary on disk at `/data/local/tmp/frida-server`.** The
  canonical path. A directory listing of `/data/local/tmp/` is the
  first thing nearly every commercial RASP product checks. The string
  `frida-server` inside any file in that directory is enough.
- **Abstract Unix sockets named `@re.frida.server` and
  `@frida:rpc-*`.** Visible to any process via `/proc/net/unix`. Frida
  binds these unconditionally on startup; the leading `@` denotes the
  abstract namespace.
- **Thread / process names containing `frida`.** Visible via
  `/proc/<pid>/comm`, `/proc/<pid>/cmdline`, and the per-thread files
  under `/proc/<pid>/task/<tid>/comm`. Frida spawns helper threads
  named `frida-helper`, `gum-js-loop`, `gmain`, and similar — those
  are nearly as distinctive as the literal string `frida`.
- **A listening TCP port (default 27042).** Less load-bearing because
  the port number is configurable, but a connect-and-banner-check
  reveals the D-Bus handshake unambiguously.

See [Frida.re's anti-anti-Frida notes](https://frida.re/docs/stalker/)
and the public write-up
[Detecting Frida the *Easy* Way (Pulsesecurity, 2024)](https://pulsesecurity.co.nz/articles/Detecting-Frida)
for catalogued detection patterns; the same indicators appear in
DroidGuard string tables.

### 1.2 Root / Magisk fingerprints

Most of these are mitigated today by `entrypoint.sh` enrolling
`com.google.android.gms` and `com.google.android.gms.unstable` in
Magisk's denylist, plus Zygisk's own
[Shamiko](https://github.com/LSPosed/LSPosed.github.io/releases/tag/shamiko)
module hiding Magisk-related paths from denylisted processes. They are
**documented here** because the mitigation is fragile — Shamiko is an
external module the user must opt into via `presets/stealth.yaml`, and
GMS occasionally ships new checks that aren't yet covered.

- **`/system/bin/su`, `/system/xbin/su`, `which su` returning non-empty.**
  Classic root checks.
- **`/data/adb/magisk*`, `/sbin/.magisk/`, `/data/adb/modules/`.**
  Magisk's own filesystem footprint. Shamiko hides these from
  denylisted callers via mount-namespace tricks.
- **`getprop ro.build.tags=test-keys`, `ro.debuggable=1`,
  `ro.secure=0`.** Build-flavor giveaways. redroid sets these to
  match a production AOSP build by default, but new GMS versions
  sometimes probe additional props (`ro.build.fingerprint` substring
  matches, `ro.bootloader=unknown`).
- **`/proc/<pid>/maps` containing `libmagisk.so` or `zygisk.so`.**
  Linker map inspection. Zygisk's own `DenyList` namespace switch
  removes these for denylisted processes.

### 1.3 Beetroot-specific fingerprints (this doc's focus)

These are the indicators **Beetroot itself adds** on top of redroid +
Magisk. They are the ones v0.4 has to mitigate, because no upstream
project will do it for us:

- **`/system/etc/init/stealth.rc`.** A non-AOSP init file with a
  literally-named giveaway. Init files in `/system/etc/init/` are
  enumerable by anything with permission to `readdir` that path
  (which is more processes than you'd think — the file mode is
  `0644`).
- **`/entrypoint.sh` at filesystem root.** Real Android images do not
  have a file at `/entrypoint.sh`. Any process that runs a
  `stat("/entrypoint.sh", &st)` and gets `ENOENT == 0` knows
  immediately it is inside a container.
- **`/flash_dir/` mount.** A non-Android mountpoint, visible in
  `/proc/mounts` and `/proc/self/mountinfo`. The name is also a
  giveaway — no production Android image mounts a directory called
  `flash_dir`.
- **Frida launched as PID-N child of `/entrypoint.sh`.** The parent
  process of `frida-server` is normally PID 1 (when launched via
  ADB-as-root) or a shell PID. Under Beetroot it's always our
  entrypoint shell, which has a distinctive cmdline.

### 1.4 Drift assumption

**This list grows.** Each new GMS / Play Services release ships new
fingerprints; researchers find and publish new detection vectors
quarterly. v0.4 ships a **CI fingerprint audit** (see
[§3.7](#37-ci-fingerprint-audit)) that runs an open-source detector
against the built image and fails CI on new hits, so we catch
regressions in *our own* container instead of waiting for users to
report them.

## 2. Current exposure inventory

The following indicators are present in the v0.3 container image
(`docker build` of `docker/Dockerfile` against the redroid base + any
preset). Mitigation owner refers to which v0.4 PR
(see [§7](#7-v04-implementation-roadmap)) takes responsibility.

| Indicator                          | Container path / signal                                                                | Risk     | Mitigation owner                   |
|------------------------------------|----------------------------------------------------------------------------------------|----------|------------------------------------|
| Frida binary at canonical path     | `/data/local/tmp/frida-server`                                                         | Critical | PR1 (frida path randomization)     |
| Frida thread / cmdline names       | `/proc/<pid>/comm`, `/proc/<pid>/task/<tid>/comm` containing `frida` / `gum-js-loop`   | Critical | PR3 (name evasion) + PR2 (Gadget)  |
| Frida abstract socket              | `@re.frida.server` and `@frida:rpc-*` in `/proc/net/unix`                              | Critical | PR2 (Gadget — no server, no socket)|
| Frida TCP listener                 | `:27042` (default) bound on all interfaces inside container                            | Medium   | PR2 (Gadget removes the listener)  |
| Custom init.rc                     | `/system/etc/init/stealth.rc`                                                          | High     | PR4 (move to Magisk module)        |
| Entrypoint shell at fs root        | `/entrypoint.sh` (mode 0755, owned by root)                                            | High     | PR4 (move to Magisk module)        |
| Module flash directory mount       | `/flash_dir/` bind-mount; visible in `/proc/mounts`                                    | High     | PR5 (move to `modules_update/`)    |
| Magisk on-disk paths               | `/data/adb/magisk*`, `/sbin/.magisk/`                                                  | High     | upstream (Shamiko); we just denylist|
| Magisk DB SQL write traces         | `/data/adb/magisk.db` modified by us; mtime / access patterns observable               | Low      | PR4 (helper runs in standard phase)|
| `BEETROOT_*` env vars in container | Container env visible via `/proc/1/environ` (PID 1 is `init`, normally locked-down)    | Low      | PR1/PR6 (use already-stealthed names)|
| Parent PID of frida-server         | `getppid(2)` returns the entrypoint shell's PID, not `init`                            | Medium   | PR2 (Gadget — Zygote is parent)    |

The **Mitigation owner** column references the v0.4 PRs in
[§7](#7-v04-implementation-roadmap). The
[T7 helper script contracts](../how-it-works/boot-flow.md#helper-scripts)
already accept all container paths as `BEETROOT_*` env vars with safe
defaults; see [§4](#4-interaction-with-t7-helper-shell-scripts) for the
full list. v0.4's path randomization (PR1, PR4, PR5, PR6) is therefore a
pure compose-template + env-var change — no helper code edits.

## 3. Mitigation playbook (priority order)

The order below is **risk-weighted** — start with what most reliably
trips DroidGuard's filesystem sweep, then work down to the indicators
that only fire on second-pass scans. Items 1–5 are independent and can
ship as separate PRs in parallel; items 6 and 7 are cross-cutting and
land after.

### 3.1 Move Frida off `/data/local/tmp/`

The single highest-value change. Frida-server moves from
`/data/local/tmp/frida-server` to a per-build randomized path of the
form `/data/adb/modules/<random>/bin/<random>`, where the two
`<random>` segments are independent base32 strings of at least 12
bytes each, generated at `beetroot create` time and persisted in the
registry.

Living under `/data/adb/modules/` has three benefits:

- The directory is automatically Magisk-denylisted — denylisted
  processes (notably GMS) can't `readdir` it via Shamiko's namespace
  switch.
- It looks like a legitimate Magisk module install, so even if a
  detection scan widens to `/data/adb/`, the contents pattern-match
  Magisk's own naming.
- The path includes the executable's containing module ID, so killing
  the module also removes Frida — no orphaned binary across resets.

The host bind-mount target moves accordingly; `frida_dl.py` is
unchanged because it never knew the container-side path (the CLI
reads it from settings, not from a constant).

### 3.2 Frida Gadget mode via Zygisk

The **long-term answer**, and the one that makes most of §1.1 vanish.
Instead of running `frida-server` as a standalone process, ship
`frida-gadget.so` as a Zygisk module that injects into a configured
list of target processes at fork time. There is no server process,
no listening socket, no on-disk binary that isn't already a Zygisk
module library, and the parent PID is Zygote (which is normal).

For the host-side consumer: `beetroot frida` no longer connects via
TCP host port-forward to a server. Instead, the Gadget library opens
a Unix socket on a configurable path inside `/data/local/tmp/` (also
randomized), and `beetroot frida` shells out to `frida -H` against an
ADB-forwarded local port that wraps that socket. The CLI surface is
unchanged; only the internal transport differs. Researchers who use
the Python API switch from
`frida.get_device_manager().add_remote_device("localhost:27042")` to
`frida.get_usb_device().attach(target)` after running
`adb forward tcp:27042 localfilesystem:<socket-path>` (the CLI's
`beetroot env` will print the right forward command).

This is **harder than items 3.1 / 3.4** because it changes the
host-side wire protocol and breaks the
[current Frida guide's "remote device" example](../guides/frida.md#using-frida-tools-python-api).
It is item 2, not item 1, on purpose: ship 3.1 first so users who
need a server-mode Frida (e.g. for `frida-trace -i`) can still get
one with the worst path-fingerprint already mitigated.

### 3.3 Process / thread name evasion

Cover the indicators that survive §3.1 — the thread names visible in
`/proc/<pid>/task/<tid>/comm`. Two approaches, in increasing
implementation cost:

- **`LD_PRELOAD` shim** that intercepts `prctl(PR_SET_NAME, ...)`
  calls and rewrites strings matching `frida` / `gum-` / `gjs-` to
  innocuous names (e.g. `RenderThread`, `binder:`). Cheap; can be a
  100-line `.so` we ship in the Magisk module from §3.1.
- **Patched Frida build** that renames threads at compile time. The
  proper fix, but expensive: requires forking frida-core, tracking
  upstream releases, rebuilding on every Frida version bump. Probably
  worth doing as a CI job that builds + caches the patched binaries
  per version.

Note that this is **incomplete** by construction. Frida's own
internals use thread names for routing and debugging; some are
load-bearing and renaming them breaks Frida (e.g. `gum-js-loop`
is what Frida looks for when bridging to the JS engine). v0.4
implementers should expect to ship a hand-curated rename map, not a
blanket regex.

### 3.4 Replace `/entrypoint.sh` + `stealth.rc` with a Magisk module

This is the largest single drop in visible-from-userspace surface.

The Magisk module model: a directory under
`/data/adb/modules/<random>/` containing a
`service.d/<random>.sh` script. Magisk runs all `service.d/*.sh`
scripts during the standard `post-fs-data` phase, under the same
SELinux context (`u:r:magisk:s0`) we use today. **There is no
custom init.rc**, **no `/entrypoint.sh` at filesystem root**, and the
script's parent directory is denylist-hidden from GMS just like
§3.1.

The Dockerfile's `COPY` step changes from copying into
`/system/etc/init/` and `/` to copying into the staging area for
Magisk's `modules_update/` directory (see §3.5), and a `module.prop`
file gets generated at build time with a randomized module ID.

The boot trigger changes from "`sys.boot_completed=1` → init runs
exec_background" to "Magisk post-fs-data → all `service.d/*.sh` run".
This is *earlier* than `sys.boot_completed=1`, so the script has to
poll for the Zygote start before it can write to the denylist —
[T7's `magisk-config.sh`](../how-it-works/boot-flow.md#helper-scripts)
already does this, so no helper change needed.

### 3.5 Drop `/flash_dir`; bind-mount to `/data/adb/modules_update/`

`/flash_dir` is a Beetroot-invented name with no upstream meaning.
Magisk already has a well-known directory for staging modules
pending install: `/data/adb/modules_update/`. When Magisk's daemon
sees a complete module tree in that directory at boot, it moves it
to `/data/adb/modules/` and runs the install.

The host-side bind-mount target moves accordingly; `compose.yaml`
gets a new `${MODULE_STAGE_PATH}` substitution that defaults to
`/data/adb/modules_update`. The CLI's
[`apply` flow](../reference/cli.md) is unchanged because it doesn't
know the container-side path either.

### 3.6 Per-build path randomization

§3.1, §3.4, and §3.5 each introduce a `<random>` segment. They are
**not independent random values**; v0.4 generates a single
`stealth_paths: dict[str, str]` blob at `beetroot create` time and
persists it in the registry entry. Subsequent `up` / `down` /
`apply` / `snapshot` / `restore` operations read from the same blob,
so the path stays stable across the instance's lifetime.

The compose template gets new env vars:

- `BEETROOT_FRIDA_BIN` — full path to frida-server inside container.
- `BEETROOT_MODULES_DIR` — container-side bind-mount target for
  `instances/<name>/modules/`.
- `BEETROOT_STEALTH_MODULE_ID` — randomized Magisk module ID that
  houses our service.d script and (optionally) the Gadget `.so`.
- `BEETROOT_FRIDA_SOCKET_PATH` — Unix socket path for Gadget mode
  (unused when Gadget is disabled).
- `BEETROOT_THREAD_NAME_SHIM` — path to the `LD_PRELOAD` shim from
  §3.3 (also lives inside the stealth module).

All five are read by [T7's helper scripts](#4-interaction-with-t7-helper-shell-scripts)
already; v0.4 just wires the values in.

### 3.7 CI fingerprint audit

The drift problem from §1.4 says we can't ship a static mitigation
list. The mitigation is automation: run an open-source root /
Frida detector against the built image on every CI run, and fail
the build if a new indicator appears that wasn't on a known-issues
allowlist.

Candidate detectors (both run from an APK installed via ADB into the
booted container):

- **[RootBeer](https://github.com/scottyab/rootbeer)** — long-running
  Android library used by many production apps; covers Magisk, su
  binaries, dangerous props, RW system mounts.
- **[FridaAntiRootDetectionBypass test
  harness](https://github.com/iddoeldor/frida-snippets#anti-root-detection)**
  — a known-good Frida-detection test app, useful for sanity-checking
  that we *also* defeat the detection it implements.

A nightly job pulls the latest of each, installs into a freshly
built Beetroot, and fails on new positives. Existing-and-accepted
positives live in a YAML allowlist checked into the repo with a
short rationale per entry — adding to the allowlist requires PR
review just like any other source change.

## 4. Interaction with T7 (helper shell scripts)

[T7 lands three helper shell scripts](../how-it-works/boot-flow.md#helper-scripts)
that today live in `docker/`: `magisk-config.sh`, `flash-modules.sh`,
and `launch-frida.sh`. Each reads every container-side path from
`BEETROOT_*` env vars with safe defaults, so v0.4's path
randomization is a pure compose-template + env-var change with **no
helper code edits required**.

The full env-var contract:

| Env var                        | Default                              | Consumer            |
|--------------------------------|--------------------------------------|---------------------|
| `BEETROOT_FRIDA_BIN`          | `/data/local/tmp/frida-server`       | `launch-frida.sh`   |
| `BEETROOT_FRIDA_SOCKET_PATH`   | unset (TCP mode)                     | `launch-frida.sh`   |
| `BEETROOT_MODULES_DIR`   | `/flash_dir`                         | `flash-modules.sh`  |
| `BEETROOT_MAGISK_DB`           | `/data/adb/magisk.db`                | `magisk-config.sh`  |
| `BEETROOT_DENYLIST_PACKAGES`   | `com.google.android.gms,com.google.android.gms.unstable` | `magisk-config.sh` |
| `BEETROOT_STEALTH_MODULE_ID`   | unset (legacy init.rc mode)          | (build-time only)   |
| `BEETROOT_THREAD_NAME_SHIM`    | unset (no shim)                      | `launch-frida.sh`   |

When `BEETROOT_STEALTH_MODULE_ID` is set, the build switches to
Magisk-module-mode (§3.4); when it's unset, the build keeps the
v0.3 init.rc layout. This gives a smooth migration path: §3.4 lands
behind an opt-in flag first, then becomes the default once the CI
fingerprint audit is green for both modes.

## 5. Interaction with T6 (snapshots)

[T6 introduces a snapshot manifest](../guides/snapshots.md) — a
machine-readable file alongside the data tarball that records what
the snapshot is and how to restore it.

The manifest **must carry the source instance's container-path
mapping**, otherwise a snapshot taken from a randomized-path source
will restore against the destination's defaults and produce a
device where Frida lives at one path but the data dump references
another.

Concretely, the manifest gains:

```yaml
path_layout:
  frida_path: /data/adb/modules/x7q.../bin/svc
  module_stage: /data/adb/modules_update
  stealth_module_id: x7q4z2m1jhkr
  frida_socket: /data/local/tmp/.x7q.sock   # if Gadget mode
```

On `beetroot restore`, the CLI:

1. Reads `path_layout` from the manifest.
2. Overwrites the target instance's randomized-paths registry blob
   with the manifest's values.
3. Re-renders `.env` so the compose template binds the same paths.
4. Starts the instance; everything inside `/data/` now points where
   the snapshotted environment expects.

This makes snapshots **portable across hosts and across Beetroot
versions** as long as the schema doesn't change. A v0.4 → v0.5
snapshot migration would bump a `path_layout_version` field.

## 6. Interaction with T9 (device backends)

T9 introduces `AdbDeviceBackend` for running Beetroot's tooling
against a real rooted phone, not just the emulator. Several of the
mitigations in §3 **don't apply** to that backend, because we don't
control the device's filesystem or build:

| Mitigation               | Emulator (default) | `AdbDeviceBackend` (T9)              |
|--------------------------|--------------------|--------------------------------------|
| §3.1 Frida path random.  | Yes                | Partial — randomize within `/data/local/tmp/` only; can't write to `/data/adb/modules/` without a user-installed Magisk module. |
| §3.2 Gadget via Zygisk   | Yes                | Yes, if user has Zygisk installed and lets us drop a module. Manual setup. |
| §3.3 Thread-name evasion | Yes                | Yes — the `LD_PRELOAD` shim works wherever Frida runs. |
| §3.4 Magisk-module init  | Yes                | **No.** Device's init.rc is whatever the user's ROM ships; we never modify it. The user's existing Magisk install (if any) is what we have to work with. |
| §3.5 `modules_update/`   | Yes                | N/A — we don't bind-mount anything on a real device. |
| §3.6 Build-time random.  | Yes                | **No.** "Build" doesn't exist for the device backend. Randomization happens once at `beetroot create` time and is then a property of the user's device install. |
| §3.7 CI fingerprint audit| Yes                | **No.** CI doesn't have a real phone. Optional: opt-in nightly run against a researcher's spare device. |

The CLI surface is unchanged — researchers who switch from emulator
to device backend see the same `beetroot frida` / `beetroot apply`
commands. The backend abstraction (T9) hides the
which-mitigations-apply detail behind feature flags on the backend
class.

## 7. v0.4 implementation roadmap

Ordered list of PRs that implement §3. Each PR cites the section it
executes against. Complexity tags: **S** (≤1 day), **M** (2–3 days),
**L** (≥1 week including review).

### PR1 — Frida path randomization

- **Scope:** Implement §3.1. Generate randomized
  `/data/adb/modules/<random>/bin/<random>` at `beetroot create`,
  persist in registry, wire into compose template via
  `BEETROOT_FRIDA_BIN`. T7's `launch-frida.sh` already reads it.
- **Complexity:** S.
- **Unblocks:** The single highest-risk indicator from §2. Makes
  `/data/local/tmp/frida-server` scans miss us.

### PR2 — Frida Gadget mode

- **Scope:** Implement §3.2. Add a `frida.mode: server | gadget`
  field to `beetroot.yaml` schema; in `gadget` mode, replace the
  server bind-mount with a Zygisk module bind-mount under the
  randomized module dir from PR1; update `beetroot frida` to use
  ADB-forwarded Unix socket transport.
- **Complexity:** L.
- **Unblocks:** Eliminates §1.1's binary, socket, and listener
  indicators in one move. Required for stealth research against
  modern GMS.

### PR3 — Thread-name `LD_PRELOAD` shim

- **Scope:** Implement §3.3 step 1 (the shim, not the patched-Frida
  alternative). 100-line C source built into a `.so` at Docker build
  time; bundled into the stealth Magisk module from PR4; loaded via
  `LD_PRELOAD` in `launch-frida.sh` when `BEETROOT_THREAD_NAME_SHIM`
  is set.
- **Complexity:** M.
- **Unblocks:** Covers the residual `/proc/<pid>/task/<tid>/comm`
  hits left over from PR1+PR2.

### PR4 — Stealth Magisk module replaces init.rc + entrypoint.sh

- **Scope:** Implement §3.4. Generate `module.prop` and
  `service.d/<random>.sh` at Docker build time with randomized IDs.
  Drop `docker/stealth.rc` from `Dockerfile`'s COPY list. Move
  `entrypoint.sh` (and its T7 split) into the module's `service.d/`.
  Behind opt-in flag `BEETROOT_STEALTH_MODULE_ID` until CI is green.
- **Complexity:** L.
- **Unblocks:** Eliminates §1.3's `/system/etc/init/stealth.rc` and
  `/entrypoint.sh` indicators. The biggest single drop in our
  Beetroot-specific surface.

### PR5 — `/flash_dir` → `/data/adb/modules_update/`

- **Scope:** Implement §3.5. Change `compose.yaml`'s bind-mount
  target via `${MODULE_STAGE_PATH}`. Update T7's `flash-modules.sh`
  default fallback. Drop the `/flash_dir` directory from `Dockerfile`'s
  setup.
- **Complexity:** S.
- **Unblocks:** Removes the `flash_dir` indicator from `/proc/mounts`.

### PR6 — Stable randomized-path registry blob

- **Scope:** Implement §3.6. Add `stealth_paths: dict[str, str]` to
  the registry entry. Generated at `create`, read by `apply`, `up`,
  `down`, `snapshot`, `restore`. T6 (PR-for-T6) consumes
  `stealth_paths` for snapshot manifests per §5.
- **Complexity:** M.
- **Unblocks:** Makes PR1, PR4, PR5 deterministic across an
  instance's lifetime — without this, every `apply` would re-randomize
  paths and break the existing data dir.

### PR7 — CI fingerprint audit

- **Scope:** Implement §3.7. New GitHub Actions job
  `.github/workflows/fingerprint-audit.yml`: build image, boot via
  redroid, install RootBeer + the Frida-snippets test APK, scrape
  results, diff against `docs/design/fingerprint-allowlist.yaml`.
- **Complexity:** L (mostly: getting redroid to boot in GHA is the
  hard part).
- **Unblocks:** §1.4's drift assumption. Without this, every GMS
  update is a manual triage exercise.

### PR8 — Documentation pass

- **Scope:** Update [Frida guide](../guides/frida.md), [Boot flow](../how-it-works/boot-flow.md),
  [Filesystem layout](../how-it-works/filesystem.md), and
  [Snapshots guide](../guides/snapshots.md) to reflect the new
  paths and Gadget option. Move this doc out of "Design notes" once
  v0.4 ships and into a normal reference page.
- **Complexity:** S.
- **Unblocks:** The "docs are part of every feature" rule from
  `CLAUDE.md`.

## 8. Out of scope (explicitly)

The following are real fingerprints that researchers will encounter,
but they are **not Beetroot's to fix**:

- **Kernel-level emulator artifacts.** `/dev/socket/qemud`,
  `/dev/qemu_pipe`, `/sys/module/qemu_pipe`, CPU feature flags
  (`AT_HWCAP` differences, missing AES-NI, etc.), missing or
  obviously-simulated sensors (`SENSOR_TYPE_ACCELEROMETER` returning
  perfectly flat data forever). These are
  [redroid](https://github.com/remote-android/redroid-doc)'s
  responsibility, not Beetroot's. v0.4 will document the residual
  list as a "known emulator tells" section but will not patch them.
  Researchers who need hardware-grade stealth should use the T9
  device backend on a physical rooted phone.
- **Active Play Integrity attestation backed by hardware keys.**
  Modern devices store an attestation key in the StrongBox / TEE
  that signs a server-issued nonce, proving that the boot chain
  hasn't been tampered with. *No software-only approach* in any
  research tool defeats this. Researchers who hit it have to switch
  to either an unpatched device (rare) or to a target that doesn't
  enforce hardware-backed attestation. This is an entirely separate
  research problem and v0.4 will not attempt to address it.
- **Frida's network-level fingerprints when used with `beetroot
  frida` over a host port.** A network observer between the host
  CLI and the container can see the Frida D-Bus handshake. This
  doesn't matter inside the container itself (the kernel is ours;
  loopback traffic isn't inspectable from a malicious app), but it
  matters for cross-host setups. v0.4 won't address it; v0.5 might
  via TLS-wrapped transport.
- **Compromised host kernels.** If the host running Docker is
  compromised, the container's stealth posture is moot — the
  attacker has higher-privileged access than the app being studied.
  Out of scope by definition.

## References

- [Frida documentation index](https://frida.re/docs/).
- [Pulse Security — Detecting Frida the *Easy* Way](https://pulsesecurity.co.nz/articles/Detecting-Frida).
- [Magisk denylist documentation](https://topjohnwu.github.io/Magisk/).
- [LSPosed Shamiko releases](https://github.com/LSPosed/LSPosed.github.io/releases/tag/shamiko).
- [Google Play Integrity API overview](https://developer.android.com/google/play/integrity).
- [redroid documentation](https://github.com/remote-android/redroid-doc).
- [RootBeer](https://github.com/scottyab/rootbeer).
- [iddoeldor/frida-snippets — anti-root-detection bypass](https://github.com/iddoeldor/frida-snippets#anti-root-detection).
