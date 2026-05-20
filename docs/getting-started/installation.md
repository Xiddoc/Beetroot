# Installation

Beetroot has a fast CLI install step and a one-time image-build step. Do them in order.

## Step 1 — Install the CLI

```bash
uv tool install git+https://github.com/Xiddoc/Beetroot.git
```

`uv tool install` puts the `beetroot` command into a system-wide, isolated tool venv on your `PATH` — no project-local `.venv`, no `uv run` prefix, no shell alias needed.

## Step 2 — Build the base image

```bash
beetroot build
```

This verb:

1. Clones [`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) into `/tmp/redroid`.
2. Runs the patcher with `uv` to produce a local Docker image tagged `redroid/redroid:14.0.0_litegapps_houdini_magisk`. The patcher bakes Magisk, LiteGapps (minimal GApps), and Houdini (ARM-on-x86\_64 translation) into the base redroid image.
3. Runs `docker compose build` to layer `entrypoint.sh` and `stealth.rc` on top, producing the final Beetroot image.

Pass a variant to pick a different GMS flavor: `beetroot build none | lite | full | mindthegapps` (default `lite`).

!!! warning "This takes a while"
    The patcher downloads several large artifacts (Magisk, GApps, Houdini). Budget 10–20 minutes depending on your connection. Re-running `beetroot build` reuses an existing clone when the work directory already matches the same repo URL, but otherwise re-runs the full patcher and image build.

Verify:

```bash
beetroot --help
```

You should see the top-level help listing all verbs. The help screen is
rendered with Typer's Rich-powered formatter (boxed sections, color when
the terminal supports it).

### Shell completion

Beetroot's CLI is built on [Typer](https://typer.tiangolo.com/), which
ships shell-completion hooks for `bash`, `zsh`, `fish`, and PowerShell.
Run once per shell:

```bash
beetroot --install-completion
```

The command auto-detects your current shell (via
[`shellingham`](https://github.com/sarugaku/shellingham)) and writes the
hook into the right rc file. Open a new terminal and `beetroot <TAB>`
completes verbs; `beetroot create --<TAB>` completes flags.

To inspect the script before installing — useful for sandboxed or
ephemeral environments where you'd rather paste the hook into a managed
rc file by hand — use:

```bash
beetroot --show-completion
```

### With Frida CLI

The host-side `frida` CLI (from [`frida-tools`](https://pypi.org/project/frida-tools/)) is only needed if you want to use `beetroot frida <name>`. It's exposed as an optional `[frida]` extra so a single command installs both:

```bash
uv tool install 'beetroot[frida]'   # bundles frida-tools so `beetroot frida` works out of the box
```

If you're working out of a clone with `uv sync` (as in step 2 above) and want the extra in your project venv, run:

```bash
uv sync --extra frida
```

Plain `uv tool install beetroot` (or plain `uv sync`) installs the CLI alone — `beetroot frida` will then error out with a hint pointing you back at this section. You can always add the extra later or `uv tool install frida-tools` separately.


## Updating

```bash
uv tool upgrade beetroot
```

To rebuild the Docker image after upstream changes:

```bash
beetroot build
```

## Contributor workflow

If you're hacking on the CLI itself, install Beetroot from a local checkout so your edits land immediately:

```bash
git clone https://github.com/Xiddoc/Beetroot.git
cd Beetroot
uv sync   # creates .venv with runtime + lockfile-pinned deps
uv run beetroot --help
```

`uv sync` keeps a project-local editable venv that picks up changes to `src/beetroot/*.py` without reinstalling. Use `uv run beetroot <verb>` (or `uv tool install .` from the repo root for a system-wide install of your working tree) when iterating.

## Docs preview (contributors)

If you're contributing to the documentation:

```bash
uv sync --group docs
uv run mkdocs serve  # http://127.0.0.1:8000
```

The dev server live-reloads as you edit files under `docs/`.

## Next

[Your First Instance](first-instance.md) — create, boot, and connect to a research phone.
