# Binder & run-modes

This page is the single source of truth for **how Beetroot can drive a device,
what each mode requires, and how to tell which modes your host supports.** If
you read one page before choosing a `binder` setting, read this one.

!!! tip "Just want the answer for your machine?"
    Run **`beetroot modes`**. It probes this host and prints, for every mode,
    whether it's `supported` / `needs-setup` / `unsupported` / `unknown`, with a
    reason and a remedy. Everything below explains *why* it says what it says.

## Why binder is the whole story

redroid is a **container, not an emulator**: it runs Android's userspace
directly against the **host kernel** and ships no kernel of its own. Android's
`init`, `servicemanager`, and `zygote` all block at boot on the kernel
**binder** IPC driver (`/dev/binder`), and redroid provides no userspace
substitute. So:

* **binder is a kernel feature.** `privileged: true` is a Docker *permission* —
  it cannot conjure a kernel driver that isn't there. A privileged container on
  a binderless kernel still won't boot Android.
* If the host kernel can't provide binder, redroid can't boot **on that host**
  at all. Your options are then to give redroid its *own* kernel (the `vm`
  backend) or to stop booting locally and drive a device that lives elsewhere
  (the `adb` backend).

That single fact is what the two axes below are organised around.

## The two axes

Beetroot's run-mode is the product of two independent choices.

### Axis 1 — device backend

| Backend | What it does | Boots anything? |
|---------|--------------|-----------------|
| `redroid` (default) | Beetroot boots and manages a Dockerized redroid Android container. | Yes — needs binder + Docker. |
| `adb` (`beetroot adopt`) | Beetroot drives an **external** rooted device/emulator over adb. | No — the device runs elsewhere. |

### Axis 2 — the `binder` switch (redroid only)

Set on `beetroot.yaml` as a top-level `binder:` key. It selects *how* redroid
obtains the binder driver, following a **capability ladder** (cheapest/most
native first):

| `binder:` | How binder is provided | Speed | Needs |
|-----------|------------------------|-------|-------|
| `host` | The host kernel's own binder driver. Strict: `up` fails fast if absent. | Native | binder present **or** loadable |
| `auto` (default) | Same as `host`, but lenient: warns and starts anyway if binder is missing (and hints at `vm`). | Native | binder present **or** loadable |
| `vm` + **KVM** | redroid runs inside a QEMU micro-VM that ships its **own** binder kernel; hardware-accelerated. | Near-native | `/dev/kvm` + QEMU |
| `vm` + **TCG** | Same micro-VM, but pure software emulation (no KVM). | ~5–20× slower | QEMU only |

The ladder, in plain terms:

1. **host binder present** → used automatically.
2. **`binder_linux` loadable as a module** → `sudo modprobe binder_linux` and you're at rung 1.
3. **`vm` + KVM** → opt-in, near-native, for hosts with no host binder but with `/dev/kvm`.
4. **`vm` + TCG** → opt-in, software emulation, for hosts with **neither** host binder **nor** KVM (hardened / `nomodule` sandboxes — e.g. the Claude Code on the web execution environment).

!!! warning "`no /dev/kvm` does **not** mean `no VM`"
    The single most common mistake. KVM is only the *fast path* for the `vm`
    backend. The `vm` backend's entire reason to exist is **binderless,
    KVM-less hosts**: it falls back to **TCG** (software emulation) and boots
    redroid anyway, just slowly. A slow first boot under TCG is expected, not a
    hang. See the [QEMU/TCG design note](../design/binderless-hosts-qemu-tcg.md)
    and the [validated recipe + measurements](../design/vm-rnd-log.md).

## What each mode needs (and how `beetroot modes` decides)

`beetroot modes` probes the host binder driver, KVM, and the QEMU / Docker /
adb binaries, then classifies each mode:

### `redroid (binder: host / auto)`

| Verdict | When |
|---------|------|
| `supported` | binder is **ready** (`/dev/binder*` nodes exist, or `binder` is in `/proc/filesystems`) **and** the Docker CLI is present. |
| `needs-setup` | binder is **loadable** (`CONFIG_ANDROID_BINDER_IPC=m`/`=y` but not loaded) → `sudo modprobe binder_linux devices=binder,hwbinder,vndbinder`; **or** binder is ready but Docker isn't installed. |
| `unsupported` | the kernel has binder **compiled out** (`# CONFIG_ANDROID_BINDER_IPC is not set`) — no Docker flag can fix this. |
| `unknown` | binder isn't present and the kernel config couldn't be read (e.g. macOS, locked-down `/proc`). |

### `redroid (binder: vm, KVM accel)`

| Verdict | When |
|---------|------|
| `supported` | `/dev/kvm` is usable **and** QEMU is installed. (You still build the guest once: `beetroot build --vm-kernel`.) |
| `needs-setup` | `/dev/kvm` usable but QEMU missing → install `qemu-system-x86`. |
| `unsupported` | no usable `/dev/kvm` — use the TCG row instead, or move to a KVM-capable host/runner. |

### `redroid (binder: vm, TCG accel)`

| Verdict | When |
|---------|------|
| `supported` | QEMU is installed. Works with **no** host binder and **no** KVM. Build the guest once with `beetroot build --vm-kernel`. |
| `needs-setup` | QEMU missing → install `qemu-system-x86`, then `beetroot build --vm-kernel`. |

### `adb backend (adopt remote device)`

| Verdict | When |
|---------|------|
| `supported` | the `adb` client is installed. Needs **no** host kernel, binder, Docker, or KVM — it drives a device that lives elsewhere. Point it at one with `beetroot adopt <serial\|host:port>`. |
| `needs-setup` | the `adb` client isn't installed → install platform-tools. |

## How this relates to `beetroot doctor`

They answer different questions and you'll use both:

| | `beetroot modes` | `beetroot doctor <name>` |
|---|---|---|
| Scope | the **host** | one **existing instance** |
| When | *before* creating an instance / picking `binder` | *after* `up`, to debug a running instance |
| Needs an instance? | No | Yes |
| Answers | "what can this machine run?" | "is this instance healthy?" (adb, magisk, frida, `host.binder`, `vm.process`, `vm.accel`) |

`doctor`'s `host.binder` row reports the same binder probe `modes` uses, but
only in the context of a single instance; `modes` is the standalone,
pre-flight survey.

## Choosing a mode

```
Run `beetroot modes`.
├── `redroid (binder: host / auto)` supported     → use the default (fastest).
├── only needs-setup (loadable / no Docker)       → do the one-time step, re-run.
├── `vm, KVM` supported                            → set `binder: vm` (near-native).
├── only `vm, TCG` supported (no binder, no KVM)   → set `binder: vm` (slow but real),
│                                                     `beetroot build --vm-kernel` first.
└── nothing local works                            → drive a remote device:
                                                      `beetroot adopt <serial|host:port>`.
```

## See also

* [Running in CI / without kernel access](../guides/running-in-ci.md) — the same
  decision tree applied to CI runners and cloud sandboxes.
* [Binderless hosts (QEMU/TCG)](../design/binderless-hosts-qemu-tcg.md) — the
  `vm` backend's design.
* [Micro-VM R&D log](../design/vm-rnd-log.md) — the validated TCG build/boot
  recipe and measured timings.
