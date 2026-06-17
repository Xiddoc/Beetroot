# Running in CI / without kernel access

redroid is a **container, not an emulator**: it runs Android's userspace
directly against the *host* kernel and ships no kernel of its own. The
one hard requirement is the kernel **binder** driver — Android's `init`,
`servicemanager`, and `zygote` all block on `/dev/binder` at boot, and
redroid provides no userspace substitute. `privileged: true` can be
narrowed on a host that *has* binder, but binder itself is a kernel
feature you can't grant from Docker.

That splits CI and cloud environments into two cases.

## Decision tree

```
Can you load a kernel module on the host (sudo modprobe)?
├── Yes  → it's a binder-capable host (e.g. GitHub-hosted runners).
│          Load binder, then run redroid normally.   →  Option A
└── No   → kernel-less sandbox (locked-down PaaS, this is also the case
           when CONFIG_ANDROID_BINDER_IPC is compiled out).
           redroid can't boot here at all. Drive a remote device
           over ADB instead.                          →  Option B
```

Run `beetroot doctor <name>` on the host to see which case you're in —
the `host.binder` row reports `pass` (ready), `fail` with
`CONFIG_ANDROID_BINDER_IPC=m` (loadable — load the module), or `fail`
with `is not set` (compiled out — kernel-less).

## Option A — load binder on a binder-capable runner

GitHub-hosted `ubuntu-latest` runners ship the `binder_linux` module and
give you passwordless `sudo`, so you can create the device nodes and boot
real redroid in CI:

```yaml
# .github/workflows/android.yml
name: android-redroid
on: [push]

jobs:
  redroid:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Load the binder kernel module
        run: |
          sudo modprobe binder_linux devices=binder,hwbinder,vndbinder
          ls -l /dev/binder*        # nodes now exist

      - name: Install Beetroot
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          uv tool install git+https://github.com/Xiddoc/Beetroot.git

      - name: Build, create, boot
        run: |
          beetroot build
          beetroot create alpha
          beetroot up alpha          # host.binder is ready, so no warning
          beetroot doctor alpha
```

!!! note "Runner images change"
    Module availability on hosted runners isn't contractual — a future
    image could drop `binder_linux`. `beetroot doctor` will tell you if
    that happens (the `host.binder` row flips to a `modprobe` remedy or
    to `compiled out`).

## Option B — drive a remote device (no kernel access)

When you can't provide binder — a sandboxed CI container, a kernel built
without `CONFIG_ANDROID_BINDER_IPC`, or any host where you can't
`modprobe` — redroid can't run *directly* on that host. Beetroot's **adb backend**
needs no kernel access at all: it drives a rooted Android device that
lives *somewhere else* (a physical phone on a self-hosted runner, a cloud
device farm, a redroid container running on a separate binder-capable
host, an emulator started outside Beetroot). Connect to it over the
network and adopt it:

```bash
# The device runs elsewhere; reach it over the network.
adb connect 192.168.1.10:5555

# Adopt it into the registry (--verify refuses if it isn't reachable).
beetroot adopt 192.168.1.10:5555 --name phone --verify

# Every Protocol-driven verb now works against it — no container, no binder.
beetroot shell phone
beetroot doctor phone
beetroot module phone ./shamiko.zip --auto-install
```

Lifecycle verbs that only make sense for a managed container (`up`,
`down`, `restart`, `apply`, `destroy`, `snapshot`) exit with code `2`
against an adb-adopted device — it's managed outside Beetroot. See
[Adding a backend](adding-a-backend.md) for the full backend capability
matrix.

!!! tip "Run redroid *locally* on a binderless host with `binder: vm`"
    On a host with no binder (and even no `/dev/kvm`), set `binder: vm` to
    boot redroid inside a QEMU micro-VM whose **own** kernel provides
    binder. Build the guest kernel + rootfs once with `beetroot build
    --vm-kernel`, point `vm.kernel` / `vm.rootfs` at them, then `beetroot
    apply` + `beetroot up`. KVM-accelerated where available; otherwise TCG
    (~5-20x slower — a slow first boot is expected, not a hang). The
    rationale, reproducible recipe, and backend design live in
    [Binderless hosts (QEMU/TCG)](../design/binderless-hosts-qemu-tcg.md).

## Reusable workflow — boot an instance in *your* CI

If you just want a rooted Android instance to run your own tests against —
say you publish a Frida script and want CI to exercise it against a real
Android-14 phone — you don't have to hand-assemble the steps above. Beetroot
ships a **reusable GitHub Actions workflow** you can call with `uses:`:

```yaml
# .github/workflows/test.yml in *your* repo
name: test-on-beetroot
on: [push]

jobs:
  frida-hook:
    uses: Xiddoc/Beetroot/.github/workflows/beetroot-ci.yml@v0.4
    with:
      test-command: |
        uv run --with frida-tools \
          frida -H "$FRIDA_HOST" -l hook.js -f com.example.app --runtime=v8 &
        # your assertions here, e.g. drive the app over adb and grep logcat
        adb -s "$ADB_SERIAL" shell input tap 200 400
```

The workflow checks out *your* repository, builds the Beetroot image, boots an
instance, and runs your `test-command` with the device reachable at:

| Env var | Value | Use |
|---------|-------|-----|
| `$ADB_SERIAL` | `127.0.0.1:5555` | `adb -s "$ADB_SERIAL" …` |
| `$FRIDA_HOST` | `127.0.0.1:27042` | `frida -H "$FRIDA_HOST" …` |
| `$BEETROOT_INSTANCE` | the instance name | `beetroot shell "$BEETROOT_INSTANCE" …` |
| `$BEETROOT_SRC` | the Beetroot checkout | `uv run --project "$BEETROOT_SRC" beetroot …` |

Useful inputs (all optional except `test-command`):

| Input | Default | Meaning |
|-------|---------|---------|
| `test-command` | — (required) | Shell command run once the instance is up. |
| `binder` | `host` | `host` loads `binder_linux` and boots redroid natively (fast); `vm` builds a binder-enabled guest kernel and boots redroid in a QEMU micro-VM (works without loadable binder, but slow under TCG). |
| `gapps` | `none` | GMS variant baked in: `none`, `lite`, `full`, `mindthegapps`. |
| `android-version` | `14` | `11`, `12`, `13`, or `14`. |
| `frida-version` | `""` | Pin a `frida-server` version (e.g. `16.4.10`); empty boots without Frida. |
| `instance-name` | `ci` | Instance name. |
| `working-directory` | `.` | Directory your `test-command` runs in. |
| `runs-on` | `ubuntu-latest` | Runner label. |
| `beetroot-ref` | reuses the `uses:` ref | Git ref of Beetroot to build from. |

!!! note "Nothing proprietary is redistributed"
    The image (redroid + Magisk + optional GApps + Houdini) is built on **your**
    runner by `beetroot build`, exactly as it would be on a developer's laptop —
    the patcher fetches GApps/Houdini from their upstreams at *your* CI runtime.
    This workflow ships only Beetroot's own MIT-licensed orchestration; it is not
    a pre-built image and does **not** appear under GitHub Packages. That is the
    same reason there is no published container image: Beetroot can't redistribute
    Google's GApps or Intel's Houdini, but it can hand you the recipe to build
    them yourself.

`binder: host` works on standard GitHub-hosted `ubuntu-latest` runners today
(they ship a loadable `binder_linux`). Choose `binder: vm` only if your runner
can't load binder — it additionally compiles a guest kernel, so expect a much
longer run.

## What about this project's own CI?

Beetroot's **unit** suite never touches a real kernel: every Docker, ADB, and
network call is stubbed (`tests/conftest.py`), and the `docker-build-smoke`
job in `.github/workflows/ci.yml` proves the image's `COPY` layers compile
against a lightweight `busybox` stand-in base — it does **not** boot Android.
So the PR gate runs fine on stock hosted runners without binder.

On top of that, a separate **`e2e.yml`** workflow boots a real Android on a
hosted runner (the Option A path above) in three tiers:

* **Tier 1** boots the upstream stock redroid image and drives it through
  Beetroot's adb backend (`adopt --verify` / `ls` / `shell` / the adb-side
  `doctor` row). Light (~1-2 min) and reliable.
* **Tier 2** (WIP) `beetroot build`s the real Magisk image, `beetroot up`s it,
  and asserts the in-device deployment (root, Zygisk, GMS denylist, Frida). It
  is heavy and non-blocking while it's hardened.
* **Tier-VM** builds the binder-enabled guest kernel + rootfs, boots redroid
  inside the `binder: vm` QEMU micro-VM, and drives it through the adb backend
  (`ls` / `shell` / the `doctor` `vm.process` + `vm.accel` rows; Frida is
  asserted to report its "not yet supported on the vm backend" message). On a
  GitHub-hosted runner there is no `/dev/kvm`, so it runs under TCG — a slow
  (~100 s+) but real boot. The kernel + rootfs build is the long pole.

Because real boots are slow, `e2e.yml` does **not** run on every push. Trigger
it by adding the **`e2e`** label to a pull request, running it manually from
the Actions tab (**Run workflow** → optionally tick *run_tier2*), or via the
nightly schedule on `master`.

