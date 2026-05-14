# Your First Instance

This page walks you through the full lifecycle of a Beetroot instance: create → boot → use → stop → wipe.

## Create

```bash
beetroot create alpha
```

This allocates port index 0 (ADB `5555`, Frida `27042`), writes `instances/alpha/beetroot.yaml` from the default preset, downloads the correct Frida server binary, and renders `instances/alpha/.env` for Docker Compose.

The output looks like:

```
[beetroot] created alpha (index 0, ADB localhost:5555, Frida localhost:27042)
[beetroot] next: beetroot up alpha
```

!!! tip "Use a preset"
    Pass `--preset stealth` to start with Shamiko and a wider Magisk denylist — useful when the target app checks for root. See [Presets](../guides/presets.md) for details.

## Boot

```bash
beetroot up alpha
```

This runs `docker compose -p alpha -f compose.yaml --env-file instances/alpha/.env up -d` under the hood. The first boot takes 30–60 seconds while Android initializes and `entrypoint.sh` configures Magisk.

Watch the logs to know when the device is ready:

```bash
beetroot logs alpha -f
```

Look for:

```
[*] Android boot detected. Applying Stealth Configuration...
[*] Enabling Zygisk and Denylist...
[*] Launching Frida server...
```

Once you see the Frida line, the device is ready to use.

## Connect

### Interactive shell

```bash
beetroot shell alpha
```

This `adb connect`s to the right port and drops you into an `adb shell`. You're root by default (Magisk handles it).

### From your own scripts

```bash
eval $(beetroot env alpha)
# Now $ANDROID_DEVICE and $FRIDA_DEVICE are set
adb -s "$ANDROID_DEVICE" install ./target.apk
frida -H "$FRIDA_DEVICE" -n com.target.app
```

`beetroot env` prints eval-able exports so you can drive ADB and Frida from any script without hardcoding port numbers.

## Install an APK

```bash
eval $(beetroot env alpha)
adb -s "$ANDROID_DEVICE" install -r ./target.apk
```

## Check status

```bash
beetroot ls
```

```
NAME          IDX  ADB                   FRIDA                 STATUS
alpha         0    localhost:5555        localhost:27042       running
```

## Stop (data preserved)

```bash
beetroot down alpha
```

Android is shut down cleanly; `instances/alpha/data/` stays intact. `beetroot up alpha` restarts from exactly where you left off.

## Wipe and start fresh

If you want a clean slate:

```bash
beetroot destroy -y alpha
beetroot create alpha
beetroot up alpha
```

`destroy` deletes `instances/alpha/` entirely, including `/data`. The `-y` flag skips the confirmation prompt.

## What's next

- [Multiple Instances](../guides/multi-instance.md) — run several phones in parallel.
- [Presets](../guides/presets.md) — start from `stealth` for anti-detection research.
- [Magisk Modules](../guides/modules.md) — flash Shamiko, LSPosed, or your own hooks.
- [Frida](../guides/frida.md) — attach scripts and use the `beetroot frida` wrapper.
