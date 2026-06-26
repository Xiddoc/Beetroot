# AGENTS.md

The contributor and agent playbook for Beetroot: how to drive the CLI, the uv-based development workflow (lint, type-check, tests, coverage, CI gates), and the gotchas to know before editing. Architecture and project orientation live in `CLAUDE.md`.

## Commands

- `uv sync` — install the CLI's Python deps (PyYAML + Pydantic) into a project-local `.venv`. See [Development workflow](#development-workflow) for lint, type-check, and dev deps. *Contributors only.* End users should install with `uv tool install git+https://github.com/Xiddoc/Beetroot.git`, which exposes `beetroot` on `PATH` without the `uv run` prefix. The host-side `frida` CLI used by `beetroot frida` is **optional** and exposed via a `[frida]` extra (`uv sync --extra frida` in-tree, or `uv tool install 'beetroot[frida]'` for end users); plain installs omit `frida-tools` and `beetroot frida` errors out with an install hint.
- `uv run beetroot <verb>` — invoke any CLI verb during development. Verbs: `create`, `register`, `adopt`, `apply`, `up`, `down`, `restart`, `destroy`, `reset`, `forget`, `ls`, `logs`, `shell`, `status`, `doctor`, `frida`, `module`, `build`, `snapshot`, `restore`. Run `beetroot <verb> --help` for flags. (With a `uv tool install`-based setup, drop the `uv run` prefix entirely.)
- `beetroot build [variant]` (or `uv run beetroot build [variant]` in-tree) — one-time bootstrap. Clones `ayasa520/redroid-script` into `/tmp/redroid`, runs its patcher via `uv` to produce a local base image (e.g. `redroid/redroid:14.0.0_litegapps_houdini_magisk`), then `docker compose build`s the research layer on top of it via the `BASE_IMAGE` build arg. The optional positional argument selects the GApps intent: `none`, `minimal` (default), or `full`; an optional `--gapps-vendor` (`litegapps`/`opengapps`/`mindthegapps`) pins a specific distribution. Re-run once per intent/vendor whenever the base image needs to be regenerated. `beetroot up` does **not** auto-rebuild — run `beetroot build` first if you want a fresh image. The implementation lives in `src/beetroot/builder.py`.
- `docker compose -p <name> -f <bundled-compose> --project-directory <instance-dir> --env-file <instance-dir>/.env <subcommand>` — the raw escape hatch. The CLI just wraps this; if the CLI breaks, you can still drive instances directly. The bundled compose template lives at `src/beetroot/templates/compose.yaml` (resolve at runtime via `paths.bundled_compose_file()`).

A typical flow: `beetroot create alpha` → `beetroot up alpha` → `beetroot shell alpha`.

## Things to know when editing

- **`docker/entrypoint.sh` runs inside Android.** Android's userland is toybox-derived — no GNU coreutils, no bash. Stick to POSIX sh and toybox-compatible flags. Magisk's sqlite schema is load-bearing; do not refactor the DB writes.
- **`docker/stealth.rc` is Android init syntax**, not arbitrary text. `exec_background u:r:magisk:s0` is a SELinux context. If you don't know what that means, don't touch this file.
- **The base image tag** is derived at runtime from `android.version`, `android.gapps`, and the optional `android.gapps_vendor` in `beetroot.yaml` by `config.base_image_tag()` (e.g. `version: 14, gapps: minimal` → `redroid/redroid:14.0.0_litegapps_houdini_magisk`; the intent resolves to a vendor via `config.resolve_gapps_vendor()`). The tag is injected into the build via the `BASE_IMAGE` ARG in `docker/Dockerfile` and the `${BASE_IMAGE}` substitution in `src/beetroot/templates/compose.yaml`. The patcher that produces the base image is `beetroot build <intent> [--gapps-vendor <vendor>]` (wrapping `ayasa520/redroid-script`); run it once per intent/vendor. Bumping Android version, gapps intent/vendor, or translation layer means re-running the patcher with the appropriate flags.
- **Adding a new Android version.** The supported set is single-sourced at `config._VALID_ANDROID_VERSIONS` (and `config.DEFAULT_ANDROID_VERSION` for the default). To support Android *N*:
  1. Add *N* to `_VALID_ANDROID_VERSIONS` (and bump `DEFAULT_ANDROID_VERSION` if it should become the new default).
  2. **Verify the upstream tags actually exist** — this is the assumption nothing else checks. Both image-tag derivations are pure functions of `version`: `base_image_tag()` builds `redroid/redroid:N.0.0[_gapps]_houdini_magisk` (via the `ayasa520/redroid-script` patcher) and `vm_redroid_image()` builds the *plain* `redroid/redroid:N.0.0-latest` (pulled straight from Docker Hub for the `binder: vm` guest). A version that upstream tags differently (no `.0.0`, no `-latest`, or a GApps/Houdini flavour that doesn't exist for *N*) passes validation and then **404s at pull time**. Confirm the tags on Docker Hub before adding the version.
  3. Update the human-readable enumerations ("11, 12, 13, or 14") in the `config.py`/`builder.py` docstrings, `README.md`, `docs/reference/config.md`, and `docs/guides/ci-reusable-workflow.md`. `tests/test_android_version_extensibility.py` **fails CI on drift** for all of these — it greps the `config.py`/`builder.py` source enumerations against the constant, and presence-checks the canonical phrase in the three doc pages — and parametrizes both tag-derivation functions across *every* supported version, so a stale enumeration or a malformed tag is caught at the unit level.
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

Tests live under `tests/` and use pytest's built-in mocking (`unittest.mock`) — no real network calls, and almost no real docker calls. `conftest.py` provides two composable fixtures: `isolated_registry` (points `$XDG_CONFIG_HOME` and `$XDG_CACHE_HOME` at a per-test tmp dir) and `isolated_instance` (creates a minimal instance dir and `chdir`s into it). Most CLI/registry tests use the `cli_root` composite fixture, which layers `isolated_registry` with stubbed `shutil.which` + a no-op `frida_download.download`.

A small set of tests *do* shell out to docker for real, and they self-gate so a daemonless host **skips** them instead of failing (#59). `tests/docker_daemon.py` exposes a cached `daemon_available()` that probes `docker info` (not just `shutil.which("docker")` — the CLI can be present with no running daemon); `test_container_boot.py` (`docker run`) and the `destroy`-driven restore tests in `test_instance_invariants.py` / `test_partial_failure_rollback.py` are marked `@pytest.mark.skipif(not daemon_available(), ...)`. Note `test_config.py`'s `docker compose config` tests deliberately keep the bare `shutil.which` guard — `compose config` only renders YAML and never touches the daemon, so gating them on daemon liveness would needlessly skip tests that work fine daemonless.

**What your environment can test (local vs GitHub CI).** Run `beetroot modes` to see, at a glance, which device backends the *current* host can boot — it prints a table (`MODE` / `STATUS` / `DETAIL`) covering redroid `binder: host|auto`, `binder: vm` under KVM and under TCG, and the adb backend. Use it to know what's testable before you reach for a backend.

- **The pure-Python suite (`uv run pytest`)** is the universal gate — it mocks docker/network and runs anywhere. The only exceptions are the daemon-gated tests above: they run wherever a Docker **daemon** is live and skip (not fail) where it isn't.
- **The Claude Code on the web sandbox** (where these agent sessions run) is daemonless and binderless: `docker` CLI present but `dockerd` opt-in (so the daemon-gated tests skip), no host `binder` and no `/dev/kvm` (so `beetroot modes` reports redroid `host/auto` and `vm,KVM` as `unsupported`). **You CAN boot a real Android VM in this sandbox — use the `binder: vm` TCG path. It is the *one* device backend reachable here, and it does not need binder, KVM, or a running Docker daemon.** `beetroot modes` reports it as `needs-setup` (not `unsupported`) precisely because it *is* available once you run the two-step setup:

    ```bash
    apt-get install -y qemu-system-x86            # the TCG (software-emulated) QEMU
    uv run beetroot build --vm-kernel             # fetches prebuilt bzImage (~12 MiB) if one matches the config, else compiles (~7 min); also fetches a prebuilt zstd rootfs (#79) if one matches, else bakes it locally. Pass --from-source to force a local build of both.
    uv run beetroot create <name>                 # then copy examples/vm.yaml over <name>/beetroot.yaml
    uv run beetroot apply <name> && uv run beetroot up <name>
    ```

    There is no `--binder` flag — the backend is selected by the `binder: vm` key in `beetroot.yaml` (copy `examples/vm.yaml` over the generated config). Its `accel: auto` prefers KVM when `/dev/kvm` exists and **automatically falls back to TCG** here, so you don't have to pin TCG by hand. The validated recipe and measurements live in `docs/design/vm-rnd-log.md`. It is **slow** (TCG is ~5–20× software emulation, no hardware accel) and a slow first boot is expected (not a hang), but it works — don't report VM boot as impossible in this environment. The adb backend is the other `needs-setup` path (install `android-tools-adb`), but it also needs an external rooted device to `beetroot adopt`, so the TCG VM is the self-contained option. **Verified `beetroot modes` in-sandbox (2026-06):** `redroid (binder: host/auto)` → `unsupported`, `binder: vm, KVM accel` → `unsupported`, `binder: vm, TCG accel` → `needs-setup` (**usable after the setup above**), `adb backend` → `needs-setup`. **Expected skip count:** a clean `uv run pytest` in the sandbox reports **16 skipped** — not 6: the 6 docker-daemon tests *plus* the 10 `test_shell_lint.py` cases that self-skip because `shellcheck`/`shfmt` aren't on `PATH` (install both, or replicate their CI gate with `uvx --from shellcheck-py shellcheck -S style -s sh docker/*.sh src/beetroot/templates/vm/*.sh`). **Bottom line: in the sandbox you can always run and trust the entire pure-Python suite (with those 16 environment-gated tests skipping, not failing); the `host`/`KVM` backends are genuinely `unsupported` here, but the TCG VM backend *is* reachable — boot it via the two-step setup above when you need a live device.**
- **GitHub-hosted CI (`ubuntu-latest`)** has a live Docker daemon (so the daemon-gated tests run there and keep coverage at 100%) and can *load* the `binder_linux` module via `.github/actions/provide-binder` (ladder rank 2), so the **`e2e.yml`** boot tiers exercise the real redroid host path — but those are **label/schedule-gated, not a per-PR gate** (see `CLAUDE.md` → "Binder runtime & CI"). The `tier-vm-qemu` tier boots the `binder: vm` micro-VM under TCG (hosted runners have no `/dev/kvm`).

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
- `shellcheck -S style -s sh docker/*.sh src/beetroot/templates/vm/*.sh` — shell linting at the strictest severity (`style`), covering both the in-container boot helpers and the micro-VM `guest-init.sh`. POSIX `sh` mode (toybox / busybox, not bash).
- `shfmt -i 4 -d docker/*.sh src/beetroot/templates/vm/*.sh` — shell formatting for the same set (CI downloads a checksum-verified pinned release binary; install `shfmt` locally to replicate).
- Packaging gate: `uv build`, `uvx twine==6.2.0 check dist/*`, then the wheel is installed into a clean venv and `beetroot --help` must run.

Every gate is version-pinned in the workflow (release binaries are checksum-verified); to replicate a gate locally, run the same command with the same pin.
