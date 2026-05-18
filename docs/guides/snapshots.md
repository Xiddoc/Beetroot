# Snapshots

There is no `beetroot snapshot` verb — and there doesn't need to be. The entire Android userdata lives at `<instance-dir>/data/`. Snapshotting is a plain filesystem copy.

Throughout this page, `$ALPHA` is the instance directory (look it up with `beetroot ls`):

```bash
ALPHA=$(beetroot ls --json | jq -r '.alpha.path')
```

## Basic snapshot and restore

```bash
# Stop the instance first (data must not be in use)
beetroot down alpha

# Snapshot
cp -a "$ALPHA/data" "$ALPHA/data.clean"

# ... do your research ...

# Restore
beetroot down alpha
rm -rf "$ALPHA/data"
cp -a "$ALPHA/data.clean" "$ALPHA/data"
beetroot up alpha
```

!!! warning "Stop before copying"
    Always run `beetroot down <name>` before touching `data/`. The Android container has open file handles into this directory; copying while it's running produces inconsistent snapshots.

## Compressed snapshots

For large `data/` directories, use `tar` with `zstd` compression:

```bash
beetroot down alpha

# Create snapshot
tar --zstd -cf snapshots/alpha-clean.tar.zst -C "$ALPHA" data

# Restore
beetroot down alpha
rm -rf "$ALPHA/data"
tar --zstd -xf snapshots/alpha-clean.tar.zst -C "$ALPHA"
beetroot up alpha
```

!!! tip "Keep a snapshots dir alongside your instances"
    Pick a dir anywhere (e.g. `~/beetroot-snapshots/`) and use it as a snapshot store. Beetroot itself doesn't care.

## Versioned snapshots

If you're running a research campaign across multiple builds or dates, label your snapshots:

```bash
mkdir -p snapshots
tar --zstd -cf snapshots/alpha-$(date +%Y%m%d).tar.zst -C "$ALPHA" data
```

## Fresh start without destroy

If you want to reset to a pristine Android install without recreating the instance (keeping the same ports and config):

```bash
beetroot down alpha
rm -rf "$ALPHA/data"   # wipe userdata only
beetroot up alpha       # Android re-provisions /data on first boot
```

Android will go through first-time setup again. Frida and modules are unaffected — they're staged separately.

## Full wipe and recreate

If you want to start completely from scratch (new config, new ports possible):

```bash
beetroot destroy -y alpha
beetroot create alpha
beetroot up alpha
```
