# CLAUDE.md

Project orientation for contributors working on Beetroot: what the repo is, how it's wired together, and where the code lives. The contributor/agent workflow (commands, uv-based dev loop, style rules, CI gates, editing gotchas) lives in `AGENTS.md`, imported below.

## What this repo is

Beetroot: a Docker-packaged rooted Android-14 sandbox (redroid + Magisk + optional GMS (LiteGapps by default) + Houdini) plus a Python CLI (`beetroot`) that lets researchers manage **multiple persistent "research phones"** at once. Each instance has its own `/data`, its own ADB + Frida ports, its own RAM/CPU caps, and its own `beetroot.yaml` config.

There is no application code here — the deliverable is the container image, the boot scripts that configure it, and the CLI that orchestrates instances.

## Contributor & agent workflow

The operational playbook — CLI commands, the uv-based development workflow (lint, type-check, tests, coverage, pre-commit, CI gates), and the gotchas to know before editing — lives in `AGENTS.md` and is imported here:

@AGENTS.md

## Documentation

The docs site lives under `docs/` and is built with [mkdocs-material](https://squidfunk.github.io/mkdocs-material/). The published site is at <https://iliketo.party/Beetroot/>.

To preview locally:

```bash
uv sync --group docs
uv run mkdocs serve  # http://127.0.0.1:8000
```

To build (without serving):

```bash
uv run mkdocs build --strict
```

The build output goes to `site/` (gitignored). The GitHub Actions workflow at `.github/workflows/docs.yml` deploys to GitHub Pages on every push to `master`.

## Architecture

**One bundled compose template, many projects.** Every instance shares the same compose template (shipped inside the wheel at `src/beetroot/templates/compose.yaml`). The CLI invokes compose with `-p <instance-name>` (separate Docker project per instance), `-f <bundled-template>`, `--project-directory <instance-dir>` (so the template's relative bind-mounts resolve correctly), and `--env-file <instance-dir>/.env` (per-instance ports, resources, display knobs). Project-per-instance gives true isolation — `docker compose -p alpha down` doesn't touch `bravo`.

**Single-stage Dockerfile** (`docker/Dockerfile`). Only the `docker/*.sh` helpers (`entrypoint.sh`, `magisk-path.sh`, `magisk-config.sh`, `magisk-env.sh`, `flash-modules.sh`, `activate-zygisk.sh`, `launch-frida.sh`) and `stealth.rc` are `COPY`'d into the redroid base — there is nothing else baked in. SQL queries against the Magisk DB use `magisk --sqlite` (Magisk ships with its own sqlite, so we don't need to ship a separate Bionic-built static binary). Frida is **not** in the image — it's bind-mounted per-instance (see below) so each phone can pin its own Frida version without rebuilding.

**Boot-time wiring is via Android init, not Docker.** There is no `ENTRYPOINT` — the container uses redroid's default boot. Android `init` parses `stealth.rc` (placed at `/system/etc/init/stealth.rc` at build time) and triggers `entrypoint.sh` on `sys.boot_completed=1` under the `u:r:magisk:s0` SELinux context. `entrypoint.sh` runs with `/system/bin/sh` (Android's toybox-derived shell) and waits for `/data/adb/magisk.db` to exist before doing anything.

**`entrypoint.sh` is glue that sources six helpers** — `magisk-path.sh` (prepends the directory that actually holds the `magisk` binary — `/sbin` on the redroid image — to `PATH`, because the Android init service that runs the entrypoint inherits init's default PATH which omits it; without this every helper's bare `magisk` call fails and `magisk-config.sh`'s daemon wait spins until it times out and aborts the boot), `magisk-config.sh` (waits for the Magisk daemon, then writes directly to its sqlite DB at `/data/adb/magisk.db` to enable Zygisk + denylist and enroll `com.google.android.gms` / `com.google.android.gms.unstable`, recording the prior `zygisk` value so the activation step knows if this boot newly enabled it), `magisk-env.sh` (populates `/data/adb/magisk` (MAGISKBIN) by copying the Magisk binaries from `/system/etc/init/magisk` and `busybox unzip`-ing the `assets/*.sh` scripts out of `magisk.apk` — without this `magisk --install-module` aborts with "Incomplete Magisk install" headlessly), `flash-modules.sh` (iterates every zip in `/data/adb/modules_update` and calls `magisk --install-module`), `activate-zygisk.sh` (on the boot that newly enabled Zygisk, runs `setprop ctl.restart zygote` so Zygisk injects and just-flashed Zygisk modules load — gated off on routine restarts), and `launch-frida.sh` (starts `/data/local/tmp/frida-server &` if it's executable). Every container-side path is read from a `BEETROOT_*` env var with a safe default, so the bundled compose template can override paths without editing helper code. The trailing `wait` keeps the entrypoint alive so logs stream to `docker compose logs`. See `docs/how-it-works/boot-scripts.md` for per-helper contracts.

**Per-instance state lives in the instance directory itself** (any path on disk that contains a `beetroot.yaml`):
- `beetroot.yaml` — the source of truth for this instance (display, resources, Frida version, modules, denylist). Commit it if you want a reproducible config.
- `.env` — generated from `beetroot.yaml`; consumed by compose. Re-rendered on `beetroot apply`.
- `data/` — **redroid backend only:** bind-mounted to `/data` inside the container; persists across restarts. Under `binder: vm` the guest's `/data` lives inside the guest rootfs (`/var/lib/redroid-data`, override with `BEETROOT_GUEST_DATA_DIR`), so this host-side `data/` dir is vestigial and not the live Android `/data`. This is why `beetroot snapshot`/`restore` are redroid-only — they pack/unpack `data/`, which holds nothing for a vm (or adb) instance (see issue #128).
- `modules/` — bind-mounted read-only to `/flash_dir`. The CLI mirrors `beetroot.yaml`'s `modules:` list into here on `apply`.
- `frida-server` — bind-mounted to `/data/local/tmp/frida-server`. Downloaded by the CLI from `github.com/frida/frida/releases` and decompressed on the host.

**Port allocation** (`src/beetroot/ports.py`): stride-of-10 by index. Index 0 → ADB 5555, Frida 27042/27043. Index 1 → ADB 5565, Frida 27052/27053. Etc. Index is stored in the cross-instance registry at `~/.config/beetroot/instances.json` (respects `$XDG_CONFIG_HOME`) and freed on `destroy`. Allocation reuses freed slots — `lowest_free_index`.

**Container status is queried live from `docker compose ps`**, never cached in the registry. The registry can't lie about runtime state.

## Binder runtime & CI

redroid runs the Android userspace against the **host kernel**, so it needs the kernel `binder` driver. Beetroot's `binder: auto|host|vm` switch picks how that's satisfied, following a **capability ladder**:

1. host binder present → used automatically;
2. `binder_linux` **loadable** as a module → loaded automatically;
3. `vm` + KVM → opt-in, near-native;
4. `vm` + TCG → opt-in, software emulation (~5–20× slower).

**On GitHub-hosted CI runners, binder is loadable (ladder rank 2)** via `.github/actions/provide-binder` (`apt-get install linux-modules-extra-$(uname -r)` + `modprobe binder_linux` + binderfs mount). So CI boots redroid on the **host** path — it does **not** need the `vm` backend. This is proven by the `e2e.yml` `tier1-stock-redroid` job, which boots stock redroid Android-14 on `ubuntu-latest` and drives it through Beetroot's adb backend.

The **`vm` (QEMU micro-VM) backend exists for hosts where binder is neither present nor loadable** — hardened / `nomodule` sandboxes (e.g. the Claude Code on the web execution environment). It ships its own binder-enabled kernel. See `docs/design/binderless-hosts-qemu-tcg.md` for the design and `docs/design/vm-rnd-log.md` for the validated TCG recipe + measurements.

**e2e boot tests are NOT a per-PR gate.** `e2e.yml` runs only on the nightly `schedule`, manual `workflow_dispatch`, or a PR carrying the **`e2e` label**. `tier2-beetroot-image` (build the Magisk image, boot it, then assert root / Zygisk / denylist / Frida) is a WIP scaffold and `continue-on-error` (non-blocking). The **`tier-vm-qemu`** tier builds the binder-enabled guest kernel + rootfs and boots redroid inside the `binder: vm` QEMU micro-VM, then drives it through the adb backend (asserting the `vm.process` / `vm.accel` doctor rows and the Frida-unsupported message); on hosted runners it runs under TCG (no `/dev/kvm`). The offline R&D that first validated the vm path lives in `docs/design/vm-rnd-log.md`.

## CLI internals

```
src/beetroot/
├── api.py         # OOP layer (Instance, Manager, DeviceBackend Protocol) — public surface
├── cli.py         # Typer verbs; bodies delegate to api.Instance/Manager
├── config.py      # beetroot.yaml schema (pydantic) + .env render
├── settings.py    # env-driven overrides (BEETROOT_* vars) via pydantic-settings
├── ports.py       # stride-10 allocator
├── registry.py    # instances.json, fcntl.flock guards mutations
├── compose.py     # subprocess wrappers around `docker compose`
├── frida_download.py    # download frida-server.xz, decompress (lzma), cache
├── modules_download.py  # fetch + sha256-verify Magisk module zips
├── snapshot.py    # pack/unpack instances as .tar.zst with manifest
├── builder.py     # one-time base-image build (`beetroot build`) + micro-VM kernel + rootfs assembly (`build_rootfs`)
└── paths.py       # single source of truth for filesystem layout
```

`api.py` composes the procedural modules — it doesn't replace them. The CLI verbs in `cli.py` stay as module-level `@app.command()` functions (Typer captures the function reference at import time, so wrapping verbs in a class would break dispatch), but each verb body is a 1-15 line shell that constructs an `api.Instance` or calls an `api.Manager` staticmethod. Programmatic users should `from beetroot import Instance, Manager, DeviceBackend`; contributors editing the CLI need the procedural modules too. The forward-looking design of the `DeviceBackend` Protocol (and how v0.4's `AdbDeviceBackend` will satisfy it) lives at `docs/design/device-backends.md`.

`paths.instance_root()` resolves to the directory containing `beetroot.yaml` via upward search from the current working directory (the `beetroot.yaml` is the marker — same model as git's `.git` and uv/pip's `pyproject.toml`). Running the CLI from a directory with no `beetroot.yaml` in any ancestor raises `paths.InstanceRootNotFoundError`, which `cli.main()` converts to a friendly `error: ...` and `exit 1`. The bundled compose template is resolved via `paths.bundled_compose_file()` (which uses `importlib.resources`), so the CLI works identically whether installed editable (`uv sync`) or as a tool (`uv tool install .`). The cross-instance registry lives at `paths.user_registry_file()`.

## What stays gitignored

Instance directories live anywhere on disk and are gitignored at the user's discretion. Within any instance dir: `data/`, `modules/`, `frida-server`, `.env` should be gitignored. `beetroot.yaml` is **not** ignored — it's a config the researcher may want to commit. The cross-instance registry (`~/.config/beetroot/instances.json`) is per-host and never tracked.
