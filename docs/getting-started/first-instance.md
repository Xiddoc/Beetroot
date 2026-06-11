# Your First Instance

This page walks you through the full lifecycle of a Beetroot instance: create → boot → use → stop → wipe.

## Create

```bash
beetroot create alpha
```

This:

1. Picks the lowest free port index (`0` → ADB `5555`, Frida `27042` on a fresh host).
2. Creates the instance directory at `./alpha/` (override with `--path /some/where`).
3. Writes a minimal `./alpha/beetroot.yaml` — just `api_version` plus `android.version`; every other field falls back to schema defaults.
4. Stages a 0-byte placeholder at `./alpha/frida-server`. Frida is opt-in starting in v0.3 — copy [`examples/with-frida.yaml`](../guides/examples.md) over `./alpha/beetroot.yaml` (or add a `frida:` block yourself) and run `beetroot apply alpha` to actually download and stage `frida-server`. See [Frida](../guides/frida.md).
5. Renders `./alpha/.env` for Docker Compose.
6. Registers the instance under `~/.config/beetroot/instances.json` (the cross-instance registry).

The output looks like:

```
[beetroot] created alpha at /home/you/alpha (index 0, ADB localhost:5555, Frida localhost:27042)
[beetroot] next: beetroot up alpha
```

!!! tip "Start from a richer baseline"
    Copy one of the [`examples/`](../guides/examples.md) YAMLs over `./alpha/beetroot.yaml` after creating the instance — e.g. `cp examples/stealth.yaml alpha/beetroot.yaml && beetroot apply alpha` to start with Shamiko and a wider Magisk denylist when the target app checks for root.

!!! tip "Adopt an existing instance dir"
    If you already have an instance dir (e.g. cloned from a teammate), use `beetroot register <path>` to add it to the registry without touching its files.

## Boot

```bash
beetroot up alpha
```

Under the hood: `docker compose -p alpha -f <bundled-template> --project-directory /path/to/alpha --env-file /path/to/alpha/.env up -d`. The first boot takes 30–60 seconds while Android initialises and `entrypoint.sh` configures Magisk.

Watch the logs to know when the device is ready:

```bash
beetroot logs alpha -f
```

Look for:

```
[*] Android boot detected. Applying Beetroot configuration...
[*] Enabling Zygisk + denylist
```

Once you see Zygisk + denylist applied, the device is ready to use. If you opted into Frida — by adding a `frida:` block to `beetroot.yaml`, or by copying `examples/with-frida.yaml` over the generated file before running `beetroot apply` — you'll also see `[*] Launching Frida from /data/local/tmp/frida-server`. A bare `beetroot create` skips the Frida launch.

## Connect

### Interactive shell

```bash
beetroot shell alpha
```

This `adb connect`s to the right port and drops you into an `adb shell`. You're root by default (Magisk handles it).

### From your own scripts

```bash
eval $(beetroot status --json alpha | python3 -c "
import json,sys
r=json.load(sys.stdin)
print(f'ANDROID_DEVICE={r[\"adb_address\"]}')
print(f'FRIDA_DEVICE={r[\"frida_address\"]}')
")
# Now $ANDROID_DEVICE and $FRIDA_DEVICE are set
adb -s "$ANDROID_DEVICE" install ./target.apk
frida -H "$FRIDA_DEVICE" -n com.target.app
```

`beetroot status --json` prints machine-readable JSON so you can drive ADB and Frida from any script without hardcoding port numbers.

## Install an APK

```bash
ANDROID_DEVICE=$(beetroot status --json alpha | python3 -c "import json,sys; print(json.load(sys.stdin)['adb_address'])")
adb -s "$ANDROID_DEVICE" install -r ./target.apk
```

## Check status

```bash
beetroot ls
```

```
NAME    KIND     IDX  ADB             FRIDA            STATUS    PATH
alpha   redroid  0    localhost:5555  localhost:27042  running   /home/you/alpha
```

## Stop (data preserved)

```bash
beetroot down alpha
```

Android is shut down cleanly; the instance's `data/` stays intact. `beetroot up alpha` restarts from exactly where you left off.

## Wipe and start fresh

```bash
beetroot destroy -y alpha
beetroot create alpha
beetroot up alpha
```

`destroy` deletes the instance directory entirely, including `/data`, and removes the registry entry. The `-y` flag skips the confirmation prompt.

## What's next

- [Multiple Instances](../guides/multi-instance.md) — run several phones in parallel.
- [Examples](../guides/examples.md) — start from `stealth.yaml` for anti-detection research.
- [Magisk Modules](../guides/modules.md) — flash Shamiko, LSPosed, or your own hooks.
- [Frida](../guides/frida.md) — attach scripts and use the `beetroot frida` wrapper.
