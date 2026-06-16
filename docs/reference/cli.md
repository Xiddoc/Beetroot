# CLI Reference

All Beetroot subcommands. Every verb accepts `--help` for full usage. The
CLI is built on [Typer](https://typer.tiangolo.com/), so `--help` renders
as boxed sections with color (via Rich); flag and argument tables in this
reference mirror the same shape.

After `uv tool install`, invocations are plain `beetroot <verb>` — the tool venv puts `beetroot` directly on your `PATH`. (Contributors hacking on Beetroot from an editable `uv sync` checkout use `uv run beetroot <verb>` instead; see [CLAUDE.md](https://github.com/Xiddoc/Beetroot/blob/main/CLAUDE.md).)

Beetroot's path model is Docker-inspired: an instance is any directory on disk containing a `beetroot.yaml`. The CLI discovers the current instance by walking up from `cwd` like `git` walks up to find `.git`. The cross-instance registry — name → absolute path — lives at `~/.config/beetroot/instances.json` (respects `XDG_CONFIG_HOME`).

## Top-level flags

| Flag | Description |
|------|-------------|
| `--install-completion` | Install shell completion for the current shell (auto-detected). Run once per shell. |
| `--show-completion` | Print the completion script without installing it. |
| `--help` | Render the top-level help. |

See [Installation → Shell completion](../getting-started/installation.md#shell-completion) for the recommended setup.

## Exit codes

This is the stable v1.0 contract. Scripts wrapping the CLI can rely on these codes:

| Code | Meaning |
|------|---------|
| `0`  | Success |
| `1`  | Not found / domain error (instance not in registry, file missing, network error, etc.) |
| `2`  | Capability error — the backend does not support the requested verb (e.g. `up` against an adb-adopted device) |

---

## `create`

Initialize a new instance.

```
beetroot create <name> [--path DIR] [--from-data PATH]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `name` | positional | Instance name (used as the Docker project name and the default directory name) |
| `--path` | path | Where to create the instance directory. Default: `./<name>`. Resolved against `cwd`. |
| `--from-data` | path | Copy an existing data directory as the new instance's `/data`. |

**What it does:**

1. Validates the name isn't already registered.
2. Creates the instance directory and writes a minimal `beetroot.yaml` into it (`api_version` + `android.version`; every other field falls back to schema defaults). To start from a richer baseline, copy one of the [example YAMLs](../guides/examples.md) over the generated file and run `beetroot apply <name>`.
3. Allocates the lowest free port index.
4. Registers `name → absolute_path` in `~/.config/beetroot/instances.json`.
5. If `--from-data` is given, copies the directory into `<instance>/data/`.
6. Renders `.env`, downloads the Frida binary, downloads + stages modules.

**Output:**

```
[beetroot] created alpha at /home/you/alpha (index 0, ADB localhost:5555, Frida localhost:27042)
[beetroot] next: beetroot up alpha
```

---

## `register`

Adopt an existing instance directory under the global registry.

```
beetroot register <path> [--name NAME]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `path` | positional | Path to a directory containing `beetroot.yaml` |
| `--name` | string | Registry name (default: basename of the path) |

Useful for picking up an instance dir cloned from a teammate, or after recovering from a corrupted registry. Allocates a port index just like `create`.

---

## `adopt`

Adopt a rooted Android device (real phone, third-party emulator, `adb connect`-ed network device) that's already reachable via the host `adb` CLI. Unlike `create`/`register`, no on-disk instance directory is made — the device is managed outside Beetroot. The adopted instance gets its own port index, so a follow-up `beetroot frida <name>` picks the same port a redroid instance with the same index would have got.

```
beetroot adopt <serial> [--name NAME] [--verify]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `serial` | positional | adb serial (e.g. `emulator-5554`, `192.168.1.10:5555`) |
| `--name` | string | Registry name. Defaults to `adb-<serial>` (lowercased, colons folded to hyphens, truncated to 24 chars). Required for IPv4-shaped serials (the default-name builder leaves dots in place and the registry-name grammar rejects them). |
| `--verify`, `-V` | flag | Check that the serial is listed in `adb devices` as `device` before writing the registry row. If not found, exits 1 without registering. Default: off (allows registering a device before it connects). |

Verbs that need an on-disk container (`up`, `down`, `restart`, `apply`, `destroy`, `snapshot`) raise `BackendCapabilityError` against an adopted device and exit with code 2 — distinct from the standard "instance not found" exit 1, so wrapping scripts can distinguish. Use `beetroot shell <name>` / `beetroot frida <name>` / `beetroot module <name>` for the universal verbs.

Adopted devices show up in `beetroot ls` like any other instance — `KIND` is `adb`, the ADB column shows the serial, and PATH is `-` (no on-disk directory). See [`ls`](#ls).

---

## `apply`

Re-render `.env` and re-stage Frida + modules from an edited `beetroot.yaml`.

```
beetroot apply <name>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |

Run this after editing the instance's `beetroot.yaml`. It re-downloads any modules whose URLs or sha256s changed, re-downloads Frida if the version changed, and re-renders `.env`. Idempotent — safe to run multiple times.

After `apply`, restart to pick up the changes:

```bash
beetroot down <name> && beetroot up <name>
```

---

## `up`

Start one or more instances.

```
beetroot up <name> [<name> ...]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `names` | positional (one or more) | Instance names to start |
| `--all` | flag | Act on every registered instance. |

Runs `docker compose -p <name> -f <bundled-template> --project-directory <instance-dir> --env-file <instance-dir>/.env up -d` for each instance. The bundled template lives inside the `beetroot` wheel, not at any project root.

!!! note "No auto-rebuild"
    `beetroot up` does not rebuild the Docker image. To rebuild before starting, run [`beetroot build`](#build) explicitly first — the verbs are decoupled so `up` stays fast and predictable.

**Output:**

```
[beetroot] alpha up — ADB localhost:5555, Frida localhost:27042
```

---

## `down`

Stop one or more instances. Data is preserved.

```
beetroot down <name> [<name> ...]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `names` | positional (one or more) | Instance names to stop |
| `--all` | flag | Act on every registered instance. |

Runs `docker compose down`. The instance's `data/` directory is untouched.

When using `--all`, instances backed by non-Lifecycle backends (e.g. adb-adopted devices) are skipped with a one-line advisory to stderr — only redroid instances are stopped. Orphan or unresolvable registry rows (a redroid instance whose `beetroot.yaml` was deleted, or an unknown backend kind) are likewise skipped with a one-line advisory rather than aborting the whole fan-out. Explicit single-name invocations still raise a clear error.

**Output:**

```
[beetroot] alpha down (data preserved)
```

---

## `restart`

Stop then start one or more instances. Useful for picking up Magisk-module changes or a fresh `apply`.

```
beetroot restart <name> [<name> ...]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `names` | positional (one or more) | Instance names to restart |
| `--all` | flag | Act on every registered instance. |

Equivalent to running `beetroot down <name>` followed by `beetroot up <name>` for each named instance, but issued as a single verb.

**Output:**

```
[beetroot] alpha restarted
```

---

## `destroy`

Stop and permanently delete an instance and all its data.

```
beetroot destroy <name> [-y]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `name` | positional | Instance name |
| `-y`, `--yes` | flag | Skip the confirmation prompt |

!!! warning "Destructive"
    This deletes the entire instance directory including `/data`. There is no undo. Use `beetroot down` to stop without deleting.

Steps:

1. (Optional) Prompts for confirmation unless `-y`.
2. Runs `docker compose down -v` to remove the container and any named volumes.
3. Deletes the instance directory with `shutil.rmtree`.
4. Removes the entry from the registry, freeing the port index.

---

## `forget`

Deregister an instance from the registry without touching its host directory.

```
beetroot forget <name>
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `name` | positional | Instance name to deregister |

Removes the registry row and frees its port index. No host-directory teardown, no `docker compose down`, no data deletion. Works for both redroid and adb-backed instances.

This is the companion to `beetroot adopt` — the safe way to remove an adb-adopted device from the registry when you no longer want Beetroot to track it. For redroid instances where you want to destroy the data too, use `beetroot destroy` instead.

**Output:**

```
[beetroot] forgot phone (registry row removed; host directory untouched)
```

---

## `ls`

List all known instances — every backend kind, so adb-adopted devices appear next to redroid containers.

```
beetroot ls [--json]
```

| Flag | Description |
|------|-------------|
| `--json` | Emit JSON instead of a table. Suitable for piping to `jq` or Python. |

Status is queried live, never cached: redroid rows from `docker compose ps`, adb rows from `adb devices` (`available` when the serial is listed in state `device`, `unavailable` otherwise).

**Table output:**

```
NAME          KIND     IDX  ADB                   FRIDA                 STATUS        PATH
alpha         redroid  0    localhost:5555        localhost:27042       running       /home/you/alpha
bravo         redroid  1    localhost:5565        localhost:27052       exited        /tmp/scratch/bravo
phone         adb      2    emulator-5554         localhost:27062       available     -
```

For adb rows the ADB column shows the device serial verbatim (the value `adb -s` targets — there is no `host:port` form), FRIDA shows the allocated host forward port for the row's index, and PATH is `-` because an adopted device has no on-disk instance directory.

**JSON output (abbreviated):**

```json
{
  "alpha": {
    "kind": "redroid",
    "path": "/home/you/alpha",
    "index": 0,
    "adb": "localhost:5555",
    "frida": "localhost:27042",
    "adb_address": "localhost:5555",
    "frida_address": "localhost:27042",
    "status": "running",
    "created_at": "2025-01-15T10:30:00+00:00"
  },
  "phone": {
    "kind": "adb",
    "index": 2,
    "serial": "emulator-5554",
    "adb_address": "emulator-5554",
    "frida_address": "localhost:27062",
    "is_available": true,
    "created_at": "2025-01-15T10:30:00+00:00"
  }
}
```

Adb-kind rows use the same shape as `beetroot status` for an adopted device: `serial` plus the Protocol-surface fields (`adb_address`, `frida_address`, `is_available`). The v0.3 back-compat keys (`path` / `adb` / `frida`) exist only on redroid rows.

---

## `logs`

Tail the Docker Compose logs for an instance.

```
beetroot logs <name> [-f]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `name` | positional | Instance name |
| `-f`, `--follow` | flag | Follow log output (equivalent to `docker compose logs -f`) |

Passes through directly to `docker compose logs`. Useful for watching `entrypoint.sh` output during boot:

```bash
beetroot logs alpha -f
# Watch for: [*] Android boot detected. Applying Beetroot configuration...
```

---

## `shell`

Open an interactive ADB shell into an instance, or run a one-shot command.

```
beetroot shell <name> [-- <args>...]
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |
| `args` | remainder | Optional extra args forwarded to `adb shell`. Use `-c 'cmd'` for non-interactive invocation. |

Calls `adb connect localhost:<adb_port>` then `adb -s localhost:<adb_port> shell [args...]`. Requires `adb` on your PATH.

Examples:

```bash
beetroot shell alpha            # interactive shell
beetroot shell alpha -c 'id'   # one-shot command
```

---

## `status`

Print a JSON snapshot of a single instance.

```
beetroot status <name>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |

Output is JSON to stdout (v0.4 has no human-readable mode — pipe to `jq`). Exits `0` on success; exits `1` if `name` is not in the registry.

Redroid-kind rows include `name`, `kind`, `index`, `created_at`, `ports`, `status`, `adb_address`, `frida_address`, `stealth_paths`, plus the v0.3 back-compat keys (`path`, `adb`, `frida`).

Adb-kind rows include `serial` instead of `absolute_path` and omit the redroid-only `ports.frida2` key.

---

## `doctor`

Run aggregated health checks for an instance.

```
beetroot doctor <name>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |

Output is one `<check>: pass|fail|skip [reason]` line per check. Exits `0` if every check passes; otherwise the exit code is the count of `fail` results (capped at 255). `skip` rows do not count toward the exit code.

Redroid checks: `compose.status`, `adb.connect`, `frida.handshake`, `magisk.zygisk`, `magisk.denylist.com.google.android.gms` (skipped if the package isn't in `magisk.denylist`).

Adb checks: `adb.serial`, `frida.handshake`, `magisk.zygisk`, `magisk.denylist.com.google.android.gms`. `compose.status` is not applicable.

---

## `modes`

Survey the **host** and report which Beetroot run-modes it supports — *before* you create an instance or pick a `binder` mode. Host-level and instance-independent, unlike `doctor <name>` (which health-checks one existing instance).

```
beetroot modes [--json]
```

| Option | Type | Description |
|--------|------|-------------|
| `--json` | flag | Emit the support matrix as JSON instead of a table. |

Probes the host binder driver, KVM, and the QEMU / Docker / adb binaries, then reports each mode as `supported` / `needs-setup` / `unsupported` / `unknown` with a reason and remedy. Always exits `0` — it reports, it does not gate.

The modes reported are `redroid (binder: host / auto)`, `redroid (binder: vm, KVM accel)`, `redroid (binder: vm, TCG accel)`, and `adb backend (adopt remote device)`. See [Binder & run-modes](../how-it-works/binder-and-modes.md) for what each one needs and why.

---

## `frida`

Invoke the host-side `frida` CLI pre-configured for an instance.

```
beetroot frida <name> [frida_args ...]
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |
| `frida_args` | remainder | Arguments passed verbatim to `frida -H localhost:<frida_port>` |

Requires `frida` on your PATH. Install via `uv tool install 'beetroot[frida]'` (bundles `frida-tools` alongside Beetroot) or `uv tool install frida-tools` separately.

Examples:

```bash
beetroot frida alpha -n com.target.app
beetroot frida alpha -f com.target.app --no-pause -l script.js
beetroot frida alpha -ps    # list processes
```

If a forwarded flag conflicts with one of Beetroot's own options (rare, but possible if `frida-tools` ever ships a flag that overlaps with Beetroot's), use `--` as a separator. Everything after `--` is passed verbatim to the underlying `frida` CLI:

```bash
beetroot frida alpha -- -l script.js
```

---

## `module`

Install a Magisk module — append + re-stage (redroid), push (adb), or auto-install via root (adb, `--auto-install`).

```
beetroot module <name> <source>... [--sha256 HEX]... [--auto-install]
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |
| `source` | positional, repeatable | Redroid instances: `https://` or `http://` URL, or a path to a `.zip` (relative paths resolve against the instance directory). adb-adopted devices: path to an existing `.zip` on the host filesystem — URLs are not accepted, and relative paths resolve against your current working directory. Multiple sources are allowed with `--auto-install` only. |
| `--sha256` | option, repeatable | Expected sha256 hex digest of the zip. With `--auto-install`, repeat once per source (or omit entirely); a mismatching zip is never pushed. Without the flag, ignored for adb pushes (verify the hash yourself) and verified at staging time for redroid instances. |
| `--auto-install` | flag | adb backend only: install via root (`su -c magisk --install-module`) instead of the safe push-to-Downloads default. |

**Redroid instances:** the module is appended to the instance's `beetroot.yaml` and immediately staged into its `modules/` directory. Restart to flash:

```bash
beetroot down <name> && beetroot up <name>
```

**adb-adopted devices (default):** the zip is pushed to `/sdcard/Download/<name>.zip` and a one-line "install via the Magisk app → Modules tab" instruction is printed — no root interaction.

**adb-adopted devices (`--auto-install`):** each zip is pushed to a synthesized temp name under `/data/local/tmp/` (`beetroot-module-<N>.zip` — the local filename never reaches the device shell) and installed with `su -c magisk --install-module <zip>` (Magisk stages it into `/data/adb/modules_update/<id>/` for the next reboot); the temp zip is removed afterwards. Every module gets its own `ok:` (stdout) or `failed:` (stderr) report line; a failed module doesn't stop the rest, and the verb exits `1` if any module failed. Before anything is pushed, a cheap pre-flight probe diagnoses whole-device problems with a single friendly `error: ...` line + exit `1` instead of one identical failed row per module: an offline / disconnected / unauthorized device (reconnect, accept the USB-debugging prompt, and check `adb devices`), no usable root (`su` missing or denied root), or root without a usable `magisk` binary. Connectivity is decided by re-running `adb devices` for the serial, never by matching the probe's error text, so untrusted module stderr can't be mistaken for a disconnect. A device that genuinely drops offline mid-batch aborts the remaining modules with the same offline diagnosis (which names how many were skipped; rows completed before the abort are still reported). Host-side validation failures (missing/non-zip path, sha256 mismatch) always stay per-module `failed:` rows and never abort the batch. Redroid instances don't support the flag and exit `2`. See [the modules guide](../guides/modules.md#modules-on-adb-adopted-devices) for examples.

---

## `build`

Build the redroid base image and Beetroot layer for a chosen GMS variant. One-time bootstrap; re-run when you want a fresh image.

```
beetroot build [<gapps>]
```

| Argument | Type | Description |
|----------|------|-------------|
| `gapps` | positional, optional | GMS variant to bake into the base image. One of `none`, `lite` (default), `full`, `mindthegapps`. |

The verb:

1. Clones [`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) into `/tmp/redroid`.
2. Runs the patcher to produce a local Docker image (e.g. `redroid/redroid:14.0.0_litegapps_houdini_magisk`).
3. Runs `docker compose build` to layer `entrypoint.sh` and `stealth.rc` on top.

`beetroot up` no longer accepts a `--build` flag — to rebuild before starting, run `beetroot build` explicitly first.

---

## `snapshot`

Pack an instance's host-side state into a `.tar.zst` archive.

```
beetroot snapshot <name> [-o <archive>]
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name to snapshot. |
| `-o`, `--output` | flag | Archive path (default: `./<name>.tar.zst`). The `.tar.zst` extension is appended automatically if you omit it. |

Stop the instance first (`beetroot down <name>`) — `tar`-ing live `data/` produces an inconsistent archive. The archive excludes `.env` (it's regenerated on the next `apply`). See [Snapshots](../guides/snapshots.md) for the round-trip workflow and the `path_layout` forward-compat story.

---

## `restore`

Unpack a snapshot archive into a new instance and register it.

```
beetroot restore <archive> [--name <name>] [--path <dir>] [--force]
```

| Argument | Type | Description |
|----------|------|-------------|
| `archive` | positional | Path to a `.tar.zst` snapshot archive. |
| `--name` | flag | Registry name for the restored instance (default: the name recorded in the manifest). |
| `--path` | flag | Directory to extract into (default: `./<name>`). |
| `--force` | flag | Wipe a non-empty destination directory before extracting. |

A fresh port index is allocated — the source's index is never reused, so the original and the restored instance can run concurrently if both directories still exist. After restore, run `beetroot apply <new-name>` to regenerate `.env`, then `beetroot up <new-name>`.
