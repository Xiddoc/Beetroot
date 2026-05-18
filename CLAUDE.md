# CLAUDE.md

Project conventions for contributors working on Beetroot. Reading this end-to-end gives you the architecture, the development workflow (uv-based), and the style rules (ruff, mypy, Google docstrings, no inline comments unless explaining *why*).

## What this repo is

Beetroot: a Docker-packaged rooted Android-14 sandbox (redroid + Magisk + optional GMS (LiteGapps by default) + Houdini) plus a Python CLI (`beetroot`) that lets researchers manage **multiple persistent "research phones"** at once. Each instance has its own `/data`, its own ADB + Frida ports, its own RAM/CPU caps, and its own `beetroot.yaml` config.

There is no application code here — the deliverable is the container image, the boot scripts that configure it, and the CLI that orchestrates instances.

## Commands

- `uv sync` — install the CLI's Python deps (PyYAML + Pydantic) into a project-local `.venv`. See [Development workflow](#development-workflow) for lint, type-check, and dev deps. *Contributors only.* End users should install with `uv tool install git+https://github.com/Xiddoc/Beetroot.git`, which exposes `beetroot` on `PATH` without the `uv run` prefix. The host-side `frida` CLI used by `beetroot frida` is **optional** and exposed via a `[frida]` extra (`uv sync --extra frida` in-tree, or `uv tool install 'beetroot[frida]'` for end users); plain installs omit `frida-tools` and `beetroot frida` errors out with an install hint.
- `uv run beetroot <verb>` — invoke any CLI verb during development. Verbs: `create`, `up`, `down`, `restart`, `destroy`, `ls`, `logs`, `shell`, `env`, `frida`, `module`, `apply`, `setup`. Run `beetroot <verb> --help` for flags. (With a `uv tool install`-based setup, drop the `uv run` prefix entirely.)
- `beetroot setup [variant]` (or `uv run beetroot setup [variant]` in-tree) — one-time bootstrap. Clones `ayasa520/redroid-script` into `/tmp/redroid`, runs its patcher via `uv` to produce a local base image (e.g. `redroid/redroid:14.0.0_litegapps_houdini_magisk`), then `docker compose build`s the research layer on top of it via the `BASE_IMAGE` build arg. The optional argument selects the GMS variant: `none`, `lite` (default), `full`, or `mindthegapps`. Re-run once per variant whenever the base image needs to be regenerated. The implementation lives in `src/beetroot/setup_runner.py` (named `setup_runner` because `setup.py` would collide with the historical Python build-system filename).
- `docker compose -p <name> -f <bundled-compose> --project-directory <instance-dir> --env-file <instance-dir>/.env <subcommand>` — the raw escape hatch. The CLI just wraps this; if the CLI breaks, you can still drive instances directly. The bundled compose template lives at `src/beetroot/templates/compose.yaml` (resolve at runtime via `paths.bundled_compose_file()`).

A typical flow: `beetroot create alpha` → `beetroot up alpha` → `beetroot shell alpha`.

## Documentation

The docs site lives under `docs/` and is built with [mkdocs-material](https://squidfunk.github.io/mkdocs-material/). The published site is at <https://xiddoc.github.io/Beetroot/>.

To preview locally:

```bash
uv sync --group docs
uv run mkdocs serve  # http://127.0.0.1:8000
```

To build (without serving):

```bash
uv run mkdocs build --strict
```

The build output goes to `site/` (gitignored). The GitHub Actions workflow at `.github/workflows/docs.yml` deploys to GitHub Pages on every push to `main`.

## Architecture

**One bundled compose template, many projects.** Every instance shares the same compose template (shipped inside the wheel at `src/beetroot/templates/compose.yaml`). The CLI invokes compose with `-p <instance-name>` (separate Docker project per instance), `-f <bundled-template>`, `--project-directory <instance-dir>` (so the template's relative bind-mounts resolve correctly), and `--env-file <instance-dir>/.env` (per-instance ports, resources, display knobs). Project-per-instance gives true isolation — `docker compose -p alpha down` doesn't touch `bravo`.

**Single-stage Dockerfile** (`docker/Dockerfile`). Only `entrypoint.sh` and `stealth.rc` are `COPY`'d into the redroid base — there is nothing else baked in. SQL queries against the Magisk DB use `magisk --sqlite` (Magisk ships with its own sqlite, so we don't need to ship a separate Bionic-built static binary). Frida is **not** in the image — it's bind-mounted per-instance (see below) so each phone can pin its own Frida version without rebuilding.

**Boot-time wiring is via Android init, not Docker.** There is no `ENTRYPOINT` — the container uses redroid's default boot. Android `init` parses `stealth.rc` (placed at `/system/etc/init/stealth.rc` at build time) and triggers `entrypoint.sh` on `sys.boot_completed=1` under the `u:r:magisk:s0` SELinux context. `entrypoint.sh` runs with `/system/bin/sh` (Android's toybox-derived shell) and waits for `/data/adb/magisk.db` to exist before doing anything.

**`entrypoint.sh` configures Magisk by writing directly to its sqlite DB** (`/data/adb/magisk.db`) — enables Zygisk + denylist, adds `com.google.android.gms` / `com.google.android.gms.unstable` to the denylist so GMS processes don't see root. Then `magisk --install-module`s every zip in `/flash_dir`. Finally launches `/data/local/tmp/frida-server &` if it's executable. The trailing `wait` keeps the script alive so logs stream to `docker compose logs`.

**Per-instance state lives in the instance directory itself** (any path on disk that contains a `beetroot.yaml`):
- `beetroot.yaml` — the source of truth for this instance (display, resources, Frida version, modules, denylist). Commit it if you want a reproducible config.
- `.env` — generated from `beetroot.yaml`; consumed by compose. Re-rendered on `beetroot apply`.
- `data/` — bind-mounted to `/data` inside the container. Persists across restarts.
- `modules/` — bind-mounted read-only to `/flash_dir`. The CLI mirrors `beetroot.yaml`'s `modules:` list into here on `apply`.
- `frida-server` — bind-mounted to `/data/local/tmp/frida-server`. Downloaded by the CLI from `github.com/frida/frida/releases` and decompressed on the host.

**Port allocation** (`src/beetroot/ports.py`): stride-of-10 by index. Index 0 → ADB 5555, Frida 27042/27043. Index 1 → ADB 5565, Frida 27052/27053. Etc. Index is stored in the cross-instance registry at `~/.config/beetroot/instances.json` (respects `$XDG_CONFIG_HOME`) and freed on `destroy`. Allocation reuses freed slots — `lowest_free_index`.

**Container status is queried live from `docker compose ps`**, never cached in the registry. The registry can't lie about runtime state.

## CLI internals

```
src/beetroot/
├── cli.py         # argparse dispatch, all verb implementations
├── config.py      # beetroot.yaml schema (pydantic) + .env render
├── settings.py    # env-driven overrides (BEETROOT_* vars) via pydantic-settings
├── ports.py       # stride-10 allocator
├── registry.py    # instances.json, fcntl.flock guards mutations
├── compose.py     # subprocess wrappers around `docker compose`
├── frida_dl.py    # download frida-server.xz, decompress (lzma), cache
├── modules_dl.py  # fetch + sha256-verify Magisk module zips
├── setup_runner.py # one-time base-image bootstrap (`beetroot setup`)
└── paths.py       # single source of truth for filesystem layout
```

`paths.instance_root()` resolves to the directory containing `beetroot.yaml` via upward search from the current working directory (the `beetroot.yaml` is the marker — same model as git's `.git` and uv/pip's `pyproject.toml`). Running the CLI from a directory with no `beetroot.yaml` in any ancestor raises `paths.InstanceRootNotFoundError`, which `cli.main()` converts to a friendly `error: ...` and `exit 1`. The bundled compose template is resolved via `paths.bundled_compose_file()` (which uses `importlib.resources`), so the CLI works identically whether installed editable (`uv sync`) or as a tool (`uv tool install .`). The cross-instance registry lives at `paths.user_registry_file()`.

## Things to know when editing

- **`docker/entrypoint.sh` runs inside Android.** Android's userland is toybox-derived — no GNU coreutils, no bash. Stick to POSIX sh and toybox-compatible flags. Magisk's sqlite schema is load-bearing; do not refactor the DB writes.
- **`docker/stealth.rc` is Android init syntax**, not arbitrary text. `exec_background u:r:magisk:s0` is a SELinux context. If you don't know what that means, don't touch this file.
- **The base image tag** is derived at runtime from `android.version` and `android.gapps` in `beetroot.yaml` by `config.base_image_tag()` (e.g. `version: 14, gapps: lite` → `redroid/redroid:14.0.0_litegapps_houdini_magisk`). The tag is injected into the build via the `BASE_IMAGE` ARG in `docker/Dockerfile` and the `${BASE_IMAGE}` substitution in `src/beetroot/templates/compose.yaml`. The patcher that produces the base image is `beetroot setup <variant>` (wrapping `ayasa520/redroid-script`); run it once per GMS variant. Bumping Android version, gapps flavor, or translation layer means re-running the patcher with the appropriate flags.
- **The bundled `compose.yaml` is templated** — every `${VAR}` must have a corresponding line in `render_env()` in `src/beetroot/config.py`. If you add a new substitution, update both.
- **`api_version` gates the schema.** `InstanceConfig` carries a top-level `api_version: int` (currently `1`, tracked by `SUPPORTED_API_VERSION` in `src/beetroot/config.py`). The default lets old YAMLs that omit the field keep working; pinning a non-matching value raises a `ValidationError` pointing at `CHANGELOG.md`. When a future change breaks the schema, bump `SUPPORTED_API_VERSION` and add a migration entry to `CHANGELOG.md`.
- **`mem_limit` and `cpus` are top-level keys, not under `deploy:`.** The `deploy:` form is Swarm-only and silently ignored by `docker compose up`. This also applies to `mem_reservation`, `memswap_limit`, and `pids_limit` — all top-level keys in the bundled compose template.
- **Environment-driven overrides** are provided by `src/beetroot/settings.py` (`Settings(BaseSettings)`). The following `BEETROOT_*` environment variables are recognised: `BEETROOT_DOCKER_BIN` (default: `docker`), `BEETROOT_FRIDA_ARCH` (default: `android-x86_64`), `BEETROOT_HTTP_TIMEOUT` (default: `30`). These are read at import time; override them before launching the CLI.
- **Docs are part of every feature.** Touching a CLI verb, install path, schema field, or any user-facing string means grepping `docs/` and `README.md` for old spellings and updating every hit — not just the obvious page. v0.2 shipped multiple features (`uv tool install`, the `[frida]` extra) with README + one docs page updated while three other pages still showed the old guidance; see retros on `fix/uv-tool-install-docs` and `fix/frida-extra-docs` in `CHANGELOG.md`. Before commit, grep the surface that changed:
  - CLI verb rename: `grep -rn '<old-verb>' docs/ README.md`
  - Install path (`alias`, `pip install`, `uv run`, `uv tool install`): `grep -rn 'alias beetroot\|pip install frida-tools\|uv run beetroot ' docs/ README.md`, then prune contributor-aside callouts from the hits.
  - Schema rename: `grep -rn '<old-field>' docs/ src/beetroot/templates/presets/ README.md`.

## Development workflow

The project uses [`uv`](https://github.com/astral-sh/uv) exclusively as the package manager. Never invoke `pip` directly — `uv` owns the virtual environment.

**Setup**

```bash
uv sync              # install runtime deps only
uv sync --group dev  # also install dev tools (ruff, mypy, types-PyYAML, pytest, pytest-cov)
```

Dev tools live in `[dependency-groups].dev` in `pyproject.toml` (PEP 735 `dependency-groups`, not `[project.optional-dependencies]`).

**Adding or updating deps**

```bash
uv add <package>              # add a runtime dep
uv add --group dev <package>  # add a dev-only dep
uv lock                       # regenerate uv.lock after manual edits
```

**Lint**

```bash
uv run ruff check src/beetroot/          # check for violations
uv run ruff check --fix src/beetroot/    # auto-fix fixable violations
uv run ruff format src/beetroot/         # auto-format (optional; no hard policy)
```

Ruff is configured in `[tool.ruff]` / `[tool.ruff.lint]` — target Python 3.10, line length 100, with a strict rule set covering 20+ families including `D` (pydocstyle, Google convention).

**Comments vs. docstrings.** Inline comments (`#`) should be rare and only explain *why* something is done — not *what*. Docstrings on public APIs are required and enforced by ruff's `D` rules with Google convention (`convention = "google"`). The docstring style is D213: the summary goes on the line *after* the opening `"""`, not on the same line. Private functions (leading `_`) do not require docstrings. Tests are per-file-ignored from `D` — test function names should be self-describing.

**Type checking**

```bash
uv run mypy src/beetroot/
uv run mypy tests/
```

Mypy is configured in `[tool.mypy]` with `strict = true` and the `pydantic.mypy` plugin enabled. The plugin gives mypy full visibility into pydantic model `__init__` signatures, `model_validate` return types, and `ConfigDict` options. Both `src/beetroot/` and `tests/` must type-check — do not add `# type: ignore` without an error code and a brief comment explaining why.

**Tests**

```bash
uv run pytest                                         # full suite (coverage gate is wired into addopts)
uv run pytest --cov=beetroot --cov-report=term-missing  # equivalent — explicit cov flags
```

Tests live under `tests/` and use pytest's built-in mocking (`unittest.mock`) — no real network or docker calls. `conftest.py` provides two composable fixtures: `isolated_registry` (points `$XDG_CONFIG_HOME` and `$XDG_CACHE_HOME` at a per-test tmp dir) and `isolated_instance` (creates a minimal instance dir and `chdir`s into it). Most CLI/registry tests use the `cli_root` composite fixture, which layers `isolated_registry` with stubbed `shutil.which` + a no-op `frida_dl.download`.

**Coverage**

100% line + branch coverage on `src/beetroot/` is mandatory. `[tool.pytest.ini_options]` invokes `--cov=beetroot --cov-report=term-missing` automatically and `[tool.coverage.report].fail_under = 100` makes `uv run pytest` exit non-zero if the threshold isn't met. New code must come with new tests. CI runs the same gate; the pre-push hook (`.pre-commit-config.yaml`) catches it locally before the push hits the remote.

**Behavior tests, not just line coverage.** Line + branch coverage is necessary but not sufficient. When a user-facing behavior emerges from the *composition* of two or more pieces of code (config-model → resolver → `.env` render; `cmd_create` → port allocator → registry write → compose start), ship at least one test that drives the full **user input → final artifact** path and asserts on the artifact. `feat/configurable-ports` hit 100% line + branch on `ports.py` and still shipped a silent port self-collision after a partial override, because no test asserted on the resolved `.env` dict given the model input; see `fix/ports-resolver-self-collision` in `CHANGELOG.md` for the fix and the corrective test pattern.

One-time setup of the pre-push hook:

```bash
uv sync --group dev
uv run pre-commit install --hook-type pre-push
```

After that, every `git push` runs the full test suite + coverage gate. Failures block the push.

**Running verbs**

```bash
uv run beetroot <verb>   # contributor workflow (project-local .venv)
```

For a system-wide install of your working tree (so plain `beetroot <verb>` works without the `uv run` prefix), run `uv tool install .` from the repo root. Re-run it whenever you want the installed copy to catch up with your edits.

**CI**

GitHub Actions runs ruff, mypy, and pytest on every push to `main` and on every pull request targeting `main`. The workflow is at `.github/workflows/ci.yml`. To replicate CI locally, run the three commands listed above under Lint, Type checking, and Tests — they are exactly what CI executes.

## What stays gitignored

Instance directories live anywhere on disk and are gitignored at the user's discretion. Within any instance dir: `data/`, `modules/`, `frida-server`, `.env` should be gitignored. `beetroot.yaml` is **not** ignored — it's a config the researcher may want to commit. The cross-instance registry (`~/.config/beetroot/instances.json`) is per-host and never tracked.
