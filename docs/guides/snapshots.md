# Snapshots

Beetroot's `snapshot` and `restore` verbs pack and unpack an instance's host-side state as a single `.tar.zst` archive. Use snapshots to roll back a research instance to a known-good baseline, hand off an instance to a colleague, or fork one instance into many to run a comparative experiment.

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

## Snapshots are *not* a warm-start (and what is)

A common hope — especially for the slow `binder: vm` TCG boot — is that
`beetroot snapshot` captures a *booted* device so `restore` can skip the boot.
It does **not**, and the distinction matters:

- **`beetroot snapshot` captures cold `/data`, not a running machine.** The
  archive is the on-disk Android userdata tree (plus `beetroot.yaml`, modules,
  the Frida placeholder). Restoring it still requires a full Android cold boot —
  `beetroot up` brings the framework up from scratch against the restored
  `/data`. It is a *state* artefact (roll back, fork, hand off), never a
  boot-time shortcut.
- **`beetroot snapshot` is redroid-only.** It is not implemented for the
  `binder: vm` backend (the manifest's `kind` is `redroid`), so
  `beetroot snapshot <vm-instance>` reports that the backend lacks the
  capability. There is nothing to snapshot host-side for a VM instance beyond
  its config — the interesting state lives inside the guest disk image.

If what you actually want is **"boot the VM once, then start near-instantly
every time after"**, that is a *QEMU machine-state* snapshot (RAM + disk),
which is a different mechanism entirely:

> **Warm-starting the `binder: vm` micro-VM** — boot once, `savevm` the running
> machine, then `loadvm` to resume an already-booted guest in seconds instead
> of paying the ~100 s+ TCG cold boot. This is the right tool for a fast cold
> start on a binderless/KVM-less host. The validated recipe and measured
> cold-vs-warm numbers live in
> [VM boot-cache (savevm)](../design/vm-savevm-cache.md#warm-start-recipe).
> Wiring it into the CLI is tracked as
> [issue #49](https://github.com/Xiddoc/Beetroot/issues/49).

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
