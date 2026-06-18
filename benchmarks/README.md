# Benchmark baselines

This directory holds the committed performance baseline the nightly
[`benchmark.yml`](../.github/workflows/benchmark.yml) workflow trends new runs
against (issue #50). Benchmarking **tracks, it does not gate** — a regression
only raises a GitHub `::warning::` annotation in the nightly job; it never fails
a PR.

## `baseline.json`

The reference wall-times, in the same schema `scripts/bench.py` reads and
writes:

```json
{ "samples": [ { "backend": "...", "metric": "...", "seconds": 0.0 } ] }
```

* `backend` — `host` (host-binder redroid), `vm-tcg` / `vm-kvm` (the QEMU
  micro-VM backend under TCG / KVM), or `build` (the one-time kernel/rootfs
  compile).
* `metric` — `boot_seconds`, `postboot_seconds`, or `compile_seconds`.

The seeded values come from the offline R&D in
[`docs/design/vm-rnd-log.md`](../docs/design/vm-rnd-log.md) Stage A (pure TCG,
no `/dev/kvm`): a ~450 s kernel build and a ~100 s TCG boot. There is no `host`
baseline yet — the first green nightly captures it.

## Refreshing the baseline

The nightly run uploads a `bench-results` artifact containing the run's
`samples.json`. To re-baseline after an intentional change (a new kernel
config, a QEMU bump), download that artifact and copy its `samples.json` over
`baseline.json` in a reviewed PR. Keep the file small — it is the *reference*,
not a history (the trend lives in the per-run artifacts).

## Coverage per binder mode

The harness *schema* spans every mode — `host` (host-binder redroid),
`vm-tcg` / `vm-kvm` (the QEMU micro-VM under TCG / KVM), and `build` (the
one-time kernel/rootfs compile). What can actually be *measured* depends on
the host (run `beetroot modes`):

| Mode | Where it runs | Measured? |
| --- | --- | --- |
| `host` (binder host/auto) | GitHub `ubuntu-latest` (binder loadable, rank 2) | nightly lane |
| `vm-kvm` | a `/dev/kvm`-capable runner | not on hosted runners (no KVM) |
| `vm-tcg` | anywhere QEMU runs (binderless, KVM-less OK) | nightly lane + the run below |
| `build` (kernel compile) | any kernel build host | nightly lane + the run below |

The `host`-vs-`vm` *ratio* (the headline number) needs a `host` sample, so it
only renders where the host binder path is reachable — **not** in a binderless
sandbox, where `host`/`vm-kvm` are `unsupported` and only `vm-tcg` + `build`
produce samples.

## Verified `vm-tcg` run (2026-06-17, binderless sandbox)

The full `vm-tcg` path was driven end-to-end *through Beetroot's own builder
and `VmDeviceBackend`* (not the offline hand-run scripts that seeded the
baseline) on the Claude Code on the web sandbox — 4 cores, 15 GiB, **no
`/dev/kvm`**, pure TCG; guest kernel 6.12.9, redroid 11.0.0:

| backend | metric | seconds | baseline | note |
| --- | --- | --- | --- | --- |
| `build` | `compile_seconds` | 541 | 450 | 1.2× (slower sandbox CPU); under the 2× alert |
| `vm-tcg` | `boot_seconds` | 98 | 100 | guest-measured (`docker run` → `boot_completed`), matches baseline |
| `vm-tcg` | `postboot_seconds` | 1 | — | `pm list packages` once booted |

`host` is `unsupported` here, so there is no `host` row and no ratio — expected
in a binderless environment.

### Kernel config trim (2026-06-17)

Off the back of the run above, the guest `kernel.config` fragment now disables
physical-hardware driver classes a QEMU `q35`+virtio guest can never bind
(`DRM_I915`, `ETHERNET`, `WLAN`, `ATA`, `SCSI_LOWLEVEL`). Measured on the same
sandbox:

| | full defconfig | trimmed | delta |
| --- | --- | --- | --- |
| `compile_seconds` | 541 | 418 | **−23%** |
| bzImage | 14 MiB | 12 MiB | **−15%** |
| built-in objects | 3013 | 2687 | −326 |
| `boot_seconds` (guest) | 98 | 101 | within TCG noise |

The win is real but bounded: `make bzImage` only compiles built-in (`=y`)
code, and defconfig already ships most vendor NIC/Wi-Fi drivers as *modules*
(`=m`) that the module-less guest never builds — so the dominant in-tree
phantom driver was `DRM_I915`. **Validated post-trim under TCG:** redroid
boots to `sys.boot_completed=1`, `screencap` returns a real 720×1280 frame
(software composition — no kernel GPU driver involved), and AudioFlinger is up.
Sound, DRM core + `virtio-gpu`, and the generic graphics infra
(`dma-buf`/`sync_file`/`memfd`) are kept on purpose (see the fragment header).

### Compile-speed levers (2026-06-17)

Three levers were evaluated for faster builds. Results on the same sandbox:

| lever | outcome | kept? |
| --- | --- | --- |
| **ccache** (`CC="ccache gcc"` in `build_vm_kernel`) | cold build ~unchanged; **warm rebuild of unchanged source 54 s @ 99.8% cache hits** (vs ~7–9 min) | **yes** — the real win for CI build lanes / local iteration |
| `CONFIG_MODULES=n` | small build/image trim; module-less guest already builds everything `=y` | **yes** — no downside |
| `-Os` (`CC_OPTIMIZE_FOR_SIZE`) | bzImage 12→10 MiB, but **boot ~doubled under TCG: guest-measured 98→201 s** | **no** — a CPU-bound redroid boot punishes `-Os`; not worth 2 MiB |

Takeaways: there is no `-O0` for the kernel, and the expensive cold-compile
features (DWARF debug info, BTF, KASAN/UBSAN/GCOV) are already off in
defconfig — so the cold compile has little fat left. **ccache is the lever that
matters** (it makes re-compiles near-free), and it's gated off in the benchmark
lane (`CCACHE_DISABLE=1`) so that lane still measures a true cold compile.

**The cold compile, however, can't be made cheap — so the real fix for a fresh
host is to not compile at all.** ccache lives in CI / on a warm host; a brand-new
CI runner or Claude Code on the web sandbox is always cold and pays the full
~7 min. So `beetroot build --vm-kernel` now **fetches a prebuilt bzImage**
(~12 MiB, seconds) from the repo's `vm-kernel` GitHub release by default,
matched to the kernel version + a fingerprint of `src/beetroot/templates/vm/kernel.config` and
sha256-verified, falling back to a source compile on any mismatch. That turns
the dominant cost from ~7 min to a few seconds for the common case. (The 2.4 GB
rootfs stays locally assembled — it's over GitHub's 2 GB asset limit and pulls
redroid on the user's machine; it's also not the long pole.)

Two notes from the run, both now fixed (see `CHANGELOG.md` → Bug fixes):

* The boot was initially blocked by a busybox **self-symlink `-ELOOP`** in the
  rootfs builder — the guest panicked exec'ing `/init`. The number above is
  *post-fix*.
* `boot_seconds` is reported **guest-measured** (consistent with the baseline's
  methodology). The host-wall figure (`beetroot up` → host `adb` sees
  `boot_completed`) read higher (~159 s) because `up`'s `adb connect` lands on
  the QEMU SLIRP forward *before* the in-guest relay is up, leaving an offline
  adb transport that recovers only after a reconnect — a host-side detection
  lag, not boot time. The nightly lane times host-wall via its own `date`
  delta and is subject to the same lag; trend the guest-measured number for an
  apples-to-apples comparison with the seeded baseline.
