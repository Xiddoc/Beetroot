# Changelog

## Unreleased

### Breaking changes (upgrading from v0.2)

Beetroot v0.3 reshapes the user-visible surface in five places. Step-by-step
walkthrough: [Migrating from v0.2 to v0.3](docs/guides/migration-v0.2-to-v0.3.md).

- The cross-instance registry moved out of the repo to
  `~/.config/beetroot/instances.json` (respects `$XDG_CONFIG_HOME`). After
  upgrading, run `beetroot register <path>` for each of your old instance
  directories to add them to the new registry.
- `api_version: 2` is the new minimum. v0.2 YAMLs that hard-pinned
  `api_version: 1` auto-bump on load with a one-line warning (the field
  default is now `2`, so omitting the field still works).
- `beetroot setup` is renamed to `beetroot build`. `beetroot up --build`
  is gone — run `beetroot build` explicitly when you want a fresh image.
- `beetroot create --preset` is removed. Starter configs live under the
  repo's top-level `examples/` directory now; copy one over the
  freshly-generated `beetroot.yaml` and run `beetroot apply <name>`.
- Frida is opt-in. A new instance no longer stages a `frida-server`
  binary unless its `beetroot.yaml` declares a `frida:` block.

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
  the default is now `2`).

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

* `beetroot up --build` flag. The CLI now intercepts the v0.2 invocation
  with a friendly migration hint pointing at `beetroot build` (kept as a
  hidden Typer option for one release; will be deleted in v0.4).
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

### v0.3 — Theme T3: presets are documentation only

`beetroot create --preset` is removed. Presets are no longer a
framework concept — they are documentation-only starter `beetroot.yaml`
files under the new top-level `examples/` directory (`default.yaml`,
`stealth.yaml`, `no-gapps.yaml`, `with-frida.yaml`). The CLI does not
load them.

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

### v0.3 — Theme T2: Frida is opt-in

`InstanceConfig.frida` now defaults to `None` instead of an implicit
`Frida(version="16.4.10")` block. New instances no longer download a
`frida-server` binary, and `entrypoint.sh` skips the launch (the
bind-mount is a 0-byte non-executable placeholder). To get the old
behavior, declare an explicit block in `beetroot.yaml`:

```yaml
frida:
  version: "16.4.10"
```

…or copy the `examples/with-frida.yaml` starter over a freshly
generated `beetroot.yaml`:

```bash
beetroot create alpha
cp examples/with-frida.yaml alpha/beetroot.yaml
beetroot apply alpha
```

**Migration for existing v0.2 users:** This is a behavior break, not a
schema break, so it rides T1's `api_version` bump (already at 2). v0.2
YAMLs that relied on the implicit Frida default now need to declare
`frida: {version: "16.4.10"}` explicitly (or copy `examples/with-frida.yaml`).
An empty block (`frida: {}`) still hydrates the model's own default
version, so the upgrade is a one-line addition.

Other changes shipped together with T2:

* New `examples/with-frida.yaml` declares the version-pin idiom for
  users who want Frida on.
* README's "What you get" bullet, the docs index "Frida" row,
  `docs/guides/frida.md`, `docs/guides/examples.md`,
  `docs/getting-started/first-instance.md`,
  `docs/how-it-works/architecture.md`,
  `docs/how-it-works/filesystem.md`,
  `docs/how-it-works/boot-flow.md`,
  `docs/reference/config.md`, and `docs/troubleshooting.md` all
  reframed to describe Frida as opt-in.
* Drive-by: stale `api_version: 1` examples in
  `docs/reference/config.md` bumped to `2` (T1 bumped the constant; the
  example snippets were missed in T1's docs sweep).

### v0.3 — Theme T4: Typer at the CLI surface

`src/beetroot/cli.py` was rewritten from `argparse` to
[Typer](https://typer.tiangolo.com/). User-facing semantics are
preserved bit-for-bit: every verb name, flag name, default value, and
exit code matches the v0.2 argparse shape. The internal procedural
modules (`compose`, `config`, `frida_dl`, `modules_dl`, `paths`,
`ports`, `registry`, `builder`) are untouched — the OOP refactor
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
- The legacy `cli.cmd_*` argparse handler functions are renamed to
  their verb names (`cli.create`, `cli.apply`, `cli.up`, …) and
  decorated with `@app.command()`. The function bodies are unchanged.
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

### v0.3 — Theme T6: snapshots

Two new CLI verbs land:

- `beetroot snapshot <name> [-o <archive>]` packs an instance's
  host-side state (`beetroot.yaml`, `data/`, `modules/`, the optional
  `frida-server` placeholder) into a single zstandard-compressed tar
  archive (`.tar.zst`). The `.env` is deliberately excluded — it's
  regenerated from `beetroot.yaml` on restore (Fix-up A's `_stage()`
  call) so `beetroot up <name>` works without an intermediate
  `beetroot apply`. If `-o` is omitted, the archive lands at
  `./<name>.tar.zst`. The `.tar.zst` extension is appended
  automatically when missing.
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
  config). Four classmethod constructors: `create` (new dir + new
  registry entry), `register` (adopt an existing dir into the
  registry), `load` (look up by name), and `from_path` (walk up to
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

### v0.3 — Theme T10: stealth-posture design doc

- T10: stealth-posture design doc landed (`docs/design/stealth-posture.md`); v0.4 will implement the playbook.

### v0.3 — Post-CR fix-up

Round of fixes surfaced by the five v0.3 CRs. Each item is paired
with at least one behavior test that asserts on the artifact (not
"function was called"); see ``tests/`` for the named files.

* **pyproject version bumped 0.1.0 → 0.3.0.** The snapshot manifest
  writer reads ``importlib.metadata.version("beetroot")`` and stamps
  every archive — every v0.3 snapshot was previously claiming
  ``"0.1.0"``. *Integrator note: bump the git tag to ``v0.3.0`` at
  release time to match.*
* **builder.DefaultRunner merges ``os.environ`` into its env arg.**
  The old code passed a 2-key dict straight to ``subprocess.run``,
  which REPLACES the parent env — ``docker compose build`` launched
  with no ``PATH`` and failed with ``FileNotFoundError: 'docker'`` on
  every fresh install. Pinned by ``tests/test_subprocess_env_merge.py``.
* **``docker/flash-modules.sh`` no longer calls ``exit 0``.** The
  helper is sourced by ``entrypoint.sh`` with ``.``, so ``exit``
  terminates the parent shell, skipping ``launch-frida.sh`` and the
  trailing ``wait``. Restructured into an ``if/else``. Pinned by
  ``tests/test_boot_scripts_sourced.py``.
* **``Instance.register`` and ``snapshot.restore`` now stage files.**
  Both registered the instance but skipped writing ``.env`` /
  ``frida-server`` / ``modules/``. A follow-up ``beetroot up`` failed
  on the missing ``.env``. ``restore`` also now port-collision-checks
  against existing instances before extracting. Pinned by
  ``tests/test_instance_invariants.py``.
* **``snapshot._is_manifest_member`` requires an exact-path match.**
  Basename-only matching also picked up a stale ``data/.beetroot-snapshot.json``
  left over from a previous restore. ``_add_instance_tree`` now skips
  the on-disk manifest, and the basename-only matcher is replaced
  with an exact-path matcher against the archive root. Pinned by the
  new ``TestManifestShadowRegression`` cases in ``tests/test_snapshot.py``.
* **``cli.main()`` catches ``ComposeError`` and ``BootstrapError``.**
  ``up`` / ``down`` / ``restart`` / ``logs`` / ``apply`` / ``build``
  used to let domain exceptions propagate as bare tracebacks; v0.2
  was uniformly ``error: ...`` + exit 1. Pinned by
  ``tests/test_cli_error_contract.py``.
* **``beetroot setup`` and ``--preset`` print migration hints.** v0.2
  users running ``beetroot setup`` or ``beetroot create --preset``
  used to get bare Typer ``No such command`` / ``No such option``.
  Hidden alias + hidden option now print an ``error: ...`` line
  with the migration path. Pinned by ``tests/test_deprecated_verbs.py``.
* **``api_version: 1`` auto-bumps to ``2`` with a stderr warning.**
  v0.2 YAMLs used to hard-fail on load; v0.2 → v2 is strictly
  additive, so we auto-bump and tell the user to run
  ``beetroot apply`` to persist. Pinned by
  ``tests/test_api_version_auto_bump.py``.
* **v0.2 registry at ``$PWD/instances.json`` surfaces a one-line
  hint.** v0.2 wrote the registry at the repo root; v0.3 expects it
  under ``$XDG_CONFIG_HOME``. The hint fires once per process and
  doesn't auto-move the file (the user may have it under VCS).
  Pinned by ``tests/test_v02_registry_detection.py``.
* **Atomic port allocation + registration via ``registry.add_allocating``.**
  The old sequence ``lowest_free_index → resolve → check → add``
  only held the lock on ``add``; two parallel ``Instance.create``
  calls could co-allocate the same stride slot. The new helper
  collapses allocation + add into one critical section, with shared
  reader locks on ``list_instances`` and atomic-replace via per-pid
  tmp file in ``_write``. Pinned by ``tests/test_registry_race.py``
  (multiprocessing fork pool, 5 parallel creates).
* **``render_env`` emits ``BEETROOT_MAGISK_DB`` / ``BEETROOT_MODULES_DIR`` /
  ``BEETROOT_FRIDA_BIN`` with empty defaults.** The bundled compose
  template already references them via ``${VAR:-}`` for v0.4
  stealth-posture work; the test ``tests/test_compose_template_envs.py``
  asserts the template ↔ render_env contract stays symmetric.
* **``snapshot.restore --force`` refuses to wipe a peer instance's
  directory.** Without the check, a careless ``restore --force
  --path=<peer-dir>`` would ``shutil.rmtree`` a sibling instance's
  ``/data``. Pinned by the new ``test_force_refuses_to_overwrite_another_instances_dir``
  case in ``tests/test_snapshot.py``.

Total test-count delta: 469 → 522 (+53).

### v0.3 — Post-CR CI hardening

Guardrails added to ``.github/workflows/ci.yml`` and
``.pre-commit-config.yaml`` so the categories of regression the v0.3
CR pass surfaced never reach ``dev/v0.3`` again. Each guardrail
matches a specific CR finding:

* **Single Python 3.13 lane.** ``pyproject.toml`` pins
  ``requires-python = ">=3.13"``, but the test job still ran a
  3.10/3.11/3.12 matrix — dead cells that wasted runner minutes and
  could not surface real regressions. Collapsed to a single
  ``actions/setup-python@v5`` step on 3.13.
* **``mkdocs build --strict`` runs on every PR.** Today's
  ``docs.yml`` only built strict on push-to-master, so PR review
  never saw doc drift (T3-style ghost references, dead nav
  entries). New ``docs-strict`` job in ``ci.yml`` runs the same
  strict build; the gh-pages deploy job in ``docs.yml`` stays put.
* **``mypy tests/`` runs in CI.** CR #4 flagged that CI only ran
  mypy against ``src/beetroot/``, while CLAUDE.md requires both
  trees to type-check. The lint-and-type-check job now passes
  both paths.
* **``scripts/lint_changelog.py`` pre-commit + CI hook.** Closes
  the failure mode where T2's CHANGELOG entry cited
  ``beetroot create alpha --preset with-frida`` — but T3 had
  already removed ``--preset`` earlier in the same Unreleased
  block. The linter parses shell fences under ``## Unreleased``,
  pulls every ``beetroot <verb>`` invocation, and validates verb +
  long-flag names against ``beetroot --help`` / ``beetroot <verb>
  --help``. It does NOT execute the cited commands. Wired as a
  ``CHANGELOG.md``-scoped pre-commit hook (``stages: [pre-commit,
  manual]``) and as a step in the CI lint job.

### v0.3 — Post-CR-CR fix-up

Three post-fix CRs ran against the previous fix-up state and surfaced
findings the original sweep missed (plus a small number of new bugs
the fix-ups themselves introduced). The must-fix items are addressed
below; lower-severity items deferred to v0.3.1. Each fix is paired
with at least one behavior test that asserts on the artifact.

* **``modules_dl.ModuleFetchError`` type.** A 404 (or any HTTP error)
  on a module download used to raise a bare ``RuntimeError`` that
  ``cli.main()`` didn't catch, so v0.2 users running the recommended
  migration recipe (``cp examples/stealth.yaml alpha/beetroot.yaml &&
  beetroot apply alpha``) saw a Rich-rendered traceback when the
  hard-coded Shamiko URL 404'd. New ``ModuleFetchError`` (subclass of
  ``RuntimeError`` for backward compat) is caught in ``cli.main``.
  Pinned by ``tests/test_module_fetch_error.py``.
* **Module URL scheme allowlist.** ``_fetch_url`` accepted any URL
  scheme; ``url: file:///etc/passwd`` in a ``beetroot.yaml`` would
  silently exfiltrate that host file into the module cache. The
  ``Module`` pydantic validator now rejects non-http(s) schemes at
  parse time and ``_fetch_url`` re-checks the prefix. Pinned by
  ``tests/test_module_url_scheme.py``.
* **``examples/stealth.yaml`` no longer ships a hard-coded Shamiko
  URL.** The upstream release URL changes whenever LSPosed cuts a
  new tag, so the example went stale silently. The ``modules:``
  block is commented out with a pointer to the LSPosed releases
  page. ``docs/guides/examples.md`` is updated to match.
* **Orphan-aware ``beetroot ls``.** A registry entry whose on-disk
  directory was ``rm -rf``'d behind the CLI's back used to crash
  ``ls`` with a Rich-rendered ``FileNotFoundError``. ``Manager.list()``
  now skips orphans; ``Manager.list_orphans()`` surfaces them; the
  CLI ``ls`` verb appends a trailing skip-line ``(skipping N orphan
  entries: <names>; clean up with 'beetroot destroy <name> -y')``.
  ``cli.destroy`` no longer calls ``compose.down`` on an orphan
  (would FileNotFoundError on the subprocess ``cwd``). ``cli.main``
  catches bare ``FileNotFoundError`` and ``ModuleFetchError`` as
  belt-and-suspenders. Pinned by ``tests/test_orphan_registry.py``.
* **``cli.shell`` and ``cli.frida`` propagate subprocess exit codes.**
  ``Instance.shell()`` and ``Instance.frida_cli(args)`` both return
  the subprocess exit code as ``int``; the verbs used to discard it.
  Research scripts that check ``$?`` after ``beetroot shell <name>
  -c '<cmd>'`` always saw 0. Now ``raise typer.Exit(code=rc)`` when
  non-zero. Pinned by
  ``tests/test_cli.py::TestCmdShell::test_shell_propagates_non_zero_exit_code``
  and the matching ``TestCmdFrida`` case.
* **Hidden ``up --build`` deprecation hint.** T5 removed
  ``--build`` from ``up``; v0.2 users running ``beetroot up alpha
  --build`` got a Rich "No such option: --build" box instead of the
  friendly migration line the other deprecations print. A hidden
  ``--build`` flag now prints
  ``error: 'beetroot up --build' was removed in v0.3 — run 'beetroot
  build' separately first to rebuild the image.``. Pinned by the
  existing
  ``tests/test_cli.py::TestCmdUp::test_up_rejects_build_flag``,
  now asserting on the hint string.
* **Partial-failure rollback in three constructors.**
  ``Instance.create`` / ``Instance.register`` / ``snapshot.restore``
  all wrote on-disk state BEFORE the port-collision check + ``_stage``.
  A failure mid-stream left the registry row, the directory, and the
  YAML behind. Each constructor now wraps the failure-prone steps in
  try/except, removes the registry row on rollback, and ``rmtree``s
  the target dir only when Beetroot created it in that call (tracked
  via a local ``created_dir`` flag — adopted dirs like ``register``'s
  user-supplied path and the CLI's ``--from-data``-prepopulated dir
  are preserved). Pinned by ``tests/test_partial_failure_rollback.py``.
* **``api_version: 1`` auto-bump warning deduplicates per path.**
  The warning used to fire on every load; over 5 v0.2 instances
  ``beetroot ls`` printed 5+ copies, and ``register bravo``
  triple-printed because ``all_resolved_ports`` cascaded into the
  same load twice. Module-level ``set[Path]`` records which paths
  we've already warned about in this process; first load prints,
  subsequent loads skip. Pinned by ``tests/test_api_version_auto_bump.py``
  (``test_auto_bump_warning_deduplicates_per_path`` +
  ``test_auto_bump_warning_fires_per_distinct_path``).
* **``cli.restore`` drops the stale apply-then-up hint.** Agent A
  made ``snapshot.restore`` call ``_stage()`` itself, so
  ``beetroot apply`` is no longer needed before ``beetroot up``;
  the printed ``next:`` line is updated to match. Pinned by the
  existing ``TestCmdRestore::test_restore_round_trips_through_cli``,
  now asserting on the post-restore hint substring.
* **``pyproject.toml`` classifiers trimmed.** Listed
  ``Python :: 3.10/3.11/3.12/3.13`` but ``requires-python =
  ">=3.13"``; the stale rows were misleading for PyPI search.
  Down to ``Python :: 3`` + ``Python :: 3.13``. Pinned by
  ``tests/test_packaging.py::test_python_version_classifiers_match_requires_python``.
* **Migration guide fixes (invented flags, auto-rename text,
  status example).** ``docs/guides/migration-v0.2-to-v0.3.md``
  recommended ``beetroot destroy --no-rm <name>`` — a flag that
  doesn't exist. It also misrepresented the auto-rename behaviour
  (the XDG-path migration auto-renames; the ``$PWD`` v0.2 detection
  just prints a stderr hint) and showed ``STATUS = exited`` in a
  context where ``not-created`` is the correct value. All three are
  corrected; ``tests/test_doc_grep.py`` is tightened with anchored
  regex rules for ``destroy --no-rm`` / ``--no-data`` /
  ``--no-deregister`` so the same class of invented flag can't
  reappear, plus a dedicated
  ``test_migration_guide_does_not_invent_destroy_flags`` that
  greps the guide specifically.
