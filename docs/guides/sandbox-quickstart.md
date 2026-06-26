# Sandbox / CI quickstart (no-KVM, no-binder TCG path)

This page is the **end-to-end runbook** for bringing up a real, rooted
Android-14 instance on a host that has **neither a kernel binder driver nor
`/dev/kvm`** — a hardened CI runner or a locked-down cloud sandbox (the
Claude-Code-on-the-web execution environment is the canonical example). On such
a host the default `redroid` backend cannot boot Android at all, but the
`binder: vm` backend **can**: it runs redroid inside a QEMU micro-VM that ships
its *own* binder-enabled kernel, falling back to **TCG** (pure software
emulation) when there is no KVM to accelerate it.

You can follow this top-to-bottom without reading any source. If you only read
one conceptual page first, read [Binder & run-modes](../how-it-works/binder-and-modes.md);
the design rationale and validated measurements live in
[Binderless hosts (QEMU/TCG)](../design/binderless-hosts-qemu-tcg.md) and the
[micro-VM R&D log](../design/vm-rnd-log.md).

!!! warning "TCG is slow — a cold first boot is *expected*, not a hang"
    Under TCG every guest instruction is emulated, so the **first** boot to
    `sys.boot_completed=1` takes **a few minutes** (~100–150 s on a 4-core host,
    longer on a busy or smaller runner). This is normal. Do not kill the VM
    thinking it has hung — watch `beetroot logs <name>` for progress. On a host
    that *does* have `/dev/kvm`, the same path is near-native; `accel: auto`
    uses KVM automatically when present.

## Step 0 — confirm this is the path you need

Run the host capability survey:

```bash
beetroot modes
```

On a binderless, KVM-less host you will see (the rows that matter):

| MODE | STATUS |
|------|--------|
| `redroid (binder: host / auto)` | `unsupported` |
| `redroid (binder: vm, KVM accel)` | `unsupported` |
| `redroid (binder: vm, TCG accel)` | `needs-setup` ← **this is your path** |
| `adb backend (adopt remote device)` | `needs-setup` |

`vm, TCG accel: needs-setup` (not `unsupported`) means the backend *is*
reachable here once you install QEMU and build the guest artifacts — that is
exactly what the rest of this page does. (The `adb` backend is the other
`needs-setup` row, but it boots nothing itself — it needs an **external** rooted
device to `beetroot adopt`, so the TCG VM is the self-contained option.)

## Step 1 — install the host prerequisites

The micro-VM build pulls and bakes a redroid image, assembles a busybox + static
Docker rootfs, and packs it into an ext4 disk, then QEMU boots it. You need:

| Tool | Why | Ubuntu/Debian package |
|------|-----|-----------------------|
| QEMU (x86-64 system) | runs the guest VM (TCG software emulation) | `qemu-system-x86` |
| busybox (static) | the guest's userland (PID 1 + applets) | `busybox-static` |
| `socat` | the host→guest ADB relay baked into the guest | `socat` |
| `iptables` (legacy) | the in-guest dockerd bridge driver needs an `iptables` binary | `iptables` |
| `adb` | `beetroot shell` / screenshots attach over ADB | `android-tools-adb` |
| `mke2fs` | packs the guest rootfs into an ext4 image | `e2fsprogs` |
| Docker daemon | the build `docker pull`s the redroid image and bakes it in | `docker.io` (or Docker Engine) |
| `uv` | runs the `beetroot` CLI | (see below) |

```bash
sudo apt-get update
sudo apt-get install -y \
    qemu-system-x86 busybox-static socat iptables android-tools-adb \
    e2fsprogs docker.io
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't already present
```

!!! note "The Docker daemon must be **running** during the build"
    `beetroot build --vm-kernel` pulls `redroid/redroid:14.0.0-latest` and
    `docker save`s it through the host daemon (it is then baked into the guest's
    `/var/lib/docker`, so the guest itself never needs the network at boot). The
    sandbox ships the Docker **CLI** but leaves `dockerd` opt-in, so start it
    first if it isn't running:

    ```bash
    sudo dockerd > /tmp/dockerd.log 2>&1 &
    docker info        # confirm the daemon is reachable before building
    ```

### Registry-mirror workaround (restricted-network sandboxes)

If the sandbox's network policy blocks or rate-limits Docker Hub, the
`docker pull` in Step 2 will fail. Two ways around it:

1. **Point the daemon at a pull-through mirror.** Start `dockerd` with a
   registry mirror (or write `/etc/docker/daemon.json` before starting it):

    ```bash
    sudo dockerd --registry-mirror=https://<your-mirror-host> \
        > /tmp/dockerd.log 2>&1 &
    ```

    ```json
    // /etc/docker/daemon.json
    { "registry-mirrors": ["https://<your-mirror-host>"] }
    ```

2. **Supply a pre-pulled image tarball.** Pull the image somewhere with Hub
   access (`docker pull redroid/redroid:14.0.0-latest && docker save … -o
   redroid.tar`, or `skopeo copy docker://redroid/redroid:14.0.0-latest
   docker-archive:redroid.tar`), then hand the tarball to the builder so it
   skips the pull entirely:

    ```bash
    REDROID_TAR=/path/to/redroid.tar uv run beetroot build --vm-kernel
    ```

## Step 2 — build the guest kernel + rootfs

```bash
uv run beetroot build --vm-kernel
```

This produces two artifacts under `~/.cache/beetroot/vm/`:

- `bzImage` — the binder-enabled guest kernel. The command fetches a
  **prebuilt** bzImage (~12 MiB) when one matches the pinned config; otherwise
  it compiles from source (~7 min).
- `rootdisk.img` — the ext4 rootfs with the redroid image baked in (~8 GiB).
  The command fetches a **prebuilt**, zstd-compressed rootfs when one matches
  the Android version + a fingerprint of the rootfs inputs; otherwise it bakes
  locally (pulls + bakes ~2 GB of redroid, needs a Docker daemon).

Pass `--from-source` to force a local build of **both** artifacts (compile the
kernel, bake the rootfs).

!!! tip "Run from a checkout, or a `uv tool install`"
    Contributors hacking on a source checkout use the `uv run beetroot …` prefix
    shown throughout this page. The micro-VM build assets are shipped as package
    data, so `beetroot build --vm-kernel` also works from a plain
    `uv tool install` (drop the `uv run` prefix). If the build can't find its
    assets, point it at a checkout with `--build-context <path>` (or
    `BEETROOT_BUILD_CONTEXT=<path>`).

## Step 3 — create an instance and opt into `binder: vm`

```bash
uv run beetroot create alpha
```

The generated `alpha/beetroot.yaml` defaults to the host-binder `redroid`
backend, which won't boot here. Switch it to the micro-VM backend by copying the
shipped reference config over it (or edit the keys by hand):

```bash
cp examples/vm.yaml alpha/beetroot.yaml
```

The relevant keys (see [`examples/vm.yaml`](examples.md)):

```yaml
binder: vm

vm:
  kernel: ~/.cache/beetroot/vm/bzImage      # from Step 2
  rootfs: ~/.cache/beetroot/vm/rootdisk.img # from Step 2
  accel: auto      # auto | kvm | tcg — auto picks KVM if present, else TCG
  smp: auto        # pins -smp to the host's physical core count
  memory_mib: 8192
```

`accel: auto` falls back to TCG here automatically — you do **not** have to pin
`tcg` by hand. The kernel/rootfs paths can also come from the
`BEETROOT_VM_KERNEL` / `BEETROOT_VM_ROOTFS` environment variables instead of the
`vm:` block.

## Step 4 — apply and boot

```bash
uv run beetroot apply alpha   # flips the registry to the vm backend, renders .env
uv run beetroot up alpha      # boots the micro-VM — slow first boot under TCG
```

`up` prints a one-time banner naming the resolved backend and acceleration
(loud for TCG, quiet for KVM). Tail the boot if you want to watch progress:

```bash
uv run beetroot logs alpha    # follow Android init → sys.boot_completed=1
```

Once it reports boot completion, confirm the instance is healthy:

```bash
uv run beetroot doctor alpha  # adb, magisk, frida, plus the vm.process / vm.accel rows
uv run beetroot shell alpha   # interactive shell on the guest
```

!!! note "Frida on the `vm` backend"
    Frida is not yet wired for the `vm` backend — `doctor` reports a
    clear "not yet supported on the vm backend" message rather than failing.

## Step 5 — take a screenshot

The instance's host ADB port is the stride-allocated default (`5555` for the
first instance — see [Port Allocation](../reference/ports.md)). Capture the
framebuffer straight over ADB:

```bash
adb connect localhost:5555
adb -s localhost:5555 exec-out screencap -p > shot.png
```

`shot.png` is the live guest screen — proof the emulated Android booted all the
way to SurfaceFlinger.

## Step 6 — make repeat boots instant (warm-start boot cache)

The cold first boot above is slow because, under TCG, booting Android is
**CPU-bound** — every instruction of ART / Zygote / `system_server` is emulated.
No entropy or disk tweak changes that (benchmarked in
[vm-rnd-log Stage E](../design/vm-rnd-log.md)). The fix is to **boot once, then
never boot again**: opt into the warm-start boot cache.

Edit `alpha/beetroot.yaml` and set `boot_cache: true` under `vm:`:

```yaml
binder: vm
vm:
  kernel: ~/.cache/beetroot/vm/bzImage
  rootfs: ~/.cache/beetroot/vm/rootdisk.img
  boot_cache: true
```

```bash
uv run beetroot apply alpha
uv run beetroot up alpha      # FIRST up: cold-boot (~3-4 min TCG), then checkpoint
uv run beetroot down alpha
uv run beetroot up alpha      # LATER up: RESUME the checkpoint — ~10 s to a live device
```

The first `up` boots through a qcow2 overlay and, once ADB is reachable, runs
QEMU `savevm` to checkpoint the *running machine state* (RAM + devices + disk).
Every later `up` finds that checkpoint and launches with `-loadvm`, resuming the
already-booted guest. Measured on this class of host (Android 14, pure TCG):
**~222 s cold → ~10 s warm, a ~22x speedup.**

!!! note "Resume is a fast *known-good boot*, not persistence"
    Each warm `up` reverts the guest to the checkpoint moment. That is ideal for
    a throwaway research sandbox (every session starts from an identical, warmed
    Android), but it means in-guest changes (installed apps, logins, flashed-module
    state) do not carry across restarts — Beetroot prints a warning on each warm
    resume so this is never a silent surprise. Set `vm.boot_cache: false` if you need
    `/data` to persist across restarts, and delete `alpha/vm-overlay.qcow2` to discard
    the cache (e.g. after rebuilding the kernel/rootfs with `beetroot build --vm-kernel`).

## Tear down

```bash
uv run beetroot down alpha       # power off the micro-VM (SIGTERM to QEMU)
uv run beetroot destroy alpha    # remove the instance + free its port slot
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `beetroot modes` shows `vm, TCG accel: unsupported` | QEMU not installed | `apt-get install qemu-system-x86` |
| `build --vm-kernel` fails on `docker pull` | no running daemon, or Hub blocked | start `dockerd`; use a [registry mirror](#registry-mirror-workaround-restricted-network-sandboxes) or `REDROID_TAR=` |
| `build --vm-kernel` can't find build assets | running from a non-source install | pass `--build-context <checkout>` or set `BEETROOT_BUILD_CONTEXT` |
| `up` seems to hang for minutes | cold TCG boot — expected | wait; watch `beetroot logs <name>` — a few minutes is normal |
| `adb connect` refused right after `up` | adbd rebinds TCP a few seconds post-boot | retry the `adb connect`; `up` already retries internally |

## See also

- [Binder & run-modes](../how-it-works/binder-and-modes.md) — what each backend
  needs and how `beetroot modes` classifies your host.
- [Running in CI / without kernel access](running-in-ci.md) — the same decision
  tree for CI runners, plus the binder-capable runner and remote-device paths.
- [Binderless hosts (QEMU/TCG)](../design/binderless-hosts-qemu-tcg.md) — the
  `vm` backend's design.
- [Micro-VM R&D log](../design/vm-rnd-log.md) — the validated TCG build/boot
  recipe and measured timings.
