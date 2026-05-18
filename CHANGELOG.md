# Changelog

## Unreleased

### v0.3 — Theme T1: explicit, Docker-inspired instance paths

Instance directories are now self-contained anywhere on disk. The
repo-rooted `instances/` and `instances.json` conventions are removed.
An "instance" is any directory containing a `beetroot.yaml`; the CLI
discovers the current one by walking up from the cwd, like git walks up
to `.git`. The cross-instance registry is now user-global at
`~/.config/beetroot/instances.json` (respects `$XDG_CONFIG_HOME`).

**Migration for existing v0.2 users:** Run `beetroot register <path>` for
each of your instance directories (whichever paths you used under
`instances/`) to add them to the new XDG-based registry. The CLI also
auto-migrates the registry: on the first load of a v1-shaped
`instances.json` it renames it to `instances.json.bak` and prints a
one-line warning telling you to re-register.

Other changes shipped together with T1:

* New `beetroot register <path>` verb adopts an existing instance dir.
* `beetroot create` learned `--path <dir>` to put the instance anywhere.
* `beetroot ls` now shows the `PATH` column / `path` field.
* The compose template moved into the wheel at
  `beetroot/templates/compose.yaml`; the repo-root `compose.yaml` is
  gone. Compose is invoked with `-f <bundled>` and
  `--project-directory <instance-dir>` so the template's
  `./data:/data` style mounts resolve correctly against any instance dir.
* Presets are bundled inside the wheel too
  (`beetroot.templates.presets`); the repo-root `presets/` is gone.
* Magisk module download cache moved to `~/.cache/beetroot/modules/`
  and the Frida binary cache to `~/.cache/beetroot/frida/` (both
  respect `$XDG_CACHE_HOME`).
* `Module.path` entries with relative paths now resolve against the
  instance directory itself (not the repo root).
* `paths.py` is rewritten: `repo_root` / `instances_dir` /
  `instance_dir(name)` / `compose_file` / `presets_dir` /
  `registry_file` are **removed**. Replacements:
  `instance_root(start=None)`, `instance_data(root)`,
  `instance_modules(root)`, `instance_frida(root)`,
  `instance_yaml(root)`, `instance_env(root)`,
  `bundled_compose_file()`, `user_registry_file()`,
  `user_cache_dir(subdir)`.
* `paths.ProjectRootNotFoundError` is renamed to
  `paths.InstanceRootNotFoundError` (still a `FileNotFoundError`).
* `registry.add(name, index)` now takes an extra `absolute_path` arg.
  New helper `registry.instance_path(name)` looks it up; new
  `registry.RegistryError` is raised for unknown-name lookups. Schema
  bumped to v2; loading a v1 registry triggers the migration described
  above.
* `SUPPORTED_API_VERSION` bumped to **2**. v0.2 YAMLs that hard-pinned
  `api_version: 1` must be updated (omitting the field still works —
  the default is now `2`). All bundled presets declare
  `api_version: 2`.

### v0.3 — Theme T10: stealth-posture design doc

- T10: stealth-posture design doc landed (`docs/design/stealth-posture.md`); v0.4 will implement the playbook.

### Added
- `api_version` top-level field in `beetroot.yaml` (default `1`). Each
  Beetroot release supports exactly one `api_version`; loading a YAML with
  a mismatched value fails loud with a pointer back to this CHANGELOG.
  Omitting the field is equivalent to writing the currently supported
  value, so existing instance YAMLs keep working. All shipped presets now
  declare `api_version: 1` explicitly as the first field. Future schema
  breaks bump `SUPPORTED_API_VERSION` and add a migration entry here.
- `android.gapps` field: choose `none | lite | full | mindthegapps` per instance.
  `lite` is the default and preserves current behavior.
- `beetroot setup [variant]`: produce the corresponding redroid base image.
  Replaces the deprecated `scripts/setup.sh` (see Breaking section below).
- `src/beetroot/setup_runner.py`: testable Python rewrite of the legacy
  bash bootstrap, with an injectable `SubprocessRunner` protocol for unit
  tests. 100% test coverage.
- New preset: `no-gapps`.
- `src/beetroot/settings.py` — `Settings(BaseSettings)` for environment-driven
  overrides. Set `BEETROOT_DOCKER_BIN`, `BEETROOT_FRIDA_ARCH`, or
  `BEETROOT_HTTP_TIMEOUT` to override the defaults (`docker`,
  `android-x86_64`, `30`). Useful for ARM-based Android VMs, slow networks,
  or non-standard docker binary locations.
- `ports:` block in `beetroot.yaml` — optional per-instance overrides for
  `adb`, `frida`, and `frida_control` host ports. Each field is independently
  optional; omitted fields fall back to the stride-of-10 allocator on the
  instance's index, so existing configs are unaffected. `beetroot create` and
  `beetroot apply` pre-validate that the resolved ports do not collide with
  any other registered instance, and exit with a clear message if they do.

### Fixed
- `ports.resolve_ports` now refuses to produce a port dict with duplicate
  values, raising a new `ports.PortCollisionError` (subclass of `ValueError`)
  that `cli.main()` surfaces as a friendly `error: ...` line. Previously a
  partial `ports:` override could silently collide with the stride-of-10
  sibling it didn't override (e.g. pinning `frida: 27043` at index 0 left
  `frida_control` on the stride default of `27043`, so the rendered `.env`
  bound two services to the same host port and `docker compose up` failed
  *after* registry mutation). The pydantic `Ports` model can't catch this
  on its own because it doesn't know the instance's stride index; the check
  belongs in the resolver.

### Changed
- Docs site updated to match the `uv tool install` install path: the
  landing-page Quick start, Getting Started overview, Installation page,
  and CLI reference no longer instruct users to run `uv run beetroot` or
  set a shell alias. Plain `beetroot <verb>` is the documented invocation;
  the `uv run beetroot` form is mentioned only as a contributor aside for
  editable `uv sync` checkouts.
- Docs now route Frida CLI install through the `[frida]` extra:
  `uv tool install 'beetroot[frida]'` is the recommended path on the
  prerequisites, CLI reference, and troubleshooting pages; the standalone
  `pip install frida-tools` recommendation has been dropped (use
  `uv tool install frida-tools` if you really want a standalone install).
  The no-frida CLI test (`tests/test_cli.py::test_frida_no_frida_exits`) was
  tightened to pin the `beetroot[frida]` wording in the install hint so a
  future regression on the error message fails the test.
- README trimmed from 386 lines to a short front-door (~70 lines). Deep
  content (CLI verb table, full `beetroot.yaml` schema, how-it-works,
  port allocation, resource defaults, snapshots, project layout,
  troubleshooting) now lives on the docs site at
  <https://xiddoc.github.io/Beetroot/> and is linked from the README.
- Mypy is now run with `strict = true` and the `pydantic.mypy` plugin. CI
  catches a significantly wider class of type errors, including incorrect
  pydantic constructor calls and unnarrowed `Optional` accesses.
- `paths.repo_root()` now discovers the project root by upward search from
  the cwd (`compose.yaml` marker), making the CLI work under
  `uv tool install` from any directory inside a project tree. Running
  outside a project tree raises `paths.ProjectRootNotFoundError`, which
  `cli.main()` surfaces as a friendly `error: ...` and `exit 1` instead of
  a Python traceback or silent wrong-location lookups.
- `CLAUDE.md` grew two process rules informed by the v0.2 retro: "Docs are
  part of every feature" (grep `docs/` and `README.md` for old spellings
  on every user-facing change) and "Behavior tests, not just line
  coverage" (cross-component features need at least one user-input →
  final-artifact test, since line coverage misses cross-product bugs).

### Fixed
- README's "What you get" bullet no longer advertises nonexistent CLI verbs
  (`snapshot`, `attach`, `list`) or misnamed verbs (`start`/`stop` instead
  of `up`/`down`). The bullet now points at the docs site for the
  authoritative verb list. Added `tests/test_readme.py` as a regression
  guard so future drift fails CI.

### Removed
- `beetroot migrate` verb (used for the v1 → v2 instance-layout transition;
  the new multi-instance layout is now the only supported one).

### Breaking: `scripts/setup.sh` removed

The bash bootstrap script has been deleted. Use the new CLI verb instead:

```bash
# Before
./scripts/setup.sh
./scripts/setup.sh full

# After
uv run beetroot setup
uv run beetroot setup full
```

Behavior is identical (same patcher, same flags, same resulting tag).
Variants accepted positionally: `none`, `lite` (default), `full`,
`mindthegapps`. The implementation is now `src/beetroot/setup_runner.py`
and is unit-tested at 100% coverage; the docker binary honours
`BEETROOT_DOCKER_BIN` for `compose build`.

### Breaking

- `resources.shm` renamed to `resources.shared_mem`. Loading a YAML with the
  legacy field raises `ValidationError` pointing at this migration.

  ```yaml
  # Before
  resources:
    shm: 256m

  # After
  resources:
    shared_mem: 256m
  ```

  The Docker-side `shm_size` field (and the `SHM_SIZE` env var rendered into
  the per-instance `.env`) are unchanged — only the user-facing YAML key
  was renamed.

### Breaking: `android.base_image` removed

`android.base_image` is no longer a valid field in `beetroot.yaml`.  Replace it
with `android.version` (an integer: `11`, `12`, `13`, or `14`):

```yaml
# Before
android:
  base_image: redroid/redroid:14.0.0_litegapps_houdini_magisk

# After
android:
  version: 14
```

The image tag is now derived automatically by the CLI (`config.base_image_tag()`).
Loading a YAML that still contains `android.base_image` raises a `ValueError` with
this migration message.  All shipped presets have been updated.
