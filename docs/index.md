# Beetroot

**The best Android research setup to beat root.**

Beetroot is a Docker-packaged rooted Android 14 environment — Magisk, LiteGapps, Houdini ARM translation, and Frida — wrapped with a Python CLI that lets you run **as many persistent research "phones" as your host can afford** side by side. Each phone has its own `/data`, its own ADB and Frida ports, its own resource caps, and a single `beetroot.yaml` that fully describes it. Commit the YAML and you have a reproducible device config you can share with collaborators.

```
$ beetroot create alpha
$ beetroot create bravo
$ beetroot up alpha bravo
[beetroot] alpha up — ADB localhost:5555, Frida localhost:27042
[beetroot] bravo up — ADB localhost:5565, Frida localhost:27052
$ beetroot ls
NAME          IDX  ADB                   FRIDA                 STATUS        PATH
alpha         0    localhost:5555        localhost:27042       running       /home/you/alpha
bravo         1    localhost:5565        localhost:27052       running       /home/you/bravo
```

## What's included

| Component | Details |
|-----------|---------|
| **Android 14** | redroid base, headless, configurable resolution and FPS |
| **Magisk root** | Zygisk + denylist enabled out of the box; GMS auto-denylisted |
| **LiteGapps** | Minimal Google services (just enough for GMS-dependent apps) |
| **Houdini** | ARM-on-x86\_64 translation — run ARM-only APKs on a standard server |
| **Frida** | Opt-in per instance — declare a `frida:` block (or copy `examples/with-frida.yaml` over the generated config) and the host CLI downloads + bind-mounts a version-pinned `frida-server` |
| **`beetroot` CLI** | Create, start, stop, snapshot, restore, and list instances |
| **Module flashing** | Declare modules in YAML (URL or local path, optional sha256); flashed at next boot |

## Quick start

```bash
# 1. Install the CLI (puts `beetroot` on your PATH; no shell alias needed)
uv tool install git+https://github.com/Xiddoc/Beetroot.git

# 2. One-time bootstrap — patches and builds the base redroid image
beetroot build

# 3. Create and start your first research phone
beetroot create alpha
beetroot up alpha

# 4. Connect
beetroot shell alpha
```

See [Your First Instance](getting-started/first-instance.md) for the full walkthrough, or [Prerequisites](getting-started/prerequisites.md) if you haven't set up your host yet.
