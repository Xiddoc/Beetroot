# Beetroot 🫜

**The best Android research setup to beat root.** _(Get it? Beat... root? It sounded funnier in my head)_

[![CI](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml/badge.svg)](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml)
[![Docs](https://github.com/Xiddoc/Beetroot/actions/workflows/docs.yml/badge.svg)](https://iliketo.party/Beetroot/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml)

Beetroot aims to simplify the process of raising lightweight rooted Android environments — worry less about how it's done, so you can focus on what you want to achieve.

Configure your environment quicker, with support for Magisk modules, LiteGapps, and Frida. Want to spin up a new phone with the same config? Everything is already in your config, just run `beetroot create` & `beetroot up` to raise it.

```
$ beetroot create alpha
$ beetroot up alpha
[beetroot] alpha up — ADB localhost:5555, Frida localhost:27042
$ beetroot ls
NAME   KIND     IDX  ADB             FRIDA            STATUS    PATH
alpha  redroid  0    localhost:5555  localhost:27042  running   /home/you/alpha
```

## Quick start

```bash
uv tool install git+https://github.com/Xiddoc/Beetroot.git
beetroot build                              # one-time: build the redroid base image
beetroot create alpha                       # creates ./alpha/ with beetroot.yaml
beetroot create beta --path ~/work/beta     # or wherever you want it
beetroot register ~/already-built-instance  # adopt an existing dir
beetroot up alpha
beetroot shell alpha
```

GApps intent for `beetroot build`: `none`, `minimal` (default), `full`. Pin a specific distribution with `--gapps-vendor litegapps|opengapps|mindthegapps`.

Hacking on Beetroot itself? `uv tool install .` from a checkout, or `uv sync` + `uv run beetroot <verb>`.

### Frida CLI (optional)

The host-side `frida` CLI is exposed via a `[frida]` extra. Install with `uv tool install 'beetroot[frida]'` to make `beetroot frida` work out of the box; plain installs omit it.

## What you get

- **Android 14 by default** (redroid base, headless, low-FPS) — pick 11, 12, 13, or 14 via `android.version` in `beetroot.yaml`
- **Magisk root** with Zygisk + denylist; GMS auto-denylisted
- **LiteGapps** + **Houdini** ARM-on-x86_64 translation
- **Frida server (opt-in, version-pinned per instance)** — declare a `frida:` block in `beetroot.yaml` (or copy [`examples/with-frida.yaml`](examples/with-frida.yaml) over the freshly-created file) and the CLI downloads, caches, and bind-mounts the matching `frida-server` into the container
- **Drop-in Magisk module flashing** via `beetroot.yaml`
- **`beetroot` CLI** — lifecycle (`create` / `register` / `up` / `down` / `destroy`), shell + module management, health checks (`status` / `doctor`), and a `build` bootstrap. See the [CLI reference](https://iliketo.party/Beetroot/reference/cli/) for every verb.
- **Already have a rooted phone?** `beetroot adopt <adb-serial>` registers it under the same registry — the same `beetroot shell` / `beetroot frida` / `beetroot module` verbs dispatch via the host `adb` CLI instead of compose. No on-disk container; the device is managed outside Beetroot.
- **Runs on binderless hosts.** redroid needs the kernel `binder` driver; the `binder: auto|host|vm` switch picks how that's satisfied. `auto`/`host` use the host kernel's binder (loading the module if needed), while `vm` boots redroid inside a QEMU micro-VM that ships its own binder-enabled kernel — for hardened/`nomodule` sandboxes where the host can't provide it. See [`examples/vm.yaml`](examples/vm.yaml).
- **Pluggable backends.** Beyond the in-tree redroid, adb, and vm backends, Beetroot ships a small extension surface so a third-party package can ship a custom backend (cloud-emulator service, network-adb gateway, …) in ~30 LOC + one `[project.entry-points."beetroot.backends"]` line. See [Adding a backend](https://iliketo.party/Beetroot/guides/adding-a-backend/) for the recipe.

## Read the docs

Full documentation lives at <https://iliketo.party/Beetroot/>.

| Page | What's there |
|------|--------------|
| [Installation](https://iliketo.party/Beetroot/getting-started/installation/) | Prerequisites, install paths, the `[frida]` extra |
| [CLI reference](https://iliketo.party/Beetroot/reference/cli/) | Every verb, every flag |
| [Configuration](https://iliketo.party/Beetroot/reference/config/) | `beetroot.yaml` schema, starter examples, resource defaults |
| [Port allocation](https://iliketo.party/Beetroot/reference/ports/) | Stride-of-10 mapping, overrides |
| [Architecture](https://iliketo.party/Beetroot/how-it-works/architecture/) | Image build, orchestration, boot flow |
| [Filesystem layout](https://iliketo.party/Beetroot/how-it-works/filesystem/) | Per-instance state, what's gitignored |
| [Python API](https://iliketo.party/Beetroot/reference/api/) | `from beetroot import Instance, Manager` — drive Beetroot programmatically |
| [Adding a backend](https://iliketo.party/Beetroot/guides/adding-a-backend/) | Ship a third-party device backend in ~30 LOC |
| [Migration](https://iliketo.party/Beetroot/guides/migration/) | Per-release schema-migration walkthroughs (v0.2→v0.3, v0.3→v0.4, v0.4→v0.6) |
| [Troubleshooting](https://iliketo.party/Beetroot/troubleshooting/) | Common breakages and how to unstick them |

Contributors should read [CLAUDE.md](CLAUDE.md) for the development workflow (uv, ruff, mypy, pytest, 100% coverage gate).
