# Cold-boot performance of the `binder: vm` TCG path (issue #83)

!!! success "Status: measured 2026-06-20 — RNG levers debunked, savevm warm-start validated"
    This is the report issue #83 asked for: real, reproduced numbers for the
    two proposed cold-boot levers (a `virtio-rng-pci` device + a
    `random.trust_cpu=on` kernel arg), plus a **validated, copy-pasteable
    snapshot/restore warm-start workflow** that takes a booted micro-VM from a
    ~123 s cold boot to a **~6.6 s** warm restore. Measured on the
    binderless, KVM-less Claude Code on the web sandbox — the exact host class
    this issue targets.

## TL;DR

| Path | Wall time to `adb sys.boot_completed=1` | vs cold |
| --- | --- | --- |
| **Cold boot** (current default) | **123 s** | 1.0× |
| `virtio-rng-pci` + `random.trust_cpu=on` | 123 s (within noise) | **no change** |
| **Warm restore** from a QEMU `savevm` checkpoint | **6.6 s** | **~19× faster** |

* **The RNG levers do nothing here** — and the root cause is conclusive, not
  statistical: the guest CRNG is already seeded ~0.15 s into boot, ~100 s
  *before* anything blocks on it, and the guest kernel doesn't even carry the
  `virtio-rng` driver, so the device is inert. Details in
  [§1](#1-the-rng-levers-virtio-rng-randomtrust_cpu).
* **The real win is snapshot/restore.** Boot once, `savevm` the running
  machine, and every subsequent start is a `-loadvm` that resumes the
  already-booted Android in **~6.6 s**. This is the "blazingly quick"
  warm-start path for repeated Claude Cloud sessions. Recipe in
  [§2](#2-the-real-win-savevmloadvm-warm-start).

## Environment & method

* **Host:** Claude Code on the web sandbox — 4 physical cores, 15 GiB RAM,
  **no `/dev/kvm`**, host kernel without `binder` (`beetroot modes` →
  `binder: vm, TCG accel: needs-setup`). All numbers are **pure TCG software
  emulation**, the rank-4 worst case.
* **QEMU:** 8.2.2. **Guest kernel:** the pinned 6.12.9 prebuilt `bzImage`
  fetched by `beetroot build --vm-kernel` (~12 MiB, seconds — no compile).
* **Guest:** redroid **Android 11** baked into the rootfs
  (`build --vm-kernel --android-version 11`). Android 11 is the
  [vm-rnd-log §B.5](vm-rnd-log.md) ~100 s baseline and keeps each A/B boot
  short enough to run many of them; the entropy and snapshot mechanisms under
  test are Android-version-independent.
* **Metric:** wall time from `qemu-system-x86_64` launch to the host's
  `adb -s localhost:<port> shell getprop sys.boot_completed` returning `1` —
  i.e. *from this moment to first interaction with the booted instance*,
  exactly the quantity issue #83 cares about.
* **Cold-boot hygiene:** every cold run boots from a **fresh qcow2 overlay**
  over the pristine raw rootfs (`qemu-img create -f qcow2 -b rootdisk.img -F
  raw overlay.qcow2`), discarded after the run, so no run is warmed by a
  prior one. Variants are interleaved to cancel host-contention drift, the
  same discipline as the [vm-rnd-log §D.4](vm-rnd-log.md) `mitigations=off`
  A/B.

## 1. The RNG levers (`virtio-rng`, `random.trust_cpu`)

### The hypothesis (issue #83)

> The QEMU launcher … appends `… panic=1 mitigations=off` — notably no RNG
> device and no `random.trust_cpu=on`, so Android init may stall on entropy
> (amplified under TCG).

A reasonable worry — early-userspace `getrandom()` (Android `init`,
`zygote`/ART, `adbd` key setup) *blocks* until the kernel CRNG is seeded, and
under TCG the emulated interrupt jitter that normally fills the entropy pool is
sparse and slow. If that were the bottleneck, a `virtio-rng-pci` device
(host-`/dev/urandom`-backed) and/or crediting `RDRAND` via `random.trust_cpu=on`
would unblock it.

### The measurement: it doesn't move boot time

A/B, interleaved, fresh overlay per run, pure TCG:

| Arm | boot → `adb` (s) |
| --- | --- |
| baseline (current default cmdline) | **123.3** |
| `-device virtio-rng-pci` + `random.trust_cpu=on` | **121.2** |
| baseline — independent cold boot #2 (savevm §2 cold) | 123.4 |
| baseline — independent cold boot #3 (smoke) | 123.6 |

The "optimized" arm (121.2 s) is, if anything, *faster* than the three
independent baseline cold boots (123.3 / 123.4 / 123.6 s) — i.e. the
difference is host-contention noise, not a treatment effect. (The interleaved
harness completed one clean A/B pair before its host-side `adb` reconnect loop
deadlocked on a stale `offline` device entry — a measurement-tool bug, not a
guest fault; the three tightly-clustered baseline cold boots from separate runs
bound the noise band well enough to read the single optimized sample against
it, and the mechanism below makes the outcome certain regardless.)

### Why — entropy is not on the critical path (conclusive, not just statistical)

Three independent probes from inside the running guest explain *why* the levers
can't help, so this isn't a "didn't measure a difference" hand-wave:

1. **The CRNG is already seeded ~0.15 s into boot.** The guest serial log's
   very first entropy line is:

    ```
    [    0.154604] random: crng init done
    ```

    That is **~123 s before `sys.boot_completed`**. By the time *any* userspace
    `getrandom()` runs, the pool has long been ready — there is nothing to
    unblock. The x86_64 `make defconfig` base (which the guest kernel is built
    from) ships `CONFIG_RANDOM_TRUST_CPU=y`, and `-cpu max` exposes `RDRAND`,
    so the kernel credits the CPU RNG and seeds the CRNG at init **by default**.
    `random.trust_cpu=on` only flips that default *on* — against a kernel where
    it is already on, it is a **no-op**.

2. **The guest can't use a `virtio-rng` device anyway.** Inside the booted
   guest:

    ```
    $ cat /sys/devices/virtual/misc/hw_random/rng_current
    none
    ```

    The pinned 6.12.9 `kernel.config` does not build in the `virtio-rng`
    driver (`CONFIG_HW_RANDOM_VIRTIO`), so `-device virtio-rng-pci` attaches a
    device **no guest driver binds to** — it is inert. Making it live would
    require editing `kernel.config`, which changes the config fingerprint and
    so **forces the ~7-min from-source kernel compile** (the prebuilt fetch
    would no longer match) — a real setup-time regression for **zero** boot
    benefit, since the CRNG is already seeded at 0.15 s.

3. **Steady-state entropy is healthy.** `cat
   /proc/sys/kernel/random/entropy_avail` → `256` (a fully-initialised pool;
   modern kernels cap the estimate here once `crng init done`).

### Verdict

**Neither lever is adopted.** The honest record (mirroring the
[vm-rnd-log §D.4](vm-rnd-log.md) `mitigations=off` finding) is that entropy is
not the TCG cold-boot bottleneck — **CPU-bound emulation of the Android
userspace boot is** — and shipping an inert `virtio-rng-pci` device plus a
no-op kernel arg would be cargo-cult. `build_qemu_argv` is left unchanged; a
docstring note records the dead end so it isn't re-proposed.

## 2. The real win: `savevm`/`loadvm` warm-start

If the bottleneck is *emulating the Android boot*, the way to be "blazingly
quick" is to **not boot Android twice**. Boot once, checkpoint the running
machine (RAM + devices + disk) with QEMU's `savevm`, and resume it with
`-loadvm` on every subsequent start.

### Measured (same host, pure TCG, Android 11)

| Step | Time | Notes |
| --- | --- | --- |
| Cold boot → `adb` | **123.4 s** | the baseline being avoided |
| `savevm beetroot-warm` (once, post-boot) | **14.4 s** | writes a **1.91 GiB** RAM image into the qcow2 |
| **Warm restore → `adb`** | **6.6 s** (6.6, 6.6 — two runs) | `-loadvm`, resumes the already-booted Android |
| **Speedup** | **~18.8×** | and rock-steady (±0 s across runs) |

The 6.6 s includes the host `adb connect`/reconnect handshake to the freshly
launched QEMU; the RAM restore itself is a few seconds of sequential qcow2
read. The checkpointed overlay is ~2.0 GiB on disk (the 1.91 GiB RAM snapshot +
the boot's disk deltas), well within a CI cache budget if keyed per guest
revision.

### The workflow (validated, copy-pasteable)

A qcow2 overlay over the raw rootfs is what makes internal `savevm` snapshots
possible (a raw disk can't hold them), and it doubles as the cold-boot-hygiene
overlay. The `savevm`/`loadvm` commands are issued over a QMP monitor socket.

```sh
CACHE=~/.cache/beetroot/vm
PORT=5599

# 0. Build the guest once (fetches the prebuilt kernel; bakes the rootfs).
beetroot build --vm-kernel            # add --android-version N to match your config

# 1. A qcow2 overlay over the pristine raw rootfs — snapshots live in here.
qemu-img create -f qcow2 -b "$CACHE/rootdisk.img" -F raw "$CACHE/warm.qcow2"

QEMU_COMMON="-M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max -smp 4 -m 8192 \
  -nographic -display none -no-reboot \
  -qmp unix:/tmp/bt-qmp.sock,server,nowait \
  -kernel $CACHE/bzImage \
  -drive file=$CACHE/warm.qcow2,format=qcow2,if=virtio \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:$PORT-:5555 \
  -device virtio-net-pci,netdev=net0 \
  -append 'console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off'"

# 2. COLD boot once, wait for the device, then checkpoint over QMP.
qemu-system-x86_64 $QEMU_COMMON &
until adb connect localhost:$PORT && \
      [ "$(adb -s localhost:$PORT shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do
  sleep 2
done
#   issue `savevm beetroot-warm` via QMP human-monitor-command, then quit QEMU:
printf '%s\n' \
  '{"execute":"qmp_capabilities"}' \
  '{"execute":"human-monitor-command","arguments":{"command-line":"savevm beetroot-warm"}}' \
  '{"execute":"quit"}' | socat - UNIX-CONNECT:/tmp/bt-qmp.sock

# 3. WARM restore — every subsequent start resumes the booted Android in ~6.6 s.
qemu-system-x86_64 $QEMU_COMMON -loadvm beetroot-warm &
adb connect localhost:$PORT        # device is ready in seconds
```

### Correctness caveats (carried from [vm-savevm-cache.md](vm-savevm-cache.md))

* **Never use a warm restore where you mean to measure a *cold* boot.** The
  nightly cold-boot benchmark and any first-boot validation must stay on the
  cold path; the snapshot is for *usability*, not for the metric.
* **Key the snapshot by the artifacts that produced it.** A snapshot resumed
  against a different `bzImage`/`rootdisk.img` than it was taken on is worse
  than a cold boot. `scripts/vm_cache_key.py` already computes that key (folds
  basenames + SHA-256 of the kernel + rootfs); a warm-start cache **must**
  invalidate on it.
* **The overlay is per-instance, mutable state.** Once you `-loadvm`, the
  qcow2 overlay carries that instance's `/data`. To re-arm a clean warm start,
  re-create the overlay from the raw image and re-snapshot.

### How this plugs into the backend (shipped — `vm.snapshot`, #49)

The validated recipe above is now a first-class, opt-in `beetroot` feature.
Setting `vm.snapshot: true` in `beetroot.yaml` makes `build_qemu_argv` launch a
**per-instance qcow2 overlay** with a **QMP monitor socket**, and the backend
drives the lifecycle automatically: the first `beetroot up` cold-boots and
checkpoints the running machine (`savevm` over QMP) once the guest is
adb-reachable; every subsequent `up`/`restart` launches with **`-loadvm`** and
resumes the booted Android in seconds. The checkpoint is gated by a
kernel+rootfs fingerprint (the safety latch below), and `beetroot up --fresh`
re-baselines by discarding the overlay and re-checkpointing. See
[Config reference § `vm.snapshot`](../reference/config.md#vmsnapshot-the-warm-start-cache).
This report was the "validate the warm-start path" half of #83's acceptance;
the `vm.snapshot` feature is the productionisation (#49).

## 3. Other levers surveyed

| Lever | Verdict | Evidence |
| --- | --- | --- |
| **More vCPUs** (`-smp 8`) | **Rejected** — regresses | [vm-rnd-log §B.5](vm-rnd-log.md): `-smp 8` is slower than `-smp 4` on a 4-core host (oversubscription → MTTCG cross-thread sync). `smp: auto` already pins the physical-core optimum. |
| **MTTCG** (`thread=multi`) | **Already on** | The single biggest TCG lever; shipped in `build_qemu_argv`. |
| **`mitigations=off`** | **Already on; boot-neutral under TCG** | [vm-rnd-log §D.4](vm-rnd-log.md) — kept (may help KVM) but not a TCG boot lever. |
| **`virtio-rng` / `random.trust_cpu`** | **Rejected** — inert / no-op | §1 above. |
| **KVM accel** (`-cpu host`) | **Not available here** | No `/dev/kvm` in the sandbox; `accel: auto` already prefers it where present and would approach native boot. |
| **`savevm`/`loadvm` warm-start** | **★ Adopt** | §2 — ~19× faster, the headline finding. |

## Recommendation

1. **Don't chase the RNG levers** — entropy is not on the critical path; record
   the dead end (done, in the `build_qemu_argv` docstring) so it isn't
   re-proposed.
2. **Ship the `savevm`/`loadvm` warm-start** (issue #49) as the cold-boot
   answer for Claude Cloud: a one-time ~123 s + 14 s cost buys **~6.6 s**
   starts thereafter. The recipe in §2 is validated end-to-end on the target
   host today.
3. For a genuinely faster *cold* boot, the only remaining big lever is
   **hardware acceleration** (`/dev/kvm`), which is a host-capability question,
   not a Beetroot code change.
