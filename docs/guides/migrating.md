# Migrating from the Legacy Layout

Beetroot's current layout stores each instance under `instances/<name>/`. The older single-instance `docker-compose.yml`-only setup used bare `data/`, `data2/`, and `data3/` directories at the repo root. If you have those directories, `beetroot migrate` moves them into the new layout automatically.

## What it does

`beetroot migrate` maps the legacy directories to canonical instance names:

| Legacy directory | New instance name | Port index |
|-----------------|-------------------|------------|
| `data/`         | `alpha`           | 0 (ADB 5555) |
| `data2/`        | `bravo`           | 1 (ADB 5565) |
| `data3/`        | `charlie`         | 2 (ADB 5575) |

For each directory found, the migration:

1. **Moves** (renames, not copies) `data*/` into `instances/<name>/data/`.
2. Writes a default `beetroot.yaml` for the instance.
3. Registers the instance in `instances.json` with its new port index.
4. Stages Frida and modules per the default config.

## Running the migration

**Stop any running legacy containers first.** If the old `docker-compose.yml` has services using `data/`, `data2/`, or `data3/`, stop them before migrating.

```bash
# Legacy containers (if any are still running):
docker compose -f docker-compose.yml down

# Run the migration:
uv run beetroot migrate
```

The command prints its plan and asks for confirmation before touching anything:

```
[beetroot] migrate plan:
    data → instances/alpha/data
   data2 → instances/bravo/data
   data3 → instances/charlie/data
Proceed? This MOVES the directories (no copy). [y/N] y
[beetroot] migrated → alpha (ADB localhost:5555, Frida localhost:27042)
[beetroot] migrated → bravo (ADB localhost:5565, Frida localhost:27052)
[beetroot] migrated → charlie (ADB localhost:5575, Frida localhost:27062)
[beetroot] done. Verify with: beetroot ls && beetroot up alpha bravo charlie
```

To skip the confirmation prompt (for automation):

```bash
uv run beetroot migrate -y
```

## Port changes

!!! warning "Port numbers changed"
    The legacy `docker-compose.yml` used ADB ports 5555 / 6555 / 7555. The new stride-10 scheme uses 5555 / 5565 / 5575. Any tooling or scripts that hardcoded the old ports needs updating.

## After migration

```bash
beetroot ls
# Should show alpha, bravo, charlie

beetroot up alpha bravo charlie
# Boot all three migrated instances
```

Your existing Android userdata (app installs, accounts, settings) is fully preserved — the move is a rename at the OS level, not a copy.

## Preconditions and safety checks

- Migration refuses to run if `instances.json` already has entries — it won't clobber existing named instances.
- It fails if any target path (`instances/alpha/`, etc.) already exists.
- It only migrates directories that exist. If only `data/` is present (no `data2/` or `data3/`), only `alpha` is created.
