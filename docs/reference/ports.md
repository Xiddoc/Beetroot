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
beetroot status alpha | python3 -c "
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

The maximum supported index is `1295` — above that, Beetroot raises a clear error at port-allocation time. The binding constraint is the **extra pool** (`40000 + N×10`), into which arbitrary host-unset `ports:` entries are auto-allocated (see [Overriding the stride](#overriding-the-stride)): the well-known bands must stay strictly below `40000` (so a high-index Frida control port doesn't climb into the extra-pool range and falsely collide cross-instance), which caps the index at `1295`. The looser "keep the extra pool ≤ 65535" constraint would allow `2552`, so the extra-pool base is what bounds the index.

## Overriding the stride

The stride scheme is the *default*. Since `api_version: 8` (issue #108) the `ports:` block is a **list** of `{service, guest, host}` mappings. To pin a port for an instance, give the entry an explicit `host`:

```yaml
ports:
  - {service: adb, guest: 5555, host: 9000}   # pin ADB
  - {service: frida, guest: 27042}            # host unset → stride default
  - {service: frida_control, guest: 27043}
```

An entry whose `host` is unset falls back to the stride allocation (for a well-known service) or a dedicated extra-pool slot (for an arbitrary one). See [`ports`](./config.md#ports) in the config reference for the full schema, and the [pinning ports](../guides/multi-instance.md#pinning-ports) section of the multi-instance guide for usage notes.

Beetroot pre-validates port collisions on every `create` and `apply` — if two instances both end up on the same host port (via stride, via overrides, via arbitrary mappings, or any mix), the command exits before staging with:

```
error: port 5555 (adb) collides with instance 'alpha' (which also uses 5555). Pin or remove one.
```

## Arbitrary guest→host mappings

The list isn't limited to the three well-known services. Add any number of extra mappings to forward additional in-guest ports to the host — e.g. an app's debug HTTP server:

```yaml
ports:
  - {service: adb, guest: 5555}
  - {service: frida, guest: 27042}
  - {service: frida_control, guest: 27043}
  - {guest: 8080, host: 9090}      # explicit host port
  - {service: metrics, guest: 9100}  # host unset → auto-allocated
```

An arbitrary entry whose `host` is unset is auto-allocated from a dedicated **extra-pool band** well clear of the ADB/Frida bands, at `40000 + index×10 + slot`, where `slot` is the entry's 0-based position among the instance's auto-allocated arbitrary entries. So at index 0 the first auto entry gets host `40000`, the second `40001`, and so on.

!!! warning "Per-instance arbitrary auto-allocation bound"
    Because the extra-pool slot shares the per-instance stride window of 10, an instance may auto-allocate at most **`STRIDE` (10) arbitrary entries** before the next index's band would overlap. Beetroot enforces this eagerly during allocation: the 11th host-unset arbitrary entry raises `PortCollisionError` (the slot would spill into the next index's window). Pin explicit `host` ports for entries beyond that bound.

!!! note "Arbitrary mappings under `binder: vm`"
    The `binder: vm` backend forwards only adb to the guest; arbitrary mappings beyond the well-known services are ignored there (`beetroot up` warns).

## Common pitfalls

### Partial override colliding with a stride sibling

An entry whose `host` is unset falls back to the stride-of-10 default for the instance's index. That means pinning one entry to a value that's already a sibling's stride default silently collides. The most common case is pinning `frida`'s host to a value that's already the stride default for `frida_control`:

```yaml
ports:
  - {service: frida, guest: 27042, host: 27043}  # at index 0 = frida_control's default!
  - {service: frida_control, guest: 27043}        # host unset → 27043
```

At index 0 the stride defaults are `frida=27042`, `frida_control=27043`. Pinning `frida`'s host to `27043` while `frida_control` stays on its `27043` stride default means both resolve to the same host port. Beetroot rejects the resolved list on `create`/`apply`/`ls`/`up` with:

```
error: resolved host ports collide on this instance: {27043: ['frida', 'frida_control']}.
Pin explicit host ports in beetroot.yaml's ports: list to avoid colliding
with stride-of-10 / extra-pool defaults.
```

The fix is to pin the colliding sibling's host explicitly too, or pick a value outside the `27042`/`27043 + index*10` window:

```yaml
ports:
  - {service: frida, guest: 27042, host: 27043}
  - {service: frida_control, guest: 27043, host: 27044}  # explicit — no stride fallback
```
