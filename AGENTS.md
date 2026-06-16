# AGENTS.md

The contributor and agent playbook for Beetroot: how to drive the CLI, the uv-based development workflow (lint, type-check, tests, coverage, CI gates), and the gotchas to know before editing. Architecture and project orientation live in `CLAUDE.md`.

## Commands

- `uv sync` — install the CLI's Python deps (PyYAML + Pydantic) into a project-local `.venv`. See [Development workflow](#development-workflow) for lint, type-check, and dev deps. *Contributors only.* End users should install with `uv tool install git+https://github.com/Xiddoc/Beetroot.git`, which exposes `beetroot` on `PATH` without the `uv run` prefix. The host-side `frida` CLI used by `beetroot frida` is **optional** and exposed via a `[frida]` extra (`uv sync --extra frida` in-tree, or `uv tool install 'beetroot[frida]'` for end users); plain installs omit `frida-tools` and `beetroot frida` errors out with an install hint.
- `uv run beetroot <verb>` — invoke any CLI verb during development. Verbs: `create`, `register`, `adopt`, `apply`, `up`, `down`, `restart`, `destroy`, `forget`, `ls`, `logs`, `shell`, `status`, `doctor`, `frida`, `module`, `build`, `snapshot`, `restore`. Run `beetroot <verb> --help` for flags. (With a `uv tool install`-based setup, drop the `uv run` prefix entirely.)
- `beetroot build [variant]` (or `uv run beetroot build [variant]` in-tree) — one-time bootstrap. Clones `ayasa520/redroid-script` into `/tmp/redroid`, runs its patcher via `uv` to produce a local base image (e.g. `redroid/redroid:14.0.0_litegapps_houdini_magisk`), then `docker compose build`s the research layer on top of it via the `BASE_IMAGE` build arg. The optional argument selects the GMS variant: `none`, `lite` (default), `full`, or `mindthegapps`. Re-run once per variant whenever the base image needs to be regenerated. `beetroot up` does **not** auto-rebuild — run `beetroot build` first if you want a fresh image. The implementation lives in `src/beetroot/builder.py`.
- `docker compose -p <name> -f <bundled-compose> --project-directory <instance-dir> --env-file <instance-dir>/.env <subcommand>` — the raw escape hatch. The CLI just wraps this; if the CLI breaks, you can still drive instances directly. The bundled compose template lives at `src/beetroot/templates/compose.yaml` (resolve at runtime via `paths.bundled_compose_file()`).

A typical flow: `beetroot create alpha` → `beetroot up alpha` → `beetroot shell alpha`.

## Things to know when editing

- **`docker/entrypoint.sh` runs inside Android.** Android's userland is toybox-derived — no GNU coreutils, no bash. Stick to POSIX sh and toybox-compatible flags. Magisk's sqlite schema is load-bearing; do not refactor the DB writes.
- **`docker/stealth.rc` is Android init syntax**, not arbitrary text. `exec_background u:r:magisk:s0` is a SELinux context. If you don't know what that means, don't touch this file.
- **The base image tag** is derived at runtime from `android.version` and `android.gapps` in `beetroot.yaml` by `config.base_image_tag()` (e.g. `version: 14, gapps: lite` → `redroid/redroid:14.0.0_litegapps_houdini_magisk`). The tag is injected into the build via the `BASE_IMAGE` ARG in `docker/Dockerfile` and the `${BASE_IMAGE}` substitution in `src/beetroot/templates/compose.yaml`. The patcher that produces the base image is `beetroot build <variant>` (wrapping `ayasa520/redroid-script`); run it once per GMS variant. Bumping Android version, gapps flavor, or translation layer means re-running the patcher with the appropriate flags.
- **The bundled `compose.yaml` is templated** — every `${VAR}` must have a corresponding line in `render_env()` in `src/beetroot/config.py`. If you add a new substitution, update both.
- **`api_version` gates the schema.** `InstanceConfig` carries a top-level `api_version: int` (currently `2`, tracked by `SUPPORTED_API_VERSION` in `src/beetroot/config.py`). The default lets old YAMLs that omit the field keep working; pinning a non-matching value raises a `ValidationError` pointing at `CHANGELOG.md`. When a future change breaks the schema, bump `SUPPORTED_API_VERSION` and add a migration entry to `CHANGELOG.md`.
- **`mem_limit` and `cpus` are top-level keys, not under `deploy:`.** The `deploy:` form is Swarm-only and silently ignored by `docker compose up`. This also applies to `mem_reservation`, `memswap_limit`, and `pids_limit` — all top-level keys in the bundled compose template.
- **Environment-driven overrides** are provided by `src/beetroot/settings.py` (`Settings(BaseSettings)`). The following `BEETROOT_*` environment variables are recognised: `BEETROOT_DOCKER_BIN` (default: `docker`), `BEETROOT_FRIDA_ARCH` (default: `android-x86_64`), `BEETROOT_HTTP_TIMEOUT` (default: `30`), and `BEETROOT_VM_ADB_CONNECT_TIMEOUT` (default: `60`; seconds `VmDeviceBackend.up()` polls `adb connect` against the freshly-launched micro-VM guest before failing — the guest re-binds adbd's TCP port a few seconds after boot). These are read at import time; override them before launching the CLI.
- **Docs are part of every feature.** Touching a CLI verb, install path, schema field, or any user-facing string means grepping `docs/` and `README.md` for old spellings and updating every hit — not just the obvious page. v0.2 shipped multiple features (`uv tool install`, the `[frida]` extra) with README + one docs page updated while three other pages still showed the old guidance; see retros on `fix/uv-tool-install-docs` and `fix/frida-extra-docs` in `CHANGELOG.md`. Before commit, grep the surface that changed:
  - CLI verb rename: `grep -rn '<old-verb>' docs/ README.md`
  - Install path (`alias`, `pip install`, `uv run`, `uv tool install`): `grep -rn 'alias beetroot\|pip install frida-tools\|uv run beetroot ' docs/ README.md`, then prune contributor-aside callouts from the hits.
  - Schema rename: `grep -rn '<old-field>' docs/ examples/ README.md`.

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
uv run ruff format src/beetroot/         # auto-format
uv run ruff format --check src/beetroot/ # CI gate — src/beetroot/ must be formatter-clean
```

Ruff is configured in `[tool.ruff]` / `[tool.ruff.lint]` — target Python 3.13, line length 100, with a strict rule set covering 20+ families including `D` (pydocstyle, Google convention).

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

The suite treats every warning as an error (`filterwarnings = ["error"]` in `pyproject.toml` — allowlist upstream deprecations individually, with a comment naming their origin), runs in **random order** (`pytest-randomly`; pass `-p no:randomly` to disable while bisecting an order-dependent failure), and fails any single test that runs past **30 seconds** (`pytest-timeout`).

Tests live under `tests/` and use pytest's built-in mocking (`unittest.mock`) — no real network or docker calls. `conftest.py` provides two composable fixtures: `isolated_registry` (points `$XDG_CONFIG_HOME` and `$XDG_CACHE_HOME` at a per-test tmp dir) and `isolated_instance` (creates a minimal instance dir and `chdir`s into it). Most CLI/registry tests use the `cli_root` composite fixture, which layers `isolated_registry` with stubbed `shutil.which` + a no-op `frida_download.download`.

**Coverage**

100% line + branch coverage on `src/beetroot/` is mandatory. `[tool.pytest.ini_options]` invokes `--cov=beetroot --cov-report=term-missing` automatically and `[tool.coverage.report].fail_under = 100` makes `uv run pytest` exit non-zero if the threshold isn't met. New code must come with new tests. CI runs the same gate; the pre-push hook (`.pre-commit-config.yaml`) catches it locally before the push hits the remote.

**Behavior tests, not just line coverage.** Line + branch coverage is necessary but not sufficient. When a user-facing behavior emerges from the *composition* of two or more pieces of code (config-model → resolver → `.env` render; the `create` verb → port allocator → registry write → compose start), ship at least one test that drives the full **user input → final artifact** path and asserts on the artifact. `feat/configurable-ports` hit 100% line + branch on `ports.py` and still shipped a silent port self-collision after a partial override, because no test asserted on the resolved `.env` dict given the model input; see `fix/ports-resolver-self-collision` in `CHANGELOG.md` for the fix and the corrective test pattern.

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

GitHub Actions runs the full gate set on every push to `master` and on every pull request targeting `master`. The workflow is at `.github/workflows/ci.yml`. The core jobs are exactly the commands listed above under Lint, Type checking, and Tests (ruff check, `ruff format --check src/beetroot/`, mypy, pytest with the 100% coverage gate) — CI additionally passes `--cov-report=xml -p no:cacheprovider` to pytest (stateless runs) and uploads the coverage report (XML + terminal) as a workflow artifact. On top of those, CI enforces:

- `uv lock --check` — `uv.lock` must be in sync with `pyproject.toml` (run `uv lock` after editing deps).
- actionlint + `uvx zizmor==1.25.2 .github/workflows/` — the workflows themselves are lint- and security-audited. All actions stay SHA-pinned and every checkout sets `persist-credentials: false`; zizmor fails the build otherwise.
- `uvx codespell==2.4.2 src/ docs/ README.md CHANGELOG.md` — spelling.
- `uvx yamllint==1.38.0 -c .yamllint src/beetroot/templates/compose.yaml .github/workflows/ examples/` — YAML style (policy lives in `.yamllint`).
- `uvx deptry==0.25.1 .` — dependency hygiene: undeclared imports and declared-but-unused deps (config in `[tool.deptry]` in `pyproject.toml`; add exceptions only for genuine false positives like the optional `frida-tools` extra).
- `shfmt -i 4 -d docker/*.sh` — boot-helper formatting (CI downloads a checksum-verified pinned release binary; install `shfmt` locally to replicate).
- Packaging gate: `uv build`, `uvx twine==6.2.0 check dist/*`, then the wheel is installed into a clean venv and `beetroot --help` must run.

Every gate is version-pinned in the workflow (release binaries are checksum-verified); to replicate a gate locally, run the same command with the same pin.
