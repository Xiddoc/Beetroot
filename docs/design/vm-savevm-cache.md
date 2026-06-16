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
