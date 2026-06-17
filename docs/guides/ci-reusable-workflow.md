# CI integration — the reusable workflow

Beetroot ships a **reusable GitHub Actions workflow** so any repository can
spin up a rooted Android-14 research phone in its own CI and run tests against
it — without copy-pasting boot scaffolding or hosting a custom runner image.

The canonical use case: you publish a Frida script (or an app, a Magisk
module, an anti-tamper test suite…) and you want CI to exercise it against a
*real* rooted Android, on every push.

```yaml
# .github/workflows/test.yml in YOUR repository
name: test-on-beetroot
on: [push]

jobs:
  frida-hook:
    uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@v0.4
    with:
      frida-version: "16.4.10"
      test-command: |
        uv run --with 'frida==16.4.10' --with frida-tools \
          frida -H "$FRIDA_HOST" -l hook.js -f com.example.app
```

!!! warning "Match the host Frida core to `frida-version`"
    The device-side `frida-server` (set by the `frida-version` input) and the
    host-side `frida` core that `frida-tools` uses must agree on **major +
    minor**, or the connection is refused. Pin the `frida` package to the same
    version as `frida-version` (e.g. `--with 'frida==16.4.10'`), as above. See
    the [Frida guide](frida.md) for the matching rule.

That single `uses:` job checks out your repo, builds the Beetroot image on the
runner, boots an instance, and runs your `test-command` against the live
device.

!!! info "This is not a published image, and it is licensing-clean"
    Beetroot does **not** publish a container image to GitHub Packages, and a
    reusable workflow never appears there either — it is consumed via `uses:`,
    not pulled. The image (redroid + Magisk + optional GApps + Houdini) is
    built **on your runner** by `beetroot build`, exactly as it would be on a
    developer's laptop: the patcher fetches GApps (Google) and Houdini (Intel)
    from *their* upstreams at *your* CI runtime. Beetroot redistributes none of
    it — this workflow ships only Beetroot's own MIT-licensed orchestration.
    That is precisely *why* there is no pre-built image to pull: Beetroot can't
    legally redistribute those proprietary blobs, but it can hand you the recipe
    to build them yourself.

## Requirements

The host path needs the kernel **binder** driver (see
[Running in CI](running-in-ci.md) for the why). Two ways to satisfy it,
selected by the `binder` input:

| `binder` | What happens | Runner needs | Speed |
|----------|--------------|--------------|-------|
| `host` (default) | Loads `binder_linux`, boots redroid natively | A runner where `modprobe binder_linux` works — **GitHub-hosted `ubuntu-latest` does today** | Fast (~1–2 min boot) |
| `vm` | Builds a binder-enabled guest kernel, boots redroid in a QEMU micro-VM | Any x86-64 runner (no binder needed) | Slow — compiles a kernel **and** boots under TCG (~100 s+ boot) |

Use `host` on standard GitHub-hosted runners. Reach for `vm` only when your
runner can't load binder (a locked-down/self-hosted environment).

!!! warning "What the runner must provide"
    Both paths need a **running Docker daemon** and **passwordless `sudo
    apt-get`** (the workflow installs `adb`, and on the `vm` path QEMU + a kernel
    toolchain). The `vm` path additionally needs **outbound network** to
    `cdn.kernel.org` (kernel source) and `download.docker.com` (the static
    Docker bundle baked into the guest). GitHub-hosted `ubuntu-latest` provides
    all of this; a custom `runs-on` may not — the `vm` path fails fast with a
    clear message if no Docker daemon is reachable. The whole job is capped at
    **120 minutes**; the boot wait alone allows ~16 minutes, so size your
    `test-command` with the (slow, kernel-compiling) `vm` path in mind.

## Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `test-command` | — (**required**) | Shell command run once the instance is booted. Runs in `working-directory`. |
| `binder` | `host` | `host` (native, fast) or `vm` (QEMU/TCG, no binder needed). |
| `gapps` | `none` | GMS variant baked into the image: `none`, `lite`, `full`, `mindthegapps`. Ignored when `binder: vm`. |
| `android-version` | `14` | Android major version: `11`, `12`, `13`, `14`. Ignored when `binder: vm`. |
| `frida-version` | `""` | Pin a `frida-server` version (e.g. `16.4.10`); empty boots without Frida. Ignored when `binder: vm` (Frida over the vm backend is [not yet supported](../design/binderless-hosts-qemu-tcg.md)). |
| `instance-name` | `ci` | Name of the instance to create. |
| `working-directory` | `.` | Directory the `test-command` step runs in (your checked-out repo). Scopes **only** that step — the image build always runs in the Beetroot checkout. |
| `runs-on` | `ubuntu-latest` | Runner label for the job. |
| `beetroot-ref` | `master` | Git ref of `Xiddoc/Beetroot` to check out for the CLI + build context. Defaults to `master`. **If you pin an older ref in `uses:`, set this to the same ref** (see [Pinning the ref](#pinning-the-ref-do-this)). |

## What the `test-command` gets

The booted device is exposed through environment variables, so your command
doesn't need to know about ports or instance internals:

| Env var | Value | Use it for |
|---------|-------|------------|
| `$ADB_SERIAL` | `127.0.0.1:5555` | `adb -s "$ADB_SERIAL" shell …` |
| `$FRIDA_HOST` | `127.0.0.1:27042` | `frida -H "$FRIDA_HOST" …` |
| `$BEETROOT_INSTANCE` | the instance name | `uv run --project "$BEETROOT_SRC" beetroot shell "$BEETROOT_INSTANCE" …` |
| `$BEETROOT_SRC` | the Beetroot checkout | invoking the CLI: `uv run --project "$BEETROOT_SRC" beetroot …` |

`adb` is already installed and connected. The CLI is *not* on `PATH` — call it
via `uv run --project "$BEETROOT_SRC" beetroot …` (the wheel strips `docker/`,
so the workflow runs the CLI from a source checkout).

## Outputs

| Output | Description |
|--------|-------------|
| `adb-serial` | The adb serial (`host:port`) of the booted instance. |

## Worked example — testing a Frida hook

```yaml
name: frida-hook-ci
on: [push, pull_request]

jobs:
  test:
    uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@v0.4
    with:
      gapps: lite            # the target app needs Play services
      frida-version: "16.4.10"
      test-command: |
        set -euo pipefail
        # Push the target APK and install it.
        adb -s "$ADB_SERIAL" install -r ./fixtures/target.apk
        # Run the hook; -l loads the script, -f spawns the app. The frida core
        # is pinned to the same version as frida-version (the matching rule).
        timeout 60 uv run --with 'frida==16.4.10' --with frida-tools \
          frida -H "$FRIDA_HOST" -l hook.js -f com.example.app \
          --runtime=v8 -o frida.log || true
        # Assert the hook fired.
        grep -q '[+] SSL pinning bypassed' frida.log
```

!!! tip "Persisting test output"
    Teardown destroys the instance and only dumps the last ~200 log lines. The
    reusable workflow can't add steps after your `test-command`, so to keep
    artifacts (logs, screenshots, reports) handle them **from inside**
    `test-command` — write a summary to `$GITHUB_STEP_SUMMARY`, or fail loudly
    with the evidence printed inline. If you need full `actions/upload-artifact`,
    call this workflow's pieces from a hand-rolled job instead (see
    [Running in CI](running-in-ci.md)).

## Testing across a matrix

Because it's a normal job, you can fan it out:

```yaml
jobs:
  test:
    strategy:
      matrix:
        android: ["13", "14"]
        gapps: [none, lite]
    uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@v0.4
    with:
      android-version: ${{ matrix.android }}
      gapps: ${{ matrix.gapps }}
      test-command: uv run --project "$BEETROOT_SRC" beetroot doctor "$BEETROOT_INSTANCE"
```

## Pinning the ref (do this)

Pin the `uses:` reference to a tag or commit SHA, not a moving branch:

```yaml
uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@v0.4      # tag — good
uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@<40-char-sha>  # SHA — best
```

The workflow file (the steps) is loaded at the ref you pin in `uses:`, but the
Beetroot **source** it checks out for the CLI + build context defaults to
`master`. A reusable workflow can't reliably read its own `uses:` ref from
inside, so for an exact version match set `beetroot-ref` to the same ref:

```yaml
jobs:
  test:
    uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@v0.4
    with:
      beetroot-ref: v0.4    # match the source to the workflow version
      test-command: ...
```

If you track a moving branch (`@master`), the default already matches and you
can omit `beetroot-ref`.

## How it works (internals)

1. **Resolve the ref** — if `beetroot-ref` is empty, derive it from
   `github.workflow_ref` (the part after the last `@`) so the CLI matches the
   `uses:` version.
2. **Two checkouts** — your repository (so your test fixtures are present) and
   `Xiddoc/Beetroot` into `.beetroot` (the wheel strips `docker/`, so the build
   context must come from source).
3. **Provide binder** (`host`) or **build the guest kernel + rootfs** (`vm`).
4. **`beetroot build`** the image (host) — the patcher runs *here*, on your
   runner.
5. **Create + configure** — `beetroot create`, then pin `android.version` +
   `gapps` (or `binder: vm`) into the generated `beetroot.yaml` so
   `config.base_image_tag()` resolves to the exact image that was built.
6. **`beetroot apply` + `beetroot up`** and wait for `sys.boot_completed`.
7. **Run your `test-command`** with `$ADB_SERIAL` / `$FRIDA_HOST` set.
8. **Always** dump logs and `beetroot destroy` on teardown.

## Limitations & gotchas

- **Runner minutes.** `beetroot build` downloads and patches a multi-GB image
  every run (no published image to pull — see the note above), so a host-path
  run is several minutes; a `vm`-path run also compiles a kernel. Budget
  accordingly, and consider caching strategies for heavy use.
- **`binder: host` needs a binder-capable runner.** GitHub-hosted
  `ubuntu-latest` qualifies today, but module availability on hosted images
  isn't contractual. If a future image drops `binder_linux`, switch to
  `binder: vm`.
- **`vm` is slow and Frida-less.** TCG software emulation is ~5–20× slower; a
  slow first boot is expected, not a hang. Frida over the vm backend is not yet
  supported, so `frida-version` is ignored under `binder: vm`.
- **No GitHub Package.** Nothing here is published to the Packages section —
  the deliverable is the `uses:`-able workflow, not an image.

## See also

- [Running in CI / without kernel access](running-in-ci.md) — the binder
  decision tree and the hand-rolled equivalents.
- [Adding a backend](adding-a-backend.md) — the backend capability matrix.
- [Binderless hosts (QEMU/TCG)](../design/binderless-hosts-qemu-tcg.md) — the
  `binder: vm` design.
