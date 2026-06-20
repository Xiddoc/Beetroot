# Caching a booted VM with QEMU `savevm` (issue #49)

!!! success "Status: shipped as the opt-in `vm.boot_cache` flag (issue #83) — validated end-to-end under TCG"
    The QEMU integration this note scoped now ships. Setting `vm.boot_cache:
    true` makes the first `beetroot up` cold-boot through a qcow2 overlay and
    `savevm`-checkpoint the running guest; every later `up` resumes it with
    `-loadvm`. **Measured on a binderless, KVM-less host (Android 14, pure
    TCG): cold first boot to first host ADB ~222 s; warm resume ~10 s — a
    ~22x speedup.** The implementation lives in
    `src/beetroot/vm/boot_cache.py` (overlay create + snapshot probe + HMP
    `savevm`) and `src/beetroot/backends/vm.py` (`VmDeviceBackend._up_cached`),
    with the qcow2/monitor/`-loadvm` argv hooks in
    `src/beetroot/vm/qemu.build_qemu_argv`. The cache-key helper
    (`scripts/vm_cache_key.py`) remains the safety latch for the *CI* cache
    story below; the per-instance flag uses a simpler "delete the overlay to
    reset" model (it is not auto-invalidated on a kernel/rootfs rebuild). See
    [config reference § warm-start boot cache](../reference/config.md#warm-start-boot-cache-vmboot_cache)
    and [vm-rnd-log Stage E](vm-rnd-log.md).

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

## Implementation hooks (shipped in #83)

The three hooks this section originally listed as follow-up work have landed:

* a **qcow2 overlay** over the raw image (`boot_cache.create_overlay`, via
  `qemu-img create -f qcow2 -F raw -b <rootfs>`), so the base stays pristine and
  internal snapshots are possible;
* an **HMP monitor** socket (`build_qemu_argv(monitor_socket=...)` →
  `-monitor unix:<path>,server,nowait`) that `boot_cache.save_snapshot` connects
  to and issues `savevm <tag>` once ADB is reachable;
* a `-loadvm` launch mode (`build_qemu_argv(loadvm=<tag>)`) so a cached snapshot
  is resumed instead of cold-booted, selected when
  `boot_cache.snapshot_present` finds the tag in the overlay.

All are additive and gated behind the opt-in `vm.boot_cache` flag — the default
cold-boot argv is byte-for-byte unchanged. The per-instance flag invalidates by
hand (delete `<instance>/vm-overlay.qcow2`); the `scripts/vm_cache_key.py` helper
remains the basis for an *automatic*, content-keyed CI cache (restore a booted VM
across jobs), which is the remaining slice on #49.
