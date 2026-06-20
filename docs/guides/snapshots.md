# Snapshots

Beetroot's `snapshot` and `restore` verbs pack and unpack an instance's host-side state as a single `.tar.zst` archive. Use snapshots to roll back a research instance to a known-good baseline, hand off an instance to a colleague, or fork one instance into many to run a comparative experiment.

!!! warning "Snapshot/restore is *not* a boot-skip (warm-start)"
    `beetroot snapshot` / `restore` captures the **cold** host-side state —
    `beetroot.yaml`, the persisted `data/` tree, staged `modules/`. Restoring
    re-creates that on-disk state, but the next `beetroot up` still runs a
    **full cold Android boot** (and, on the `binder: vm` backend, a full
    cold *VM* boot under QEMU). It does not resume a running machine.

    If your goal is to make repeated starts **near-instant** — the common
    case for CI and ephemeral agent sandboxes paying the slow TCG cold boot
    on every run — the boot-skipping warm-start is the QEMU `savevm`/`loadvm`
    whole-machine snapshot, designed in
    [vm-savevm-cache.md](../design/vm-savevm-cache.md) (issue #49) and
    summarised in [Warm-starting the `binder: vm`
    backend](#warm-starting-the-binder-vm-backend) below. The two mechanisms
    are orthogonal: snapshot/restore is for *portability and rollback of
    userdata*; savevm is for *skipping the boot*.

## When to snapshot

A snapshot is the right tool when you need to capture an instance's *complete persisted state* and later re-create it byte-for-byte:

- **Before a destructive experiment.** Take a snapshot, then hack on the live instance — if you brick it, `restore` the archive over a fresh instance.
- **Forking for comparison.** Snapshot `alpha` once, restore it as `beta`, `gamma`, `delta` — each one starts from the identical Android userdata, has its own ports, and can run concurrently with the source.
- **Hand-off.** Send the `.tar.zst` to a teammate. They run `beetroot restore` on their host and pick up exactly where you left off.

For *destructive* experimentation against a single instance where you just want a quick "undo button" without leaving the host, the [filesystem `cp -a` recipe](#low-overhead-alternatives) at the bottom of this page is faster.

## What's captured

The archive is rooted at the instance directory and contains:

```
./beetroot.yaml
./data/...                       (full Android /data tree)
./modules/...                    (staged Magisk module zips)
./frida-server                   (if it exists in the source)
./.beetroot-snapshot.json        (manifest)
```

The manifest records the source instance's name, port index, the
`beetroot` release that produced the snapshot, an ISO-8601 timestamp,
and a `path_layout` field carrying the source's `stealth_paths` blob
(see [path_layout round-trip](#path_layout-round-trip) below). It also
records a `schema_version` (the manifest format version) and a `kind`
discriminator — snapshots are redroid-only today (`kind: "redroid"`), and
the field exists so a future cross-backend snapshot story doesn't need a
second schema bump.

**The `.env` file is deliberately excluded.** It's regenerated from `beetroot.yaml` the next time you run `beetroot apply`, so leaving it out of the archive means the restored instance picks up its (freshly allocated) port indices and host paths cleanly. Don't commit the archive's contents — assume the next `beetroot apply` is load-bearing.

!!! info "Docker overlay layer is not captured by design"
    The container's writable overlay layer (everything inside the container that's not under the `/data` bind-mount) is *not* snapshotted. Redroid regenerates the overlay deterministically from the base image plus the persisted `/data` bind-mount, so `beetroot up` after a restore produces an equivalent container. If you need a snapshot of a customized base image, snapshot the Docker image itself (`docker image save`) — Beetroot snapshots are an instance-state artefact, not a Docker-image artefact.

## Taking a snapshot

Stop the instance first — the Android container has open file handles into `data/`, and `tar`-ing a live directory produces an inconsistent archive.

```bash
beetroot down alpha
beetroot snapshot alpha
```

By default, the archive lands at `./alpha.tar.zst`. Use `-o` (or `--output`) to redirect:

```bash
beetroot snapshot alpha -o ~/beetroot-snapshots/alpha-$(date +%Y%m%d).tar.zst
```

The `.tar.zst` extension is appended automatically if you omit it.

!!! tip "Keep a snapshots dir alongside your instances"
    Pick a dir anywhere (e.g. `~/beetroot-snapshots/`) and route every snapshot through it. Versioned filenames (`alpha-20260518.tar.zst`) let you keep a campaign-wide rollback ladder without inventing a per-instance scheme.

## Restoring a snapshot

```bash
beetroot restore ~/beetroot-snapshots/alpha-20260518.tar.zst --name beta
```

Without `--name <name>`, the restored instance uses the source's recorded name (from the manifest). Without `--path <dir>`, it lands at `./<name>/`. Both flags are optional:

```bash
# Most explicit form
beetroot restore archive.tar.zst --name beta --path /srv/instances/beta

# Defaults: name from manifest, path is ./<name>
beetroot restore archive.tar.zst
```

After restore, the new instance is registered with a freshly allocated port index — **the source's index is never reused**, so the original and the restored instance can run concurrently if both directories still exist on disk:

```bash
beetroot up alpha &   # original keeps its index 0 (ADB 5555)
beetroot up beta      # restored copy gets index 1 (ADB 5565)
```

`restore` stages the restored instance for you — it re-renders the per-instance `.env` (which isn't in the archive) and lays down the Frida placeholder and `data/` / `modules/` directories, exactly as `beetroot create` does. So no intermediate `beetroot apply` is required; `beetroot up <new-name>` works directly. The CLI prints the exact next-step command on success (`next: beetroot up <name>`).

### Restore over an existing directory: `--force`

Restoring into a non-empty target directory fails by default — Beetroot won't silently wipe your data:

```bash
$ beetroot restore archive.tar.zst --path ./existing-data
error: /home/x/existing-data already exists and is non-empty; pass --force to overwrite, or pick another path
```

Pass `--force` to overwrite. The target is `rm -rf`'d, then the archive is extracted into the freshly empty directory:

```bash
beetroot restore archive.tar.zst --name beta --path ./existing-data --force
```

`--force` does *not* touch the registry. If a name already exists there, you'll get a separate error and need to `beetroot destroy <name>` first.

## `path_layout` round-trip

The manifest carries a `path_layout: dict[str, str]` field. `snapshot` reads the source instance's [`RedroidBackendConfig.stealth_paths`](../design/stealth-posture.md) blob and writes it verbatim into this field; `restore` reads the field and replays it into the new instance's slot via the registry. The recognised keys today are `magisk_db`, `modules_dir`, and `frida_bin`, each overriding the corresponding `BEETROOT_*` line in the rendered `.env`.

In v0.4 the slot defaults to `{}` — `Instance.create` does not yet generate a randomized layout — so today's snapshots carry `path_layout: {}` and `restore` is a structural no-op. A future release will populate the slot in `Instance.create` once stealth research validates a safe path; from that point on, snapshot → restore will carry the per-instance randomized paths through to the destination's `.env` on the very first `beetroot apply`.

Unknown keys in `path_layout` are silently ignored by `render_env`, so a v0.6 snapshot carrying a future key (e.g. `stealth_module_id`) restores cleanly against a v0.4 host without faulting — the recognised keys still take effect, and the unknown one is preserved in the registry slot for a later upgrade to consume.

## Round-tripping in scripts

The exit code semantics match the rest of the CLI: `0` on success, `1` on any user-recoverable error (missing instance, malformed archive, name collision), with a single-line `error: <reason>` on stderr. Pipe-friendly:

```bash
set -e
beetroot down alpha
beetroot snapshot alpha -o ./baseline.tar.zst
# ... destructive experiment ...
beetroot down alpha
beetroot destroy -y alpha
beetroot restore ./baseline.tar.zst --name alpha
beetroot up alpha
```

## Warm-starting the `binder: vm` backend

On a host with no kernel binder driver and no KVM (the Claude Code on the web
sandbox, many CI runners), Beetroot boots redroid inside a QEMU micro-VM under
**TCG software emulation**. That cold boot is the slow path — Android takes
~100 s to several minutes to reach `sys.boot_completed` under TCG (see
[vm-rnd-log.md](../design/vm-rnd-log.md)). Paying it *once* is unavoidable;
paying it on *every* start is not.

The warm-start trick is a QEMU **whole-machine snapshot**: boot the micro-VM
once to `boot_completed`, checkpoint the running machine (RAM + disk) with
`savevm`, then `loadvm` it on subsequent starts to resume an already-booted
guest — ART/Zygote warmed, `system_server` up — in **seconds** instead of
re-running the cold boot. This is distinct from `beetroot snapshot`/`restore`
(which re-boots; see the warning at the top of this page).

### Why this is the right warm-start for the VM path

A whole-machine QEMU snapshot sidesteps the fragility of checkpointing a live
Android: the host-binder container path would mean CRIU-dumping live
binder/ashmem/socket FDs, but a QEMU snapshot captures the *entire guest* as an
opaque RAM+disk image. A restored guest is also a *more deterministic*
post-boot baseline (caches primed), which cuts the runner-noise that makes CI
benchmarks flaky.

### The manual recipe (today)

The `binder: vm` backend currently launches QEMU with a **raw** root disk and
no monitor socket, so the integrated `-loadvm` launch path is the
[#49](https://github.com/Xiddoc/Beetroot/issues/49) follow-up. Until it lands,
you can drive the warm-start by hand against the artifacts
`beetroot build --vm-kernel` produced (`~/.cache/beetroot/vm/bzImage` +
`rootdisk.img`):

```bash
VM=~/.cache/beetroot/vm
# 1. Make a qcow2 overlay over the raw rootfs (internal snapshots need qcow2).
qemu-img create -f qcow2 -b "$VM/rootdisk.img" -F raw "$VM/overlay.qcow2"

# 2. Cold-boot ONCE with a QMP monitor; wait for boot_completed, then savevm.
qemu-system-x86_64 -M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max \
  -smp "$(nproc)" -m 8192 -nographic -no-reboot \
  -kernel "$VM/bzImage" -drive file="$VM/overlay.qcow2",if=virtio \
  -device virtio-rng-pci \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:5555-:5555 \
  -device virtio-net-pci,netdev=net0 \
  -monitor unix:"$VM/mon.sock",server,nowait \
  -append "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off random.trust_cpu=on"
# In another shell, once `beetroot logs` / the serial shows boot_completed:
echo savevm warm | socat - unix-connect:"$VM/mon.sock"

# 3. Every subsequent start RESUMES the booted machine in seconds:
qemu-system-x86_64 ... -loadvm warm ...   # same argv as step 2, plus -loadvm warm
```

The saved state lives **inside** `overlay.qcow2`. Keep the overlay keyed to the
exact `bzImage` + `rootdisk.img` it was taken on — a snapshot resumed against a
different guest kernel/rootfs is worse than a cold boot. The
[`scripts/vm_cache_key.py`](https://github.com/Xiddoc/Beetroot/blob/master/scripts/vm_cache_key.py)
helper computes that key (it folds every guest-defining input), and the
[vm-savevm-cache.md](../design/vm-savevm-cache.md) design note specifies how
the integrated path will cache and restore it safely.

!!! tip "Cold boot still needs to be fast"
    Warm-start removes the boot from the *steady state*, but the first boot —
    and any boot after a guest-artifact change invalidates the snapshot —
    still runs cold. Beetroot ships the cold-boot levers (`-smp auto`, MTTCG,
    `mitigations=off`, and the `virtio-rng-pci` + `random.trust_cpu=on`
    entropy fix from issue #83) so that first boot is as fast as TCG allows.
    On a host **with** `/dev/kvm`, `vm.accel: auto` picks KVM and the cold boot
    approaches native speed — warm-start matters most precisely where it
    doesn't (TCG).

## Low-overhead alternatives

For the simplest case — quick "undo button" for a single instance, no
hand-off, no forking — a plain filesystem copy is faster than packing a
`.tar.zst`:

```bash
beetroot down alpha
cp -a "$(beetroot ls --json | jq -r '.alpha.path')/data" /tmp/alpha-data.clean

# ... do your research ...

beetroot down alpha
rm -rf "$(beetroot ls --json | jq -r '.alpha.path')/data"
cp -a /tmp/alpha-data.clean "$(beetroot ls --json | jq -r '.alpha.path')/data"
beetroot up alpha
```

This is fine for a transient snapshot you'll throw away in the same session. For anything you might `git annex add`, hand off to a colleague, or keep around for a campaign, use `beetroot snapshot`.

## Fresh start without snapshot

If you want to reset to a pristine Android install without recreating the instance (keeping the same ports and config):

```bash
beetroot down alpha
rm -rf "$(beetroot ls --json | jq -r '.alpha.path')/data"
beetroot up alpha
```

Android will go through first-time setup again. Frida and modules are unaffected — they're staged separately.

## Full wipe and recreate

```bash
beetroot destroy -y alpha
beetroot create alpha
beetroot up alpha
```
