# Caching a booted VM with QEMU `savevm` (issue #49)

!!! info "Status: design + cache-key helper landed; the QEMU integration is the follow-up implementation"
    This note specifies the savevm boot-cache and ships its load-bearing,
    unit-tested piece — the cache-key helper (`scripts/vm_cache_key.py`). The
    QEMU side (qcow2 overlay + QMP `savevm`/`loadvm`) is described here as the
    implementation that plugs into the `binder: vm` backend next.

## Problem

Booting redroid in the `binder: vm` backend under TCG takes ~100 s (see
[vm-rnd-log.md](vm-rnd-log.md)). CI jobs that need a **booted** device but do
**not** measure boot time — the functional [`tier-vm-qemu` e2e tier](../guides/running-in-ci.md)
and the post-boot rows of the [nightly benchmark](../guides/running-in-ci.md) —
repay that ~100 s on every run for no benefit.

## Approach

Boot the micro-VM **once** to `sys.boot_completed`, checkpoint the *running
machine state* (RAM + disk), cache the artifact, and **restore** it (seconds)
in downstream jobs instead of cold-booting:

1. **Checkpoint.** With the rootfs as a qcow2 (or a qcow2 overlay over the raw
   image), issue QEMU `savevm <tag>` over a QMP/monitor socket once the guest
   reports `boot_completed`. This writes an internal snapshot (RAM + block
   state) into the qcow2. `migrate "exec:gzip -c > state.gz"` is the
   alternative when an external state file is preferred.
2. **Cache.** Key the cache entry with `scripts/vm_cache_key.py` over the
   `bzImage` + `rootdisk.img` (and/or the guest-defining sources). The key
   changes the instant any input changes, so a snapshot is **never** restored
   against a kernel/rootfs it wasn't taken on.
3. **Restore.** Launch QEMU with `-loadvm <tag>` (internal snapshot) or
   `-incoming "exec:gzip -dc < state.gz"` (migration file). The guest resumes
   already-booted — ART/Zygote settled — in seconds.

## Why the VM path is the right place to do this

* The VM path is both the **slowest** boot and the one that **snapshots
  cleanly**. Checkpointing the host-binder container path would mean
  CRIU-dumping live binder/ashmem/socket FDs — fragile. A whole-machine QEMU
  snapshot sidesteps all of that.
* A restored **warm** VM is a *more deterministic* post-boot baseline (Zygote
  warmed, caches primed), which also cuts the runner-noise that makes CI
  benchmarks flaky.

## Correctness caveats

* **Never use the cached boot for the cold-boot benchmark.** That metric must
  measure a real cold boot; the cache is for functional / post-boot jobs only.
  The benchmark lane keeps the boot-time samples on the cold path.
* **The cache key is the safety latch.** A stale snapshot booted against a
  newer guest is worse than a cold boot, so the key (this PR's helper) must
  fold every input that affects the snapshot. Tests enforce that it changes on
  any content or filename change.
* **Mind the cache budget.** A RAM image is multi-GB; compress it (gzip/zstd)
  and stay within GitHub Actions' ~10 GB per-repo cache budget and per-entry
  limits. Evict by keying narrowly (one entry per guest revision).

## Distinct from `beetroot snapshot`

Beetroot's existing `beetroot snapshot` / `restore` packs the **cold** `/data`
directory as a `.tar.zst` — restoring it still requires a full cold Android
boot, so it does **not** skip the boot. The savevm path is QEMU-specific
(running-RAM checkpoint) and orthogonal.

## Implementation hooks (follow-up)

The current backend launches QEMU with a **raw** root disk and no monitor
(`build_qemu_argv` in `src/beetroot/vm/qemu.py`). The savevm capability needs:

* a **qcow2** root disk (or a qcow2 overlay over the raw image) so internal
  snapshots are possible;
* a **QMP/monitor** socket so a helper can issue `savevm` after boot;
* a `-loadvm` / `-incoming` launch mode so a cached snapshot is resumed instead
  of cold-booted.

These are additive backend changes (a new "resume from snapshot" launch path)
gated behind an opt-in, and are tracked as the next slice on #49. The cache key
that makes any of it safe lands here.

## Warm-start recipe

Issue #83 asked for a documented/validated warm-start workflow. The recipe below
is the manual precursor to the CLI integration above — it needs no Beetroot code
change, only the stock guest kernel + rootfs from `beetroot build --vm-kernel`
and a qcow2 overlay. Use it today to skip the ~120 s cold boot on repeat starts;
the measured cold-vs-warm numbers from an in-sandbox run are in
[vm-rnd-log Stage E](vm-rnd-log.md#micro-vm-rd-log-stage-e-cold-boot-entropy-levers-warm-start-issue-83).

**1. Boot once over a qcow2 overlay, with a monitor socket.** A qcow2 overlay
keeps the committed raw `rootdisk.img` immutable and is what `savevm` writes its
internal snapshot into:

```sh
qemu-img create -q -f qcow2 -b ~/.cache/beetroot/vm/rootdisk.img -F raw warm.qcow2
qemu-system-x86_64 \
  -M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max -smp 4 -m 8192 \
  -nographic -display none -no-reboot -kernel ~/.cache/beetroot/vm/bzImage \
  -drive file=warm.qcow2,format=qcow2,if=virtio \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:5555-:5555 \
  -device virtio-net-pci,netdev=net0 -device virtio-rng-pci \
  -qmp unix:qmp.sock,server,nowait \
  -append "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off random.trust_cpu=on"
```

**2. Once the guest reports `sys.boot_completed=1`, checkpoint the running
machine** (RAM + disk) into the qcow2 as a named internal snapshot, then quit.
Over QMP, `savevm` is reached via `human-monitor-command`:

```sh
# pause vCPUs, snapshot, quit — via the QMP socket above
printf '%s\n' '{"execute":"qmp_capabilities"}' \
  '{"execute":"stop"}' \
  '{"execute":"human-monitor-command","arguments":{"command-line":"savevm warm"}}' \
  '{"execute":"quit"}' | socat - UNIX-CONNECT:qmp.sock
```

**3. Resume — every subsequent start is a `-loadvm`, not a cold boot.** Relaunch
with the identical argv plus `-loadvm warm`; the guest resumes already-booted
(ART/Zygote settled, the in-guest adb relay already listening), reachable over
`adb connect localhost:5555` in seconds:

```sh
qemu-system-x86_64 ... -drive file=warm.qcow2,format=qcow2,if=virtio \
  ... -loadvm warm
```

The launch-time flags (`-netdev`/`hostfwd`, `-smp`, `-m`) are re-applied fresh
on resume, so the host ADB port forward is re-established without being part of
the saved state. The cache key from `scripts/vm_cache_key.py` over
(`bzImage` + `rootdisk.img`) is the safety latch: only restore `warm.qcow2`
against the exact kernel/rootfs it was taken on.

`benchmarks/` is the *cold* path's home — never serve a benchmark cold-boot
sample from a `loadvm` (it would measure resume, not boot). The two metrics are
orthogonal.
