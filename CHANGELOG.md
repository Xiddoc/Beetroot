# Changelog

## Unreleased

### Breaking changes

- **`ports` generalised to a list of named guest→host mappings; `api_version` bumped 7 → 8 (#108).**
  The old `ports:` block was a fixed mapping that only overrode the HOST side of
  exactly three well-known services (`ports: {adb, frida, frida_control}`); the
  guest side was hardcoded in the compose template. It's now a **list** of
  `{service, guest, host}` mappings supporting arbitrary services and explicit
  guest ports:

  ```yaml
  ports:
    - {service: adb, guest: 5555, host: 9000}   # pin a well-known host port
    - {service: frida, guest: 27042}            # host unset → stride default
    - {guest: 8080, host: 9090}                 # arbitrary, explicit host
    - {service: metrics, guest: 9100}           # arbitrary, auto-allocated host
  ```

  `service` is optional (`adb` / `frida` / `frida_control` are the well-known
  names the stride allocator and the `adb_address` / `frida_address` accessors
  key off). `host` unset auto-allocates — a stride base for a well-known service,
  or a dedicated extra-pool slot (`40000 + index*10 + slot`) for an arbitrary
  one. An instance can auto-allocate at most `STRIDE` (10) arbitrary host-unset
  ports before pre-validation rejects it (the next slot would spill into the next
  index's window); pin explicit host ports for entries beyond that. The
  variable-length port list can't live in the flat `.env`, so it is
  rendered into a per-instance `compose.override.yaml` that the CLI layers on top
  of the bundled template with a second `-f`. **Migration:** rewrite the old
  mapping as a list and set `api_version: 8`. A YAML still carrying the old
  mapping form with only well-known keys is migrated losslessly on load (one-line
  note) and auto-bumps; a mapping with any non-well-known key raises a migration
  error naming the new list shape; a YAML pinning `api_version: 7` with a
  list-form (or absent) `ports` auto-bumps silently, exactly like the 6 → 7
  `gapps` handling. Under `binder: vm` only adb is forwarded to the guest;
  arbitrary mappings beyond the well-known services are ignored (`beetroot up`
  warns, mirroring the gapps/frida vm-inert advisories).

- **`android.gapps` split into intent + vendor; `api_version` bumped 6 → 7 (#107).**
  The old `gapps: none | lite | full | mindthegapps` enum fused two axes: *intent*
  (what you get) and *vendor* (which distribution bakes it). It's now split so the
  easy path stays easy and the compatibility path stays possible:
  `gapps: none | minimal | full` is the **intent** (the field 95% of users touch),
  and a new optional `gapps_vendor: litegapps | opengapps | mindthegapps` is the
  **vendor** escape hatch for apps that detect or prefer a specific GApps build.
  Unset, the intent picks the vendor (`minimal` → LiteGApps, `full` → OpenGApps),
  reproducing the historical base-image tags exactly. `gapps: none` and
  `gapps: full` are unchanged. **Migration:** replace `gapps: lite` with
  `gapps: minimal`, and `gapps: mindthegapps` with `gapps: full` +
  `gapps_vendor: mindthegapps` (the resulting base image is byte-for-byte the
  same), then set `api_version: 7`. A YAML still carrying a vendor-named `gapps`
  is rejected at load with this mapping; a YAML pinning `api_version: 6` *without*
  a vendor-named `gapps` auto-bumps to 7 on load (one-line warning), exactly like
  the 5 → 6 `lifecycle` handling. `beetroot build` likewise takes the intent
  positionally (default `minimal`) plus an optional `--gapps-vendor`; the reusable
  CI workflow gains a matching `gapps-vendor` input.

- **`frida` passthrough verb replaced by `beetroot frida-addr` (#109).**
  The old `frida` passthrough verb was a thin wrapper that ran the host
  `frida` CLI with `-H localhost:<frida_port>` prepended. Its only real value was
  resolving the instance's stride-allocated Frida port — and to do it it consumed
  every argument opaquely, which silently broke `frida`'s own shell completion,
  `--help`, and flag validation. It's replaced by **`beetroot frida-addr <name>`**,
  which prints just the address (`localhost:<frida_port>`) to stdout so you invoke
  native `frida` directly and keep all of its ergonomics:

  ```bash
  frida -H "$(beetroot frida-addr alpha)" -n com.target.app
  ```

  The emitter needs nothing installed (it only resolves a port), generalises to
  future transports (e.g. Frida Gadget, #3), and is less code than the wrapper.
  **Migration:** where you ran the old `frida` passthrough verb, now run
  `frida -H "$(beetroot frida-addr <name>)" <args...>`. The programmatic
  `Instance.frida_cli(...)` API and the `[frida]` extra are unchanged.

- **`display.gpu_mode` → `display.rendering`; `api_version` bumped 4 → 5 (#106).**
  The least-validated field in the schema (`gpu_mode: str = "host"` — not even a
  `Literal`, so `gpu_mode: hostt` passed silently) is replaced by an intent-named,
  validated enum `rendering: gpu | software | auto`, defaulting to `auto`. `auto`
  probes the host for a DRM render node (`/dev/dri/renderD*`) and picks `gpu` when
  present, else `software` — so a headless / GPU-less box renders in software
  instead of silently misbehaving under the old aggressive `host` default. The
  value is mapped to redroid's `gpu_mode` string internally (`gpu`→`host`,
  `software`→`guest`). **Migration:** replace `display.gpu_mode: host` with
  `rendering: gpu`, `gpu_mode: guest` with `rendering: software` (or just
  `rendering: auto`), and set `api_version: 5`. A YAML that still carries
  `display.gpu_mode` is rejected at load with this mapping; a YAML pinning
  `api_version: 4` *without* `gpu_mode` auto-bumps to 5 on load (one-line
  warning), exactly like the 3 → 4 `stealth:` handling.

### Features

- **Prebuilt, zstd-compressed `binder: vm` guest rootfs fetch (#79).**
  `beetroot build --vm-kernel` previously always assembled the guest rootfs
  locally — pulling + baking a ~2 GB redroid image into `/var/lib/docker`, which
  needs a running Docker daemon and a Docker Hub round-trip a fresh host
  (CI runner, Claude Code on the web sandbox) can't get cheaply. The rootfs is
  now **fetched prebuilt by default**, reusing the prebuilt-kernel scheme's
  per-fingerprint-immutable-release + `.sha256`-sidecar mechanics: a new
  `beetroot.rootfs_download` module computes a composite fingerprint over three
  rootfs-shaping inputs (Android major version, pinned Docker static-bundle
  version, and `guest-init.sh`), downloads the matching zstd-compressed ext4
  image + its `.sha256` sidecar from a per-fingerprint immutable GitHub release
  (`vm-rootfs-<version>-<fp>`), verifies the digest of the **streamed**
  compressed bytes before stream-decompressing to disk (neither the multi-GiB
  payload nor the ~8 GiB image is ever held in memory), and writes the
  `.android-version` skew marker the local bake also writes. On any miss (a
  fingerprinted input changed, the release isn't published, or the network is
  blocked) it falls back to the local bake. **Fingerprint caveat:** unlike the
  kernel fingerprint (a hash of a single vendored config file that *fully*
  determines the build), this fingerprint does **not** cover every input to the
  baked bytes — the local bake also folds in host-resolved static-binary
  versions (busybox/socat/iptables + libc), `adbprobe`, and the resolved redroid
  image. Same-fingerprint *prebuilts* are byte-identical (CI pins those), but a
  *local* bake on a host with different static binaries can legitimately differ;
  this is a documented, weaker invariant than the kernel scheme, not a
  one-to-one mirror. The power-user bake-override env vars
  (`REDROID_TAR`/`REDROID_IMAGE`/`IMAGE_SIZE_MB`/`DOCKER_URL`) sit outside the
  fingerprint, so setting any of them **skips the prebuilt fetch and forces a
  local bake** — keeping the documented `REDROID_TAR` rate-limit workaround
  honest. The heavyweight bake-only host prerequisites
  (busybox/socat/iptables/`ldd`/`mke2fs` + a responsive Docker daemon) are now
  enforced **only when a local bake actually runs**, so the default fetch path
  works on a dockerless/busybox-less host; the lightweight `curl`/`tar` fetch
  prerequisites are still checked up front. `--from-source` forces a local build
  of **both** the kernel and the rootfs. A sibling `rootfs-release.yml` workflow
  bakes + publishes one release per (version, fingerprint), with its
  `DOCKER_VERSION` and Android-version matrix drift-guarded against the CLI by
  unit tests. No schema / `api_version` change — purely additive.

- **First-class `lifecycle: ephemeral | durable` persistence intent; `api_version`
  bumped 5 → 6 (#124).** Beetroot is bimodal in practice — some instances are
  long-lived "research phones" whose `/data` must survive (the namesake
  guarantee), others are throwaway (CI/E2E, comparative fleets, reset between
  runs). That intent used to live only in tribal knowledge and scattered flags
  (`vm.boot_cache`, `destroy`, the `rm -rf data/` recipe). It's now a committed,
  greppable, top-level `beetroot.yaml` field, **default `durable`** (preserves
  today's contract exactly). It is a **label + guardrails, not a runtime
  persistence switch**: `beetroot down` never wipes `/data` for either value;
  only `destroy` / `reset` (and a `vm.boot_cache` warm resume) drop it. Effects:
  `beetroot create --lifecycle ephemeral|durable` writes the key; `destroy`
  escalates its confirmation copy for a `durable` instance; an `ephemeral`
  instance opts into `vm.boot_cache`'s revert-on-resume **quietly** (the #123
  advisory is suppressed — a reset each boot is what `ephemeral` asked for); and
  the snapshot manifest stamps `lifecycle` (pre-field archives restore as
  `durable`). The 5 → 6 bump is strictly additive — a YAML omitting `lifecycle`,
  or pinning `api_version: 5`, auto-bumps on load (silent) and defaults to
  `durable`. Migration: nothing required; optionally add `lifecycle:` and
  `beetroot apply`.
- **`beetroot build --vm-kernel` preflights all host prerequisites in one pass
  (#78).** Assembling the micro-VM rootfs used to fail on a single missing host
  dependency per run — busybox → socat → iptables → a running Docker daemon →
  the Docker Hub pull-rate limit — each a raw `[Errno 2]` with no install hint,
  forcing ~5 full re-runs to enumerate the prerequisites. The build now runs a
  preflight that checks every prerequisite (`busybox`/`socat`/`iptables-legacy`,
  `curl`/`tar`/`ldd`/`mke2fs`, the Docker CLI + a responsive daemon) up front and
  reports **all** of them together, each with the apt package (or command) that
  fixes it, before building. `beetroot build --vm-kernel --check` runs just the
  preflight (exit 0 = ready, 1 = missing prerequisites) without building. The
  daemon check is skipped when `REDROID_TAR` is set, and the daemon-down hint
  names the Docker Hub rate-limit workarounds (`REDROID_TAR` / a registry
  mirror).
- **Opt-in on-device diagnostics capture in the reusable CI workflow (#118).**
  The reusable workflow (`.github/workflows/beetroot-ci.yml`) used to capture
  almost nothing when a caller's `test-command` failed — only the last ~200
  lines of `beetroot logs` (host-side container/serial console), with **no
  on-*device* diagnostics**, and because a reusable workflow structurally can't
  add steps after the caller's `test-command`, callers couldn't bolt on their
  own `actions/upload-artifact` either (a gotcha `docs/guides/ci-reusable-workflow.md`
  documented). Two new inputs close the gap: `capture-diagnostics` (boolean,
  default `false`) and `artifact-name` (default `beetroot-diagnostics`). When
  `capture-diagnostics: true`, an `always()` step running **before** teardown
  destroys the instance collects an on-device bundle — `adb logcat -d`, a
  *bounded* `dumpsys` subset (`activity` / `meminfo` / `window` / `package` /
  `battery`, never a full `bugreport`), a `screencap` PNG, `/data/tombstones`
  (native crash dumps), and the LSPosed per-module logs from `/data/adb/lspd/log/`
  (where `XposedBridge.log(...)` lands), plus the host-side `beetroot logs`
  tail — and `actions/upload-artifact` (SHA-pinned, `if: always()`) uploads it
  so it survives `beetroot destroy`. Every probe is best-effort (`|| true`, no
  `set -e`), so a missing tool, an offline device, or an absent file never fails
  the job. The default (`capture-diagnostics: false`) is byte-for-byte the prior
  behaviour — no new artifact, no extra cost — so existing callers are
  unaffected. Docs:
  [CI integration § Persisting test output](https://iliketo.party/Beetroot/guides/ci-reusable-workflow/#persisting-test-output).
- **`binder: vm` `boot_cache` auto-invalidates when the kernel/rootfs changes
  (#126).** The warm-start qcow2 overlay used to resume whatever checkpoint it
  held — even one taken against a since-rebuilt kernel/rootfs — and the only
  guard was a docs note telling you to delete `vm-overlay.qcow2` by hand. Now
  Beetroot records a digest of the `vm.kernel` + `vm.rootfs` the overlay was
  built from (`<instance>/vm-overlay.cache-key`, the same content-hash shape as
  `scripts/vm_cache_key.py`); on each warm `up` it compares that against the
  current artifacts and, on a mismatch (or a pre-#126 overlay with no recorded
  identity), discards the stale checkpoint and cold-boots once to re-cache — no
  manual cleanup. Resolves the "delete it yourself after rebuilding" footgun.
- **New `beetroot reset` verb — drop an instance's `/data` while keeping the
  instance (#127).** Promotes the ad-hoc "`rm -rf data/`" fresh-start recipe to
  a first-class, confirmation-gated verb. `reset` stops the container and wipes
  the bind-mounted `data/` (redroid regenerates a clean `/data` from the base
  image on the next `up`), but — unlike `destroy` — keeps the instance's
  identity (registry row, port index) and its staged tooling (`frida-server` /
  `modules/` live outside `/data`). It's the explicit counterpart to the silent
  `boot_cache` `/data` revert, and the clean between-runs reset primitive for
  the Frida / CI loops. redroid-only for now (a new `Resettable` capability
  sub-protocol); `binder: vm` keeps `/data` in the guest and adb devices have no
  host-side `/data`, so both report a capability error.
- **`frida.version` now accepts `auto` / `latest` and defaults to `auto` (#105).**
  The frozen `16.4.10` default rotted with every upstream release. `frida.version`
  now takes `auto` (the new default) — match the host's installed `frida-tools`
  version so the staged server and the client you attach with agree on
  major+minor, falling back to `latest` when `frida-tools` isn't installed —
  `latest` (resolved to a concrete tag via GitHub's latest-release redirect at
  download time), or a pinned `major.minor.patch` exactly as before
  (reproducible, and now *required* if you also set `frida.sha256`, since a
  digest can't match a moving target). `beetroot apply` warns when a pinned /
  `latest` server's major+minor diverges from the host `frida-tools`, because
  the `frida` client would otherwise fail to attach.
- **Full LSPosed module-hook e2e — proves a real Xposed hook fires (#29).**
  Completes the LSPosed recipe with an end-to-end test of the *whole* pipeline:
  flash Vector (LSPosed) → install an Xposed module **as an app** → enable it in
  scope via `modules_config.db` → launch the target → assert the module's method
  hook fired. Ships a minimal, self-contained Xposed module fixture
  (`tests/fixtures/xposed-hook-module/` — sources + `build.sh` + prebuilt apk)
  that hooks `android.app.Activity` `onCreate`/`onResume` and logs
  `BEETROOT_HOOK_FIRED`, a reusable driver (`scripts/lsposed-hook-e2e.sh`,
  `setup`/`check` phases around a reboot), a structural fixture test, and a
  non-blocking `e2e.yml` `tier-lsposed-hook` CI job. **Verified live on the
  `binder: vm` TCG VM**: launching Settings produced `BEETROOT_HOOK_FIRED
  onCreate` and `onResume`. Docs:
  [guides/lsposed § automated e2e](https://iliketo.party/Beetroot/guides/lsposed/#automated-end-to-end-test).
  (Gotcha captured in the fixture: Vector obfuscates + remaps the
  `de.robv.android.xposed` API, so a module's compile-only stubs must match the
  real signatures exactly — `findAndHookMethod` returns `XC_MethodHook.Unhook`.)

- **First-class LSPosed / Vector (Xposed) recipe (#29).** A new guide
  ([guides/lsposed.md](https://iliketo.party/Beetroot/guides/lsposed/)) plus a
  pinned `examples/lsposed.yaml` turn "run a real LSPosed install" into a
  declarative flow: add the Vector (LSPosed) Zygisk module to `modules:`, boot,
  `beetroot restart` (Zygisk goes live on the second boot), then install an
  Xposed module **as an app** (so the package manager populates its
  `nativeLibraryDir` — the thing LSPatch-embedded patching can't do) and enable
  it in scope **non-interactively** by writing LSPosed's
  `/data/adb/lspd/config/modules_config.db` (`modules` + `scope` tables,
  documented from the live v4 schema). **Verified end-to-end on the `binder: vm`
  TCG VM**: the `lspd` daemon starts, Zygisk injects `zygote64` +
  `system_server`, and Vector reports `version 2.0 (3021)`.
- **`binder: vm` now persists redroid's `/data` across reboots.** The micro-VM
  guest-init recreated the redroid container fresh every boot (`docker rm -f` +
  `docker run`) with **no `/data` volume**, so the Magisk DB, MAGISKBIN, and
  installed modules reset on every `beetroot down`/`up` — making a flash →
  reboot → activate flow (which every Zygisk module, LSPosed included, needs)
  impossible on the VM. guest-init now bind-mounts a directory on the
  persistent guest rootfs as the container's `/data` (override with
  `BEETROOT_GUEST_DATA_DIR`), matching the redroid `host`/`auto` backend's
  persistent `/data`. Verified: `zygisk=1` and a flashed module survive a VM
  reboot. (Leave `vm.boot_cache: false` while iterating — the warm-start cache
  reverts to its checkpoint on resume by design.)

- **`beetroot logs` now works for the `binder: vm` backend — the micro-VM is
  finally debuggable.** Previously `logs` was redroid-only (`docker compose
  logs`); a `vm` instance raised `'logs' is not supported by the 'vm'
  backend`, even though `beetroot up`'s own error hint and the config docs
  told users to run it to watch a slow TCG boot. The gap was real: QEMU runs
  `-nographic` (guest serial → process stdout) and `QemuProcess.start` left
  that stdout un-redirected, so the boot trace died with the `up` process and
  there was **no way to see why a guest never reached `sys.boot_completed`**.
  Now `start` redirects the serial console to a persisted
  `<instance>/qemu-console.log` (truncated per boot; the child dups the fd so
  the VM keeps running detached), and a new `LogReader` capability protocol
  (satisfied by both the redroid `Instance` and `VmDeviceBackend`) gates the
  `logs` verb so `beetroot logs <vm-instance> [-f]` prints (or `tail -f`s) the
  kernel boot trace, `guest-init` output, and the in-guest redroid container's
  stdout. New code: `qemu.QemuProcess.console_log`, `VmDeviceBackend.logs`,
  `api.LogReader`. Docs: [CLI § logs](https://iliketo.party/Beetroot/reference/cli/#logs).

- **`binder: vm` warm-start boot cache (`vm.boot_cache`) — ~22x faster repeat
  boots.** Booting redroid in the micro-VM under TCG is CPU-bound (emulating
  ART / Zygote / `system_server`), so a cold boot to first ADB takes ~3-4 min
  on a 4-core host (Android 14) and no entropy/disk micro-lever moves it (see
  the investigation below). The new opt-in `vm.boot_cache: true` skips the boot
  on repeat starts: the **first** `beetroot up` cold-boots through a qcow2
  overlay and checkpoints the running machine state with QEMU `savevm`; every
  **later** `up` *resumes* that checkpoint with `-loadvm`. Measured on a
  binderless, KVM-less host (Android 14, pure TCG): cold first boot to first
  host ADB **~222 s**, warm resume **~10 s**. Resume reverts the guest to the
  checkpoint each time — a fast known-good boot, not a persistence mechanism
  (use `beetroot snapshot` for that); the checkpoint lives at
  `<instance>/vm-overlay.qcow2` (~2 GiB) and is reset by deleting it (e.g.
  after rebuilding the kernel/rootfs). Requires `qemu-img`
  (`BEETROOT_QEMU_IMG_BIN` to override). Additive: the default cold-boot path
  is unchanged. New code: `src/beetroot/vm/boot_cache.py` and
  `build_qemu_argv`'s qcow2/monitor/`-loadvm` hooks. Docs:
  [config § warm-start boot cache](https://iliketo.party/Beetroot/reference/config/#warm-start-boot-cache-vmboot_cache),
  [sandbox quickstart](https://iliketo.party/Beetroot/guides/sandbox-quickstart/),
  and [vm-savevm-cache design](https://iliketo.party/Beetroot/design/vm-savevm-cache/).

- **Investigated cold TCG boot-time levers (issue #83): RNG options are
  neutral.** Benchmarked `-device virtio-rng-pci` and `random.trust_cpu=on`
  (and a `cache=unsafe` root-disk variant) against the cold boot on a
  binderless, KVM-less host, several runs each. All are **boot-neutral**: the
  guest CRNG is already seeded at ~0.19 s (the defconfig
  `CONFIG_RANDOM_TRUST_CPU=y` plus `-cpu max` exposing RDRAND even under TCG), so
  `random.trust_cpu=on` is redundant; and `virtio-rng-pci` creates no
  `/dev/hw_random` without `CONFIG_HW_RANDOM_VIRTIO=y` (a `=m` in defconfig,
  absent from the module-less guest), so it is inert. The boot is CPU-bound,
  not entropy- or disk-bound, which is why the warm-start cache above — not
  these micro-levers — is the real win. Control runs (force `trust_cpu` off, or
  hide RDRAND with `-cpu qemu64`) prove the mechanism rather than just measuring
  a near-miss. The one cold-boot dial that *does* move is the Android version
  (14 cold-boots in ~190–200 s under TCG; 11 in ~100 s), now noted in
  `examples/vm.yaml`. The QEMU argv is unchanged; the findings and measurements
  are recorded in
  [vm-rnd-log Stage E](https://iliketo.party/Beetroot/design/vm-rnd-log/).

- **Reusable CI workflow — boot a Beetroot instance in *your* repo's CI.**
  A new `on: workflow_call` workflow at
  `.github/workflows/beetroot-ci.yml` lets any other repository raise a rooted
  Android instance and run its own tests against it with a single `uses:`
  reference (e.g. a Frida-script author whose CI exercises a hook against a
  real Android-14 phone). It checks out the caller's repo, builds the Beetroot
  image on the caller's runner, boots an instance, and runs a caller-supplied
  `test-command` with the device reachable at `$ADB_SERIAL` / `$FRIDA_HOST`.
  Inputs cover `binder` (`host` fast path or `vm` for runners with no loadable
  binder), `gapps`, `android-version`, `frida-version`, and more. Because the
  image is built on the *caller's* runner — the patcher fetches GApps/Houdini
  from their upstreams at the caller's CI runtime — Beetroot redistributes
  nothing proprietary; this ships only Beetroot's own MIT-licensed
  orchestration and is **not** a published container image (it does not appear
  under GitHub Packages). New docs:
  [Running in CI § Reusable workflow](https://iliketo.party/Beetroot/guides/running-in-ci/#reusable-workflow-boot-an-instance-in-your-ci).

- **`beetroot modes` — host capability survey.** A new host-level,
  instance-independent command that probes the host binder driver, KVM, and
  the QEMU / Docker / adb binaries and reports, for every run-mode
  (`redroid` on host binder, `binder: vm` under KVM, `binder: vm` under TCG,
  and the `adb` backend), whether it is `supported` / `needs-setup` /
  `unsupported` / `unknown` — each with a reason and a remedy. Answers "what
  can this machine run *before* I create an instance or pick a `binder`
  mode?", complementing the per-instance `beetroot doctor <name>`. `--json`
  for scripts; always exits 0 (reports, never gates). Crucially distinguishes
  "no `/dev/kvm`" (KVM fast path unavailable) from "no VM support" — the
  `binder: vm` TCG path works on binderless, KVM-less hosts. New docs:
  [Binder & run-modes](https://iliketo.party/Beetroot/how-it-works/binder-and-modes/).

- **`binder` config switch (`auto` / `host` / `vm`).** A new top-level
  `binder:` key on `beetroot.yaml` selects how redroid obtains the kernel
  binder driver it needs to boot. `auto` (default) keeps the historical
  behaviour — use the host binder, and on a host that can't provide it
  `beetroot up` warns once and starts anyway. `host` is the strict
  variant: `beetroot up` fails fast (exit 1) rather than leave a
  container that silently never boots Android — better for CI. `vm` opts
  into running redroid inside an emulated QEMU micro-VM that ships its own
  binder kernel, for hosts with no host binder at all; the micro-VM engine
  now ships (see the next entry). The slow emulated path is never engaged
  automatically — it is always an explicit opt-in. `beetroot doctor`
  reflects the mode: under `vm` it runs VM-specific checks (`vm.process`,
  `vm.accel`), and under strict `host` the `host.binder` row fails (not
  warns). A validated proof-of-concept (booting redroid on a binderless,
  KVM-less host) and the full backend/fallback design live in
  [Binderless hosts (QEMU/TCG)](https://iliketo.party/Beetroot/design/binderless-hosts-qemu-tcg/).

- **`binder: vm` micro-VM engine (QEMU/TCG, KVM fast path).** Selecting
  `binder: vm` now dispatches `beetroot up` to a real QEMU micro-VM backend
  (`VmDeviceBackend`) instead of failing fast. The launcher detects the
  accelerator (`/dev/kvm` -> KVM; otherwise TCG with MTTCG `thread=multi`,
  `-cpu max`), builds the `qemu-system-x86_64` argv per the validated PoC
  recipe, forwards the guest's ADB port to a per-instance host loopback
  port, and manages the QEMU process via a pidfile in the instance dir.
  The capability-ladder UX is preserved: a one-line banner on KVM, a loud
  banner (noting the ~5-20x slowdown -- a slow first boot is expected, not
  a hang) on TCG, and a hard, actionable error if `vm.accel: kvm` is
  demanded on a host without `/dev/kvm`. An optional `vm:` block tunes the
  kernel / rootfs paths, accelerator, vCPUs, and memory (with
  `BEETROOT_QEMU_BIN`, `BEETROOT_VM_KERNEL`, `BEETROOT_VM_ROOTFS` env
  defaults). The new `beetroot build --vm-kernel` builds the guest kernel +
  rootfs from the vendored `docker/vm/` artifacts (kernel-config fragment,
  rootfs builder, guest init). This is additive -- no `api_version` bump;
  redroid stays the default. See
  [Binderless hosts (QEMU/TCG)](https://iliketo.party/Beetroot/design/binderless-hosts-qemu-tcg/).

- **Faster `binder: vm` boots: auto-sized `-smp` + `mitigations=off`.** Two
  boot-speed levers ship for the QEMU micro-VM backend, on top of the
  existing MTTCG + KVM fast path. (1) **`vm.smp` now defaults to `auto`**,
  which pins `-smp` to the host's *physical* core count (HyperThread siblings
  collapsed via `/proc/cpuinfo`, capped by `sched_getaffinity` so it is
  correct inside a constrained CI container too). This bakes in the
  vm-rnd-log §B.5 sweep finding that the real redroid boot scales with vCPUs
  up to the host core count and regresses past it (oversubscription →
  cross-thread TCG sync overhead): matching `-smp` to the host's *physical*
  cores is the measured optimum, where the old hardcoded `-smp 4` left
  non-4-core hosts mistuned — and a *logical*-CPU count would oversubscribe a
  hyperthreaded host (picking `-smp 8` on a 4c/8t box, which §B.5 measured
  slower than `-smp 4`). An explicit integer still pins the count. (2) The
  guest kernel command line carries **`mitigations=off`**: the
  speculative-execution barriers (retpolines, lfence) buy nothing for an
  ephemeral, single-tenant research sandbox and are pure overhead — extra
  *emulated* work under TCG, real serialization under KVM (measured
  boot-neutral under TCG, but harmless and kept). Additive — no `api_version`
  bump; old `vm.smp: <int>` YAMLs keep working unchanged.

- **End-to-end CI that boots a real Android (`e2e.yml`).** A new workflow
  boots Android on a hosted runner — the behavioural counterpart to the
  kernel-less unit suite — in two tiers: **Tier 1** boots the upstream stock
  redroid image and drives it through Beetroot's adb backend (`beetroot adopt
  --verify`, `beetroot ls --json`, `beetroot shell`, the adb-side `beetroot
  doctor` row); **Tier 2** (WIP, non-blocking) `beetroot build`s the real
  Magisk image, `beetroot up`s it, and asserts root / Zygisk / GMS denylist /
  Frida in-device. Real boots are slow, so the workflow runs on the `e2e` PR
  label, manual dispatch, or a nightly schedule — not on every push. A shared
  `provide-binder` composite action loads the host binder driver
  (`modprobe binder_linux` / binderfs) on the runner. See
  [Running in CI](https://iliketo.party/Beetroot/guides/running-in-ci/).
- **Host binder preflight + `host.binder` doctor check** — redroid runs
  Android's userspace against the *host* kernel and has no kernel of its
  own, so the binder driver is a hard, non-negotiable requirement
  (independent of `privileged: true`, which is a Docker permission, not a
  kernel feature). A new `beetroot.hostcheck` module probes the host
  (`/dev/binder*` nodes, binderfs in `/proc/filesystems`, and
  `CONFIG_ANDROID_BINDER_IPC` in the kernel config) and classifies it as
  `ready` / `loadable` / `unsupported` / `unknown`. `beetroot up` now
  emits a one-line advisory (once per fan-out) when the host can't
  satisfy binder — previously `docker compose up -d` "succeeded" by
  creating a container that never booted Android, with no symptom but
  ADB failing to connect. `beetroot doctor <name>` gains a `host.binder`
  row: `pass` when ready, `skip` when undeterminable (e.g. macOS),
  `fail` with the exact remedy otherwise (load `binder_linux` on a
  capable host, or adopt a remote device on a kernel-less one).
- **Docs: running in CI / without kernel access.** New guide covering the
  binder-capable path (load the module on GitHub-hosted runners, then
  `beetroot up` normally) and the kernel-less path (drive a remote device
  with `beetroot adopt`). The prerequisites and troubleshooting pages now
  spell out that binder is a kernel feature `privileged` can't substitute.
- **`beetroot module --auto-install`** — root-driven Magisk-module install
  for adb-adopted devices (#7; the variant deferred from v0.4 T5 / v0.5).
  Each zip is pushed to a synthesized temp name under `/data/local/tmp/`
  (`beetroot-module-<N>.zip` — the untrusted local filename never reaches
  the device shell) and installed with
  `su -c magisk --install-module <zip>` — Magisk's supported
  non-interactive install primitive (the same one the redroid backend's
  `flash-modules.sh` uses), which stages the module into
  `/data/adb/modules_update/<id>/` for the next reboot; the pushed temp
  zip is removed afterwards. `--sha256` is **enforced fail-closed** on
  this path — a mismatching zip is never pushed (on the safe-default
  push-to-Downloads path it is ignored — verify the hash yourself).
  Multiple zips install in one
  invocation (`beetroot module phone a.zip b.zip --auto-install`, with
  `--sha256` repeated once per source when pinning): every module gets
  its own `ok:` (stdout) / `failed:` (stderr) report line, a failure
  never aborts the rest of the batch, and the verb exits non-zero if any
  module failed. Redroid instances don't implement the capability and
  exit 2 (they flash staged modules at boot).
- **New public API:** the `beetroot.api.AutoModuleInstaller` capability
  sub-protocol and its `beetroot.api.ModuleInstallResult` row model, plus
  `beetroot.modules_download.verify_sha256` (extracted from the staging
  resolver so both install paths share one digest check).
- **`--auto-install` pre-flight diagnostics** (#38). Whole-device
  problems no longer surface as N identical opaque `failed:` rows with
  raw adb stderr. Before anything is pushed, the adb backend probes the
  device (`su -c true` for usable root, then `su -c 'command -v magisk'`
  for the Magisk binary, quoted exactly like the install command) and
  fails fast with a single friendly `error: ...` line + exit 1: offline
  / not-connected (or unauthorized) devices get a reconnect-and-check-`adb
  devices` hint, unrooted devices get a "has no usable root (su missing
  or denied root — check the device is rooted and approve the Magisk
  superuser prompt)" diagnosis, and rooted-but-Magisk-less devices are
  told to install or repair the Magisk app. Connectivity is always
  decided by re-running `adb devices` for the device's serial, never by
  matching the probe's error text — so untrusted host paths or
  module-controlled stderr can't be mistaken for a connectivity failure.
  A device that genuinely drops offline mid-batch aborts the remaining
  modules with the same offline diagnosis (which names how many were
  skipped) — rows completed before the abort are still reported.
  Genuinely per-module failures (missing/non-zip path, sha256 mismatch)
  always keep the per-row reporting contract and never abort the batch.
  New public API: `beetroot.api.DevicePreflightError` (carries the
  pre-abort rows in its `results` attribute).

### Quality & internals
- **The supported Android-version list is now drift-checked and the "add a new
  version" path is documented + tested (#98).** `config._VALID_ANDROID_VERSIONS`
  has always been the single source of truth, but the human-readable "11, 12,
  13, or 14" enumerations hand-copied into docstrings would silently lie when a
  version was added, and both image-tag derivations were only spot-checked for a
  couple versions. `tests/test_android_version_extensibility.py` now greps the
  `config.py`/`builder.py` enumerations against the constant (failing CI on
  drift) and parametrizes `base_image_tag` + `vm_redroid_image` across *every*
  supported version, and AGENTS.md gains an "Adding a new Android version"
  checklist that names the touch-points and the unverified upstream-tag
  assumption (both `N.0.0_..._magisk` and `N.0.0-latest` must exist on Docker
  Hub, or the version 404s at pull time).
- **The whole CLI now speaks through one rich-rendered voice.** Every
  user-facing line — verb outcomes, the verbose step narration, next-step
  hints, advisories, errors, and the migration hints emitted from
  `registry`/`config`/`adb` — now flows through `beetroot.console` instead of
  ad-hoc `typer.echo` / `print()` calls. On a TTY you get colour (cyan brand,
  dim narration, yellow advisories, red errors, green/red/yellow `doctor`
  rows) plus existing progress bars; off-TTY (pipes, CI, logs) rich strips the
  styling and emits the exact same plain text — the `[beetroot]` brand prefix
  is preserved, so existing scripts and `grep`s keep working. Output is also
  more verbose: slow verbs (`up`/`down`/`create`/`apply`/`destroy`/`snapshot`/
  `restore`) narrate what they're doing (`→ starting alpha`) before printing
  the outcome, and more verbs print a `next: …` suggestion. Two robustness
  fixes fall out of the consolidation: long status lines no longer hard-wrap
  at 80 columns (`soft_wrap`), and every message is markup-escaped so a stray
  `[` in a path or exception can never crash the output path. Out-of-band
  advisories (best-effort `compose down` failures, orphan-cleanup notes,
  `ls` orphan-skips) now go to **stderr** so they never pollute a piped
  stdout; machine-readable output (`--json`, `doctor` rows, `ls`/`modes`
  tables) is unchanged on stdout. The `beetroot.console` module gains
  `status` / `note` / `step` / `hint` / `out` helpers and its stdout/stderr
  consoles now track the live streams, retiring the per-call console-rebind
  shim the `ls` / `modes` tables needed.
- **Friendlier capability errors + clearer `beetroot modes`.** Capability
  *mismatch* failures now point at `beetroot modes` so you can see the host's
  full menu: the `vm.accel: kvm`-without-`/dev/kvm` error and the doctor
  `host.binder` fail row both append "Run `beetroot modes` …". And the `modes`
  `adb backend` row now states it **needs an external rooted device/emulator to
  adopt** in *both* the installed and not-yet-installed states (previously the
  not-installed row only mentioned installing platform-tools, leaving the
  external-device requirement implicit). The artifact-missing preflight is left
  pointing at `beetroot build --vm-kernel` (you picked the right mode — just
  finish setup), so the `modes` pointer stays signal, not noise.
- **Trimmed the `binder: vm` guest kernel config (~23% faster compile, ~15%
  smaller bzImage).** `docker/vm/kernel.config` now disables physical-hardware
  driver classes a QEMU `q35`+virtio guest can never bind (`DRM_I915`,
  `ETHERNET`, `WLAN`, `ATA`, `SCSI_LOWLEVEL`) — pure compile-time / image-size
  win, no runtime change (those drivers never probe a device on our
  `-nographic -display none` launch line). `compile_seconds` dropped 541→418
  and the bzImage 14→12 MiB on the in-sandbox build. **Validated under TCG:**
  redroid still boots to `sys.boot_completed=1` (~101 s, within noise of the
  98 s full build), `screencap` returns a real 720×1280 frame, and AudioFlinger
  is up. Sound, DRM core + `virtio-gpu`, and the generic graphics infra
  (`dma-buf`/`sync_file`/`memfd`) are deliberately kept (Beetroot doubles as a
  dev target — app audio and screenshots must keep working). Numbers and the
  keep/cut rationale live in `benchmarks/README.md`.
- **ccache for the guest-kernel build + `CONFIG_MODULES=n`.**
  `build_vm_kernel` now injects `CC="ccache gcc"` when ccache is on PATH — a
  no-op on a cold build, but a re-compile of unchanged source (CI build lanes,
  local iteration) drops from ~7–9 min to **~54 s (99.8% cache hits)**. The
  benchmark lane keeps `CCACHE_DISABLE=1` so it still times a cold compile.
  `CONFIG_MODULES=n` matches the module-less guest (everything is built in) for
  a small, no-downside build/image trim. **`-Os` was evaluated and rejected:**
  it shrank the bzImage 12→10 MiB but ~doubled the TCG boot (guest-measured
  98→201 s — the boot is CPU-bound and `-Os` trades run-time speed for size),
  so the guest stays on the `-O2` default. The empirical comparison is in
  `benchmarks/README.md`.
- **Prebuilt `binder: vm` guest kernels (`beetroot build --vm-kernel` fetches a
  ~12 MiB bzImage instead of compiling).** The cold kernel compile (~7 min) is
  the long pole for a fresh host, and ccache only helps *re*builds — a brand-new
  CI runner or Claude Code on the web sandbox always pays full price. The CLI
  now downloads a prebuilt `bzImage` from a per-kernel GitHub release, keyed on
  the pinned kernel version **and** a fingerprint of the local
  `docker/vm/kernel.config` (sha256-verified via a `.sha256` sidecar). A config
  edit / version bump / unpublished release / blocked network all miss cleanly
  and fall back to a source compile, so the vendored config stays authoritative
  — you can never boot a stale prebuilt kernel. `--from-source` forces a
  compile. New `src/beetroot/kernel_download.py` (mirrors `frida_download.py`);
  publishing handled by `.github/workflows/vm-kernel-release.yml` (builds with
  ccache on config changes / manual dispatch). Each kernel is published as its
  **own** release tagged `vm-kernel-<version>-<fingerprint>`, with the `bzImage`
  + `.sha256` attached at creation — compatible with **immutable releases**,
  which freeze a release's assets at creation and so cannot be appended to (an
  earlier rolling single-`vm-kernel`-release design hit `HTTP 422: Cannot upload
  assets to an immutable release`). The 2.4 GB rootfs is still assembled locally
  (over GitHub's 2 GB asset limit, and it pulls redroid on the user's machine).
- **The micro-VM rootfs builder is now typed, tested Python.** The former
  `docker/vm/build-rootfs.sh` has been ported to `build_rootfs` in
  `src/beetroot/builder.py` — same recipe (busybox-static + Docker static
  bundle + iptables-legacy + socat, the redroid image baked into
  `/var/lib/docker`, `guest-init.sh` as `/init`), but as strict-mypy,
  100%-covered Python behind an injectable `RootfsRunner`. The historical
  `IMAGE_SIZE_MB` / `DOCKER_VERSION` / `DOCKER_URL` / `REDROID_IMAGE` /
  `REDROID_TAR` / `ADBPROBE_BIN` / `BUSYBOX_BIN` env knobs are preserved.
- **Shell linting now covers every script at the strictest severity.** CI's
  `shellcheck` gate moved from `-S warning` on `docker/*.sh` only to
  `-S style` on `docker/*.sh docker/vm/*.sh`, so the micro-VM `guest-init.sh`
  is now linted too. A new `tests/test_shell_lint.py` runs the same
  `shellcheck` + `shfmt` checks under pytest (skipping cleanly when the tools
  are absent), so shell regressions are caught locally before the push.

### Bug fixes
- **A validation-passing-but-index-colliding `ports:` config no longer orphans
  or poisons instances.** A `ports:` list can pass pydantic validation
  (`config._check_ports_distinct` only checks distinctness among the *explicit*
  host ports — it has no knowledge of the instance index) yet still raise
  `PortCollisionError` at resolution time, because an entry pinned to a sibling's
  stride default only collides once that stride port is computed for the
  instance's index (e.g. `ports: [{service: adb, guest: 5555}, {service: x,
  guest: 8080, host: 5555}]` resolves adb→5555 and x→5555 at index 0). Two bugs
  shared this root cause:
  - **Registry poisoning:** `registry.all_resolved_host_ports` resolved every
    registered instance's ports *outside* the `FileNotFoundError` guard, so one
    such poisoned row crashed every *other* instance's cross-instance scan
    (`create` / `register` / `apply` / `restore`), aborting the unrelated
    operation with a misattributed error. The scan now falls back to the poisoned
    instance's well-known stride defaults instead of crashing — its protected
    ports still count cross-instance.
  - **Orphaned registry row:** `Instance.create` / `Instance.register` resolved
    the ports *after* committing the registry row but *before* the rollback
    `try`/`except`, so a resolution raise escaped before rollback ran, leaving an
    orphaned row the user assumed had failed. The resolve now runs inside the
    rollback `try`, so a raise cleans up the row (and, for `create`, the
    freshly-made directory).
- **`beetroot build --vm-kernel`'s source-compile fallback is now self-contained
  (#74).** When the prebuilt-kernel fetch misses (config edited, version bumped,
  release unpublished, or network blocked) the build falls back to compiling the
  guest kernel from source. That fallback ran `make defconfig` (and friends) in
  the current working directory but never fetched the kernel tree, so unless the
  cwd already *was* an extracted `linux-<version>` tree it died immediately with
  `No rule to make target 'defconfig'` — leaving a fresh host (no prebuilt asset
  **and** no kernel tree) with no working path. The fallback now fetches the
  pinned `linux-<version>.tar.xz` from `cdn.kernel.org`, extracts it into a
  throwaway scratch tree, and compiles there (mirroring what
  `vm-kernel-release.yml` does), so a prebuilt miss degrades to a slow-but-working
  compile instead of a hard error. (The companion fix — publishing the missing
  release asset so the *fast* path also works — already landed via the
  per-fingerprint `vm-kernel-<version>-<fp>` immutable-release scheme.)
- **`beetroot snapshot`/`restore` now give a clear "redroid-only" error for a
  `binder: vm` (or adb) instance instead of a misleading "not registered" one
  (#128, low-risk half).** `snapshot`/`restore` pack and unpack the host-side
  `data/` directory, which *is* the live Android `/data` only for the redroid
  backend — a `binder: vm` instance keeps `/data` inside the guest rootfs
  (`/var/lib/redroid-data`) and an adb device has no host-side `/data` at all,
  so the host `data/` dir is vestigial for both. Snapshotting one used to fall
  through `snapshot._find_registry_entry`'s redroid-only filter and raise a
  confusing "instance at … is not registered" error even though the instance
  *was* registered. Now `snapshot` (whether via `beetroot snapshot`, which keeps
  its exit-code-2 capability contract, or the programmatic `snapshot.snapshot`)
  and `restore` detect a registered-but-non-redroid backend and raise
  `snapshot is only supported for the redroid backend; instance 'X' uses the vm
  backend — vm snapshot is not yet supported (see issue #128).` The genuine
  "not registered at all" and redroid name-collision messages are unchanged.
  Cross-backend snapshots remain a tracked follow-up; the `data/`→`/data`
  mapping is now documented as redroid-only in `CLAUDE.md`,
  `docs/how-it-works/filesystem.md`, and `docs/guides/snapshots.md`. The
  vestigial vm `data/` dir is left in place (out of scope).
- **`binder: vm` warm resume now warns that it reverts `/data` (#123).** With
  `vm.boot_cache: true`, every warm `up` resumes the first-boot checkpoint with
  `-loadvm`, which rolls the whole machine — including the qcow2 overlay that
  backs the guest's `/data` — back to that checkpoint. So everything written to
  `/data` since then (installed apps, account logins, flashed-module / LSPosed
  scope state) was silently discarded on every resume. The behaviour was
  documented but never surfaced at runtime; `_up_cached()` now prints a
  non-fatal advisory on each warm resume naming the reset and the remedy
  (`vm.boot_cache: false` — *not* `beetroot snapshot`, which is redroid-only).
  No schema change. The durable-and-fast fix (a separate `/data` disk excluded
  from the checkpoint) and an explicit `lifecycle: ephemeral|durable` intent are
  tracked as follow-ups.
- **Hardened the micro-VM guest's missing-marker fallback so it can't silently
  re-introduce the "boots Android 11" bug (#97).** `guest-init.sh` reads the
  baked-image marker (`/etc/beetroot/redroid-image`, issue #82) to boot the
  Android version the rootfs was built for, falling back to the historical
  `redroid/redroid:11.0.0-latest` when it's absent. That fallback used to be
  **silent**, so any future path where the marker went missing on an otherwise
  current rootfs (a reordered bake, an interrupted write, a hand-assembled
  rootfs) would quietly resurrect Android 11 with zero diagnostics. Now the
  resolver is its own `resolve_redroid_image()` step (run first in `main`) that
  **warns prominently** — naming the fallback image and pointing at #97 — and
  also treats an empty/whitespace-only marker as missing. On the build side,
  `build_rootfs` now verifies the marker exists and is non-empty *before*
  packing the ext4 image, so a marker-write failure surfaces at build time
  rather than weeks later as a wrong-OS boot. The `11.0.0` fallback is kept
  on purpose (a pre-#82 rootfs baked that image into `/var/lib/docker`) and is
  documented as the deliberate legacy value, not `DEFAULT_ANDROID_VERSION`.
- **`beetroot build` no longer aborts on the empty `container_name` validation
  error (#114).** The bundled compose template carries the runtime-only
  `container_name: ${INSTANCE_NAME}`, which is unset during a bare build. Recent
  Docker Compose validates `container_name` against `[a-zA-Z0-9][a-zA-Z0-9_.-]+`
  even at `build` time, so the Beetroot-layer build step failed before building
  with `services.phone.container_name '' does not match pattern` — the base
  image built fine, but `beetroot build` still exited 1. The build step now
  passes a throwaway `INSTANCE_NAME=beetroot-build` in its env (the build never
  starts a container, so the value is otherwise inert), satisfying the pattern
  without touching the runtime template.
- **The boot helpers couldn't find the `magisk` binary on a real boot — so
  none of the Magisk configuration ran.** `entrypoint.sh` is launched by Android
  init (`stealth.rc`'s `exec_background`), which inherits init's default service
  PATH (`/system/bin:/system/xbin:/vendor/bin:…`). That PATH does **not**
  include the directory Magisk installs its `magisk` binary into — `/sbin/magisk`
  on the redroid Magisk image. Every helper calls bare `magisk`, so each call
  failed with "not found": `magisk-config.sh`'s daemon-wait loop spun on
  `magisk --sqlite "SELECT 1"` until it hit `BEETROOT_MAGISK_WAIT_SECS` (~120 s),
  exited 1, and **aborted the entire entrypoint before Zygisk, the denylist,
  MAGISKBIN, or modules were ever configured**. The smoking gun on a real booted
  image: `init: Service 'exec 38 (/system/bin/sh /entrypoint.sh)' … exited with
  status 1 … took 145 seconds`, with an empty settings table and empty
  `/data/adb/magisk`. This made the two fixes above (and the pre-existing
  Zygisk/denylist writes) **no-ops on every real boot** — it was invisible
  because the unit tests put a fake `magisk` on PATH and the e2e boot tier is
  WIP/`continue-on-error`. New helper `docker/magisk-path.sh` (sourced **first**)
  prepends the first directory from `BEETROOT_MAGISK_DIRS` (default
  `/sbin:/debug_ramdisk`) that actually holds an executable `magisk`; it's a
  no-op when `magisk` already resolves. **Verified end-to-end on a live
  `binder: vm` TCG VM**: with the fix, an unattended boot populates MAGISKBIN
  (`util_functions.sh` present), `magisk --install-module` succeeds, and the
  settings table shows `zygisk=1` / `denylist=1`. The container-boot test now
  places its fake `magisk` off-PATH (reachable only via `BEETROOT_MAGISK_DIRS`)
  so it's a real regression guard. Docs:
  [boot scripts](https://iliketo.party/Beetroot/how-it-works/boot-scripts/),
  [boot flow](https://iliketo.party/Beetroot/how-it-works/boot-flow/).
- **Magisk module install + Zygisk activation now work on a real booted
  instance (the two gaps behind a broken declarative module flow).** Two
  structural gaps in the redroid-script Magisk Delta image — both stemming from
  it expecting a human to open the Magisk app once to finish setup — broke
  Beetroot's headless boot:
  1. **`magisk --install-module` aborted with "Incomplete Magisk install".**
     The image bakes the Magisk binaries into `/system/etc/init/magisk` but its
     bootanim.rc only `mkdir`s `/data/adb/magisk` (MAGISKBIN) *empty* — the
     per-install scripts (`util_functions.sh`, `module_installer.sh`, …) live
     only inside `magisk.apk` and are normally extracted by the Magisk app.
     Headless redroid never runs that, so MAGISKBIN stayed empty and the
     installer aborted (`module_installer.sh` sources
     `/data/adb/magisk/util_functions.sh`). This broke **every** module flash
     (`flash-modules.sh`), not just LSPosed. A new boot helper
     `docker/magisk-env.sh` (sourced before `flash-modules.sh`) replicates the
     app's environment-fix headlessly: it copies the binaries from
     `/system/etc/init/magisk` and `busybox unzip`s the `assets/*.sh` scripts
     out of `magisk.apk` into MAGISKBIN. Idempotent (skips when
     `util_functions.sh` is already present).
  2. **Zygisk never actually activated on the first boot.** Zygisk injects
     zygote at zygote *start*, but `magisk-config.sh` enables it on
     `boot_completed` — after the first zygote already started without it — so
     the setting landed but Zygisk (and any flashed Zygisk module) stayed
     dormant until a reboot. A new helper `docker/activate-zygisk.sh` restarts
     zygote once (`setprop ctl.restart zygote`) on the boot that *newly* enables
     Zygisk (gated via a prior-value read in `magisk-config.sh`, exported as
     `BEETROOT_ZYGISK_NEWLY_ENABLED`), so a declarative `up` → module-flashed →
     active flow works without a manual `beetroot restart`. Routine restarts
     skip it; opt out with `BEETROOT_ZYGOTE_RESTART=0`. New env vars
     (`BEETROOT_MAGISK_BIN_DIR`, `BEETROOT_MAGISK_SRC_DIR`,
     `BEETROOT_ZYGOTE_RESTART`) are script-level (like `BEETROOT_MAGISK_WAIT_SECS`)
     and don't cross compose. The `e2e.yml` Tier 2 job now flashes a no-op module
     and asserts MAGISKBIN is populated — the regression guard the old
     doctor-only assertions lacked. Docs:
     [boot scripts](https://iliketo.party/Beetroot/how-it-works/boot-scripts/),
     [boot flow](https://iliketo.party/Beetroot/how-it-works/boot-flow/).
- **Docs links now point directly at the canonical HTTPS host (#80).** The
  README badge, README doc table, `CLAUDE.md` "published site" link, the
  `mkdocs.yml` `site_url`, and every CHANGELOG doc link used
  `https://xiddoc.github.io/Beetroot/`, which 301-redirects cross-host and
  downgrades to plain HTTP (`http://iliketo.party/Beetroot/`). Many fetchers
  and security-conscious tools refuse to follow that redirect, so the docs
  path dead-ended. Every human/tool-clickable link now targets
  `https://iliketo.party/Beetroot/` directly (HTTPS, no cross-host redirect);
  the GitHub Pages deploy mechanism is unchanged. (Repo side only — the
  external host's TLS is out of scope here.)
- **`beetroot build --vm-kernel` now works from a `uv tool install` wheel (#77).**
  The micro-VM build assets (`kernel.config`, `guest-init.sh`, `adbprobe.c`)
  lived under `docker/vm/`, which the wheel never ships — so a tool install
  resolved the build context to a cache dir with no `docker/vm/` and crashed
  with `[Errno 2] No such file or directory: '.../docker/vm/kernel.config'`.
  The three assets are now shipped as package data under
  `src/beetroot/templates/vm/` (the single source of truth) and resolved via
  `importlib.resources` (`paths.bundled_vm_dir`), mirroring how `compose.yaml`
  is bundled. The shellcheck/shfmt CI globs and the kernel-compile lanes in
  `e2e.yml` / `beetroot-ci.yml` / `vm-kernel-release.yml` were updated to the
  new path. New overrides: a `--build-context PATH` flag on `beetroot build`
  and a `BEETROOT_BUILD_CONTEXT` env var point the build at a source checkout's
  `docker/vm`; both also apply to the redroid base-image build. When the assets
  are genuinely missing the build now raises an actionable error naming both
  fixes (run from a checkout, or pass `--build-context` / set
  `BEETROOT_BUILD_CONTEXT`).
- **`binder: vm` rootfs now bakes the Android version a default instance
  expects (#82).** `beetroot create` defaults `android.version` to `14`, but
  the micro-VM rootfs baker hardcoded `redroid/redroid:11.0.0-latest`, so
  `beetroot build --vm-kernel` produced an Android-11 guest that mismatched a
  default instance (minSdk > 30 APKs would not install). The default Android
  version is now a single source of truth — `config.DEFAULT_ANDROID_VERSION` —
  shared by the `Android` schema default, the redroid base-image build, and the
  VM rootfs baker. `beetroot build --vm-kernel` derives the plain upstream
  redroid image from that version (`config.vm_redroid_image`) and accepts a new
  `--android-version` flag to bake a different one (mirroring how the gapps
  argument selects the base image). The baker records the baked version in a
  marker beside the rootfs (`rootdisk.img.android-version`) and writes the baked
  image tag into the guest at `/etc/beetroot/redroid-image` so `guest-init.sh`
  runs whatever was baked rather than a hardcoded default. `beetroot up` /
  `beetroot apply` read the marker and warn (without aborting) on a
  config/rootfs version skew; a pre-#82 rootfs with no marker is treated as a
  match for backward compatibility. Regression tests cover the
  create→build version consistency, the `--android-version` image selection,
  and the up/apply mismatch warning (firing on skew, silent on match and on the
  no-marker case).
- **`binder: vm` now warns when `android.gapps` / `magisk` / `frida` settings
  can't be honoured (#96).** The micro-VM guest boots an *unmodified upstream*
  redroid image — no GApps, no Magisk, no Houdini — so a `gapps: full` (or any
  non-`none` value), a `frida:` block, or a customised `magisk.denylist` in a
  `binder: vm` config was silently inert, leaving a researcher to debug missing
  Play Services rather than the backend. This is the same silent
  config-vs-reality gap #82 closed for `android.version`, and it now gets the
  same treatment: `beetroot up` / `beetroot apply` emit a single non-fatal
  advisory naming every ignored setting. The shipped `examples/vm.yaml` now
  pins `android.gapps: none` so the canonical config matches what the VM
  actually runs (and stays warning-free), and `docs/reference/config.md` gains a
  "boots plain redroid" warning callout alongside the existing Frida one.
  Regression tests cover each field (gapps / frida / customised denylist), the
  silent paths (gapps `none`, the inherited default denylist), the consolidated
  single-note output, and the up/apply end-to-end emission.
- **`binder: vm` guest no longer kernel-panics on boot (ELOOP on `/init`).**
  `build_rootfs` symlinked *every* applet from `busybox --list` to `busybox` —
  but `busybox` is itself in that list, so it overwrote the real `/bin/busybox`
  binary with a self-referential symlink. In the packed image `/bin/busybox`
  became a link to itself (ELOOP), which also breaks `/bin/sh`, so the guest
  kernel could not exec `/init`'s `#!/bin/sh` and panicked at ~4 s
  (`Requested init /init failed (error -40)`) — the micro-VM had never actually
  booted from the committed builder. Fixed by skipping the `busybox` applet when
  laying down symlinks (with a regression test). Found by booting the committed
  recipe end-to-end under TCG; the full validation (kernel build → rootfs →
  `sys.boot_completed=1` in ~105 s, plus an A/B showing the #66 `mitigations=off`
  flag is boot-neutral under TCG) is recorded in `docs/design/vm-rnd-log.md` §D.
- **`vm.kernel` / `vm.rootfs` now expand a leading `~`.** `_resolve_artifact`
  ran `Path(raw)` directly, so the `~/.cache/beetroot/vm/...` paths shipped in
  `examples/vm.yaml` (exactly where `beetroot build --vm-kernel` writes the
  artifacts) were taken literally and never resolved — the documented example
  config failed with "VM kernel '~/.cache/…' does not exist on the host". The
  resolver now `expanduser()`s the configured/env path. Regression test:
  `test_build_argv_expands_tilde_in_config_paths`.
- **A hostile or corrupt `beetroot.yaml` now surfaces as `error: …` + exit 1,
  never a traceback** (#21, adversarial-config-corpus slice). `cli.main()`
  caught every domain exception (`ComposeError`, `RegistryError`,
  `InstanceRootNotFoundError`, …) but **not** the two raised by
  `config.load_yaml`: a `pydantic.ValidationError` (wrong field types,
  out-of-range geometry, unsupported `api_version`, the removed `stealth:`
  section) and a `yaml.YAMLError` (syntactically broken YAML). The
  `register`/`adopt` verbs caught `ValueError` inline — and `ValidationError`
  subclasses it — so those two looked fine, but every **name-resolved** verb
  (`status`, `up`, `apply`, …) let a `ValidationError` propagate as a
  Rich-rendered traceback, and `yaml.YAMLError` (which is *not* a
  `ValueError`) tracebacked from **every** verb, `register` included. `main()`
  now nets both for the uniform `error: …` contract the rest of the CLI
  upholds. Guarded by a new `tests/corpus/` of nine hostile `beetroot.yaml`
  files driven end-to-end through the CLI (`tests/test_cli_error_contract.py`),
  asserting each yields `error: …` + exit 1 with no `Traceback` — the
  "behavior tests, not just line coverage" pattern from `CLAUDE.md`.
- **Docker-dependent tests now *skip* (not *fail*) on a daemonless host** (#59).
  `test_container_boot.py` and the `destroy`-driven restore tests in
  `test_instance_invariants.py` / `test_partial_failure_rollback.py` shell out
  to a real `docker run` / `docker compose down`, but their skip guard probed
  only `shutil.which("docker")` — true whenever the **CLI** is installed, even
  with **no running daemon** (e.g. the Claude Code on the web sandbox, where
  `dockerd` is opt-in). They therefore *failed* with a socket-connection error
  instead of skipping. A new cached `tests/docker_daemon.py::daemon_available()`
  probes `docker info` (which needs the daemon) and the affected tests gate on
  it. CI (daemon present) still runs them, so the 100% coverage gate is
  unaffected. `test_config.py`'s `docker compose config` tests keep the bare
  `shutil.which` guard on purpose — `compose config` only renders YAML and never
  touches the daemon. `AGENTS.md` gains a "What your environment can test (local
  vs GitHub CI)" note anchored on `beetroot modes`.
- **Adopted adb devices are now visible to the `ls` verb** (#15). The verb
  walks every backend kind via `Manager.all()` instead of the redroid-only
  `Manager.list_instances()`, so `adopt`-ed devices appear next to
  redroid instances. The table gains a `KIND` column; adb rows show the
  device serial in the ADB column, live availability (from `adb devices`)
  in STATUS, and `-` for PATH (no on-disk directory). With `--json`,
  adb-kind rows use the same shape as the `status` verb (`serial`,
  `adb_address`, `frida_address`, `is_available`); redroid rows are
  unchanged, including the v0.3 back-compat `path`/`adb`/`frida` keys.
  Orphan entries are still skipped with the trailing stderr advisory.
- **`binder: vm` guest rootfs now actually builds and boots.** Two bugs in
  `docker/vm/build-rootfs.sh`, both caught by booting the micro-VM locally
  under TCG (the `vm` path's Stage B was never run before — see
  `docs/design/vm-rnd-log.md`):
    - *Corrupted build:* `stage_docker_root()` returns the staging path on
      **stdout** via command substitution
      (`_dockerroot="$(stage_docker_root)"`), but `log()` *and* the inner
      `docker pull` / `docker load` also wrote to stdout. Their interleaved
      output was captured into the path, so `cp -a "$_dockerroot" …` failed
      with `cannot stat` and no `rootdisk.img` was produced. All three now
      write to stderr.
    - *Kernel panic on boot:* the rootfs shipped only `/bin/busybox` with no
      applet symlinks — a comment claimed `guest-init.sh` ran
      `busybox --install -s`, but it never did. `/init` is a `#!/bin/sh`
      script, so the kernel panicked instantly (`Requested init /init failed
      (error -2)` — no `/bin/sh`). The build now lays down every busybox
      applet symlink (`sh`, `mount`, …) at build time.
  Together these unblock the `binder: vm` e2e tier (#48), which runs this
  script.

### Known limitations

- **Frida is not yet supported on the `binder: vm` backend** (#44 follow-up).
  The QEMU micro-VM runs redroid with `--network none`, and nothing yet
  forwards the guest Frida port or bind-mounts a staged `frida-server` into
  the network-isolated guest. `beetroot frida-addr <vm-instance>` and
  `install_frida` therefore raise a friendly `BackendCapabilityError`
  rather than silently no-op; `beetroot doctor` omits the `frida.handshake`
  row for vm instances (it could never pass), and `ls` / `status` report the
  Frida address as `unsupported`. The vm backend is scoped to ADB
  forwarding (`beetroot shell`) only; Frida-over-VM forwarding is tracked as
  a follow-up. Use `binder: auto`/`host` (redroid) or `beetroot adopt` an
  external rooted device for Frida in the meantime.

### CI hardening, part 1 (#21)

No schema or CLI changes — this slice hardens the quality gates and the
CI pipeline itself.

- Test runs now treat **every warning as an error**
  (`filterwarnings = ["error"]`; allowlist upstream deprecations
  individually), execute in **random order** (`pytest-randomly`), and
  fail any single test that exceeds **30 seconds** (`pytest-timeout`).
  CI additionally runs pytest with `-p no:cacheprovider` so runs are
  stateless, and uploads the coverage report (XML + terminal) as a
  workflow artifact.
- `src/beetroot/` is now `ruff format`-formatted and CI enforces
  `ruff format --check` as a hard gate.
- New CI gates, all version-pinned (release binaries are
  checksum-verified): `uv lock --check` (lockfile drift), actionlint +
  zizmor (workflow lint + security audit), codespell (spelling, over
  `src/`, `docs/`, `README.md`, `CHANGELOG.md`), yamllint (policy in the
  new `.yamllint`, over the bundled compose template, the workflows, and
  `examples/`), deptry (dependency hygiene; config in
  `[tool.deptry]` in `pyproject.toml`), and `shfmt -i 4 -d docker/*.sh`
  (boot-helper comment spacing normalized to match).
- New packaging gate: `uv build`, `twine check dist/*`, then the wheel
  is installed into a clean venv and smoke-tested with `beetroot --help`.
- Workflow security fixes flagged by zizmor: the docs deploy workflow's
  actions are SHA-pinned and its `pages: write` / `id-token: write`
  permissions are scoped to the deploy job; every checkout across all
  workflows sets `persist-credentials: false`.

### CI: host-vs-VM benchmark harness (#50)

- New nightly **benchmark lane** (`.github/workflows/benchmark.yml`;
  `schedule` + `workflow_dispatch`) that measures and **trends** — never
  gates — the capability ladder's cost: kernel compile time, cold-boot
  time, and a fixed post-boot workload for the host-binder path vs the
  `binder: vm` (QEMU/TCG) path, on the same runner in the same run so the
  host-vs-vm *ratio* cancels per-runner hardware noise. A regression over
  `benchmarks/baseline.json` raises a `::warning::` annotation only —
  benchmarking tracks, it does not gate.
- The analysis is a standalone, fully unit-tested harness
  (`scripts/bench.py` with `measure` / `record` / `report` subcommands),
  so the aggregation, ratio, and regression logic is covered without a
  runner. The committed baseline is seeded from the offline R&D in
  `docs/design/vm-rnd-log.md` (see `benchmarks/README.md` for the refresh
  flow).

### CI: `binder: vm` savevm boot-cache — design + cache key (#49)

- Design note ([VM boot-cache (savevm)](https://iliketo.party/Beetroot/design/vm-savevm-cache/))
  specifying how a booted micro-VM is checkpointed once with QEMU
  `savevm` (qcow2 internal snapshot) / `migrate`, cached, and restored
  (seconds) in downstream jobs to skip the ~100 s TCG boot — for the
  functional vm e2e tier (#48) and post-boot benchmark (#50), never for
  the cold boot-speed metric.
- The load-bearing, unit-tested piece lands now: `scripts/vm_cache_key.py`
  computes a stable, order-independent cache key over the guest kernel +
  rootfs (and/or the guest-defining sources) that changes the instant any
  input does — the safety latch that stops a stale snapshot from being
  restored against a guest it wasn't taken on. The QEMU integration
  (qcow2 overlay + QMP `savevm`/`loadvm` launch path) is the tracked
  follow-up.

### CI: `binder: vm` e2e tier (#48)

- New **`tier-vm-qemu`** job in `e2e.yml` that exercises the QEMU micro-VM
  backend end-to-end (previously covered only by the mocked unit suite):
  it builds the binder-enabled guest kernel + rootfs, boots redroid inside
  the `binder: vm` micro-VM, and drives it through the adb backend —
  asserting `beetroot ls --json` availability, `beetroot shell getprop
  sys.boot_completed`, the `doctor` `vm.process` / `vm.accel` rows, and
  that `beetroot frida-addr` reports its "not yet supported on the vm backend"
  message. Gated like Tier 1 (nightly `schedule` / `workflow_dispatch` /
  PR `e2e` label). On GitHub-hosted runners there is no `/dev/kvm`, so it
  runs under TCG — a slow (~100 s+) but real boot; the kernel + rootfs
  build is the long pole (the savevm boot-cache, #49, is the planned
  lever to skip it on repeat runs).


## v0.6.0 — 2026-05-20

A stability + cleanup release on the road to v1.0 — bug-fixing, OOP/CLI
API cleanliness, and making the CLI + Python API stable enough to freeze.
**Several breaking changes** (all pre-1.0, intentional). Stealth/anti-root
work is deprioritized to a future release.

### Breaking changes
- **`env` verb removed.** Use `beetroot status --json` for machine-readable
  endpoints; `beetroot frida` / `beetroot shell` for interactive use.
- **`stealth.denylist` → `magisk.denylist`** in `beetroot.yaml` (api_version
  3 → 4). A `stealth:` key now raises a migration error.
- **Second Frida port renamed to `frida_control`** (YAML field + env var
  `FRIDA_PORT_CONTROL`).
- **`restore --as` → `--name`** (hidden `--as` alias kept for one release).
- **`DeviceBackend` Protocol redesigned** — third-party backends are now a
  first-class, stable contract. `frida_cli` takes a `Sequence`;
  `install_frida(version=None)`; `shell(args=...)`; `from_meta` takes
  `BackendConfigBase`. Capabilities are opt-in sub-protocols
  (`Lifecycle` / `ModuleInstaller` / `HealthCheckable` / `Snapshottable`).
- **`Manager.list` → `list_instances`**; added `Manager.all()`;
  `Manager.get` returns a resolved backend.
- **`registry.add` removed** — use `add_allocating(name, backend=...)`.
- **`Instance.destroy(yes=False)` raises** instead of prompting (the prompt
  moved to the CLI).

### Bug fixes
- Download cache poisoning: a sha256 mismatch no longer leaves a bad cached
  file that re-fails forever (frida-server + module zips); the module cache
  key now incorporates the full URL so same-basename URLs can't collide.
- The registry no longer wipes itself on an unknown backend `kind` — unknown
  rows are preserved opaquely and round-trip byte-for-byte.
- Cross-backend port collision: adopted adb devices' Frida ports are now
  counted in collision detection.
- `status` reports an adb device's real allocated Frida endpoint (was
  hardcoded `localhost:27042`).
- `snapshot restore` rolls back a partially-extracted directory on a
  mid-extraction failure, rejects a file destination cleanly, and writes
  byte-deterministic manifests.
- The port index is bounded so an allocation can't overflow 65535.
- `builder` skips re-cloning when the work dir already matches the repo URL,
  and derives its build context from the package location, not `cwd`.
- `up`/`down`/`restart --all` skip non-container backends instead of aborting.
- `beetroot shell <name> -c '<cmd>'` now passes the command through.
- `ls --json` no longer prints advisories to stdout.

### Quality / UX
- New `rich`-based output layer: `ls`/`status` tables, styled errors, and
  progress bars for downloads / snapshot pack-unpack / build (TTY-aware;
  `--json` stays plain and stable).
- Mutation testing (`mutmut`) now covers the whole package; the docstring
  gate is raised to 100%; new container-level CI (image build smoke +
  a dockerized boot test of `docker/*.sh`).
- `Display` / `Resources` fields are validated at config-load time.

### Deferred to a future release
Stealth / anti-root path randomization and the related `stealth_paths`
plumbing (kept provisional).

## v0.5.0 — 2026-05-20

A tech-debt and completeness release. **No schema change** (`api_version`
stays `3`) and **no breaking changes** — v0.4 instances and registries
work unchanged. The sprint closed the shippable-without-research items
from v0.4's deferred list, hardened the quality gates, and added the
first container-level CI coverage. The stealth track (randomized Frida
paths, Gadget mode) and the remaining backend-completeness items move to
v0.6.

### Theme T1: AdbDevice bug fixes + input validation

- **`AdbDevice.install_frida` now guards against a missing `adb`.** It
  raises `AdbNotInstalledError` with an install hint up front instead of
  failing mid-`adb push` with a cryptic subprocess error — matching the
  guard `shell()` and `frida_cli()` already had.
- **`AdbDevice.add_module` validates its source before pushing.** The
  host path must exist, be a regular file, and end in `.zip`; bad input
  raises `ValueError` with no partial `adb push` side effect.
- **Fixed a literal `{name}` in adb capability errors.** The `down` /
  `destroy` "use `beetroot forget`" hints were plain (non-f) strings, so
  `{name}` printed verbatim; they now interpolate the instance name.

### Theme T2: `adopt --verify` + the `forget` verb

- **`beetroot adopt --verify` / `-V`.** Opt-in flag that runs
  `adb devices` and refuses to register a serial that isn't a connected
  `device`, so a typo no longer creates a dead registry row. The default
  stays pre-registration-friendly (adopt a device before plugging it in).
- **`beetroot forget <name>`.** New verb that deregisters an instance —
  removing its registry row and freeing its port index — **without** the
  destroy side effects (no host-directory teardown, no `docker compose`
  call). It's the clean way to drop an adopted device and works for both
  redroid and adb backends. (This is the verb the adb capability errors
  already pointed users to.)

### Theme T3: quality gates

- **Mutation testing covers the whole package.** The nightly `mutmut`
  scope went from 4 modules to all 14 under `src/beetroot/`. The
  `--paths-to-mutate` CLI flag (removed in mutmut 3.x) was dropped in
  favour of `[tool.mutmut].paths_to_mutate` as the single source of
  truth, and the nightly timeout was raised for the larger surface.
- **Docstring-coverage gate raised 95% → 100%** (`interrogate`), to
  match the project's max-strictness posture.

### Theme T4: container-level CI

- **`docker-build-smoke` job.** Builds the `docker/Dockerfile` COPY
  layers on a `busybox` stand-in base to catch Dockerfile drift that
  hadolint can't. The real redroid base can't be pulled on GitHub
  runners, so this validates structure only (no boot).
- **Dockerized boot test.** `tests/test_container_boot.py` runs
  `docker/*.sh` end-to-end in a real `busybox` container with fake
  `magisk` / `frida-server` shims, asserting the Zygisk + denylist SQL
  writes, the module install, and the frida launch all fire. It's the
  first test that executes the boot scripts as a real shell would.

### Deferred to v0.6

- **Stealth PR1** — flipping the default Frida path off
  `/data/local/tmp/` to a randomized layout. Still gated on a written
  research decision (does GMS scan all of `/data/adb/modules/`
  regardless of Shamiko's namespace switch?). The v0.4 plumbing
  (`stealth_paths`, `BEETROOT_FRIDA_BIN`, the snapshot `path_layout`
  round-trip) remains in place and unused.
- **AdbDevice module auto-install** (the `sha256`-enforcing
  push-to-`modules_update` variant). The safe-default push-to-Downloads
  is what ships today.
- **Third-party backend JSON round-trip** through the registry, and
  marking the dual-form `registry.add` positional signature
  `@deprecated`.

## v0.4.0 — 2026-05-19

### Breaking changes (upgrading from v0.3)

Beetroot v0.4 is a foundation release: pydantic-typed registry,
second device backend (`AdbDevice`), three new user-facing verbs,
hardened CI / lint / type-check, and the plumbing for v0.5's
randomized-path stealth work. The CLI auto-migrates v0.3 registries
and YAMLs (one warning per process), but a handful of changes need
attention.

Step-by-step walkthrough:
[Migrating from v0.3 to v0.4](docs/guides/migration-v0.3-to-v0.4.md).

- **Schema bump 2 → 3.** `SUPPORTED_API_VERSION = 3`; v0.3 YAMLs
  with `api_version: 2` auto-bump on load (one-line stderr warning,
  no field renames). v0.2 YAMLs still auto-bump too — v0.2 → v0.4 in
  one hop is supported.
- **Registry schema v2 → v3.** `instances.json` round-trips through
  the strict pydantic `RegistryFile` → `InstanceMeta` →
  discriminated-union `BackendConfig` over `kind: "redroid"` /
  `"adb"`. v2 registries are renamed to `instances.json.bak` and a
  fresh empty v3 file is emitted. Re-register existing instances
  with `beetroot register <path>` after the upgrade.
- **`Manager.allocate_port_index` removed.** Now module-private
  `_allocate_port_index`. The public method was a footgun (Agent 2
  F-4) — use `registry.add_allocating(name, ...)` for atomic allocate
  + register.
- **`Settings.extra="forbid"`.** Typo'd `BEETROOT_*` env vars now
  fail loudly at import time with `ValidationError`. The four
  forwarded vars (`BEETROOT_MAGISK_DB`, `BEETROOT_MODULES_DIR`,
  `BEETROOT_FRIDA_BIN`, `BEETROOT_BUILD_CONTEXT`) are declared as
  fields so they pass the gate.
- **`Settings` dropped `env_file=".env"`.** No more CWD-based `.env`
  auto-load — Beetroot reads settings strictly from `os.environ`.
- **`Frida.version` regex.** Only `X.Y.Z` shapes are accepted —
  typos surface at config-load time instead of as a download 404.
  A new optional `Frida.sha256` field verifies the cached binary.
- **Module renames `frida_dl` → `frida_download`, `modules_dl` →
  `modules_download`.** Update `from beetroot import frida_dl`
  imports to the expanded names. Public function/class surface is
  otherwise unchanged.
- **Instance-name regex `[a-z0-9_-]+`** (the Docker compose
  project-name grammar). v0.3 silently accepted `Alpha` /
  `alpha bravo` / `alpha.bravo`, then compose blew up at the first
  `up`. v0.4 validates at the OOP boundary before any side effect.
- **Mount target `/flash_dir` → `/data/adb/modules_update/`.**
  Existing v0.3 instances need one `beetroot down && beetroot up`
  cycle to rebind. No data movement on the host side.
- **`BackendCapabilityError` → exit code 2.** Lifecycle verbs
  (`up` / `down` / `apply` / `destroy` / `snapshot`) raised against a
  non-redroid backend now exit with code 2 — distinct from
  "instance not found" (exit 1) and from generic errors. Wrapping
  scripts can now distinguish via `$?`.

### v0.4 — Theme T1: pydantic foundation + schema v3 + Protocol expansion + backend registry

**Breaking changes**

- **`api_version` bumped to `3`.** v0.3 YAMLs that hard-pinned
  `api_version: 2` auto-bump on load with a one-line stderr warning
  (the bump is strictly additive — no fields renamed). v0.2's
  `api_version: 1` continues to auto-bump too, so users on the v0.2 →
  v0.4 path don't need an intermediate stop. Persistence happens
  organically on the next `beetroot apply`.
- **Registry schema v2 → v3 migration.** `instances.json` now
  round-trips through a strict pydantic model (`RegistryFile` →
  `InstanceMeta` → discriminated-union `BackendConfig` over `kind:
  "redroid"` and `kind: "adb"`). v2 registries are renamed to
  `instances.json.bak` on first read and a fresh empty v3 file is
  emitted — same backup-and-empty pattern v0.3 used for v1 → v2.
  Re-register your existing instances with `beetroot register <path>`
  after the upgrade.
- **`Manager.allocate_port_index` is removed** (Agent 2 F-4: the index
  isn't reserved by this call, so calling it without an immediate
  follow-up `registry.add` is a footgun). Use
  `registry.add_allocating(name, path)` for atomic allocate +
  register.

**New surface**

- **`DeviceBackend` Protocol expansion.** New members on the Protocol:
  `name: str`, `kind: str`, `shell() -> int`, `frida_cli(args:
  list[str]) -> int`, and a `from_meta(name, backend_config)`
  classmethod used by the backend-registry dispatcher.
- **`beetroot.backends` registry.** Discovers third-party backends via
  `[project.entry-points."beetroot.backends"]`; in-tree backends
  (currently just `redroid`) register programmatically at import time.
  `Manager.resolve(name)` dispatches via the registry.
- **`BackendCapabilityError(RuntimeError)`.** Verbs that don't
  generalise across backends (`up`, `down`, `apply`, `snapshot`)
  raise this when called on a backend that doesn't expose them.
- **`Stealth.denylist` regex validator.** Per-entry packages must
  match the Android package-id grammar (`[a-zA-Z0-9._]+`). SQL-injection
  prophylaxis for T2's wire-up of the denylist through
  `magisk-config.sh`'s SQLite REPLACE INTO.

**Internal**

- `Manifest` (snapshot.py) is a frozen pydantic `BaseModel` with
  `extra="forbid"` and a new `kind: Literal["redroid"]` discriminator
  + typed `path_layout: dict[str, str]` field. The `_coerce_manifest`
  helper is gone.
- `compose.ps_status` returns a closed `Literal` (`ComposeStatus`)
  rather than free-form `str`. Adds explicit
  `"docker-unreachable"` / `"starting"` / `"created"` / `"paused"`
  / `"unknown"` mapping. Agent 2 B-7.

### v0.4 — Theme T2: audit-pass bug fixes + rename pass

**Breaking changes**

- **`beetroot.frida_dl` → `beetroot.frida_download`** and
  **`beetroot.modules_dl` → `beetroot.modules_download`.** Python is
  explicit-by-design — `_dl` was the only ambiguity in the public
  module surface. Update `from beetroot import frida_dl` imports to
  `from beetroot import frida_download`. The module-level public
  surface (`download`, `stage_for_instance`, `stage_empty`,
  `sha256_of`, `release_url`, `cached_binary`, `frida_cache_dir`,
  `ModuleFetchError`) is otherwise unchanged.
- **`/flash_dir` → `/data/adb/modules_update` bind-mount target.** v0.3
  bind-mounted `<instance-dir>/modules` to `/flash_dir`; v0.4 moves
  the target to Magisk's standard module-staging directory
  (`/data/adb/modules_update`), driven by `BEETROOT_MODULES_DIR`. The
  default applies to every new `down + up` cycle — existing running
  v0.3 containers see the change at next restart.

**Bug fixes (audit-flagged)**

- **`Stealth.denylist` wired through to `magisk-config.sh`.** v0.3 read
  the YAML field and threw it away — the helper enrolled a hard-coded
  GMS pair regardless. v0.4 pipes `cfg.stealth.denylist` through
  `render_env` as `BEETROOT_DENYLIST_PACKAGES` (comma-separated; toybox
  sh has no array support), and the helper iterates the list via
  `IFS=,`. The pydantic regex from T1 still gates per-package shape so
  the SQL is safe to compose without escaping. `Stealth.denylist`'s
  default now ships the GMS pair so a bare `beetroot create` keeps the
  v0.3 enrolment behaviour intact. (Agent 2 B-1, Agent 3 1.3.)
- **Compose mount targets parameterised.** The bundled compose template
  now reads `${BEETROOT_FRIDA_BIN}` / `${BEETROOT_MODULES_DIR}` on
  **both sides** of the bind-mount entry (v0.3 hardcoded the container
  side). `render_env` emits both with the known-safe v0.3 paths
  (`/data/local/tmp/frida-server`, `/data/adb/modules_update`). v0.5's
  PR1 will replace these defaults with randomised values once stealth
  research validates a path. (Agent 1 1.1, Agent 3 1.1.)
- **Magisk Zygisk write is verified.** After the `REPLACE INTO settings
  VALUES ('zygisk', 1)`, `magisk-config.sh` now `SELECT`s the row back
  and exits non-zero if Magisk returned anything other than `1`. v0.3
  silently trusted the REPLACE; this catches schema drift or daemon-race
  regressions loudly. (Agent 1, Agent 2 F-9, Agent 3 1.2.)
- **`Instance.add_module` is now stage-first.** v0.3 mutated
  `self._cfg.modules` and wrote `beetroot.yaml` BEFORE downloading the
  zip — a 404 left the YAML polluted with a module the user couldn't
  reach. v0.4 stages first against a transient `InstanceConfig` and
  only commits to the YAML + in-memory model on success. Re-running
  the verb with a corrected URL is now safe. (Agent 2 B-6, Agent 3 1.6.)
- **`Instance._stage` split into `_stage_local` + `_stage_network`.**
  Local artefacts (`.env`, data/modules dirs, Frida placeholder) are
  rollback-fatal — a failure there destroys the partial install. The
  network step (real Frida binary, module zips) runs AFTER the
  registry commits and is soft-fail: a Frida 404 prints a hint and
  leaves the instance registered for the user to recover via
  `beetroot apply <name>`. (Agent 2 B-2.)
- **`snapshot.restore --force` validates the archive before wiping
  the target.** v0.3 ordered `shutil.rmtree(target)` before
  `read_manifest(archive)` — a corrupted archive paired with `--force`
  destroyed the user's existing directory AND then bailed out with
  no way back. v0.4 swaps the order so the manifest read is the gate.
  (Agent 3 1.4.)
- **`Instance.destroy` reorders cleanup steps.** v0.3 ran
  `compose.down` → `shutil.rmtree` → `registry.remove`. A ^C between
  the rmtree and `registry.remove` stranded a registry row pointing
  at a now-gone directory — an orphan the user could only fix by
  re-creating the dir then running destroy again. v0.4 reorders to
  `compose.down` → `registry.remove` → `shutil.rmtree` so a ^C
  between the last two steps leaves a tidy registry + a stale dir
  the user wipes manually. The CLI verb's order already matched;
  this aligns the OOP path. (Agent 2 B-4.)
- **`paths.bundled_compose_file` uses `importlib.resources.as_file`.**
  v0.3 stringified the `Traversable` returned by `files()` and
  wrapped it in `Path()` — fine for editable installs (where the
  resource lives on disk) but breaks wheel installs where the
  resource lives inside a zip. v0.4 uses `as_file()` to materialise
  a stable on-disk copy under `user_cache_dir("templates")` (which
  T3 will migrate to `platformdirs.user_cache_path`); the path is
  cached at module level so subsequent `docker compose -f` calls
  resolve identically. (Agent 2 B-8.)
- **`fcntl.flock` on snapshot + destroy.** v0.3 had no inter-process
  coordination — a `beetroot snapshot foo` racing a `beetroot
  destroy foo` could rmtree the source directory mid-archive read,
  producing a torn `.tar.zst`. v0.4 adds an advisory lock at
  `<instance_root>/.beetroot.lock`: `snapshot()` takes `LOCK_SH`
  (parallel snapshots are fine) and `Instance.destroy()` takes
  `LOCK_EX` (blocks every reader and waits for in-flight readers
  to release). (Agent 2 B-12.)
- **`Frida.version` regex validator + optional `sha256`.**
  `Frida.version` now matches `^[0-9]+\.[0-9]+\.[0-9]+$` so typos
  surface at config-load time instead of as a 404 from
  `github.com/frida/frida/releases` at `frida_download.download`
  time. A new optional `Frida.sha256` field is forwarded to
  `download(..., expected_sha256=...)`; if set, the cached binary's
  digest is verified case-insensitively and a mismatch raises
  `ValueError` (defends against a hostile mirror substituting the
  release). (Agent 1.)
- **`Settings` no longer auto-loads `.env`.** v0.3's
  `SettingsConfigDict(env_file=".env", ...)` made every
  `Settings()` instantiation walk the *current working directory's*
  `.env` file. Inside an instance directory that's the
  Docker-compose env file (`INSTANCE_NAME=…`, `ADB_PORT=…`, etc.) —
  values Beetroot must not pick up. v0.4 drops `env_file`; settings
  read strictly from `os.environ`. (Agent 3 1.5, Agent 4.)
- **Instance-name regex on `Instance.create` / `Instance.register`.**
  Names must match `[a-z0-9_-]+` (the Docker compose project-name
  grammar). v0.3 silently accepted `Alpha` / `alpha bravo` /
  `alpha.bravo`, then compose blew up with a cryptic error at the
  first `up`. v0.4 validates at the OOP boundary before any side
  effect runs — no mkdir, no registry write, no port allocation
  for a bad name. The default basename used by `register` (when
  `name=` is omitted) goes through the same gate. (v0.3.1 deferred.)
- **`Manager.list` + `Manager.list_orphans` now surface unparsable
  YAML.** v0.3's `list_orphans` only surfaced rows whose
  `beetroot.yaml` was *missing*; a corrupted or
  api_version-mismatched YAML was invisible to both `list` and
  `list_orphans`, so the user had no way to surface the row for
  cleanup. v0.4 treats "can't parse" identically to "doesn't
  exist". `Manager.list` also stops swallowing every
  `FileNotFoundError` from `Instance.load` — only the
  yaml-missing pre-check filters orphans now; any unexpected
  OSError bubbles. (v0.3.1 deferred, Agent 2 F-12, Agent 3 1.7.)
- **`cli.main` catches `registry.RegistryError`.** v0.3 let
  `RegistryError` ("unknown instance X", "X is an adb backend, no
  on-disk dir") propagate as a Rich-rendered traceback. v0.4
  catches it alongside `ComposeError` / `BootstrapError` /
  `ModuleFetchError` for the same friendly `error: ...` line.
  (Agent 3 1.9.)

### v0.4 — Theme T3: max-strictness CI / pre-commit / lint / type-check / test investment

**Runtime dependencies**

- **Added `platformdirs>=4`** as a runtime dep. `paths._xdg_dir` and
  its hand-rolled `os.environ.get("XDG_*_HOME")` plumbing is gone —
  user config / cache paths now resolve via
  `platformdirs.user_config_path("beetroot")` and
  `platformdirs.user_cache_path("beetroot")`. On Linux the same env
  vars are honoured automatically; on macOS / Windows the paths now
  match platform conventions.
- **`builder._DEFAULT_WORK_DIR = Path("/tmp/redroid")` is gone** —
  the redroid-script clone now lives under the per-user cache
  (`user_cache_dir("redroid-script")`). Closes Agent 4's `S108`
  bandit finding and stops the clone from being wiped by aggressive
  `/tmp` cleaners between builds.

**Settings hardening**

- **`Settings` is now frozen + `extra="forbid"`**. The forwarded
  container-bound vars (`BEETROOT_MAGISK_DB` / `BEETROOT_MODULES_DIR`
  / `BEETROOT_FRIDA_BIN` / `BEETROOT_BUILD_CONTEXT`) are declared as
  fields so the strict-extras flip doesn't break researchers who
  export them. Mutating `settings` in-process now raises
  `ValidationError` — tests that used to mutate `settings.docker_bin`
  in-place were updated to swap the module-level singleton instead
  (see `settings.py`'s module docstring for the new pattern).
- **`docker/*.sh` boot helpers** all start with `set -eu` (Agent 2
  CI-4). A typo'd `magisk --sqlite` or an unbound env var now fails
  the boot loud instead of silently coming up half-configured.

**Lint / type-check / test gates (all new, all blocking unless noted)**

| Gate | Tool | Threshold |
|------|------|-----------|
| Docstring coverage | `interrogate` | `--fail-under=95` (currently 99.2%) |
| Cyclomatic complexity | `radon cc -n C` | no function grade C or worse |
| Dead-code finder | `vulture --min-confidence 80` | no findings outside the allowlist |
| Dependency CVE scan | `pip-audit` | no high-severity CVEs (PYSEC-2022-42969 suppressed; transitive via interrogate) |
| Second-opinion type-check | `pyright src/ tests/` | clean |
| Dockerfile linter | `hadolint docker/Dockerfile` | clean (`DL3007` whitelisted for `${BASE_IMAGE}` ARG) |
| Shellcheck severity | `shellcheck -S warning` | bumped from default |
| Mutation testing | `mutmut run` (nightly cron) | non-blocking; survival rate artefact |

**Mypy tightening**

- `warn_return_any = true`
- `disallow_any_explicit = true`
- `warn_unused_configs = true`
- explicit `strict_optional = true`
- `enable_error_code` gained `narrowed-type-not-subtype`

Every explicit `Any` annotation in src and tests was replaced with a
concrete type, `object`, or removed entirely. The two surviving
`# type: ignore[call-overload]` suppressions (in `compose.run` and
`tests/test_subprocess_env_merge.py`) are needed because
`**kwargs: object` is incompatible with subprocess.run's overload
set — both narrow back to `CompletedProcess[str]` at the call
boundary.

**Ruff tightening**

Added rule families: `ICN`, `DTZ`, `ASYNC`, `BLE`, `S`, `EXE`, `Q`,
`INP`, `T20`, `SLF`, `RUF100`. Removed the stale `max-statements = 60`
pylint exemption.

Per-line `# noqa` justifications added to the four subprocess sites
Agent 4 flagged (`api.shell`, `api.frida_cli`,
`builder.DefaultRunner.run`, `compose.run`), the two `urlopen` sites
(`frida_dl.download`, `modules_dl._fetch_url`), and the three
stderr-migration print calls in `config.py` / `registry.py`.

**Property-based tests (hypothesis, derandomized seed in CI)**

- `tests/test_property_registry.py` — `InstanceMeta` and
  `RegistryFile` JSON round-trip is identity across both backend-
  config variants.
- `tests/test_property_ports.py` — `lowest_free_index` never
  collides under arbitrary `(used_indices, allocation_count)`;
  `ports_for_index(N)` is always exactly `base + N*STRIDE` with all
  three ports pairwise distinct.
- `tests/test_property_render_env.py` — every `render_env` line is a
  shell-safe `KEY=VALUE` pair (regex `^[A-Z_][A-Z0-9_]*=.*$`) free
  of `'`, `"`, `` ` ``, `$`.

**Refactor**

- `snapshot.restore` was scoring radon grade C — split into
  `_prepare_destination` + `_check_restored_port_collision` helpers.
  No public-surface change; `restore` itself is now a 15-line
  orchestrator that reads top-to-bottom.

**Pre-commit & scripts**

- `.pre-commit-config.yaml` — `changelog-lint` hook now fires on
  changes to `src/beetroot/cli.py` too (a verb rename must
  invalidate the linter). New `interrogate` hook keeps docstring
  coverage above 95% on every commit.
- `scripts/lint_changelog.py` — now scans prose-inline backtick
  spans inside `## Unreleased`, not just fenced shell blocks. The
  v0.3 retro showed inline drift sails through fenced-only matchers
  too. Six new tests in `tests/test_lint_changelog.py` exercise the
  extractor + matcher directly.

**CI workflow (`.github/workflows/ci.yml`)**

- Every third-party action SHA-pinned with a trailing `# v<version>`
  comment.
- New `docstring-and-complexity` and `dependency-audit` jobs.
- `hadolint` job added.

**Mutation-testing nightly (`.github/workflows/mutation-nightly.yml`)**

New cron `0 4 * * *` runs `mutmut` against the four load-bearing
modules (`registry`, `snapshot`, `api`, `config`). Survival rate is
published as a 30-day-retained workflow artefact. Expanding the
surface to all of `src/beetroot/` is on the v0.5 deferred list.

### v0.4 — Theme T4: light stealth plumbing — stable registry blob + modules_update path

This is the **plumbing-only** slice of `docs/design/stealth-posture.md`'s
v0.4 scope (PR5 + PR6). The actual `/data/adb/modules/<random>/...`
Frida-path move (PR1) is deferred to v0.5 pending stealth research —
user concern: GMS may scan the entirety of `/data/adb/modules/`
regardless of Shamiko's namespace switch. v0.4 lands the wiring so
v0.5 can ship the default flip as a one-line change in
`Instance.create`'s generator once a safe path is validated.

**Mount-target swap (PR5 of stealth-posture.md)**

- **`/flash_dir` → `/data/adb/modules_update` is the new default
  bind-mount target.** T2 already shifted the bundled compose template
  and `render_env`'s `BEETROOT_MODULES_DIR` default; T4 completes the
  swap by also updating `docker/flash-modules.sh`'s
  `${BEETROOT_MODULES_DIR:-/data/adb/modules_update}` POSIX fallback,
  so a bare `docker run` without a Beetroot-rendered `.env` lands on
  the same path as the CLI would emit. The `${VAR:-default}` form is
  the right one under T3's `set -eu` (a bare `$VAR` would explode).
  `/data/adb/modules_update` is Magisk's well-known module-staging
  directory — Magisk's daemon recognises modules there at boot and
  installs them on the next reboot, no user action needed.

**`stealth_paths` round-trip (PR6 of stealth-posture.md)**

- **`snapshot()` writes the source's
  `RedroidBackendConfig.stealth_paths` blob into the manifest's
  `path_layout` field.** v0.4's slot defaults to `{}`, so today's
  snapshots carry `path_layout: {}` and round-trips are byte-identical
  to v0.3-shape — but the moment v0.5's `Instance.create` generator
  populates the slot, `snapshot → restore` will carry the randomized
  layout through to the destination without any further code change.
- **`restore()` replays `manifest.path_layout` into the new
  instance's `stealth_paths` slot** via a new
  `registry.set_stealth_paths(name, blob)` helper (exclusive-locked,
  atomic-replace via the existing `_write` pattern; rejects unknown
  names and adb-backed rows). The replay lives inside `restore`'s
  rollback try/except so a malformed blob from a future manifest
  schema bump tears down the half-registered row cleanly.
- **`render_env` gains an optional `stealth_paths` argument** that
  merges over the v0.4 defaults: `magisk_db` → `BEETROOT_MAGISK_DB`,
  `modules_dir` → `BEETROOT_MODULES_DIR`, `frida_bin` →
  `BEETROOT_FRIDA_BIN`. The key vocabulary matches the snapshot
  manifest's `path_layout` naming so the round-trip is direct.
  Unknown keys are silently ignored (forward-compat for v0.5/v0.6
  schema bumps that add e.g. `stealth_module_id`). `_stage_local`
  reads the per-instance slot from the registry and forwards it.

**Migration**

- **v0.3 instances need one `beetroot down && beetroot up` cycle**
  after the v0.4 upgrade to rebind to the new
  `/data/adb/modules_update` mount target. The host-side
  `<instance-dir>/modules` directory does not move; only the
  container-side bind-mount target changes. Magisk picks up modules
  staged in `modules_update/` on the next boot the same way it
  picked them up from `/flash_dir`.
- Snapshots produced before T4 (with `path_layout: {}`) restore
  cleanly against the T4 codepath — the empty replay is a no-op.

**Tests**

- `tests/test_stealth_paths.py` — five behaviour-test classes pin
  the full round-trip surface: snapshot writes the blob, restore
  replays it, empty manifests fall through to `modules_update`
  defaults (including a `.env` artefact assertion proving the
  `/flash_dir` invention is gone), `render_env` byte-pinned with
  each combination of overrides + unknown-key forward-compat,
  `set_stealth_paths` error paths (unknown name, adb-kind row,
  caller-mutation-leak guard).

### v0.4 — Theme T5: `AdbDevice` backend + `beetroot adopt` + 30-LOC backend recipe

**The second-backend deliverable.** v0.3's `DeviceBackend` Protocol existed
on paper only — `Instance` was the sole implementation. T5 ships the
real `AdbDevice` (driving rooted phones / emulators / network-adb
devices via the host `adb` CLI) so the Protocol's modularity is now
load-bearing, plus a synthetic third-backend test that grades the
"~30 LOC + one entry-point line" extension recipe at every CI run.

**New backend: `AdbDevice`**

- **`src/beetroot/backends/adb.py`** — implements every property and
  method on the `DeviceBackend` Protocol. `install_frida` downloads
  via the existing `frida_download.download` cache, then runs the
  full `adb push` + `chmod 755` + `su -c '... &'` + `adb forward
  tcp:<host_port> tcp:27042` sequence. `shell` / `frida_cli` are
  thin shells over `adb -s <serial> shell` and `frida -H
  localhost:<host_port>`. `add_module` ships the **safe-default
  variant**: pushes the zip to `/sdcard/Download/<basename>` and
  prints a "install via Magisk app → Modules tab" instruction; the
  `--auto-install` direct-to-`/data/adb/modules_update/` variant is
  deferred to v0.5.
- Lifecycle methods (`up` / `down` / `restart` / `apply` / `destroy`
  / `snapshot`) raise `BackendCapabilityError` with a friendly
  message — adb-adopted devices are managed outside Beetroot, so
  there's no container to start/stop and no on-disk directory to pack.
- Registers itself at import time as `kind="adb"` in the in-tree
  backend registry; `Manager.resolve("phone")` returns an `AdbDevice`
  for every adb-kind registry row.

**New CLI verb: `beetroot adopt`**

- `beetroot adopt <serial> [--name <n>]` — registers a rooted device
  that's already reachable via `adb` under the global registry. Picks
  the lowest free stride-of-10 port index so a follow-up
  `beetroot frida <name>` lands on the same port a redroid instance
  with the same index would have got. The default name is
  `adb-<serial>` (lowercased, colons folded to hyphens, truncated to
  24 chars to fit the Docker compose project-name grammar). IPv4-
  shaped serials (with dots) require an explicit `--name`.

**CLI Protocol-dispatch refactor**

- `shell` / `env` / `frida` / `module` now resolve via
  `Manager.resolve(name)` so the same verb body works uniformly for
  redroid and adb backends. `module` keeps the redroid-specific
  beetroot.yaml update path for `Instance` backends, dispatches via
  the AdbDevice helper for adb backends, and emits a friendly error
  for third-party backends without `add_module`.
- Lifecycle verbs (`up` / `down` / `restart` / `apply` / `destroy`
  / `snapshot`) narrow via the new `cli._resolve_redroid` helper.
  Non-redroid backends surface `BackendCapabilityError` — caught by
  `cli.main` and rendered as `error: ...` + **exit code 2** (distinct
  from "instance not found" → 1) so wrapping scripts can distinguish.
- `destroy` checks the registry kind directly (rather than via
  `Manager.resolve`) so orphan redroid rows (registered, on-disk dir
  removed) still flow through the v0.3 orphan-destroy path.

**Registry surface**

- `registry.add` and `registry.add_allocating` now accept a `backend=
  <BackendConfig>` keyword for the discriminated-union form alongside
  the v0.3 positional `(name, absolute_path, index)` form. The v0.3
  form is preserved for source-compat; the v0.4 form is what
  `beetroot adopt` calls.

**Documentation**

- `docs/reference/cli.md` — new `## adopt` section documenting the
  verb, the default-name builder, and the exit-code-2 convention.
- `docs/reference/api.md` — new `## beetroot.backends.adb` section
  for the `AdbDevice` class.
- `examples/adb-device.yaml` — documentation-only file describing the
  conceptual shape of an adb-kind registry row (because adb-backed
  instances do **not** have a real `beetroot.yaml`).

**Tests**

- `tests/test_adb_device.py` — every `subprocess.run(["adb", ...])`
  stubbed; per-method argv assertions cover `is_available` (parses
  `adb devices`), `install_frida` (full 4-call sequence with the
  exact argv shape), `shell`, `frida_cli`, `add_module`, `from_meta`,
  and every lifecycle stub.
- `tests/test_adopt_verb.py` — `CliRunner` tests for `beetroot adopt`
  default-name + explicit-name + collision + invalid-name paths, plus
  Protocol-dispatch tests confirming `shell` / `env` work and `up` /
  `destroy` / `snapshot` exit 2.
- `tests/test_manager_polymorphism.py` — registers one redroid + one
  adb instance; asserts `Manager.list()` returns both as
  `DeviceBackend`-typed objects, narrows correctly via `isinstance`,
  and that lifecycle calls on the adb backend raise
  `BackendCapabilityError`.
- `tests/test_backend_extension.py` — **the load-bearing synthetic
  third-backend test**. Defines a `FakeBackend` + `FakeBackendConfig`
  inline in ~30 LOC, registers via `register_backend("fake",
  FakeBackend)` in an autouse-cleanup fixture, and asserts (a) the
  Protocol is satisfied structurally, (b) `Manager.resolve` returns
  the fake class, (c) `shell()` dispatches via the Protocol surface
  with the right argv, (d) the `_resolve_redroid_for_backend` helper
  raises `BackendCapabilityError` cleanly for non-Instance backends,
  and (e) the third-party config round-trips through pydantic JSON.
  If this test passes, third-party backends will work too.
- `tests/conftest.py` — autouse `_snapshot_backend_registry` fixture
  snapshots `_BACKEND_REGISTRY` before each test and restores after,
  defending against the existing `pop("adb", None)` pattern in
  `test_backend_registry.py` permanently dropping AdbDevice from
  later tests in the same process.

**Migration**

- Pure addition. No breaking changes. Existing redroid workflows
  unchanged. Programmatic users that did
  `registry.add_allocating(name, path)` keep working; the new
  `backend=...` keyword is optional.

### v0.4 — Theme T6: new user-facing verbs — `status`, `doctor`, `env --all`

**New CLI verbs**

- **`beetroot status <name>`** — print a single-instance JSON snapshot to stdout. Reuses the row formatter that backs `ls --json` (factored into a new private `_instance_json_row` helper) so the per-instance shape is a strict superset of the v0.3 ls row. Required fields per the T6 spec: `name`, `kind`, `index`, `created_at`, `ports`, `status` (or `is_available` for adb), `adb_address`, `frida_address`, `stealth_paths` (empty `dict` in v0.4 — populated by the v0.5 stealth-paths PR). v0.3 back-compat keys (`path`, `adb`, `frida`) are retained alongside the new fields so existing `jq` pipelines keep working. Exits 0 on success; exits 1 if `name` is not in the registry.
- **`beetroot doctor <name>`** — run aggregated health checks. Output is one machine-parseable `<check>: pass|fail|skip [reason]` line per check. Exit code is the count of `fail` results, clamped to `min(fail_count, 255)` (POSIX exit-code ceiling). `skip` rows do not count toward the exit code. Redroid checks: `compose.status`, `adb.connect`, `frida.handshake` (skip if `cfg.frida is None`), `magisk.zygisk`, `magisk.denylist.com.google.android.gms`. Adb checks: `adb.serial`, `frida.handshake`, `magisk.zygisk`, `magisk.denylist.com.google.android.gms` (no `compose.status` — not applicable to a physical phone). Check names are shared verbatim across backends so downstream tools can grep uniformly.
- **`beetroot env <name> --all`** — extends the existing `env` verb. Without `--all`, `env` keeps its v0.3 shape (exactly two `export` lines: `ANDROID_DEVICE` + `FRIDA_DEVICE`) so `eval $(beetroot env alpha)` scripts keep working. With `--all`, every key from `config.render_env()` is emitted as a shell export (`ADB_PORT`, `FRIDA_PORT`, `BEETROOT_MAGISK_DB`, `BEETROOT_DENYLIST_PACKAGES`, etc.) followed by the v0.3 `ANDROID_DEVICE` / `FRIDA_DEVICE` pair. For adb-backed instances `--all` emits a minimal `ADB_SERIAL` + `FRIDA_HOST` pair — `render_env` assumes a redroid backend, so the compose `.env` keys don't apply to a physical phone.

**`Instance.health()` + `CheckResult` + `adb_device_health()` API**

- **`api.CheckResult`** — frozen pydantic model with `status: Literal["pass", "fail", "skip"]` + optional `reason: str | None`. Returned from the new health surface keyed by check name. `frozen=True` + `extra="forbid"` so accidental mutation / typo'd fields surface at construction time.
- **`Instance.health() -> dict[str, CheckResult]`** — the redroid-backed health surface that `beetroot doctor` consumes. NOT part of the `DeviceBackend` Protocol — it's a capability method that not every backend supports (third-party cloud backends may not have any equivalent), so per the v0.3 device-backend design doc it lives on the concrete class rather than the Protocol. Callers narrow via `isinstance(b, Instance)` (or the free function below for adb).
- **`api.adb_device_health(device: DeviceBackend) -> dict[str, CheckResult]`** — a free function (not a method on `AdbDevice`) because T6 lands BEFORE T5's `AdbDevice` exists. T5 (or a follow-up commit after both T5 and T6 merge into `dev/v0.4`) wires this in as an actual method — either by aliasing `AdbDevice.health = lambda self: adb_device_health(self)` or by migrating the body to a proper method. Uses only the Protocol surface (`adb_address`, `frida_address`) so it works against a minimal stub `DeviceBackend` in tests before `AdbDevice` lands.

**Subprocess calls + noqa rationale**

The doctor verb's `subprocess.run` sites (`adb connect`, `adb -s <serial> shell magisk --sqlite`, `nc -zw 1 host port`, `adb devices`) all carry per-line `# noqa: S603` / `S607` rationale comments. SQL composition for `magisk.denylist.<pkg>` is grammar-validated upstream by `config.Stealth` (only `[a-zA-Z0-9._]`), so the bandit `S608` warning is suppressed with a justification comment.

**Output rules**

- Doctor's stdout output uses `typer.echo(...)` (T3 enabled `T201`).
- Status's JSON output uses `json.dumps(..., indent=2, sort_keys=True)` for byte-stable output that `jq` and `diff` consume reliably.
- Doctor's `pass` rows elide the reason; `fail` and `skip` rows include it (separated by a single space).

**Tests**

- `tests/test_status_verb.py` — redroid happy path, adb happy path (asserts `serial` is present and `absolute_path` / `ports.frida2` are absent), error path (`status nonexistent` → exit 1 + `error: ...` line).
- `tests/test_doctor.py` — healthy redroid → exit 0, unhealthy redroid (zygisk = 0) → exit 1 with `magisk.zygisk: fail expected 1, got 0`, multi-fail → exit code = fail count, healthy adb → exit 0 with no `compose.status` line, frida-disabled → `frida.handshake: skip frida not configured`, frida-enabled → handshake runs.
- `tests/test_env_all.py` — bare `env` emits exactly the v0.3 two-line shape, `env --all` emits every `BEETROOT_*` key + the v0.3 pair, adb `env --all` falls back to `ADB_SERIAL` + `FRIDA_HOST`.
- `tests/test_health_checks.py` — unit coverage for every private `_check_*` helper's skip / OSError / nonzero-exit / value=0 / unknown-output / offline-state branch, plus the `min(fail_count, 255)` exit-code clamp.

**T5 coordination seam**

T6 landed before T5's `AdbDevice` class. The dispatch in `beetroot doctor` checks `meta.backend.kind` directly: redroid → `Instance.load(name).health()`; adb → `Manager.resolve(name)` then `adb_device_health(...)`. T5 (or a follow-up commit once both have landed) can attach `adb_device_health` as a method on `AdbDevice` — the free function already takes only Protocol-surface attributes, so the migration is a one-line `AdbDevice.health = lambda self: adb_device_health(self)` if a class-method body isn't preferred.

---

### v0.4 — Theme T7: documentation pass + v0.4.0 release roll

The closing theme of v0.4. Lands the documentation work the prior
themes flagged but couldn't ship cleanly without all surfaces in
place, plus the version + CHANGELOG roll that promotes the sprint to
a tagged release.

**New documentation**

- `docs/guides/migration-v0.3-to-v0.4.md` — schema bump, the new
  backend discriminated-union shape, the `stealth_paths` slot, new
  verbs (`adopt`, `status`, `doctor`, `env --all`), module renames,
  `Manager.allocate_port_index` removal, `Settings.extra="forbid"`
  + the four newly-declared `BEETROOT_*` vars, `Frida.version`
  regex + optional `sha256`, mount-target swap, exit-code-2
  convention for `BackendCapabilityError`, instance-name regex,
  `bundled_compose_file` via `importlib.resources.as_file()`, and
  `platformdirs` for cache + config paths. Closes with a v0.5
  known-limitations subsection covering the three T5 CR risks
  (`beetroot adopt` serial verification, dual-form
  `add_allocating`, third-party `kind` JSON round-trip).
- `docs/guides/adding-a-backend.md` — the T5 30-LOC recipe expanded
  end-to-end: why-third-backend scenarios, the Protocol surface
  verbatim, a complete `CloudBackend` + `CloudBackendConfig`
  example, entry-point registration, the in-process vs entry-point
  split, and the explicit "what works now vs deferred to v0.5"
  callout (registry-side JSON discrimination for third-party kinds
  is the v0.5 piece). Cross-links to the design doc and the API
  reference.

**Updated reference + design docs**

- `docs/reference/api.md` — new "Surfaces introduced in v0.4"
  section cataloguing `AdbDevice`, the expanded `DeviceBackend`
  Protocol, `BackendCapabilityError`, `Manager.resolve`,
  `register_backend`, the `BackendConfig` discriminated union,
  `CheckResult` + `Instance.health()` + `AdbDevice.health()`, and
  `registry.set_stealth_paths`. Cross-link to the adding-a-backend
  guide.
- `docs/reference/cli.md` — gains `## adopt` / `## status` /
  `## doctor` sections and the `--all` flag documentation on the
  `env` verb (these landed in the T5 + T6 docs commits but are
  cross-referenced from T7's release block).
- `docs/design/device-backends.md` — §6 PR1–PR5 marked DONE in v0.4
  with the v0.4-shipped class/file shape; the stale
  `Manager.allocate_port_index()` reference fixed (replaced with
  `registry.add_allocating`); forward-pointer to the new
  adding-a-backend guide.
- `docs/design/stealth-posture.md` — §7 PR5 + PR6 (plumbing only)
  marked DONE in v0.4; PR1 (default-path flip) explicitly deferred
  to v0.5 pending stealth research, with the research prerequisite
  documented inline in §3.1.
- `docs/how-it-works/boot-flow.md` — prepended with the
  `beetroot.yaml → render_env → .env → compose → helper-sh` diagram
  from the v0.4 plan's Context section, noting the env-driven chain
  is now plumbed end-to-end after T2's compose-template
  parameterisation fix.

**`AdbDevice.health()` follow-up (T5 / T6 coordination seam)**

T6 landed `adb_device_health` as a free function in `beetroot.api`
because T5's `AdbDevice` class didn't yet exist. T7 closes the seam:
the body lives in one canonical place (the free function), and
`AdbDevice.health()` is a real method that delegates to it so
backends own their own health surface. The free function is
preserved as a back-compat shim — programmatic callers that
imported `api.adb_device_health` pre-T7 keep working, and the
`cli.doctor` dispatch (which calls the free function on a
`DeviceBackend`-typed `Manager.resolve` result) is unchanged.

**`README.md` + `CLAUDE.md` updates**

README's "What you get" gains two v0.4 bullets — `beetroot adopt`
for researchers with rooted phones, and the third-party-backend
extension point (`[project.entry-points."beetroot.backends"]`). The
docs table grows rows for the adding-a-backend guide and the v0.3 →
v0.4 migration guide. CLAUDE.md's verb-list in the dev-workflow
section gains `adopt` / `status` / `doctor` so contributor muscle
memory matches the v0.4 CLI surface.

**Version + nav**

- `pyproject.toml` — `version = "0.3.0"` → `version = "0.4.0"`.
- `mkdocs.yml` — Guides section grows the two new pages; `mkdocs
  build --strict` is clean.

## v0.3.0 — 2026-05-19

### Breaking changes (upgrading from v0.2)

Beetroot v0.3 reshapes the user-visible surface in five places. Step-by-step
walkthrough: [Migrating from v0.2 to v0.3](docs/guides/migration-v0.2-to-v0.3.md).

- The cross-instance registry moved out of the repo to
  `~/.config/beetroot/instances.json` (respects `$XDG_CONFIG_HOME`). After
  upgrading, run `beetroot register <path>` for each of your old instance
  directories to add them to the new registry.
- `api_version: 2` is the new minimum. v0.2 YAMLs that hard-pinned
  `api_version: 1` auto-bump on load with a one-line warning (the field
  default is now `2`, so omitting the field still works).
- `beetroot setup` is renamed to `beetroot build`. `beetroot up --build`
  is gone — run `beetroot build` explicitly when you want a fresh image.
- `beetroot create --preset` is removed. Starter configs live under the
  repo's top-level `examples/` directory now; copy one over the
  freshly-generated `beetroot.yaml` and run `beetroot apply <name>`.
- Frida is opt-in. A new instance no longer stages a `frida-server`
  binary unless its `beetroot.yaml` declares a `frida:` block.

### v0.3 — Theme T1: explicit, Docker-inspired instance paths

Instance directories are now self-contained anywhere on disk. The
repo-rooted `instances/` and `instances.json` conventions are removed.
An "instance" is any directory containing a `beetroot.yaml`; the CLI
discovers the current one by walking up from the cwd, like git walks up
to `.git`. The cross-instance registry is now user-global at
`~/.config/beetroot/instances.json` (respects `$XDG_CONFIG_HOME`).

**Migration for existing v0.2 users:** Run `beetroot register <path>` for
each of your instance directories (whichever paths you used under
`instances/`) to add them to the new XDG-based registry. The CLI also
auto-migrates the registry: on the first load of a v1-shaped
`instances.json` it renames it to `instances.json.bak` and prints a
one-line warning telling you to re-register.

Other changes shipped together with T1:

* New `beetroot register <path>` verb adopts an existing instance dir.
* `beetroot create` learned `--path <dir>` to put the instance anywhere.
* `beetroot ls` now shows the `PATH` column / `path` field.
* The compose template moved into the wheel at
  `beetroot/templates/compose.yaml`; the repo-root `compose.yaml` is
  gone. Compose is invoked with `-f <bundled>` and
  `--project-directory <instance-dir>` so the template's
  `./data:/data` style mounts resolve correctly against any instance dir.
* Magisk module download cache moved to `~/.cache/beetroot/modules/`
  and the Frida binary cache to `~/.cache/beetroot/frida/` (both
  respect `$XDG_CACHE_HOME`).
* `Module.path` entries with relative paths now resolve against the
  instance directory itself (not the repo root).
* `paths.py` is rewritten: `repo_root` / `instances_dir` /
  `instance_dir(name)` / `compose_file` / `presets_dir` /
  `registry_file` are **removed**. Replacements:
  `instance_root(start=None)`, `instance_data(root)`,
  `instance_modules(root)`, `instance_frida(root)`,
  `instance_yaml(root)`, `instance_env(root)`,
  `bundled_compose_file()`, `user_registry_file()`,
  `user_cache_dir(subdir)`.
* `paths.ProjectRootNotFoundError` is renamed to
  `paths.InstanceRootNotFoundError` (still a `FileNotFoundError`).
* `registry.add(name, index)` now takes an extra `absolute_path` arg.
  New helper `registry.instance_path(name)` looks it up; new
  `registry.RegistryError` is raised for unknown-name lookups. Schema
  bumped to v2; loading a v1 registry triggers the migration described
  above.
* `SUPPORTED_API_VERSION` bumped to **2**. v0.2 YAMLs that hard-pinned
  `api_version: 1` must be updated (omitting the field still works —
  the default is now `2`).

### v0.3 — Theme T5: setup renamed to build

The one-time base-image bootstrap verb is renamed from `setup` to
`build`. `beetroot up` no longer accepts a `--build` flag — building
the image and starting an instance are two separate concerns, and
`up` should be fast and predictable.

**Migration for existing v0.2 users:** Run `beetroot build` instead
of `beetroot setup`. To get a fresh image before `beetroot up`, run
`beetroot build` explicitly first — `up` no longer accepts `--build`.

**Renamed:**

* CLI verb: `beetroot setup [variant]` → `beetroot build [variant]`.
* Module: `src/beetroot/setup_runner.py` → `src/beetroot/builder.py`.
* Function: `setup_runner.bootstrap_base_image()` →
  `builder.build_image()`. Signature and semantics are otherwise
  unchanged.

**Removed:**

* `beetroot up --build` flag. The CLI now intercepts the v0.2 invocation
  with a friendly migration hint pointing at `beetroot build` (kept as a
  hidden Typer option for one release; will be deleted in v0.4).
* `compose.up()`'s `build: bool` kwarg. The `compose.build()` helper
  is unchanged — call it separately if you need to rebuild from
  Python.

**Tests:**

* `tests/test_setup_runner.py` → `tests/test_builder.py`. Class names
  `TestGappsFlags`, `TestBootstrapErrorType`, etc. are unchanged;
  imports switch to `beetroot.builder`.
* New behavior test
  `tests/test_cli.py::TestCmdUp::test_up_does_not_pass_build_flag`
  asserts that `beetroot up alpha` runs and the compose argv it
  produces contains no `--build` token.
* Companion test `TestCmdUp::test_up_rejects_build_flag` asserts
  that passing `--build` now fails Typer's option parsing.

### v0.3 — Theme T3: presets are documentation only

`beetroot create --preset` is removed. Presets are no longer a
framework concept — they are documentation-only starter `beetroot.yaml`
files under the new top-level `examples/` directory (`default.yaml`,
`stealth.yaml`, `no-gapps.yaml`, `with-frida.yaml`). The CLI does not
load them.

`beetroot create <name>` now writes a minimal `beetroot.yaml` that
contains exactly:

```yaml
api_version: 2
android:
  version: 14
```

Every other field falls back to schema defaults. To recreate a v0.2
preset behaviour, copy the matching file from `examples/` over the
freshly generated `beetroot.yaml` and run `beetroot apply <name>`:

```bash
beetroot create alpha
cp examples/stealth.yaml alpha/beetroot.yaml
beetroot apply alpha
```

**Removed:**

* `beetroot create --preset` flag.
* `config.load_preset(preset_name)` function.
* The `beetroot.templates.presets` sub-package (the wheel-bundled
  starter YAMLs introduced in T1).

**Moved:**

* `src/beetroot/templates/presets/*.yaml` →
  top-level `examples/` directory, with a `# Documentation only —
  not loaded by the CLI` header comment at the top of every file.
* `docs/guides/presets.md` → `docs/guides/examples.md` (mkdocs nav
  entry renamed Presets → Examples).

### v0.3 — Theme T2: Frida is opt-in

`InstanceConfig.frida` now defaults to `None` instead of an implicit
`Frida(version="16.4.10")` block. New instances no longer download a
`frida-server` binary, and `entrypoint.sh` skips the launch (the
bind-mount is a 0-byte non-executable placeholder). To get the old
behavior, declare an explicit block in `beetroot.yaml`:

```yaml
frida:
  version: "16.4.10"
```

…or copy the `examples/with-frida.yaml` starter over a freshly
generated `beetroot.yaml`:

```bash
beetroot create alpha
cp examples/with-frida.yaml alpha/beetroot.yaml
beetroot apply alpha
```

**Migration for existing v0.2 users:** This is a behavior break, not a
schema break, so it rides T1's `api_version` bump (already at 2). v0.2
YAMLs that relied on the implicit Frida default now need to declare
`frida: {version: "16.4.10"}` explicitly (or copy `examples/with-frida.yaml`).
An empty block (`frida: {}`) still hydrates the model's own default
version, so the upgrade is a one-line addition.

Other changes shipped together with T2:

* New `examples/with-frida.yaml` declares the version-pin idiom for
  users who want Frida on.
* README's "What you get" bullet, the docs index "Frida" row,
  `docs/guides/frida.md`, `docs/guides/examples.md`,
  `docs/getting-started/first-instance.md`,
  `docs/how-it-works/architecture.md`,
  `docs/how-it-works/filesystem.md`,
  `docs/how-it-works/boot-flow.md`,
  `docs/reference/config.md`, and `docs/troubleshooting.md` all
  reframed to describe Frida as opt-in.
* Drive-by: stale `api_version: 1` examples in
  `docs/reference/config.md` bumped to `2` (T1 bumped the constant; the
  example snippets were missed in T1's docs sweep).

### v0.3 — Theme T4: Typer at the CLI surface

`src/beetroot/cli.py` was rewritten from `argparse` to
[Typer](https://typer.tiangolo.com/). User-facing semantics are
preserved bit-for-bit: every verb name, flag name, default value, and
exit code matches the v0.2 argparse shape. The internal procedural
modules (`compose`, `config`, `frida_dl`, `modules_dl`, `paths`,
`ports`, `registry`, `builder`) are untouched — the OOP refactor
is reserved for a later theme.

**What changes for users:**

- `beetroot --help` (and `beetroot <verb> --help`) now renders through
  Typer's Rich-powered formatter: boxed sections, color when the
  terminal supports it, an auto-included `--help` row, and an
  auto-included `--install-completion` / `--show-completion` row.
- Shell completion is available out of the box. Run
  `beetroot --install-completion` once per shell — Typer auto-detects
  bash, zsh, fish, or PowerShell via
  [shellingham](https://github.com/sarugaku/shellingham) and writes
  the hook into the right rc file. `beetroot --show-completion`
  prints the script without installing it.
- `beetroot frida alpha -- -l script.js` now uses the `--` separator
  to disambiguate forwarded flags from Beetroot's own option-parser.
  Plain `beetroot frida alpha -n com.app` keeps working unchanged
  (`-n com.app` is not a Typer/Click option, so it falls through);
  `--` is only needed if a forwarded flag would otherwise collide
  with one of Beetroot's options.

**What changes for contributors:**

- New runtime dependency: `typer>=0.12`. Pulls in `click`, `rich`,
  and `shellingham`. No optional extras — every Beetroot install
  gets the Rich help and shell completion.
- `cli.build_parser()` is removed. Tests that introspect the verb set
  iterate `cli.app.registered_commands` instead.
- The legacy `cli.cmd_*` argparse handler functions are renamed to
  their verb names (`cli.create`, `cli.apply`, `cli.up`, …) and
  decorated with `@app.command()`. The function bodies are unchanged.
- `tests/test_cli.py` and `tests/test_port_collisions.py` drive every
  verb through `typer.testing.CliRunner().invoke(cli.app, [...])`
  and assert on `result.exit_code` + `result.stdout` / `result.stderr`.
  The legacy `_ns(...)` / `_create_ns(...)` argparse-Namespace
  helpers are gone.
- A new behavior test
  (`tests/test_cli.py::TestCmdFrida::test_frida_forwards_remainder_args_verbatim`)
  pins the `beetroot frida <name> -- <args>` round-trip contract:
  any tokens after `--` reach the underlying `frida` subprocess
  verbatim, in order, after Beetroot's `-H localhost:<port>` prefix.
- `cli.main()` still catches `paths.InstanceRootNotFoundError` and
  `ports.PortCollisionError` from deep in the call tree and surfaces
  them as `error: <msg>` on stderr + `exit 1`, matching v0.2.

### v0.3 — Theme T6: snapshots

Two new CLI verbs land:

- `beetroot snapshot <name> [-o <archive>]` packs an instance's
  host-side state (`beetroot.yaml`, `data/`, `modules/`, the optional
  `frida-server` placeholder) into a single zstandard-compressed tar
  archive (`.tar.zst`). The `.env` is deliberately excluded — it's
  regenerated from `beetroot.yaml` on restore (Fix-up A's `_stage()`
  call) so `beetroot up <name>` works without an intermediate
  `beetroot apply`. If `-o` is omitted, the archive lands at
  `./<name>.tar.zst`. The `.tar.zst` extension is appended
  automatically when missing.
- `beetroot restore <archive> [--as <name>] [--path <dir>] [--force]`
  unpacks an archive into a new instance and registers it with a
  **fresh** port index — the source's index is never reused, so the
  original and the restored copy can run concurrently. Without `--as`,
  the manifest's stored name is used; without `--path`, the directory
  defaults to `./<name>`. `--force` wipes a non-empty destination
  before extracting; without it, the verb refuses and exits with
  `error: ...`.

The archive carries a `.beetroot-snapshot.json` manifest at its root
with six fields: `schema_version` (currently `1`), `name`,
`source_index`, `created_at` (ISO 8601 UTC), `beetroot_version` (from
`importlib.metadata.version("beetroot")`), and `path_layout` (a
`dict[str, str]`, always `{}` in v0.3). v0.4's stealth-posture work
(see `docs/design/stealth-posture.md`) will populate `path_layout`
with the source instance's randomized container-path mapping and
replay it into the restored instance's `BEETROOT_*` env vars on
`beetroot apply`. v0.3's `restore` doesn't act on `path_layout`, but
it never strips or rewrites the field — the manifest is preserved on
disk inside the restored instance dir at `.beetroot-snapshot.json`,
so a v0.4-produced archive restored by v0.3 keeps its layout intact
for the next read.

**The Docker overlay layer is not captured by design.** Redroid
regenerates the writable overlay deterministically from the base
image plus the persisted `/data` bind-mount, so `beetroot up` after a
restore produces an equivalent container without us having to ship
the overlay bytes. If you need to snapshot a customized base image
itself, use `docker image save` — Beetroot snapshots are an
instance-state artefact, not a Docker-image artefact.

Other changes shipped together with T6:

- New runtime dependency: `zstandard>=0.22`. The snapshot module
  composes the standard-library `tarfile` with
  `zstandard.ZstdCompressor().stream_writer()` /
  `ZstdDecompressor.stream_reader()` so the archive is produced and
  consumed as a true byte stream — no intermediate `BytesIO` blob
  the size of `data/`.
- `src/beetroot/snapshot.py` is the new pure module: three composable
  functions (`snapshot`, `restore`, `read_manifest`) plus the
  frozen-dataclass `Manifest` and the `SnapshotError` exception type.
  Stdlib `dataclass` rather than a pydantic model — the manifest
  shape is documented and validated explicitly without dragging
  `arbitrary_types_allowed` into the project.
- `tests/test_snapshot.py` ships the round-trip behavior test
  (`TestSnapshotRoundTrip::test_round_trip_preserves_data_bytes`):
  it creates an instance, writes known bytes into `data/marker.txt`,
  snapshots, destroys, restores under a new name + new path, and
  asserts the bytes are byte-identical and the new registry entry
  has a freshly allocated port index. The full test runs in well
  under 2s without network or Docker.
- `tests/test_readme.py::GHOST_VERBS` drops `"snapshot"` (it's a
  registered verb now).
- Docs sweep: `docs/guides/snapshots.md` is rewritten from the v0.2
  filesystem-recipe stub into the v0.3 `snapshot` / `restore` guide;
  `docs/reference/cli.md` gains the two new verb sections;
  `docs/index.md`, `docs/guides/index.md`, and
  `docs/how-it-works/filesystem.md` are reframed away from the
  "there is no snapshot verb" wording.

### v0.3 — Theme T8: high-level OOP Python API

A new `beetroot.api` module exports an object-oriented surface that
composes the procedural modules without replacing them. Researchers
who want to drive Beetroot from Python can now write
`from beetroot import Instance, Manager, DeviceBackend` instead of
reaching into the cross-module function vocabulary.

```python
from beetroot import Instance

inst = Instance.create("alpha")            # registers + stages
inst.up()                                  # docker compose up -d
inst.frida_cli(["-n", "com.victim"])       # frida -H localhost:27042 -n ...
inst.snapshot(Path("alpha-clean.tar.zst")) # pack to archive
inst.destroy(yes=True)                     # tear down + deregister
```

**Public surface:**

- `Instance` — single research phone (name + on-disk root + parsed
  config). Four classmethod constructors: `create` (new dir + new
  registry entry), `register` (adopt an existing dir into the
  registry), `load` (look up by name), and `from_path` (walk up to
  the nearest `beetroot.yaml` and match the registry by resolved
  path). Lifecycle methods (`up`, `down`, `restart`, `apply`,
  `destroy`) and operations (`shell`, `frida_cli`, `install_frida`,
  `add_module`, `snapshot`, `logs`). Introspection properties
  (`status`, `ports`, `adb_address`, `frida_address`,
  `is_available`) query live state — never cached on the object.
- `Manager` — stateless aggregate operations over the registry:
  `list()` (sorted), `get(name)` (`None` on miss), and
  `allocate_port_index()`.
- `DeviceBackend` — a `@runtime_checkable` Protocol that v0.3's
  `Instance` satisfies and that v0.4's `AdbDeviceBackend` will too.
  Four members: `adb_address`, `frida_address`, `is_available`,
  `install_frida(version)`. See the
  [device backends design doc](docs/design/device-backends.md) for
  the v0.4 roadmap.
- New exception types: `InstanceNotFoundError` (`LookupError`
  subclass; raised by `Instance.load` / `Instance.from_path`),
  `FridaNotInstalledError`, `AdbNotInstalledError`.

**CLI internals (refactored, no behavior change):**

- Every Typer command body in `src/beetroot/cli.py` is now a 1-15 line
  shell that constructs an `api.Instance` or calls an `api.Manager`
  method, then handles CLI-specific concerns (`error: ...` lines,
  `typer.Exit`, stdout formatting). The verbs stay as module-level
  `@app.command()` functions (Typer captures the function reference
  at import time, per the T4 CR — wrapping verbs in a class would
  break dispatch). `cli.py` shrinks from 554 to 487 LOC.
- The `destroy` verb keeps its own `compose.down` call rather than
  going through `Instance.destroy` so the registry-only-orphan path
  (where the on-disk `beetroot.yaml` has gone missing but the
  registry entry survives) still tears down cleanly.

**Procedural modules unchanged.** `compose`, `config`, `frida_dl`,
`modules_dl`, `paths`, `ports`, `registry`, `snapshot`, and
`builder` keep their public surface — `api.py` composes them.

**Tests:** `tests/test_api.py` exercises every `Instance` / `Manager`
method directly, asserting on observable side-effects (compose argv,
registry entries, filesystem artifacts) rather than just "didn't
raise". The required behavior tests landed: `Instance.create` →
`Instance.load` round-trip, `Instance.from_path` walk-up from a
subdir, `isinstance(inst, DeviceBackend)`. CLI dispatch assertions
cover `up`, `down`, `apply`, and `ls` — each mocks the corresponding
`Instance` / `Manager` method to confirm the Typer command really
calls into the OOP layer.

**Docs:**

- `docs/reference/api.md` now leads with `beetroot.api` and explains
  the two-audience split (programmatic users vs. CLI contributors).
- `README.md` gains a "Python API" row in the docs table.
- `docs/reference/index.md` calls out the OOP entry point.
- `CLAUDE.md`'s `src/beetroot/` tree gets `api.py` and `snapshot.py`,
  plus a paragraph explaining why the verbs stay module-level.

### v0.3 — Theme T7: shell scripts split, shellcheck in CI

`docker/entrypoint.sh` is split into a 12-line glue file plus three
single-purpose helpers, all in POSIX `/system/bin/sh`:

- `docker/magisk-config.sh` — Magisk daemon wait + Zygisk/denylist
  SQL + GMS denylist enrolment.
- `docker/flash-modules.sh` — iterate `*.zip` in the modules dir and
  install each one via `magisk --install-module`.
- `docker/launch-frida.sh` — check the executable bit on the frida
  binary and launch it as a backgrounded child of the entrypoint shell.

Every container-side path the helpers touch is read from a `BEETROOT_*`
env var with a safe default (`${VAR:-/safe-default}`); no path is
hard-coded. v0.3 keeps the defaults at the well-known paths
(`/data/adb/magisk.db`, `/flash_dir`, `/data/local/tmp/frida-server`),
and the bundled compose template threads `BEETROOT_MAGISK_DB`,
`BEETROOT_MODULES_DIR`, `BEETROOT_FRIDA_BIN` through its service
`environment:` block as empty-by-default host overrides. v0.4's
stealth-posture work (see `docs/design/stealth-posture.md`) sets these
to randomized per-build paths without editing any helper.

`docker/Dockerfile`'s `COPY` step changes from a single-file copy of
`entrypoint.sh` to `COPY --chmod=755 docker/*.sh /`, which places all
four shell files at filesystem root.

New CI job `shellcheck` runs `shellcheck -s sh docker/*.sh` on every
push and PR; any violation in any boot-helper script fails the build.

New docs page: `docs/how-it-works/boot-scripts.md` documents each
helper's env-var contract, idempotency model, and exit semantics, plus
the shared "POSIX sh only, no hardcoded paths" contract. `boot-flow.md`
gains a `## Helper scripts` anchor pointing at the new page so T10's
stealth-posture design doc's four existing `boot-flow.md#helper-scripts`
references resolve unchanged.

### v0.3 — Theme T9: device-backends design doc

A new design doc at `docs/design/device-backends.md` formalises the
`DeviceBackend` Protocol that v0.3's `beetroot.api` declares.
The doc lands:

- The rationale for a backend abstraction (real rooted phones over
  ADB; remote device farms).
- The Protocol surface (copy-pasted from `api.py` so the doc is the
  canonical reference).
- Two concrete backends — `RedroidBackend` (today's `Instance`,
  satisfying the Protocol directly) and `AdbDeviceBackend` (v0.4,
  wrapping an `adb`-connected device).
- The capability methods that aren't universal
  (`apply_stealth_config`, `shell`, `add_module`) and a new
  `BackendCapabilityError` exception type for backends that can't
  honour them.
- Emulator-only features (per-build path randomization, container
  overlay manipulation) cross-referenced against the
  [stealth-posture doc](docs/design/stealth-posture.md).
- A 5-PR roadmap for the v0.4 `AdbDeviceBackend` rollout (scaffolding
  → `install_frida` → `shell` → `add_module` → `beetroot adopt
  <serial>` CLI integration).
- Explicit out-of-scope list (rooting the device, MDM bypass,
  hardware-backed attestation).

1781 words, design-only. `mkdocs build --strict` passes; the page is
linked from the design-notes index and the mkdocs nav.

### v0.3 — Theme T10: stealth-posture design doc

- T10: stealth-posture design doc landed (`docs/design/stealth-posture.md`); v0.4 will implement the playbook.

### v0.3 — Post-CR fix-up

Round of fixes surfaced by the five v0.3 CRs. Each item is paired
with at least one behavior test that asserts on the artifact (not
"function was called"); see ``tests/`` for the named files.

* **pyproject version bumped 0.1.0 → 0.3.0.** The snapshot manifest
  writer reads ``importlib.metadata.version("beetroot")`` and stamps
  every archive — every v0.3 snapshot was previously claiming
  ``"0.1.0"``. *Integrator note: bump the git tag to ``v0.3.0`` at
  release time to match.*
* **builder.DefaultRunner merges ``os.environ`` into its env arg.**
  The old code passed a 2-key dict straight to ``subprocess.run``,
  which REPLACES the parent env — ``docker compose build`` launched
  with no ``PATH`` and failed with ``FileNotFoundError: 'docker'`` on
  every fresh install. Pinned by ``tests/test_subprocess_env_merge.py``.
* **``docker/flash-modules.sh`` no longer calls ``exit 0``.** The
  helper is sourced by ``entrypoint.sh`` with ``.``, so ``exit``
  terminates the parent shell, skipping ``launch-frida.sh`` and the
  trailing ``wait``. Restructured into an ``if/else``. Pinned by
  ``tests/test_boot_scripts_sourced.py``.
* **``Instance.register`` and ``snapshot.restore`` now stage files.**
  Both registered the instance but skipped writing ``.env`` /
  ``frida-server`` / ``modules/``. A follow-up ``beetroot up`` failed
  on the missing ``.env``. ``restore`` also now port-collision-checks
  against existing instances before extracting. Pinned by
  ``tests/test_instance_invariants.py``.
* **``snapshot._is_manifest_member`` requires an exact-path match.**
  Basename-only matching also picked up a stale ``data/.beetroot-snapshot.json``
  left over from a previous restore. ``_add_instance_tree`` now skips
  the on-disk manifest, and the basename-only matcher is replaced
  with an exact-path matcher against the archive root. Pinned by the
  new ``TestManifestShadowRegression`` cases in ``tests/test_snapshot.py``.
* **``cli.main()`` catches ``ComposeError`` and ``BootstrapError``.**
  ``up`` / ``down`` / ``restart`` / ``logs`` / ``apply`` / ``build``
  used to let domain exceptions propagate as bare tracebacks; v0.2
  was uniformly ``error: ...`` + exit 1. Pinned by
  ``tests/test_cli_error_contract.py``.
* **``beetroot setup`` and ``--preset`` print migration hints.** v0.2
  users running ``beetroot setup`` or ``beetroot create --preset``
  used to get bare Typer ``No such command`` / ``No such option``.
  Hidden alias + hidden option now print an ``error: ...`` line
  with the migration path. Pinned by ``tests/test_deprecated_verbs.py``.
* **``api_version: 1`` auto-bumps to ``2`` with a stderr warning.**
  v0.2 YAMLs used to hard-fail on load; v0.2 → v2 is strictly
  additive, so we auto-bump and tell the user to run
  ``beetroot apply`` to persist. Pinned by
  ``tests/test_api_version_auto_bump.py``.
* **v0.2 registry at ``$PWD/instances.json`` surfaces a one-line
  hint.** v0.2 wrote the registry at the repo root; v0.3 expects it
  under ``$XDG_CONFIG_HOME``. The hint fires once per process and
  doesn't auto-move the file (the user may have it under VCS).
  Pinned by ``tests/test_v02_registry_detection.py``.
* **Atomic port allocation + registration via ``registry.add_allocating``.**
  The old sequence ``lowest_free_index → resolve → check → add``
  only held the lock on ``add``; two parallel ``Instance.create``
  calls could co-allocate the same stride slot. The new helper
  collapses allocation + add into one critical section, with shared
  reader locks on ``list_instances`` and atomic-replace via per-pid
  tmp file in ``_write``. Pinned by ``tests/test_registry_race.py``
  (multiprocessing fork pool, 5 parallel creates).
* **``render_env`` emits ``BEETROOT_MAGISK_DB`` / ``BEETROOT_MODULES_DIR`` /
  ``BEETROOT_FRIDA_BIN`` with empty defaults.** The bundled compose
  template already references them via ``${VAR:-}`` for v0.4
  stealth-posture work; the test ``tests/test_compose_template_envs.py``
  asserts the template ↔ render_env contract stays symmetric.
* **``snapshot.restore --force`` refuses to wipe a peer instance's
  directory.** Without the check, a careless ``restore --force
  --path=<peer-dir>`` would ``shutil.rmtree`` a sibling instance's
  ``/data``. Pinned by the new ``test_force_refuses_to_overwrite_another_instances_dir``
  case in ``tests/test_snapshot.py``.

Total test-count delta: 469 → 522 (+53).

### v0.3 — Post-CR CI hardening

Guardrails added to ``.github/workflows/ci.yml`` and
``.pre-commit-config.yaml`` so the categories of regression the v0.3
CR pass surfaced never reach ``dev/v0.3`` again. Each guardrail
matches a specific CR finding:

* **Single Python 3.13 lane.** ``pyproject.toml`` pins
  ``requires-python = ">=3.13"``, but the test job still ran a
  3.10/3.11/3.12 matrix — dead cells that wasted runner minutes and
  could not surface real regressions. Collapsed to a single
  ``actions/setup-python@v5`` step on 3.13.
* **``mkdocs build --strict`` runs on every PR.** Today's
  ``docs.yml`` only built strict on push-to-master, so PR review
  never saw doc drift (T3-style ghost references, dead nav
  entries). New ``docs-strict`` job in ``ci.yml`` runs the same
  strict build; the gh-pages deploy job in ``docs.yml`` stays put.
* **``mypy tests/`` runs in CI.** CR #4 flagged that CI only ran
  mypy against ``src/beetroot/``, while CLAUDE.md requires both
  trees to type-check. The lint-and-type-check job now passes
  both paths.
* **``scripts/lint_changelog.py`` pre-commit + CI hook.** Closes
  the failure mode where T2's CHANGELOG entry cited
  ``beetroot create alpha --preset with-frida`` — but T3 had
  already removed ``--preset`` earlier in the same Unreleased
  block. The linter parses shell fences under ``## Unreleased``,
  pulls every ``beetroot <verb>`` invocation, and validates verb +
  long-flag names against ``beetroot --help`` / ``beetroot <verb>
  --help``. It does NOT execute the cited commands. Wired as a
  ``CHANGELOG.md``-scoped pre-commit hook (``stages: [pre-commit,
  manual]``) and as a step in the CI lint job.

### v0.3 — Post-CR-CR fix-up

Three post-fix CRs ran against the previous fix-up state and surfaced
findings the original sweep missed (plus a small number of new bugs
the fix-ups themselves introduced). The must-fix items are addressed
below; lower-severity items deferred to v0.3.1. Each fix is paired
with at least one behavior test that asserts on the artifact.

* **``modules_dl.ModuleFetchError`` type.** A 404 (or any HTTP error)
  on a module download used to raise a bare ``RuntimeError`` that
  ``cli.main()`` didn't catch, so v0.2 users running the recommended
  migration recipe (``cp examples/stealth.yaml alpha/beetroot.yaml &&
  beetroot apply alpha``) saw a Rich-rendered traceback when the
  hard-coded Shamiko URL 404'd. New ``ModuleFetchError`` (subclass of
  ``RuntimeError`` for backward compat) is caught in ``cli.main``.
  Pinned by ``tests/test_module_fetch_error.py``.
* **Module URL scheme allowlist.** ``_fetch_url`` accepted any URL
  scheme; ``url: file:///etc/passwd`` in a ``beetroot.yaml`` would
  silently exfiltrate that host file into the module cache. The
  ``Module`` pydantic validator now rejects non-http(s) schemes at
  parse time and ``_fetch_url`` re-checks the prefix. Pinned by
  ``tests/test_module_url_scheme.py``.
* **``examples/stealth.yaml`` no longer ships a hard-coded Shamiko
  URL.** The upstream release URL changes whenever LSPosed cuts a
  new tag, so the example went stale silently. The ``modules:``
  block is commented out with a pointer to the LSPosed releases
  page. ``docs/guides/examples.md`` is updated to match.
* **Orphan-aware ``beetroot ls``.** A registry entry whose on-disk
  directory was ``rm -rf``'d behind the CLI's back used to crash
  ``ls`` with a Rich-rendered ``FileNotFoundError``. ``Manager.list()``
  now skips orphans; ``Manager.list_orphans()`` surfaces them; the
  CLI ``ls`` verb appends a trailing skip-line ``(skipping N orphan
  entries: <names>; clean up with 'beetroot destroy <name> -y')``.
  ``cli.destroy`` no longer calls ``compose.down`` on an orphan
  (would FileNotFoundError on the subprocess ``cwd``). ``cli.main``
  catches bare ``FileNotFoundError`` and ``ModuleFetchError`` as
  belt-and-suspenders. Pinned by ``tests/test_orphan_registry.py``.
* **``cli.shell`` and ``cli.frida`` propagate subprocess exit codes.**
  ``Instance.shell()`` and ``Instance.frida_cli(args)`` both return
  the subprocess exit code as ``int``; the verbs used to discard it.
  Research scripts that check ``$?`` after ``beetroot shell <name>
  -c '<cmd>'`` always saw 0. Now ``raise typer.Exit(code=rc)`` when
  non-zero. Pinned by
  ``tests/test_cli.py::TestCmdShell::test_shell_propagates_non_zero_exit_code``
  and the matching ``TestCmdFrida`` case.
* **Hidden ``up --build`` deprecation hint.** T5 removed
  ``--build`` from ``up``; v0.2 users running ``beetroot up alpha
  --build`` got a Rich "No such option: --build" box instead of the
  friendly migration line the other deprecations print. A hidden
  ``--build`` flag now prints
  ``error: 'beetroot up --build' was removed in v0.3 — run 'beetroot
  build' separately first to rebuild the image.``. Pinned by the
  existing
  ``tests/test_cli.py::TestCmdUp::test_up_rejects_build_flag``,
  now asserting on the hint string.
* **Partial-failure rollback in three constructors.**
  ``Instance.create`` / ``Instance.register`` / ``snapshot.restore``
  all wrote on-disk state BEFORE the port-collision check + ``_stage``.
  A failure mid-stream left the registry row, the directory, and the
  YAML behind. Each constructor now wraps the failure-prone steps in
  try/except, removes the registry row on rollback, and ``rmtree``s
  the target dir only when Beetroot created it in that call (tracked
  via a local ``created_dir`` flag — adopted dirs like ``register``'s
  user-supplied path and the CLI's ``--from-data``-prepopulated dir
  are preserved). Pinned by ``tests/test_partial_failure_rollback.py``.
* **``api_version: 1`` auto-bump warning deduplicates per path.**
  The warning used to fire on every load; over 5 v0.2 instances
  ``beetroot ls`` printed 5+ copies, and ``register bravo``
  triple-printed because ``all_resolved_ports`` cascaded into the
  same load twice. Module-level ``set[Path]`` records which paths
  we've already warned about in this process; first load prints,
  subsequent loads skip. Pinned by ``tests/test_api_version_auto_bump.py``
  (``test_auto_bump_warning_deduplicates_per_path`` +
  ``test_auto_bump_warning_fires_per_distinct_path``).
* **``cli.restore`` drops the stale apply-then-up hint.** Agent A
  made ``snapshot.restore`` call ``_stage()`` itself, so
  ``beetroot apply`` is no longer needed before ``beetroot up``;
  the printed ``next:`` line is updated to match. Pinned by the
  existing ``TestCmdRestore::test_restore_round_trips_through_cli``,
  now asserting on the post-restore hint substring.
* **``pyproject.toml`` classifiers trimmed.** Listed
  ``Python :: 3.10/3.11/3.12/3.13`` but ``requires-python =
  ">=3.13"``; the stale rows were misleading for PyPI search.
  Down to ``Python :: 3`` + ``Python :: 3.13``. Pinned by
  ``tests/test_packaging.py::test_python_version_classifiers_match_requires_python``.
* **Migration guide fixes (invented flags, auto-rename text,
  status example).** ``docs/guides/migration-v0.2-to-v0.3.md``
  recommended ``beetroot destroy --no-rm <name>`` — a flag that
  doesn't exist. It also misrepresented the auto-rename behaviour
  (the XDG-path migration auto-renames; the ``$PWD`` v0.2 detection
  just prints a stderr hint) and showed ``STATUS = exited`` in a
  context where ``not-created`` is the correct value. All three are
  corrected; ``tests/test_doc_grep.py`` is tightened with anchored
  regex rules for ``destroy --no-rm`` / ``--no-data`` /
  ``--no-deregister`` so the same class of invented flag can't
  reappear, plus a dedicated
  ``test_migration_guide_does_not_invent_destroy_flags`` that
  greps the guide specifically.
