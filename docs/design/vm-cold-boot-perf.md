# Cold-boot performance of the `binder: vm` TCG path (issue #83)

!!! success "Status: investigation complete — measured on the canonical no-KVM sandbox (2026-06-20)"
    This is the report issue [#83](https://github.com/Xiddoc/Beetroot/issues/83)
    asked for: real cold-boot numbers for the `binder: vm` micro-VM under pure
    TCG, with vs. without the proposed entropy levers, plus a **validated**
    snapshot/restore warm-start workflow. All measurements were taken
    end-to-end on the Claude-Code-on-the-web sandbox class the issue targets —
    4 physical cores, 15 GiB RAM, **no `/dev/kvm`**, host kernel without binder
    (`beetroot modes` → `binder: vm, TCG accel: needs-setup`) — using the
    committed builder (`beetroot build --vm-kernel`, redroid Android **14**,
    guest kernel 6.12.9) and the committed `build_qemu_argv` invocation.

## TL;DR

| Lever | Effect on cold boot | Ship it? |
| --- | --- | --- |
| `random.trust_cpu=on` kernel cmdline | **none** — the guest CRNG already seeds at **~0.15 s** | no (no-op) |
| `-device virtio-rng-pci` (+ `CONFIG_HW_RANDOM_VIRTIO=y`) | **none** — within run-to-run noise | no (for boot speed) |
| **QEMU `savevm`/`loadvm` warm-start** | **~200 s → ~10 s (≈20×)** | **yes — the real win** (issue #49) |
| Android version `14 → 11` | **~165 s → ~98 s guest (≈40% faster)** | a user dial, documented |

**Bottom line.** The entropy-stall hypothesis behind the two RNG levers does
**not** hold under TCG — neither lever moves the needle. The cold boot is
dominated by emulated ART / Zygote / `system_server` CPU work, which only two
things meaningfully cut: **not cold-booting at all** (the QEMU `savevm`
warm-start) and **a lighter Android** (version 11 vs 14). The QEMU savevm path
is the headline recommendation and is specced for productization in
[vm-savevm-cache.md](vm-savevm-cache.md) (#49).

## Method

* Each arm boots the guest under the exact committed TCG invocation
  (`-M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max -smp 4 -m 8192`,
  cmdline `… mitigations=off`), measuring **host wall time from QEMU launch to
  the host's first real `adb` interaction** — `adb connect localhost:<port>`
  then `adb shell getprop sys.boot_completed == 1`. This is the metric the
  issue cares about: "from this moment to first interaction with the booted
  instance."
* The root disk is opened `snapshot=on` (a discarded COW overlay) so **every
  run is a true cold boot from the baked image** — no state leaks between runs,
  no `e2fsck` needed.
* Arms are **interleaved** (baseline, treatment, baseline, …) to cancel the
  monotonic host-contention drift the [vm-rnd-log §D.4](vm-rnd-log.md) noted.
* The guest serial console (`-serial file:`) is parsed for the kernel
  `random: crng init done` timestamp and the guest-init `boot_seconds` marker.

## 1. Entropy levers (the issue's primary question)

### 1.1 `random.trust_cpu=on` is a no-op

Every boot's kernel log shows the CRNG fully seeded almost immediately:

```
[    0.15] random: crng init done
```

On x86-64 the guest kernel already trusts the CPU RNG (`CONFIG_RANDOM_TRUST_CPU`
defaults `y`) and `-cpu max` exposes `RDRAND`, so the CRNG is seeded from the
CPU at ~0.15 s **without** the cmdline flag. Adding `random.trust_cpu=on`
changes nothing — the kernel was never entropy-starved at boot. (Measured CRNG
init across runs: 0.15–0.17 s, with and without the flag.)

### 1.2 `-device virtio-rng-pci` needs a kernel driver — and still does not help boot

A subtler finding. The guest **does** log one entropy-related stall — but it is
**Android's** `prng_seeder`, not the kernel:

```
[   15.5] prng_seeder: Hanging forever because setup failed: Unable to open hwrng /dev/hw_random
```

That looks like exactly the kind of thing `-device virtio-rng-pci` should fix.
It does not, for two compounding reasons:

1. **The prebuilt guest kernel has `CONFIG_HW_RANDOM_VIRTIO` unset.** With no
   virtio-rng *driver*, attaching the device is inert: QEMU instantiates it
   (confirmed in `info qtree`: `dev: virtio-rng-pci`), but the guest never
   binds it and `/dev/hwrng` never appears. So the issue's lever **as written**
   (argv-only) cannot work — it needs a kernel rebuild first.
2. **Even with a rebuilt kernel (`CONFIG_HW_RANDOM_VIRTIO=y`) the boot time is
   unchanged**, because the `prng_seeder` hang is **off the boot critical
   path**. It runs *inside the redroid container* (whose `/dev` is Android's,
   not the guest's, so the host hwrng would still need passing through), and
   the container reaches `sys.boot_completed=1` at the same time whether or not
   `prng_seeder` is happy.

**Measured (3 interleaved runs each, Android 14, pure TCG):**

| arm | host-wall mean | guest `boot_seconds` mean | CRNG init | `prng_seeder` hang |
| --- | --- | --- | --- | --- |
| baseline (committed argv) | **193.7 s** | 165.0 s | ~0.16 s | yes |
| rebuilt kernel `HW_RANDOM_VIRTIO=y` + `virtio-rng-pci` | **193.7 s** | 163.3 s | ~0.15 s | yes¹ |

¹ Still hangs: the rebuilt kernel gains the driver, but the container’s `/dev`
does not expose `/dev/hw_random`, so Android’s seeder is unchanged. Closing it
would mean plumbing the hwrng into the redroid container — a hygiene fix, not a
boot-time one.

The two arms are identical within noise (per-arm spread alone is ±3–6 s).
**Conclusion: do not add the RNG levers for boot speed.** They are documented
here so the negative result is not re-investigated.

## 2. The real warm-start: QEMU `savevm` / `loadvm`

Booting redroid is ~200 s of emulated CPU work; *resuming a checkpoint* of an
already-booted machine skips all of it. Validated end-to-end on the same host
with a qcow2 overlay over the raw rootfs, a unix monitor socket, HMP
`savevm <tag>` after `boot_completed`, and a `-loadvm <tag>` relaunch:

| phase | wall time |
| --- | --- |
| cold boot (qcow2 overlay) to first `adb` interaction | **200.3 s** |
| `savevm booted` checkpoint (RAM + disk → qcow2) | **6.6 s** |
| **warm restore (`-loadvm booted`) to first `adb` interaction** | **~9–10 s** (10.3 s, then 8.9 s on a repeat) |

Snapshot artifact: the qcow2 grew by ~0.6 GB of RAM image during `savevm`
(~2.2 GB total including COW disk deltas) — compressible, and within reach of a
CI cache budget if keyed per guest revision.

**The restored device is genuinely usable, not just "boot_completed":**

```console
$ adb -s localhost:<port> shell getprop ro.build.version.release   # 14
$ adb -s localhost:<port> shell getprop ro.product.cpu.abi          # x86_64
$ adb -s localhost:<port> shell cat /proc/uptime                    # 211.27 …  (carried over from the checkpoint — proof it is the restored RAM state, not a fresh boot)
$ adb -s localhost:<port> shell id                                  # uid=2000(shell) … live interactive shell
```

This is a **~20× cold-start reduction** (200 s → ~10 s) and is the single
biggest lever found.

### 2.1 This is *not* `beetroot snapshot` / `restore`

A clarification the issue invites. Beetroot's existing
[`beetroot snapshot` / `restore`](../guides/snapshots.md) packs the host-side
instance directory (`/data`, `modules/`, `beetroot.yaml`) as a `.tar.zst`.
Restoring it still requires a **full cold Android boot**, and it is redroid-only
(it skips `vm` registry rows by design). It is **orthogonal** to the QEMU
running-RAM checkpoint described here — it does not, and is not meant to, skip
the boot.

### 2.2 Manual warm-start recipe (usable today)

The `VmDeviceBackend` launches a **raw** disk with no monitor, so the
warm-start is not yet a `beetroot` verb (that is issue #49's follow-up — design
in [vm-savevm-cache.md](vm-savevm-cache.md), now with the measurements above).
Until then, the validated recipe is:

```sh
# one-time: a qcow2 overlay over the baked raw rootfs
qemu-img create -f qcow2 -b ~/.cache/beetroot/vm/rootdisk.img -F raw booted.qcow2

# cold-boot once, with a monitor socket, then checkpoint after boot_completed:
qemu-system-x86_64 -M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max -smp 4 -m 8192 \
  -nographic -display none -no-reboot \
  -kernel ~/.cache/beetroot/vm/bzImage \
  -drive file=booted.qcow2,format=qcow2,if=virtio \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:5555-:5555 -device virtio-net-pci,netdev=net0 \
  -monitor unix:mon.sock,server,nowait \
  -append "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off"
# … wait for `adb connect localhost:5555 && adb shell getprop sys.boot_completed` == 1, then over mon.sock:
#   (qemu) stop
#   (qemu) savevm booted
#   (qemu) quit

# thereafter, resume the booted machine in ~10 s:
qemu-system-x86_64 … (same line) … -loadvm booted
```

Cache safety is already handled by `scripts/vm_cache_key.py` (#49) — key the
saved overlay on the `bzImage` + `rootdisk.img` so a snapshot is never resumed
against a guest it was not taken on.

## 3. Android version is a cold-boot dial

The committed benchmark baseline (`benchmarks/baseline.json`; redroid
**11.0.0**, same sandbox class, kernel 6.12.9) is **~98–101 s** guest
`boot_seconds`. The default is now redroid **14** (#82), measured here at
**~165 s** guest (~194 s host-wall) — i.e. the version bump is a **~40 % / ~67 s
per-boot TCG tax**. Researchers who do not need Android 14 APIs and want faster
TCG iteration can set `android.version: 11` in `beetroot.yaml` (then rebuild the
guest: `beetroot build --vm-kernel --android-version 11`). This is a tradeoff
note, not a default change — 14 stays the default.

## 4. Incidental finding: intermittent QEMU TCG SIGSEGV

Across ~13 boots, one QEMU process died with `SIGSEGV` (`segfault … error 4`,
not OOM — 11 GiB was free) during boot. This is a known class of intermittent
`qemu-system-x86_64` TCG crashes with `-cpu max` + MTTCG, unrelated to
Beetroot. Today `VmDeviceBackend.up()` would wait the full
`vm_adb_connect_timeout` and then report a generic timeout. A small robustness
nicety (out of scope for this perf report, noted for a follow-up): have `up()`
detect the QEMU process exiting and fail fast / retry rather than blocking on
the adb deadline.

## Recommendations

1. **Do not** add `random.trust_cpu=on` or `-device virtio-rng-pci` to
   `build_qemu_argv` for boot speed — measured neutral (this report).
2. **Prioritize the QEMU `savevm` warm-start** (#49 / [vm-savevm-cache.md](vm-savevm-cache.md)):
   it is the ~20× lever, validated here. The cold-boot benchmark lane must keep
   measuring the *cold* path (never the cache) per that doc's correctness caveat.
3. **Document `android.version: 11`** as the faster-iteration option under TCG
   (done — see [Running in CI](../guides/running-in-ci.md) and `examples/vm.yaml`).
