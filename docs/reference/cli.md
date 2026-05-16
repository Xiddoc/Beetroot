# CLI Reference

All Beetroot subcommands. Every verb accepts `--help` for full usage.

After `uv tool install`, invocations are plain `beetroot <verb>` — the tool venv puts `beetroot` directly on your `PATH`. (Contributors hacking on Beetroot from an editable `uv sync` checkout use `uv run beetroot <verb>` instead; see [CLAUDE.md](https://github.com/Xiddoc/Beetroot/blob/main/CLAUDE.md).)

---

## `create`

Initialize a new instance.

```
beetroot create <name> [--preset PRESET] [--from-data PATH]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `name` | positional | Instance name (used as the Docker project name and directory name under `instances/`) |
| `--preset` | string | Preset name to load from `presets/`. Default: `default` |
| `--from-data` | path | Copy an existing data directory as the new instance's `/data`. Useful for cloning an existing Android environment. Path is relative to the project root. |

**What it does:**

1. Validates the name isn't already registered.
2. Loads the named preset from `presets/<preset>.yaml`.
3. Creates `instances/<name>/` and writes `beetroot.yaml`.
4. Allocates the lowest free port index.
5. Registers the instance in `instances.json`.
6. If `--from-data` is given, copies the directory into `instances/<name>/data/`.
7. Calls `_stage_instance`: renders `.env`, downloads Frida binary, downloads and stages modules.

**Output:**

```
[beetroot] created alpha (index 0, ADB localhost:5555, Frida localhost:27042)
[beetroot] next: beetroot up alpha
```

---

## `apply`

Re-render `.env` and re-stage Frida + modules from an edited `beetroot.yaml`.

```
beetroot apply <name>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |

Run this after editing `instances/<name>/beetroot.yaml`. It re-downloads any modules whose URLs or sha256s changed, re-downloads Frida if the version changed, and re-renders `.env`. Idempotent — safe to run multiple times.

After `apply`, restart to pick up the changes:

```bash
beetroot down <name> && beetroot up <name>
```

---

## `up`

Start one or more instances.

```
beetroot up <name> [<name> ...] [--build]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `names` | positional (one or more) | Instance names to start |
| `--build` | flag | Rebuild the Docker image before starting. Use after changing `docker/Dockerfile`. |

Runs `docker compose -p <name> -f compose.yaml --env-file instances/<name>/.env up -d` for each instance.

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

| Argument | Type | Description |
|----------|------|-------------|
| `names` | positional (one or more) | Instance names to stop |

Runs `docker compose -p <name> down`. The `instances/<name>/data/` directory is untouched.

**Output:**

```
[beetroot] alpha down (data preserved)
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
    This deletes `instances/<name>/` including `/data`. There is no undo. Use `beetroot down` to stop without deleting.

Steps:

1. (Optional) Prompts for confirmation unless `-y`.
2. Runs `docker compose down -v` to remove the container and any named volumes.
3. Deletes `instances/<name>/` with `shutil.rmtree`.
4. Removes the entry from `instances.json`, freeing the port index.

---

## `ls`

List all known instances.

```
beetroot ls [--json]
```

| Flag | Description |
|------|-------------|
| `--json` | Emit JSON instead of a table. Suitable for piping to `jq` or Python. |

Container status is queried live from `docker compose ps` — it's never cached.

**Table output:**

```
NAME          IDX  ADB                   FRIDA                 STATUS
alpha         0    localhost:5555        localhost:27042       running
bravo         1    localhost:5565        localhost:27052       exited
```

**JSON output (abbreviated):**

```json
{
  "alpha": {
    "index": 0,
    "adb": "localhost:5555",
    "frida": "localhost:27042",
    "status": "running",
    "created_at": "2025-01-15T10:30:00+00:00"
  }
}
```

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
# Watch for: [*] Android boot detected. Applying Stealth Configuration...
```

---

## `shell`

Open an interactive ADB shell into an instance.

```
beetroot shell <name>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |

Calls `adb connect localhost:<adb_port>` then `adb -s localhost:<adb_port> shell`. Requires `adb` on your PATH.

---

## `env`

Print eval-able environment variable exports for an instance.

```
beetroot env <name>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |

Output:

```bash
export ANDROID_DEVICE=localhost:5555
export FRIDA_DEVICE=localhost:27042
```

Use with `eval`:

```bash
eval $(beetroot env alpha)
adb -s "$ANDROID_DEVICE" install ./target.apk
frida -H "$FRIDA_DEVICE" -n com.target.app
```

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

---

## `module`

Append a Magisk module to `beetroot.yaml` and re-stage.

```
beetroot module <name> <source>
```

| Argument | Type | Description |
|----------|------|-------------|
| `name` | positional | Instance name |
| `source` | positional | `https://` or `http://` URL, or a local filesystem path to a `.zip` |

The module is appended to `instances/<name>/beetroot.yaml` and immediately staged into `instances/<name>/modules/`. Restart to flash:

```bash
beetroot down <name> && beetroot up <name>
```
