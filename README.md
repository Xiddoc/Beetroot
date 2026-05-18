# Beetroot 🫜

**The best Android research setup to beat root.**

[![CI](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml/badge.svg)](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml)
[![Docs](https://github.com/Xiddoc/Beetroot/actions/workflows/docs.yml/badge.svg)](https://xiddoc.github.io/Beetroot/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml)

Beetroot is a Docker-packaged rooted Android 14 environment — Magisk, LiteGapps, Houdini ARM translation, and Frida — wrapped with a Python CLI that runs as many persistent research "phones" as your host can afford, side by side. Each phone is a self-contained directory anywhere on disk: its own `beetroot.yaml`, its own `/data`, its own ADB and Frida ports, its own resource caps. A cross-instance registry at `~/.config/beetroot/instances.json` tracks them all by name.

```
$ beetroot create alpha --preset stealth
$ beetroot up alpha
[beetroot] alpha up — ADB localhost:5555, Frida localhost:27042
$ beetroot ls
NAME   IDX  ADB             FRIDA            PATH         STATUS
alpha  0    localhost:5555  localhost:27042  ./alpha      running
```

## Quick start

```bash
uv tool install git+https://github.com/Xiddoc/Beetroot.git
beetroot setup                              # one-time: build the redroid base image
beetroot create alpha                       # creates ./alpha/ with beetroot.yaml
beetroot create beta --path ~/work/beta     # or wherever you want it
beetroot register ~/already-built-instance  # adopt an existing dir
beetroot up alpha
beetroot shell alpha
```

Variants for `beetroot setup`: `none`, `lite` (default), `full`, `mindthegapps`.

Hacking on Beetroot itself? `uv tool install .` from a checkout, or `uv sync` + `uv run beetroot <verb>`.

### Frida CLI (optional)

The host-side `frida` CLI is exposed via a `[frida]` extra. Install with `uv tool install 'beetroot[frida]'` to make `beetroot frida` work out of the box; plain installs omit it.

## What you get

- **Android 14** (redroid base, headless, low-FPS by default)
- **Magisk root** with Zygisk + denylist; GMS auto-denylisted
- **LiteGapps** + **Houdini** ARM-on-x86_64 translation
- **Frida server (opt-in, version-pinned per instance)** — declare a `frida:` block in `beetroot.yaml` (or start from the `with-frida` preset) and the CLI downloads, caches, and bind-mounts the matching `frida-server` into the container
- **Drop-in Magisk module flashing** via `beetroot.yaml`
- **`beetroot` CLI** — lifecycle (`create` / `register` / `up` / `down` / `destroy`), shell + module management, and a `setup` bootstrap. See the [CLI reference](https://xiddoc.github.io/Beetroot/reference/cli/) for every verb.

## Read the docs

Full documentation lives at <https://xiddoc.github.io/Beetroot/>.

| Page | What's there |
|------|--------------|
| [Installation](https://xiddoc.github.io/Beetroot/getting-started/installation/) | Prerequisites, install paths, the `[frida]` extra |
| [CLI reference](https://xiddoc.github.io/Beetroot/reference/cli/) | Every verb, every flag |
| [Configuration](https://xiddoc.github.io/Beetroot/reference/config/) | `beetroot.yaml` schema, presets, resource defaults |
| [Port allocation](https://xiddoc.github.io/Beetroot/reference/ports/) | Stride-of-10 mapping, overrides |
| [Architecture](https://xiddoc.github.io/Beetroot/how-it-works/architecture/) | Image build, orchestration, boot flow |
| [Filesystem layout](https://xiddoc.github.io/Beetroot/how-it-works/filesystem/) | Per-instance state, what's gitignored |
| [Troubleshooting](https://xiddoc.github.io/Beetroot/troubleshooting/) | Common breakages and how to unstick them |

Contributors should read [CLAUDE.md](CLAUDE.md) for the development workflow (uv, ruff, mypy, pytest, 100% coverage gate).

## Credits

- [redroid](https://github.com/remote-android/redroid-doc) — the Android-in-a-container project Beetroot builds on.
- [`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) — the patcher that bakes Magisk, gapps, and Houdini into redroid images.
- [Magisk](https://github.com/topjohnwu/Magisk), [Frida](https://frida.re/), [LSPosed/Shamiko](https://github.com/LSPosed) — the building blocks Beetroot packages together.
