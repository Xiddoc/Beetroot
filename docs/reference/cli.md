# CLI Reference

All Beetroot subcommands. Every verb accepts `--help` for full usage.

After `uv tool install`, invocations are plain `beetroot <verb>` — the tool venv puts `beetroot` directly on your `PATH`. (Contributors hacking on Beetroot from an editable `uv sync` checkout use `uv run beetroot <verb>` instead; see [CLAUDE.md](https://github.com/Xiddoc/Beetroot/blob/main/CLAUDE.md).)

Beetroot's path model is Docker-inspired: an instance is any directory on disk containing a `beetroot.yaml`. The CLI discovers the current instance by walking up from `cwd` like `git` walks up to find `.git`. The cross-instance registry — name → absolute path — lives at `~/.config/beetroot/instances.json` (respects `XDG_CONFIG_HOME`).

---

## `create`

Initialize a new instance.

```
beetroot create <name> [--preset PRESET] [--path DIR] [--from-data PATH]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `name` | positional | Instance name (used as the Docker project name and the default directory name) |
| `--preset` | string | Bundled preset name. Default: `default` |
| `--path` | path | Where to create the instance directory. Default: `./<name>`. Resolved against `cwd`. |
| `--from-data` | path | Copy an existing data directory as the new instance's `/data`. |

**What it does:**

1. Validates the name isn't already registered.
2. Loads the bundled preset.
3. Creates the instance directory and writes `beetroot.yaml` into it.
4. Allocates the lowest free port index.
5. Registers `name → absolute_path` in `~/.config/beetroot/instances.json`.
6. If `--from-data` is given, copies the directory into `<instance>/data/`.
7. Renders `.env`, downloads the Frida binary, downloads + stages modules.

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
beetroot up <name> [<name> ...] [--build]
```

| Argument / Flag | Type | Description |
|----------------|------|-------------|
| `names` | positional (one or more) | Instance names to start |
| `--build` | flag | Rebuild the Docker image before starting. Use after changing `docker/Dockerfile`. |

Runs `docker compose -p <name> -f <bundled-template> --project-directory <instance-dir> --env-file <instance-dir>/.env up -d` for each instance. The bundled template lives inside the `beetroot` wheel, not at any project root.

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

Runs `docker compose down`. The instance's `data/` directory is untouched.

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
    This deletes the entire instance directory including `/data`. There is no undo. Use `beetroot down` to stop without deleting.

Steps:

1. (Optional) Prompts for confirmation unless `-y`.
2. Runs `docker compose down -v` to remove the container and any named volumes.
3. Deletes the instance directory with `shutil.rmtree`.
4. Removes the entry from the registry, freeing the port index.

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
NAME          IDX  ADB                   FRIDA                 STATUS        PATH
alpha         0    localhost:5555        localhost:27042       running       /home/you/alpha
bravo         1    localhost:5565        localhost:27052       exited        /tmp/scratch/bravo
```

**JSON output (abbreviated):**

```json
{
  "alpha": {
    "path": "/home/you/alpha",
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
| `source` | positional | `https://` or `http://` URL, or a path to a `.zip` (relative paths resolve against the instance directory). |

The module is appended to the instance's `beetroot.yaml` and immediately staged into its `modules/` directory. Restart to flash:

```bash
beetroot down <name> && beetroot up <name>
```
