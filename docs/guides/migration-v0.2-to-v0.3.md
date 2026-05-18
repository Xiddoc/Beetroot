# Migrating from v0.2 to v0.3

Beetroot v0.3 reshapes five user-visible surfaces. The CLI is forgiving — it auto-bumps `api_version: 1` YAMLs and prints a one-line warning when it sees a v0.2-shaped registry — but the file-system and verb conventions are different enough that "do nothing" is not a complete migration. This page walks you through the exact steps end-to-end.

If you'd rather scan the changes than walk through them, the headline list lives at the top of [`CHANGELOG.md`](https://github.com/Xiddoc/beetroot/blob/main/CHANGELOG.md#unreleased) under "Breaking changes". This page expands each bullet into the commands you actually run.

## 1. Move the registry to `~/.config/beetroot/instances.json`

In v0.2 the cross-instance registry lived at the repo root as `./instances.json`. In v0.3 it moved to `~/.config/beetroot/instances.json` (respecting `$XDG_CONFIG_HOME`) so the CLI no longer needs a checkout-relative location.

There are two auto-detection paths depending on where your old registry sits:

* If a **v1-shaped registry already exists at the XDG path** (`~/.config/beetroot/instances.json`) — for example, because a previous `pip install`-based install wrote there — Beetroot renames it to `instances.json.bak` next to its original location on first load and starts with an empty v2 registry.
* If a **v0.2 registry sits at `$PWD/instances.json`** (the repo-root location v0.2 wrote it to), Beetroot detects it once per process and prints a one-line hint to stderr telling you to move it manually. We don't auto-move from `$PWD` because the file may be under version control or in a different repo, and silently relocating it would surprise you.

Either way, re-registering each instance is the safe path — it tells the new registry where on disk your `beetroot.yaml`s live:

```bash
# Before upgrade — record the paths and indices for reference
cat instances.json  # back this up somewhere safe

# After installing v0.3 — adopt each old instance dir
beetroot register ./instances/alpha
beetroot register ./instances/bravo
# ...
```

`beetroot register <path>` reads the `beetroot.yaml` at that path, allocates a fresh port index, and writes the entry into the new registry. The instance's `data/`, `modules/`, and `beetroot.yaml` are untouched.

You can also move the instance directory out of `./instances/` to anywhere on disk before registering — v0.3 has no special "instances directory" convention. Wherever the `beetroot.yaml` lives is the instance root.

## 2. Update `beetroot.yaml` to `api_version: 2`

`SUPPORTED_API_VERSION` is now `2`. v0.2 YAMLs that hard-pinned `api_version: 1` get auto-bumped on load with a one-line warning, so this step is informational — your existing files keep working. To opt out of the warning, edit each `beetroot.yaml` and either bump the field or omit it entirely (the default is now `2`):

```yaml
# Before
api_version: 1
android:
  version: 14

# After (either form is fine)
api_version: 2
android:
  version: 14
```

If you have YAMLs that never declared `api_version`, no edit is needed — the default kicks in.

## 3. Re-register each existing instance

This is the same step as #1 above for users coming from a clean v0.2 install. If you already had the v0.2 registry, the auto-migration in #1 leaves an `instances.json.bak` and an empty new registry. Run `beetroot register <path>` for each instance directory you want to keep:

```bash
beetroot register /path/to/alpha
beetroot register /path/to/bravo
```

The CLI is content for the directory to live anywhere on disk — `~/work/alpha`, `/srv/research/bravo`, the original `./instances/alpha` from v0.2, etc.

## 4. Verify with `beetroot ls`

```bash
beetroot ls
```

```
NAME    IDX  ADB             FRIDA            STATUS         PATH
alpha   0    localhost:5555  localhost:27042  not-created    /home/you/alpha
bravo   1    localhost:5565  localhost:27052  not-created    /home/you/bravo
```

A freshly-registered instance shows `STATUS = not-created` because no container has been built for it yet. After `beetroot up alpha` the status will flip to `running`; `beetroot down alpha` flips it to `exited` (container present but stopped, data preserved).

The `PATH` column is new in v0.3 — it shows the absolute path each instance was registered at. If you see an entry whose path is wrong, `beetroot destroy <name> -y` removes the registry entry (and the on-disk directory). Register again from the correct path.

## 5. Update your muscle memory

- `beetroot setup` → `beetroot build`. The verb is renamed; the variants (`none`, `lite`, `full`, `mindthegapps`) are unchanged.
- `beetroot up --build` is gone. To rebuild before starting, run `beetroot build` explicitly first.
- `beetroot create --preset <name>` is gone. Starter configs live in the repo's top-level `examples/` directory. To recreate a v0.2 preset workflow:

  ```bash
  beetroot create alpha
  cp examples/stealth.yaml alpha/beetroot.yaml
  beetroot apply alpha
  ```

  See [Examples](examples.md) for the full menu.

## 6. (If applicable) Re-declare Frida explicitly

Frida is opt-in in v0.3. v0.2 instances that relied on the implicit `Frida(version="16.4.10")` default no longer download or launch `frida-server` after upgrade — the bind-mount becomes a 0-byte placeholder and `launch-frida.sh` skips it.

If you depend on Frida for an instance, declare the block in its `beetroot.yaml`:

```yaml
frida:
  version: "16.4.10"
```

…then `beetroot apply <name>` to download and stage the binary. Or, if you'd prefer to start from a complete example, copy [`examples/with-frida.yaml`](examples.md#with-fridayaml) over the file before applying.

## Troubleshooting

**`beetroot ls` shows zero instances after upgrade.**
If a v1-shaped registry sat at the XDG path it was renamed to `instances.json.bak`; if your v0.2 registry sits at `$PWD/instances.json`, Beetroot prints a one-line stderr hint and leaves the file in place. Either way, re-register each instance with `beetroot register <path>`.

**Stale registry entry (the on-disk directory is gone but `beetroot ls` still lists the name).**
`beetroot ls` skips orphans automatically and surfaces them on a trailing `(skipping N orphan entries: <names>; clean up with 'beetroot destroy <name> -y')` advisory. Run that destroy command to remove the orphan registry row.

**An instance directory still references `./compose.yaml`.**
The repo-root `compose.yaml` was deleted in v0.3 — the template now ships inside the wheel at `beetroot/templates/compose.yaml` and is resolved via `importlib.resources`. You don't need to do anything; the CLI handles it. If you were calling `docker compose` directly without the CLI, use `paths.bundled_compose_file()` (or `python -c "from beetroot.paths import bundled_compose_file; print(bundled_compose_file())"`) to find the new path.

**`beetroot.yaml` has `paths.repo_root()` or `paths.compose_file()` referenced in custom tooling.**
Those helpers are gone. The replacements are listed in `CHANGELOG.md` under T1 — most callers want `instance_root(start=None)` or `bundled_compose_file()`.
