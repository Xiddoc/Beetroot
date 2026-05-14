# Beetroot 🫜

**The best Android research setup to beat root.**

[![CI](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml/badge.svg)](https://github.com/Xiddoc/Beetroot/actions/workflows/ci.yml)
[![Docs](https://github.com/Xiddoc/Beetroot/actions/workflows/docs.yml/badge.svg)](https://xiddoc.github.io/Beetroot/)

Beetroot is a Docker-packaged rooted Android 14 environment — Magisk, LiteGapps, Houdini ARM translation, and Frida — wrapped with a Python CLI that lets you run **as many persistent research "phones" as your host can afford** side by side. Each phone has its own `/data`, its own ADB and Frida ports, its own resource caps, and a single `beetroot.yaml` config file that describes it. Commit the YAML and you have a reproducible build of the device.

```
$ beetroot create alpha --preset stealth
$ beetroot create bravo --preset default
$ beetroot up alpha bravo
[beetroot] alpha up — ADB localhost:5555, Frida localhost:27042
[beetroot] bravo up — ADB localhost:5565, Frida localhost:27052
$ beetroot ls
NAME          IDX  ADB                   FRIDA                 STATUS
alpha         0    localhost:5555        localhost:27042       running
bravo         1    localhost:5565        localhost:27052       running
```

---

## Table of contents

- [What you get](#what-you-get)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [The CLI](#the-cli)
- [`beetroot.yaml` — deterministic configs](#beetrootyaml--deterministic-configs)
- [How it works](#how-it-works)
- [Port allocation](#port-allocation)
- [Resource defaults](#resource-defaults)
- [Migrating from the single-instance layout](#migrating-from-the-single-instance-layout)
- [Snapshots / reset](#snapshots--reset)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)

---

## What you get

- **Android 14** (redroid base, headless, low-FPS by default)
- **Magisk root** — Zygisk + denylist enabled out of the box, GMS auto-denylisted
- **LiteGapps** — minimal Google services
- **Houdini** — ARM-on-x86_64 translation
- **Frida server, version-pinned per instance** — downloaded by the CLI on the host, bind-mounted in
- **`beetroot` CLI** — create, start, stop, snapshot, attach, list, migrate
- **Drop-in module flashing** — declare modules in `beetroot.yaml` (URL or local path, optional sha256) and they're staged + flashed on next boot

---

## Prerequisites

- Linux host with Docker + `docker compose` (redroid relies on host kernel features like binder and ashmem; macOS and Windows are not supported)
- [`uv`](https://github.com/astral-sh/uv) for the Python CLI
- `git`, `curl` (standard)
- ADB on the host: `adb` (most distros ship `android-tools`)
- Optional: Frida CLI (`pip install frida-tools`) if you want `beetroot frida` to work

The container itself runs `privileged: true` and publishes ADB + Frida on host ports.

---

## Quick start

```bash
# 1. One-time bootstrap: patches and builds the base redroid image with
#    Magisk + LiteGapps + Houdini, then builds the Beetroot layer.
#    Pass a variant to use a different GMS flavor:
#      ./scripts/setup.sh            → lite (LiteGapps, default)
#      ./scripts/setup.sh none       → no GMS at all
#      ./scripts/setup.sh full       → full Gapps
#      ./scripts/setup.sh mindthegapps → MindTheGapps
./scripts/setup.sh

# 2. Install the CLI's deps
uv sync

# 3. Create your first instance
uv run beetroot create alpha
uv run beetroot up alpha

# 4. Attach
uv run beetroot shell alpha
# or, if you want to drive it from your own scripts:
eval $(uv run beetroot env alpha)
adb connect "$ANDROID_DEVICE"
adb -s "$ANDROID_DEVICE" shell
```

To stop without losing data: `uv run beetroot down alpha`. To wipe completely: `uv run beetroot destroy alpha`.

> **Tip.** Stick `alias beetroot="uv run beetroot"` in your shell rc to drop the `uv run` prefix.

---

## The CLI

All commands accept `--help` for full flags. Common verbs:

| Verb       | What it does                                                              |
|------------|---------------------------------------------------------------------------|
| `create`   | Initialize `instances/<name>/`, allocate ports, stage Frida + modules     |
| `up`       | Start one or more instances (`docker compose up -d` per instance)         |
| `down`     | Stop one or more instances; **data preserved**                            |
| `restart`  | Stop then start one or more instances (shorthand for `down && up`)        |
| `destroy`  | Stop and delete `instances/<name>/`. Prompts unless `-y`                  |
| `ls`       | Table of instances (or `--json`)                                          |
| `logs`     | `docker compose logs` — `-f` to follow                                    |
| `shell`    | `adb connect` + `adb shell` to the right port automatically               |
| `env`      | Print eval-able `ANDROID_DEVICE=...` `FRIDA_DEVICE=...` exports           |
| `frida`    | Wrap `frida -H localhost:<frida_port>` for the right instance             |
| `module`   | Append a Magisk module to `beetroot.yaml` and re-stage modules/           |
| `apply`    | After editing `beetroot.yaml`, re-render `.env` + re-stage everything     |
| `migrate`  | One-shot: move legacy `data/`, `data2/`, `data3/` → `instances/<name>/`   |

`up`, `down`, and `restart` accept `--all` to act on every registered instance at once:

```bash
beetroot up --all
beetroot down --all
beetroot restart --all
```

Passing `--all` together with explicit names is an error. Passing neither is also an error.

A typical session:

```bash
beetroot create research-clean --preset default
beetroot create research-stealth --preset stealth
beetroot up research-clean research-stealth

# Check status
beetroot ls

# Connect from your own tooling
eval $(beetroot env research-clean)
adb -s "$ANDROID_DEVICE" install ./target.apk
frida -H "$FRIDA_DEVICE" -n com.target.app

# Add a module on the fly (--sha256 is optional but recommended for URL sources)
beetroot module research-clean ./local-modules/MyHook.zip
beetroot module research-clean https://example.com/Mod.zip --sha256 abc123…
beetroot restart research-clean
```

---

## `beetroot.yaml` — deterministic configs

Every instance has an `instances/<name>/beetroot.yaml` that fully describes the device. Edit it, run `beetroot apply <name>`, restart, and the change takes effect. Commit the YAML to share a reproducible config with collaborators.

**Only `android.version` is required.** All other sections are optional and default to the values shown below. A minimal YAML is just two lines:

```yaml
android:
  version: 14   # 11, 12, 13, or 14
```

The full configurable shape (all fields optional except `android.version`):

```yaml
android:
  version: 14           # required — determines the redroid base image
  gapps: lite           # optional — none | lite (default) | full | mindthegapps

display:                # optional — defaults shown
  width: 540
  height: 960
  fps: 3
  gpu_mode: host

resources:              # optional — defaults shown
  mem: 3g
  cpus: 2.0
  shm: 256m

frida:                  # optional — set to `null` to disable; omit to use the default version
  version: "16.4.10"

modules:                # optional — Magisk modules to flash on boot
  - url: https://github.com/LSPosed/.../Shamiko-v0.7.4-426-release.zip
    sha256: <optional but recommended>
  - path: ./local-modules/MyResearchHook.zip

stealth:                # optional
  denylist:
    - com.google.android.gms
    - com.google.android.gms.unstable
    - com.android.vending
```

Three presets ship in `presets/`:

- **`default.yaml`** — cheap baseline, no extra modules, GMS denylist on.
- **`stealth.yaml`** — adds Shamiko (turns Magisk's denylist into a true allowlist hide) and a wider denylist.
- **`no-gapps.yaml`** — Android 14 without any GMS. Requires `./scripts/setup.sh none` to have been run first.

Use any of these via `beetroot create <name> --preset <preset>`.

---

## How it works

### Image build

Single-stage `docker/Dockerfile` from the redroid base image. The only things added are `entrypoint.sh` and `stealth.rc` (`COPY`'d in). SQL queries against the Magisk DB use `magisk --sqlite`, so there's no separate sqlite binary to ship. **Frida is not in the image** — it lives at `instances/<name>/frida-server` on the host and is bind-mounted in.

The base image is derived from `android.version` and `android.gapps` in `beetroot.yaml` (e.g. `version: 14, gapps: lite` → `redroid/redroid:14.0.0_litegapps_houdini_magisk`). It is produced by the external [`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) patcher, which `scripts/setup.sh <variant>` runs once per GMS variant. The tag is passed to `docker compose build` via the `BASE_IMAGE` build arg.

### Instance orchestration

The CLI never generates new compose files. There's exactly one `compose.yaml`, parameterized via env vars. Each instance is a separate Docker project:

```bash
docker compose -p alpha -f compose.yaml --env-file instances/alpha/.env up -d
```

`-p alpha` namespaces the project — `compose down` of one instance can't clobber another. The CLI just wraps that command line.

### Boot

Inside the container, redroid's default `/init` runs as PID 1. Android `init` parses `stealth.rc` (placed at `/system/etc/init/stealth.rc` at build time) and triggers `entrypoint.sh` on `sys.boot_completed=1` under the `u:r:magisk:s0` SELinux context.

`entrypoint.sh`:

1. Waits for `/data/adb/magisk.db` to exist.
2. Writes directly to that DB to enable Zygisk + denylist and add the GMS processes.
3. Walks `/flash_dir` and `magisk --install-module`s every zip.
4. Launches `/data/local/tmp/frida-server &` if it's executable.

The trailing `wait` keeps the script alive so its child Frida process stays attached for log inspection.

### Per-instance state

```
instances/<name>/
├── beetroot.yaml      # source of truth (you can commit this)
├── .env               # generated; consumed by compose
├── data/              # bind mount → /data
├── modules/           # bind mount → /flash_dir (read-only)
└── frida-server       # bind mount → /data/local/tmp/frida-server
```

`beetroot apply <name>` re-renders the `.env`, re-downloads any modules whose URLs/SHAs changed, and re-stages the Frida binary if you bumped `frida.version`.

---

## Port allocation

Stride-of-10 by index. Index is allocated on `create` (lowest free wins) and freed on `destroy`. The mapping:

| Index | ADB port  | Frida port | Frida control |
|-------|-----------|------------|---------------|
| 0     | 5555      | 27042      | 27043         |
| 1     | 5565      | 27052      | 27053         |
| 2     | 5575      | 27062      | 27063         |
| N     | 5555+N×10 | 27042+N×10 | 27043+N×10    |

`beetroot ls` always shows the current mapping. `beetroot env <name>` emits eval-able exports for use in other scripts.

---

## Resource defaults

Tuned from real measurements: each idle Android-14 + GMS + Magisk + Frida instance uses **~1.2 GB / <5% CPU**. Defaults:

| Knob          | Default | Why                                     |
|---------------|---------|-----------------------------------------|
| `mem`         | `3g`    | 2× idle, survives a Frida-instrumented app |
| `cpus`        | `2.0`   | One core for Android, one for Frida     |
| `shm`         | `256m`  | redroid GPU passthrough uses /dev/shm   |
| `pids_limit`  | `4096`  | Android forks aggressively              |

Three instances at these defaults = **9 GB / 6 vCPU** committed. Override per-instance by editing `resources:` in `beetroot.yaml` and running `beetroot apply`. Remember: you usually want "cheap" optimizations (lower FPS, smaller resolution, host GPU) over higher RAM/CPU caps.

---

## Migrating from the single-instance layout

If you already have `data/`, `data2/`, `data3/` from the old `docker-compose.yml`-only setup, run:

```bash
uv run beetroot migrate
```

This **moves** (renames, doesn't copy) the directories into `instances/{alpha,bravo,charlie}/data/`, generates a `beetroot.yaml` for each from the default preset, and registers them at indices 0/1/2. Stop the legacy containers first if any are still running.

Note: the new ADB ports are 5555 / 5565 / 5575 (not the old 5555 / 6555 / 7555). The new stride-10 scheme is more port-economical. Any tool that hardcoded the old ports needs updating.

---

## Snapshots / reset

There's no `beetroot snapshot` verb — the data lives at a known host path, so a single shell command does it:

```bash
# Snapshot
beetroot down alpha
cp -a instances/alpha/data instances/alpha/data.clean

# Restore
beetroot down alpha
rm -rf instances/alpha/data
cp -a instances/alpha/data.clean instances/alpha/data
beetroot up alpha

# Or, for a fresh boot from scratch:
beetroot destroy -y alpha
beetroot create alpha --preset default
```

For tar+zstd of large data dirs, `tar --zstd -cf snap.tar.zst -C instances/alpha data` works fine.

---

## Project layout

```
android-emulator/                # the Beetroot project (this dir)
├── README.md
├── CLAUDE.md                    # project conventions for contributors
├── pyproject.toml               # beetroot CLI package
├── compose.yaml                 # ONE templated service, all params via .env
├── docker/
│   ├── Dockerfile               # single-stage, only static sqlite3 baked in
│   ├── entrypoint.sh            # runs INSIDE Android via stealth.rc
│   └── stealth.rc               # Android init trigger
├── presets/                     # checked-in starter configs
│   ├── default.yaml
│   ├── stealth.yaml
│   └── no-gapps.yaml
├── scripts/
│   └── setup.sh                 # one-time base-image patcher
├── src/beetroot/                # CLI package
│   ├── cli.py
│   ├── config.py                # beetroot.yaml schema (pydantic)
│   ├── ports.py                 # stride-10 allocator
│   ├── registry.py              # instances.json with file lock
│   ├── compose.py               # subprocess wrappers around docker compose
│   ├── frida_dl.py              # download + cache frida-server
│   ├── modules_dl.py            # fetch + sha256-verify Magisk module zips
│   └── paths.py                 # filesystem layout
├── instances/                   # gitignored runtime state (beetroot.yaml inside is NOT)
└── instances.json               # gitignored registry
```

Non-obvious notes:

- `entrypoint.sh` runs inside Android with `/system/bin/sh` — toybox shell, no GNU coreutils, no bash.
- `stealth.rc` is Android init syntax, not arbitrary text. Don't refactor unless you know what `exec_background u:r:magisk:s0` means.
- `compose.yaml` uses top-level `mem_limit:` / `cpus:` / `shm_size:` — the `deploy:` form is Swarm-only and silently ignored by `docker compose up`.
- `instances/<name>/beetroot.yaml` is **not** gitignored. Commit it if you want a reproducible setup; everything else under `instances/<name>/` is local-only.

---

## Troubleshooting

**`docker compose up` fails with binder / ashmem errors.**
Your host kernel is missing redroid's required modules. On Debian/Ubuntu: `sudo apt install linux-modules-extra-$(uname -r)` and `sudo modprobe binder_linux ashmem_linux`.

**`adb connect` succeeds but `adb shell` hangs.**
First boot takes 30–60 seconds. `beetroot logs <name> -f` shows the entrypoint output once Android init has fired (`[*] Android boot detected. Applying Stealth Configuration...`).

**Magisk shows installed but Zygisk/denylist is off.**
The DB writes happen only after `/data/adb/magisk.db` exists. If you mounted a `data/` from a Magisk-less image, run `beetroot destroy -y <name>` and recreate.

**Frida can't see processes.**
- Confirm the binary is staged: `ls -lh instances/<name>/frida-server` (should be ~10 MB and executable).
- Confirm it's running: `beetroot shell <name>` then `ps -A | grep frida`.
- Check `beetroot logs <name>` for download / launch errors from `entrypoint.sh`.

**`beetroot module` added a zip but it didn't get flashed.**
`entrypoint.sh` only iterates `/flash_dir/*.zip` once at boot. Re-run `beetroot restart <name>` after `module`.

**`beetroot create` fails with "preset not found".**
Run from the project root (`android-emulator/`). The CLI looks for `presets/` relative to the package install location.

---

## Credits

- [redroid](https://github.com/remote-android/redroid-doc) — the Android-in-a-container project this is built on.
- [`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) — the patcher that bakes Magisk, gapps, and Houdini into redroid images.
- [Magisk](https://github.com/topjohnwu/Magisk), [Frida](https://frida.re/), [LSPosed/Shamiko](https://github.com/LSPosed) — the building blocks Beetroot just packages together.
