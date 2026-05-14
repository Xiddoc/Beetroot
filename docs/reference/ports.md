# Port Allocation

Beetroot assigns ports by **index** using a stride-of-10 scheme. The index is allocated when you run `beetroot create` (lowest free non-negative integer wins) and freed when you run `beetroot destroy`. Within an instance's lifetime, its ports never change.

## Port table

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
NAME          IDX  ADB                   FRIDA                 STATUS
alpha         0    localhost:5555        localhost:27042       running
bravo         1    localhost:5565        localhost:27052       running
```

## Getting ports programmatically

```bash
eval $(beetroot env alpha)
echo "$ANDROID_DEVICE"  # localhost:5555
echo "$FRIDA_DEVICE"    # localhost:27042
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

Frida uses two consecutive ports per instance: the data port and the control port (data+1). The stride of 10 leaves 8 unused ports between instances as headroom — if Frida's port requirements change in a future version, there's room to absorb the change without a layout migration.

ADB at `5555 + N×10` follows the same stride so all three port families stay aligned by index.
