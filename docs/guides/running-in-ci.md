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

