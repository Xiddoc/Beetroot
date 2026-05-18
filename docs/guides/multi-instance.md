# Multiple Instances

One of Beetroot's core features is running several independent Android research phones side by side, each with its own `/data`, ports, resource caps, and config. This page covers how to set them up and coordinate across them.

## Create multiple instances

```bash
beetroot create research-clean
beetroot create research-stealth
cp examples/stealth.yaml research-stealth/beetroot.yaml
beetroot apply research-stealth
beetroot up research-clean research-stealth
```

`up` accepts multiple names in one call. Each instance starts in parallel.

## Port allocation

Ports are assigned by **index** — the lowest free non-negative integer at creation time. The stride is 10:

| Index | ADB port  | Frida port | Frida control |
|-------|-----------|------------|---------------|
| 0     | 5555      | 27042      | 27043         |
| 1     | 5565      | 27052      | 27053         |
| 2     | 5575      | 27062      | 27063         |
| N     | 5555+N×10 | 27042+N×10 | 27043+N×10    |

Index is assigned at `create` time and freed on `destroy`. If you destroy index 1 and create a new instance, it gets index 1 again. The index (and therefore ports) never change for the lifetime of an instance.

`beetroot ls` always shows the current mapping:

```
NAME              IDX  ADB                   FRIDA                 STATUS        PATH
research-clean    0    localhost:5555        localhost:27042       running       /home/you/research-clean
research-stealth  1    localhost:5565        localhost:27052       running       /home/you/research-stealth
```

## Pinning ports

By default ports are allocated from the stride table above. To pin a port for a specific instance (for example, to keep a stable host port across destroy/recreate cycles, or to coordinate with a tool that targets a fixed port), add a `ports:` block to its `beetroot.yaml`:

```yaml
ports:
  adb: 9000           # pin ADB to 9000
  # frida: 9001       # optional — omit to take the stride default
  # frida_control: 9002
```

Then re-stage:

```bash
beetroot apply research-clean
beetroot down research-clean && beetroot up research-clean
```

Each field is independently optional. Fields you omit fall back to the stride allocation, so you can pin one port (say, ADB) and leave the Frida ports on stride.

If you pin a port that another instance already uses, `beetroot create` and `beetroot apply` exit with a clear error before staging:

```
error: port 5555 (adb) collides with instance 'alpha' (which also uses 5555). Pin or remove one.
```

See [`ports` in the config reference](../reference/config.md#ports) for the full schema.

## Working with a specific instance

Every Beetroot verb that targets an instance takes its name:

```bash
beetroot shell research-clean     # adb shell into instance 0
beetroot logs research-stealth -f # tail logs of instance 1
beetroot down research-clean      # stop instance 0 only
```

`down` and `up` also accept multiple names:

```bash
beetroot down research-clean research-stealth
beetroot up research-clean research-stealth
```

## Scripting across instances

`beetroot env` emits eval-able shell exports for one instance at a time:

```bash
eval $(beetroot env research-clean)
# $ANDROID_DEVICE = localhost:5555
# $FRIDA_DEVICE   = localhost:27042

adb -s "$ANDROID_DEVICE" install ./target.apk
frida -H "$FRIDA_DEVICE" -n com.target.app
```

For automation that touches all running instances, iterate over `beetroot ls --json`:

```bash
beetroot ls --json | python3 -c "
import json, sys
for name, meta in json.load(sys.stdin).items():
    if meta['status'] == 'running':
        print(name, meta['adb'])
"
```

## Resource budgeting

Each instance at default settings uses roughly **1.2 GB RAM / <5% CPU** at idle, rising to **~2.5 GB** when a Frida-instrumented app is active. At the defaults (`mem: 3g`, `cpus: 2.0`):

- 3 instances = ~9 GB committed / 6 vCPUs
- 5 instances = ~15 GB committed / 10 vCPUs

Override per-instance by editing the `resources:` block in that instance's `beetroot.yaml` and running `beetroot apply <name>`. You generally want to lower the FPS and resolution before raising RAM/CPU — display overhead is non-trivial.

## Stopping and cleaning up

```bash
# Stop all running instances without deleting data
beetroot down research-clean research-stealth

# Destroy (wipe data) a specific instance
beetroot destroy research-stealth
```

!!! warning "Destroy is permanent"
    `beetroot destroy` deletes the entire instance directory, including `/data`. Use `beetroot down` if you want to keep state.
