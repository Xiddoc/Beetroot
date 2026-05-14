# Installation

Beetroot has a one-time image-build step and a fast CLI install step. Do them in order.

## Step 1 — Build the base image

```bash
./scripts/setup.sh
```

This script:

1. Clones [`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) into `/tmp/redroid`.
2. Runs the patcher with `uv` to produce a local Docker image tagged `redroid/redroid:14.0.0_litegapps_houdini_magisk`. The patcher bakes Magisk, LiteGapps (minimal GApps), and Houdini (ARM-on-x86\_64 translation) into the base redroid image.
3. Runs `docker compose build` to layer `entrypoint.sh` and `stealth.rc` on top, producing the final Beetroot image.

!!! warning "This takes a while"
    The patcher downloads several large artifacts (Magisk, GApps, Houdini). Budget 10–20 minutes depending on your connection. Re-running `./scripts/setup.sh` is safe — it only rebuilds what changed.

## Step 2 — Install the CLI

```bash
uv sync
```

This creates a `.venv` under the project root and installs the `beetroot` CLI and its dependencies (`pyyaml`, `pydantic`). The install is editable, so changes to `src/beetroot/*.py` take effect immediately.

Verify:

```bash
uv run beetroot --help
```

You should see the top-level help listing all verbs.

!!! tip "Shell alias"
    Typing `uv run beetroot` gets old quickly. Add this to your shell rc:

    ```bash
    alias beetroot="uv run beetroot"
    ```

    All examples in this docs site use the bare `beetroot` form for readability.

## Updating

To update the CLI after a `git pull`:

```bash
uv sync   # picks up any new dependencies
```

To rebuild the Docker image after upstream changes:

```bash
./scripts/setup.sh
```

## Docs preview (contributors)

If you're contributing to the documentation:

```bash
uv sync --group docs
uv run mkdocs serve  # http://127.0.0.1:8000
```

The dev server live-reloads as you edit files under `docs/`.

## Next

[Your First Instance](first-instance.md) — create, boot, and connect to a research phone.
