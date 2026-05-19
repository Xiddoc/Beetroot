# Migrating from v0.3 to v0.4

Beetroot v0.4 is a foundation release: it lands the pydantic-typed
registry, the second device backend (`AdbDevice` over the host `adb`
CLI), three new user-facing verbs (`adopt`, `status`, `doctor`), a
hardened CI / lint / type-check stack, and the plumbing for v0.5's
randomized-path stealth work. The CLI auto-migrates v0.3 registries
and YAMLs (one warning per process), so "do nothing" gets you most of
the way — but a handful of breaking changes need attention.

If you'd rather scan the changes than walk through them, the headline
list lives at the top of
[`CHANGELOG.md`](https://github.com/Xiddoc/beetroot/blob/main/CHANGELOG.md#v040--2026-05-19)
under **Breaking changes**. This page expands each bullet into the
exact command or edit you run.

## 1. Schema bump: `api_version: 2` → `api_version: 3`

`SUPPORTED_API_VERSION` is now `3`. v0.3 `beetroot.yaml`s that
hard-pinned `api_version: 2` auto-bump on load with a one-line stderr
warning (the bump is strictly additive — no field renames, no
defaults moved), so this is informational. v0.2 YAMLs (`api_version:
1`) continue to auto-bump too, so v0.2 → v0.4 in one hop is
supported. Persistence happens organically on the next `beetroot
apply <name>`.

To silence the warning, either bump the field or omit it (the default
is now `3`):

```yaml
# Before
api_version: 2
android:
  version: 14

# After (either form is fine)
api_version: 3
android:
  version: 14
```

## 2. Registry schema v2 → v3 migration

The cross-instance registry at
`~/.config/beetroot/instances.json` (now resolved via `platformdirs`
— XDG vars are honoured automatically on Linux) is now round-tripped
through a strict pydantic model (`RegistryFile` →
`InstanceMeta` → discriminated-union `BackendConfig` over `kind:
"redroid"` and `kind: "adb"`). v2 registries are renamed to
`instances.json.bak` on first read and a fresh empty v3 file is
emitted — same backup-and-empty pattern v0.3 used for v1 → v2.

After the upgrade, re-register each of your existing instances:

```bash
beetroot register /path/to/alpha
beetroot register /path/to/bravo
```

`beetroot register <path>` reads the `beetroot.yaml` at the path,
allocates a fresh port index, and writes a `kind: "redroid"` row into
the new v3 registry. The instance's `data/`, `modules/`, and
`beetroot.yaml` are untouched.

The v0.3 backup at `instances.json.bak` is left in place — keep it
until you've confirmed the upgrade went through cleanly.

## 3. The `kind` discriminator + `backend` field on `InstanceMeta`

Programmatic users who read the registry directly will see the new
shape:

```python
from beetroot import registry

meta = registry.get("alpha")
# meta is registry.InstanceMeta:
#   meta.backend        — discriminated-union BackendConfig
#   meta.backend.kind   — "redroid" | "adb" | <third-party>
#   meta.index          — stride-of-10 port index
#   meta.created_at     — datetime
#
# Narrow on .kind / isinstance to access kind-specific fields:
if isinstance(meta.backend, registry.RedroidBackendConfig):
    print(meta.backend.absolute_path)
elif isinstance(meta.backend, registry.AdbBackendConfig):
    print(meta.backend.serial)
```

The legacy `meta["absolute_path"]` / `meta["index"]` dict-access shape
is gone. v0.3 code that walked the raw JSON needs to either go
through `registry.list_instances()` (which returns
`dict[str, InstanceMeta]`) or read `registry.instance_path(name)` /
`registry.used_indices()` etc.

## 4. The `stealth_paths` slot (PR6 plumbing landed; defaults empty)

`registry.RedroidBackendConfig` gains an empty `stealth_paths:
dict[str, str]` field. v0.4 defaults it to `{}`; v0.5's stealth PR1
will populate it with `{magisk_db, modules_dir, frida_bin}` keys once
a safe randomized path is validated (see the
[stealth-posture design doc](../design/stealth-posture.md#31-move-frida-off-datalocaltmp)
for the research prerequisite). Snapshot / restore round-trips the
slot through the manifest's `path_layout` field, so a v0.5 snapshot
restored against a v0.4 host (or vice versa) lands cleanly.

A new `registry.set_stealth_paths(name, blob)` helper writes the
slot; `config.render_env(..., stealth_paths=...)` consumes it. v0.4
researchers can pre-populate the slot by hand if they want to test
custom paths before v0.5 ships, but the load-bearing user story is
"don't touch this".

## 5. New verbs: `adopt`, `status`, `doctor`, and `env --all`

* **`beetroot adopt <serial> [--name NAME]`** — register a
  rooted Android device that's already reachable via `adb` under the
  global registry. No on-disk instance directory is made — the
  device is managed outside Beetroot. The adopted instance gets its
  own stride-of-10 port index so a follow-up `beetroot frida <name>`
  picks the same port a redroid instance with the same index would
  have got. See [`adopt` in the CLI reference](../reference/cli.md#adopt).

* **`beetroot status <name>`** — print a single-instance JSON snapshot
  to stdout (reuses the row formatter that backs `ls --json`).
  Exits 0 on success, 1 if the name isn't in the registry. See
  [`status` in the CLI reference](../reference/cli.md#status).

* **`beetroot doctor <name>`** — run aggregated health checks.
  Output is one `<check>: pass|fail|skip [reason]` line per check.
  Exit 0 if every check passes; otherwise the exit code is the count
  of `fail` results (capped at 255). See
  [`doctor` in the CLI reference](../reference/cli.md#doctor).

* **`beetroot env <name> --all`** — extends the existing `env` verb.
  Without `--all`, `env` keeps its v0.3 shape (exactly two `export`
  lines: `ANDROID_DEVICE` + `FRIDA_DEVICE`) so `eval $(beetroot env
  alpha)` scripts keep working. With `--all`, every key from
  `config.render_env()` is emitted as a shell export. See
  [`env` in the CLI reference](../reference/cli.md#env).

## 6. Module renames: `frida_dl` → `frida_download`, `modules_dl` → `modules_download`

The two `_dl` suffixes were the last ambiguity in the public module
surface. Programmatic users:

```python
# Before
from beetroot import frida_dl, modules_dl

# After
from beetroot import frida_download, modules_download
```

The module-level public surface (`download`, `stage_for_instance`,
`stage_empty`, `sha256_of`, `release_url`, `cached_binary`,
`frida_cache_dir`, `ModuleFetchError`) is otherwise unchanged.

## 7. `Manager.allocate_port_index` removed

The public `Manager.allocate_port_index()` method is gone (Agent 2
F-4: the index isn't reserved by the call, so calling it without an
immediate follow-up `registry.add` is a footgun — and we hit that
race more than once during v0.3). Use the atomic
`registry.add_allocating(name, ...)` instead:

```python
# Before (footgun — index can collide if another caller allocates
# between this line and your registry.add)
index = api.Manager.allocate_port_index()
# ... maybe yield ...
registry.add(name, absolute_path=path, index=index)

# After (atomic — one file lock spans allocation + write)
index = registry.add_allocating(name, path)
# OR for the v0.4 backend-typed form (what `beetroot adopt` calls):
index = registry.add_allocating(
    name, backend=registry.AdbBackendConfig(serial="emulator-5554"),
)
```

The private `api._allocate_port_index()` helper exists for internal
`Manager` callers but is not public surface.

## 8. `Settings.extra="forbid"` — typo'd env vars now fail loudly

v0.3's `Settings(extra="ignore")` silently dropped misspelled
`BEETROOT_*` env vars. v0.4 flips to `extra="forbid"`, so any
`BEETROOT_*` env var that isn't a declared field on `Settings` now
raises `ValidationError` at import time:

```bash
# v0.3: silently ignored; user thought they were overriding the
# Magisk DB path but Beetroot was reading the default.
$ BEETROOT_MAGSIK_DB=/custom/path beetroot up alpha   # typo
[beetroot] alpha up — ADB localhost:5555, Frida localhost:27042

# v0.4: fails at import time with a clear pointer.
$ BEETROOT_MAGSIK_DB=/custom/path beetroot up alpha
ValidationError: 1 validation error for Settings
BEETROOT_MAGSIK_DB
  Extra inputs are not permitted [type=extra_forbidden, ...]
```

The four `BEETROOT_*` vars **newly declared as fields** in v0.4 (so
they don't trip the strict-extras flip if you were already
exporting them through the helper-shell chain):

* `BEETROOT_MAGISK_DB` — container-side path to Magisk's sqlite DB.
* `BEETROOT_MODULES_DIR` — container-side bind-mount target for
  staged modules (defaults to `/data/adb/modules_update/`; see §11
  below).
* `BEETROOT_FRIDA_BIN` — container-side path to the staged frida-server.
* `BEETROOT_BUILD_CONTEXT` — `beetroot build` working-dir override.

The v0.3 trio (`BEETROOT_DOCKER_BIN`, `BEETROOT_FRIDA_ARCH`,
`BEETROOT_HTTP_TIMEOUT`) is unchanged.

## 9. `Settings` dropped `env_file=".env"`

v0.3's `SettingsConfigDict(env_file=".env", ...)` made every
`Settings()` instantiation walk the **current working directory's**
`.env` file. Inside an instance directory that's the
Docker-compose env file (`INSTANCE_NAME=…`, `ADB_PORT=…`, etc.) —
values Beetroot must not pick up. v0.4 drops `env_file`; settings
read strictly from `os.environ`.

If you genuinely want a host-wide `.env` to bootstrap `BEETROOT_*`
vars, export them via your shell's profile or use a tool like
`direnv` outside Beetroot's import path.

## 10. `Frida.version` regex validation + optional `Frida.sha256`

`Frida.version` now matches `^[0-9]+\.[0-9]+\.[0-9]+$`. Typos like
`frida: { version: "16.4.10a" }` or `16.4` now fail at config-load
time with a clear pydantic error rather than as a 404 from
`github.com/frida/frida/releases` at download time:

```yaml
# Fails at load
frida:
  version: "16.4"           # missing patch component
  version: "16.4.10-rc1"    # non-numeric
  version: latest           # non-version-shaped

# OK
frida:
  version: "16.4.10"
```

A new optional `sha256` field is forwarded to the downloader; if set,
the cached binary's digest is verified case-insensitively and a
mismatch raises `ValueError`:

```yaml
frida:
  version: "16.4.10"
  sha256: "abcd0123..."   # hex; case-insensitive
```

## 11. Mount target `/flash_dir` → `/data/adb/modules_update/`

v0.3 bind-mounted `<instance-dir>/modules/` to `/flash_dir` inside
the container — a Beetroot-invented mountpoint with no upstream
meaning, and a fingerprint in `/proc/mounts` for any GMS-style scan.
v0.4 moves the target to `/data/adb/modules_update/`, Magisk's
standard module-staging directory. Magisk's daemon recognises modules
there at boot and installs them on the next reboot, exactly as it
did from `/flash_dir`.

**Migration for existing v0.3 instances:** one `down + up` cycle
after the upgrade rebinds the mount:

```bash
beetroot down alpha && beetroot up alpha
```

The host-side `<instance-dir>/modules/` directory does not move; only
the container-side bind-mount target changes. No `apply` is needed.

## 12. `BackendCapabilityError` → exit code 2

Verbs that don't generalise across backends (`up`, `down`,
`restart`, `apply`, `destroy`, `snapshot`) raise
`BackendCapabilityError` when called against a non-redroid backend
(typically: an adb-adopted device). v0.4 exits the CLI with **code
2** for this case — distinct from "instance not found" → exit `1`
and from generic uncaught errors → exit `>= 1`. Wrapping shell
scripts can now distinguish:

```bash
beetroot up some-adopted-phone
case $? in
  0) echo "ok" ;;
  1) echo "no such instance" ;;
  2) echo "verb unsupported by this backend" ;;
  *) echo "other error" ;;
esac
```

## 13. Instance-name regex `[a-z0-9_-]+`

Names must match the Docker compose project-name grammar
(`[a-z0-9_-]+`) at the OOP boundary, before any side effect runs. v0.3
silently accepted `Alpha`, `alpha bravo`, `alpha.bravo` (capitals,
spaces, dots), then `docker compose` blew up at the first `up` with a
cryptic error. v0.4 rejects bad names cleanly in
`Instance.create` / `Instance.register`:

```python
# v0.4 raises ValueError early — no mkdir, no registry write, no port
# allocation for a bad name.
Instance.create("Alpha")           # ValueError: invalid name
Instance.create("alpha bravo")     # ValueError
Instance.create("alpha.bravo")     # ValueError
Instance.create("alpha-bravo")     # OK
Instance.create("alpha_bravo")     # OK
Instance.create("alpha2")          # OK
```

The default basename used by `Instance.register(path)` (when `name=`
is omitted) goes through the same gate.

## 14. `bundled_compose_file` uses `importlib.resources.as_file()`

This is invisible to most users — but if you call the
`paths.bundled_compose_file()` helper directly (e.g. to drive
`docker compose` outside of the CLI), v0.4 now resolves the bundled
template via `importlib.resources.as_file()` and caches it under
`platformdirs.user_cache_path("beetroot") / "templates" /
"compose.yaml"`. This works correctly under wheel installs (where
the resource lives inside a zip), pipx / pex / shiv bundles, and
plain `uv tool install` setups alike. v0.3's `Path(str(files()))`
form silently broke under zip-backed installs.

## 15. `platformdirs` for cache + config paths

`paths._xdg_dir` is gone; user config + cache paths now resolve via
`platformdirs.user_config_path("beetroot")` and
`platformdirs.user_cache_path("beetroot")`. On Linux the `XDG_*` env
vars are honoured automatically (so `~/.config/beetroot/` and
`~/.cache/beetroot/` still work); on macOS / Windows the paths now
match platform conventions (e.g.
`~/Library/Application Support/beetroot` on macOS).

The `builder._DEFAULT_WORK_DIR = Path("/tmp/redroid")` from v0.3 is
also gone — `beetroot build` now clones `ayasa520/redroid-script`
into `user_cache_dir("redroid-script")` instead of `/tmp`. This stops
the clone from being wiped by aggressive `/tmp` cleaners between
builds and closes Agent 4's `S108` bandit finding.

## 16. Verify with `beetroot ls` + `beetroot doctor`

```bash
beetroot ls
```

```
NAME    IDX  ADB             FRIDA            STATUS         PATH
alpha   0    localhost:5555  localhost:27042  not-created    /home/you/alpha
bravo   1    localhost:5565  localhost:27052  not-created    /home/you/bravo
```

Re-registered redroid instances show `STATUS = not-created` until
their first `beetroot up <name>`.

```bash
beetroot doctor alpha
```

Runs the v0.4 health-check verb and prints one line per check.
Useful for sanity-checking that `magisk.zygisk = 1`, `adb.connect`
succeeds, and the GMS denylist enrolment is in place after the upgrade.

## v0.5 known limitations (deferred from v0.4)

A handful of items the v0.4 sprint flagged but couldn't ship cleanly
in-window. These are tracked for v0.5; here so contributors and
researchers know the rough edges.

1. **`beetroot adopt` doesn't verify the serial exists** via `adb
   devices` before writing to the registry. The pre-registration
   shape is intentional (you can adopt a device before plugging it
   in), but a typo silently creates a dead registry row. v0.5
   candidate: opt-in `--verify` flag that runs `adb devices` first
   and refuses to write if the serial isn't listed.
2. **Dual-form `registry.add` / `add_allocating` lives forever.**
   v0.4 accepts both the v0.3 positional `(name, absolute_path,
   index)` form and the new `backend=BackendConfig(...)` keyword form,
   so existing programmatic code keeps working. v0.5+ should consider
   marking the v0.3 positional form `@deprecated` and dropping it in
   v0.6.
3. **Third-party backends can't round-trip through `RegistryFile` JSON
   yet.** v0.4's `BackendConfig` discriminated union only knows about
   `redroid` and `adb`; a third-party `kind` value in a saved registry
   row currently raises `ValidationError` on the next `_read`. This
   means in v0.4, a third-party backend can be **used in-process**
   (via `register_backend("cloud-xyz", CloudBackend)` + an in-memory
   `Manager.resolve` call), but its registry rows can't survive a
   Beetroot restart. v0.5 will add a registry-side extension hook for
   third-party `BackendConfig` subclasses to opt into JSON
   discrimination. See [Adding a backend](adding-a-backend.md) for
   the full "what works now vs deferred" breakdown.
4. **Stealth research prerequisite gates the PR1 default flip.** v0.4
   shipped the `stealth_paths` plumbing (PR6) and the
   `/data/adb/modules_update/` mount swap (PR5), but did **not** flip
   the default Frida path off `/data/local/tmp/`. The flip is
   deferred to v0.5 pending a written decision on a safe randomized
   path — see the
   [stealth-posture design doc](../design/stealth-posture.md#31-move-frida-off-datalocaltmp)
   for the research prerequisite and the candidate paths.

## Troubleshooting

**`ValidationError: Extra inputs are not permitted` at startup.**
Cross-reference §8 above — you have a typo'd `BEETROOT_*` env var
exported. The error message lists the offending key. Fix the typo
(or un-export it) and retry.

**`beetroot up` complains about `/flash_dir` not being a mount target.**
Run `beetroot down <name> && beetroot up <name>` once. The bundled
compose template's bind-mount target moved to
`/data/adb/modules_update/` (§11) and the rebind happens on the next
`up`.

**`beetroot adopt phone` exits 1 with "registry name 'phone' is
invalid".** Names must match `[a-z0-9_-]+` (§13). `phone` itself is
fine — but if you fed an IPv4-shaped serial without `--name`, the
default-name builder (which folds colons to hyphens but leaves dots
in place) produces an invalid name. Pass `--name <something>`
explicitly:

```bash
beetroot adopt 192.168.1.10:5555 --name phone-lab-1
```

**Programmatic code that walked `meta["absolute_path"]` raises
`TypeError`.** §3 — switch to attribute-access on `InstanceMeta` and
narrow via `isinstance(meta.backend, registry.RedroidBackendConfig)`.
