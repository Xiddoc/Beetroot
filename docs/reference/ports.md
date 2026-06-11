# Port Allocation

Beetroot assigns ports by **index** using a stride-of-10 scheme — unless explicitly overridden in YAML (see [`ports` in the config reference](./config.md#ports)). The index is allocated when you run `beetroot create` (lowest free non-negative integer wins) and freed when you run `beetroot destroy`. Within an instance's lifetime, its ports never change.

## Port table

The defaults below apply unless overridden by a `ports:` block in the instance's `beetroot.yaml`.

| Index | ADB port  | Frida (data) | Frida (control) |
|-------|-----------|--------------|-----------------|
| 0     | 5555      | 27042        | 27043           |
| 1     | 5565      | 27052        | 27053           |
| 2     | 5575      | 27062        | 27063           |
| 3     | 5585      | 27072        | 27073           |
| 4     | 5595      | 27082        | 27083           |
| 5     | 5605      | 27092        | 27093           |
| N     | 5555+N×10 | 27042+N×10   | 27043+N×10      |

## Allocation algorithm

```
lowest_free_index(used: set[int]) → int:
    i = 0
    while i in used:
        i += 1
    return i
```

Freed indices are reused. If you have instances at indices 0, 1, 2 and destroy the one at index 1, the next `beetroot create` gets index 1 (ADB port 5565) — not index 3.

## Viewing current assignments

```bash
beetroot ls
```

```
NAME          KIND     IDX  ADB                   FRIDA                 STATUS        PATH
alpha         redroid  0    localhost:5555        localhost:27042       running       /home/you/alpha
bravo         redroid  1    localhost:5565        localhost:27052       running       /home/you/bravo
```

## Getting ports programmatically

```bash
beetroot status --json alpha | python3 -c "
import json,sys; r=json.load(sys.stdin)
print(r['adb_address'])    # localhost:5555
print(r['frida_address'])  # localhost:27042
"
```

Or from JSON:

```bash
beetroot ls --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(data['alpha']['adb'])   # localhost:5555
print(data['alpha']['frida']) # localhost:27042
"
```

## Why stride-10?

Frida uses two consecutive ports per instance: the data port (`27042 + N×10`) and the control port (`27043 + N×10`, used by Frida's RPC/command channel). The stride of 10 leaves 8 unused ports between instances as headroom — if Frida's port requirements change in a future version, there's room to absorb the change without a layout migration.

ADB at `5555 + N×10` follows the same stride so all three port families stay aligned by index.

The maximum supported index is `3849` — above that, the Frida control port (`27043 + N×10`) would exceed 65535 and Beetroot raises a clear error at port-allocation time. The Frida control port has the highest base and is therefore the binding constraint; the ADB port at this index (`5555 + 3849×10 = 44045`) is well within range.

## Overriding the stride

The stride scheme is the *default*. To pin a port for an instance, add a `ports:` block to its `beetroot.yaml`:

```yaml
ports:
  adb: 9000          # pin ADB; frida + frida_control stay on the stride
  # frida: 9001
  # frida_control: 9002
```

Each field is independently optional — fields you omit fall back to the stride allocation for the instance's index. See [`ports`](./config.md#ports) in the config reference for the full schema, and the [pinning ports](../guides/multi-instance.md#pinning-ports) section of the multi-instance guide for usage notes.

Beetroot pre-validates port collisions on every `create` and `apply` — if two instances both end up on the same host port (via stride, via overrides, or any mix), the command exits before staging with:

```
error: port 5555 (adb) collides with instance 'alpha' (which also uses 5555). Pin or remove one.
```

## Common pitfalls

### Partial override colliding with a stride sibling

Each field of the `ports:` block is independently optional, and the ones you omit fall back to the stride-of-10 default for the instance's index. That means a partial override can silently collide with a sibling that wasn't overridden. The most common case is pinning `frida` to a value that's already the stride default for `frida_control`:

```yaml
ports:
  frida: 27043   # at index 0 this lands on frida_control's stride default!
```

At index 0 the stride defaults are `frida=27042`, `frida_control=27043`. Pinning `frida: 27043` leaves `frida_control` on `27043` — both ports collide on the same host port. Beetroot rejects the resolved dict on `create`/`apply`/`ls`/`up` with:

```
error: resolved ports collide on this instance: {27043: ['frida', 'frida_control']}.
Override ports.adb / ports.frida / ports.frida_control in beetroot.yaml
to avoid colliding with stride-of-10 defaults.
```

The fix is to pin the colliding sibling explicitly too, or pick a value outside the `27042`/`27043 + index*10` window:

```yaml
ports:
  frida: 27043
  frida_control: 27044   # explicit — no longer falls back to the stride default
```
