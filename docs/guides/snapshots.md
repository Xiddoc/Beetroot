# Snapshots

There is no `beetroot snapshot` verb — and there doesn't need to be. The entire Android userdata lives at `instances/<name>/data/` on the host. Snapshotting is a plain filesystem copy.

## Basic snapshot and restore

```bash
# Stop the instance first (data must not be in use)
beetroot down alpha

# Snapshot
cp -a instances/alpha/data instances/alpha/data.clean

# ... do your research ...

# Restore
beetroot down alpha          # stop if still running
rm -rf instances/alpha/data
cp -a instances/alpha/data.clean instances/alpha/data
beetroot up alpha
```

!!! warning "Stop before copying"
    Always run `beetroot down <name>` before touching `data/`. The Android container has open file handles into this directory; copying while it's running produces inconsistent snapshots.

## Compressed snapshots

For large `data/` directories, use `tar` with `zstd` compression:

```bash
beetroot down alpha

# Create snapshot
tar --zstd -cf snapshots/alpha-clean.tar.zst -C instances/alpha data

# Restore
beetroot down alpha
rm -rf instances/alpha/data
tar --zstd -xf snapshots/alpha-clean.tar.zst -C instances/alpha
beetroot up alpha
```

!!! tip "Keep a `snapshots/` directory"
    Add `snapshots/` to your project-level `.gitignore` (it's already large and binary) and use it as a snapshot store alongside the repo.

## Versioned snapshots

If you're running a research campaign across multiple builds or dates, label your snapshots:

```bash
mkdir -p snapshots
tar --zstd -cf snapshots/alpha-$(date +%Y%m%d).tar.zst -C instances/alpha data
```

## Fresh start without destroy

If you want to reset to a pristine Android install without recreating the instance (keeping the same ports and config):

```bash
beetroot down alpha
rm -rf instances/alpha/data    # wipe userdata only
beetroot up alpha              # Android re-provisions /data on first boot
```

Android will go through first-time setup again. Frida and modules are unaffected — they're staged separately.

## Full wipe and recreate

If you want to start completely from scratch (new config, new ports possible):

```bash
beetroot destroy -y alpha
beetroot create alpha --preset default
beetroot up alpha
```
