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

### v0.3 — Theme T2: Frida is opt-in

`InstanceConfig.frida` now defaults to `None` instead of an implicit
`Frida(version="16.4.10")` block. New instances created from the
`default` preset no longer download a `frida-server` binary, and
`entrypoint.sh` skips the launch (the bind-mount is a 0-byte
non-executable placeholder). To get the old behavior, declare an
explicit block in `beetroot.yaml`:

```yaml
frida:
  version: "16.4.10"
```

…or copy/start from the new `with-frida` bundled preset:

```bash
beetroot create alpha --preset with-frida
```

**Migration for existing v0.2 users:** This is a behavior break, not a
schema break, so it rides T1's `api_version` bump (already at 2). v0.2
YAMLs that relied on the implicit Frida default now need to declare
`frida: {version: "16.4.10"}` explicitly (or copy the `with-frida`
preset). An empty block (`frida: {}`) still hydrates the model's own
default version, so the upgrade is a one-line addition.

Other changes shipped together with T2:

* New `with-frida` preset (`beetroot.templates.presets.with-frida`)
  declares the version-pin idiom for users who want Frida on.
* Default preset (`beetroot.templates.presets.default`) gains a header
  comment documenting that Frida is opt-in.
* README's "What you get" bullet, the docs index "Frida" row,
  `docs/guides/frida.md`, `docs/guides/presets.md`,
  `docs/getting-started/first-instance.md`,
  `docs/how-it-works/architecture.md`,
  `docs/how-it-works/filesystem.md`,
  `docs/how-it-works/boot-flow.md`,
  `docs/reference/config.md`, and `docs/troubleshooting.md` all
  reframed to describe Frida as opt-in.
* Drive-by: stale `api_version: 1` examples in
  `docs/reference/config.md` bumped to `2` (T1 bumped the constant; the
  example snippets were missed in T1's docs sweep).

### v0.3 — Theme T10: stealth-posture design doc

- T10: stealth-posture design doc landed (`docs/design/stealth-posture.md`); v0.4 will implement the playbook.

### v0.3 — Theme T3: presets are documentation only

`beetroot create --preset` is removed. Presets are no longer a
framework concept — they are documentation-only starter `beetroot.yaml`
files under the new top-level `examples/` directory (`default.yaml`,
`stealth.yaml`, `no-gapps.yaml`). The CLI does not load them.

`beetroot create <name>` now writes a minimal `beetroot.yaml` that
contains exactly:

```yaml
api_version: 2
android:
  version: 14
```

Every other field falls back to schema defaults. To recreate a v0.2
preset behaviour, copy the matching file from `examples/` over the
freshly generated `beetroot.yaml` and run `beetroot apply <name>`:

```bash
beetroot create alpha
cp examples/stealth.yaml alpha/beetroot.yaml
beetroot apply alpha
```

**Removed:**

* `beetroot create --preset` flag.
* `config.load_preset(preset_name)` function.
* The `beetroot.templates.presets` sub-package (the wheel-bundled
  starter YAMLs introduced in T1).

**Moved:**

* `src/beetroot/templates/presets/*.yaml` →
  top-level `examples/` directory, with a `# Documentation only —
  not loaded by the CLI` header comment at the top of every file.
* `docs/guides/presets.md` → `docs/guides/examples.md` (mkdocs nav
  entry renamed Presets → Examples).

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

### v0.3 — Theme T7: shell scripts split, shellcheck in CI

`docker/entrypoint.sh` is split into a 12-line glue file plus three
single-purpose helpers, all in POSIX `/system/bin/sh`:

- `docker/magisk-config.sh` — Magisk daemon wait + Zygisk/denylist
  SQL + GMS denylist enrolment.
- `docker/flash-modules.sh` — iterate `*.zip` in the modules dir and
  install each one via `magisk --install-module`.
- `docker/launch-frida.sh` — check the executable bit on the frida
  binary and launch it as a backgrounded child of the entrypoint shell.

Every container-side path the helpers touch is read from a `BEETROOT_*`
env var with a safe default (`${VAR:-/safe-default}`); no path is
hard-coded. v0.3 keeps the defaults at the well-known paths
(`/data/adb/magisk.db`, `/flash_dir`, `/data/local/tmp/frida-server`),
and the bundled compose template threads `BEETROOT_MAGISK_DB`,
`BEETROOT_MODULES_DIR`, `BEETROOT_FRIDA_BIN` through its service
`environment:` block as empty-by-default host overrides. v0.4's
stealth-posture work (see `docs/design/stealth-posture.md`) sets these
to randomized per-build paths without editing any helper.

`docker/Dockerfile`'s `COPY` step changes from a single-file copy of
`entrypoint.sh` to `COPY --chmod=755 docker/*.sh /`, which places all
four shell files at filesystem root.

New CI job `shellcheck` runs `shellcheck -s sh docker/*.sh` on every
push and PR; any violation in any boot-helper script fails the build.

New docs page: `docs/how-it-works/boot-scripts.md` documents each
helper's env-var contract, idempotency model, and exit semantics, plus
the shared "POSIX sh only, no hardcoded paths" contract. `boot-flow.md`
gains a `## Helper scripts` anchor pointing at the new page so T10's
stealth-posture design doc's four existing `boot-flow.md#helper-scripts`
references resolve unchanged.

### v0.3 — Theme T4: Typer at the CLI surface

`src/beetroot/cli.py` was rewritten from `argparse` to
[Typer](https://typer.tiangolo.com/). User-facing semantics are
preserved bit-for-bit: every verb name, flag name, default value, and
exit code matches the v0.2 argparse shape. The internal procedural
modules (`compose`, `config`, `frida_dl`, `modules_dl`, `paths`,
`ports`, `registry`, `setup_runner`) are untouched — the OOP refactor
is reserved for a later theme.

**What changes for users:**

- `beetroot --help` (and `beetroot <verb> --help`) now renders through
  Typer's Rich-powered formatter: boxed sections, color when the
  terminal supports it, an auto-included `--help` row, and an
  auto-included `--install-completion` / `--show-completion` row.
- Shell completion is available out of the box. Run
  `beetroot --install-completion` once per shell — Typer auto-detects
  bash, zsh, fish, or PowerShell via
  [shellingham](https://github.com/sarugaku/shellingham) and writes
  the hook into the right rc file. `beetroot --show-completion`
  prints the script without installing it.
- `beetroot frida alpha -- -l script.js` now uses the `--` separator
  to disambiguate forwarded flags from Beetroot's own option-parser.
  Plain `beetroot frida alpha -n com.app` keeps working unchanged
  (`-n com.app` is not a Typer/Click option, so it falls through);
  `--` is only needed if a forwarded flag would otherwise collide
  with one of Beetroot's options.

**What changes for contributors:**

- New runtime dependency: `typer>=0.12`. Pulls in `click`, `rich`,
  and `shellingham`. No optional extras — every Beetroot install
  gets the Rich help and shell completion.
- `cli.build_parser()` is removed. Tests that introspect the verb set
  iterate `cli.app.registered_commands` instead.
- `cli.cmd_*` functions are renamed to their verb names (`cli.create`,
  `cli.apply`, `cli.up`, …) and decorated with `@app.command()`. The
  function bodies are unchanged.
- `tests/test_cli.py` and `tests/test_port_collisions.py` drive every
  verb through `typer.testing.CliRunner().invoke(cli.app, [...])`
  and assert on `result.exit_code` + `result.stdout` / `result.stderr`.
  The legacy `_ns(...)` / `_create_ns(...)` argparse-Namespace
  helpers are gone.
- A new behavior test
  (`tests/test_cli.py::TestCmdFrida::test_frida_forwards_remainder_args_verbatim`)
  pins the `beetroot frida <name> -- <args>` round-trip contract:
  any tokens after `--` reach the underlying `frida` subprocess
  verbatim, in order, after Beetroot's `-H localhost:<port>` prefix.
- `cli.main()` still catches `paths.InstanceRootNotFoundError` and
  `ports.PortCollisionError` from deep in the call tree and surfaces
  them as `error: <msg>` on stderr + `exit 1`, matching v0.2.

### v0.3 — Theme T5: setup renamed to build

The one-time base-image bootstrap verb is renamed from `setup` to
`build`. `beetroot up` no longer accepts a `--build` flag — building
the image and starting an instance are two separate concerns, and
`up` should be fast and predictable.

**Migration for existing v0.2 users:** Run `beetroot build` instead
of `beetroot setup`. To get a fresh image before `beetroot up`, run
`beetroot build` explicitly first — `up` no longer accepts `--build`.

**Renamed:**

* CLI verb: `beetroot setup [variant]` → `beetroot build [variant]`.
* Module: `src/beetroot/setup_runner.py` → `src/beetroot/builder.py`.
* Function: `setup_runner.bootstrap_base_image()` →
  `builder.build_image()`. Signature and semantics are otherwise
  unchanged.

**Removed:**

* `beetroot up --build` flag. Typer now rejects it.
* `compose.up()`'s `build: bool` kwarg. The `compose.build()` helper
  is unchanged — call it separately if you need to rebuild from
  Python.

**Tests:**

* `tests/test_setup_runner.py` → `tests/test_builder.py`. Class names
  `TestGappsFlags`, `TestBootstrapErrorType`, etc. are unchanged;
  imports switch to `beetroot.builder`.
* New behavior test
  `tests/test_cli.py::TestCmdUp::test_up_does_not_pass_build_flag`
  asserts that `beetroot up alpha` runs and the compose argv it
  produces contains no `--build` token.
* Companion test `TestCmdUp::test_up_rejects_build_flag` asserts
  that passing `--build` now fails Typer's option parsing.
### v0.3 — Theme T6: snapshots

Two new CLI verbs land:

- `beetroot snapshot <name> [-o <archive>]` packs an instance's
  host-side state (`beetroot.yaml`, `data/`, `modules/`, the optional
  `frida-server` placeholder) into a single zstandard-compressed tar
  archive (`.tar.zst`). The `.env` is deliberately excluded — it's
  regenerated by the next `beetroot apply`. If `-o` is omitted, the
  archive lands at `./<name>.tar.zst`. The `.tar.zst` extension is
  appended automatically when missing.
- `beetroot restore <archive> [--as <name>] [--path <dir>] [--force]`
  unpacks an archive into a new instance and registers it with a
  **fresh** port index — the source's index is never reused, so the
  original and the restored copy can run concurrently. Without `--as`,
  the manifest's stored name is used; without `--path`, the directory
  defaults to `./<name>`. `--force` wipes a non-empty destination
  before extracting; without it, the verb refuses and exits with
  `error: ...`.

The archive carries a `.beetroot-snapshot.json` manifest at its root
with six fields: `schema_version` (currently `1`), `name`,
`source_index`, `created_at` (ISO 8601 UTC), `beetroot_version` (from
`importlib.metadata.version("beetroot")`), and `path_layout` (a
`dict[str, str]`, always `{}` in v0.3). v0.4's stealth-posture work
(see `docs/design/stealth-posture.md`) will populate `path_layout`
with the source instance's randomized container-path mapping and
replay it into the restored instance's `BEETROOT_*` env vars on
`beetroot apply`. v0.3's `restore` doesn't act on `path_layout`, but
it never strips or rewrites the field — the manifest is preserved on
disk inside the restored instance dir at `.beetroot-snapshot.json`,
so a v0.4-produced archive restored by v0.3 keeps its layout intact
for the next read.

**The Docker overlay layer is not captured by design.** Redroid
regenerates the writable overlay deterministically from the base
image plus the persisted `/data` bind-mount, so `beetroot up` after a
restore produces an equivalent container without us having to ship
the overlay bytes. If you need to snapshot a customized base image
itself, use `docker image save` — Beetroot snapshots are an
instance-state artefact, not a Docker-image artefact.

Other changes shipped together with T6:

- New runtime dependency: `zstandard>=0.22`. The snapshot module
  composes the standard-library `tarfile` with
  `zstandard.ZstdCompressor().stream_writer()` /
  `ZstdDecompressor.stream_reader()` so the archive is produced and
  consumed as a true byte stream — no intermediate `BytesIO` blob
  the size of `data/`.
- `src/beetroot/snapshot.py` is the new pure module: three composable
  functions (`snapshot`, `restore`, `read_manifest`) plus the
  frozen-dataclass `Manifest` and the `SnapshotError` exception type.
  Stdlib `dataclass` rather than a pydantic model — the manifest
  shape is documented and validated explicitly without dragging
  `arbitrary_types_allowed` into the project.
- `tests/test_snapshot.py` ships the round-trip behavior test
  (`TestSnapshotRoundTrip::test_round_trip_preserves_data_bytes`):
  it creates an instance, writes known bytes into `data/marker.txt`,
  snapshots, destroys, restores under a new name + new path, and
  asserts the bytes are byte-identical and the new registry entry
  has a freshly allocated port index. The full test runs in well
  under 2s without network or Docker.
- `tests/test_readme.py::GHOST_VERBS` drops `"snapshot"` (it's a
  registered verb now).
- Docs sweep: `docs/guides/snapshots.md` is rewritten from the v0.2
  filesystem-recipe stub into the v0.3 `snapshot` / `restore` guide;
  `docs/reference/cli.md` gains the two new verb sections;
  `docs/index.md`, `docs/guides/index.md`, and
  `docs/how-it-works/filesystem.md` are reframed away from the
  "there is no snapshot verb" wording.

### v0.3 — Theme T8: high-level OOP Python API

A new `beetroot.api` module exports an object-oriented surface that
composes the procedural modules without replacing them. Researchers
who want to drive Beetroot from Python can now write
`from beetroot import Instance, Manager, DeviceBackend` instead of
reaching into the cross-module function vocabulary.

```python
from beetroot import Instance

inst = Instance.create("alpha")            # registers + stages
inst.up()                                  # docker compose up -d
inst.frida_cli(["-n", "com.victim"])       # frida -H localhost:27042 -n ...
inst.snapshot(Path("alpha-clean.tar.zst")) # pack to archive
inst.destroy(yes=True)                     # tear down + deregister
```

**Public surface:**

- `Instance` — single research phone (name + on-disk root + parsed
  config). Three classmethod constructors: `create` (new dir + new
  registry entry), `load` (look up by name), `from_path` (walk up to
  the nearest `beetroot.yaml` and match the registry by resolved
  path). Lifecycle methods (`up`, `down`, `restart`, `apply`,
  `destroy`) and operations (`shell`, `frida_cli`, `install_frida`,
  `add_module`, `snapshot`, `logs`). Introspection properties
  (`status`, `ports`, `adb_address`, `frida_address`,
  `is_available`) query live state — never cached on the object.
- `Manager` — stateless aggregate operations over the registry:
  `list()` (sorted), `get(name)` (`None` on miss), and
  `allocate_port_index()`.
- `DeviceBackend` — a `@runtime_checkable` Protocol that v0.3's
  `Instance` satisfies and that v0.4's `AdbDeviceBackend` will too.
  Four members: `adb_address`, `frida_address`, `is_available`,
  `install_frida(version)`. See the
  [device backends design doc](docs/design/device-backends.md) for
  the v0.4 roadmap.
- New exception types: `InstanceNotFoundError` (`LookupError`
  subclass; raised by `Instance.load` / `Instance.from_path`),
  `FridaNotInstalledError`, `AdbNotInstalledError`.

**CLI internals (refactored, no behavior change):**

- Every Typer command body in `src/beetroot/cli.py` is now a 1-15 line
  shell that constructs an `api.Instance` or calls an `api.Manager`
  method, then handles CLI-specific concerns (`error: ...` lines,
  `typer.Exit`, stdout formatting). The verbs stay as module-level
  `@app.command()` functions (Typer captures the function reference
  at import time, per the T4 CR — wrapping verbs in a class would
  break dispatch). `cli.py` shrinks from 554 to 487 LOC.
- The `destroy` verb keeps its own `compose.down` call rather than
  going through `Instance.destroy` so the registry-only-orphan path
  (where the on-disk `beetroot.yaml` has gone missing but the
  registry entry survives) still tears down cleanly.

**Procedural modules unchanged.** `compose`, `config`, `frida_dl`,
`modules_dl`, `paths`, `ports`, `registry`, `snapshot`, and
`builder` keep their public surface — `api.py` composes them.

**Tests:** `tests/test_api.py` exercises every `Instance` / `Manager`
method directly, asserting on observable side-effects (compose argv,
registry entries, filesystem artifacts) rather than just "didn't
raise". The required behavior tests landed: `Instance.create` →
`Instance.load` round-trip, `Instance.from_path` walk-up from a
subdir, `isinstance(inst, DeviceBackend)`. CLI dispatch assertions
cover `up`, `down`, `apply`, and `ls` — each mocks the corresponding
`Instance` / `Manager` method to confirm the Typer command really
calls into the OOP layer.

**Docs:**

- `docs/reference/api.md` now leads with `beetroot.api` and explains
  the two-audience split (programmatic users vs. CLI contributors).
- `README.md` gains a "Python API" row in the docs table.
- `docs/reference/index.md` calls out the OOP entry point.
- `CLAUDE.md`'s `src/beetroot/` tree gets `api.py` and `snapshot.py`,
  plus a paragraph explaining why the verbs stay module-level.

### v0.3 — Theme T9: device-backends design doc

A new design doc at `docs/design/device-backends.md` formalises the
`DeviceBackend` Protocol that v0.3's `beetroot.api` declares.
The doc lands:

- The rationale for a backend abstraction (real rooted phones over
  ADB; remote device farms).
- The Protocol surface (copy-pasted from `api.py` so the doc is the
  canonical reference).
- Two concrete backends — `RedroidBackend` (today's `Instance`,
  satisfying the Protocol directly) and `AdbDeviceBackend` (v0.4,
  wrapping an `adb`-connected device).
- The capability methods that aren't universal
  (`apply_stealth_config`, `shell`, `add_module`) and a new
  `BackendCapabilityError` exception type for backends that can't
  honour them.
- Emulator-only features (per-build path randomization, container
  overlay manipulation) cross-referenced against the
  [stealth-posture doc](docs/design/stealth-posture.md).
- A 5-PR roadmap for the v0.4 `AdbDeviceBackend` rollout (scaffolding
  → `install_frida` → `shell` → `add_module` → `beetroot adopt
  <serial>` CLI integration).
- Explicit out-of-scope list (rooting the device, MDM bypass,
  hardware-backed attestation).

1781 words, design-only. `mkdocs build --strict` passes; the page is
linked from the design-notes index and the mkdocs nav.
