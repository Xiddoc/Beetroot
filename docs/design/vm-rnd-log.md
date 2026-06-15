# Micro-VM R&D log — Stage A (kernel + minimal rootfs under TCG)

!!! info "Status: Stage A validated (2026-06-15)"
    The vendored kernel-config fragment and the QEMU/TCG invocation from
    [binderless-hosts-qemu-tcg.md](binderless-hosts-qemu-tcg.md) were taken
    from spec to a **real, reproducible build and boot** on a binderless,
    KVM-less host. A pinned mainline kernel was built from source, a minimal
    busybox rootfs was booted under pure TCG, and **binderfs was confirmed
    mounted** with the three configured device nodes. This log records the
    exact toolchain, the build, the working invocation, measured timings, and
    the one correction the fragment needed. Stage B (docker + redroid in the
    guest) is not yet run — the artifacts are ready for it.

## Environment

* Host: 4 CPU cores, 15 GiB RAM, ~30 GiB free on `/`. Running as root.
* **No `/dev/kvm`, no vmx/svm CPU flags** → nested virt unavailable. All
  measurements are **pure TCG (software emulation)**, the rank-4 worst case.
  This matches the PoC's Firecracker/TCG host.

## 1. Toolchain (exact packages + versions)

Installed via `apt-get` on Ubuntu 24.04 (noble):

| Package | Version |
| --- | --- |
| qemu-system-x86 | `1:8.2.2+ds-0ubuntu1.16` (QEMU 8.2.2 — matches PoC "QEMU 8.2") |
| build-essential | `12.10ubuntu1` (gcc 13.3.0, GNU ld 2.42) |
| flex | `2.6.4-8.2build1` |
| bison | `2:3.8.2+dfsg-1build2` |
| libelf-dev | `0.190-1.1ubuntu0.1` |
| libssl-dev | `3.0.13-0ubuntu3.11` |
| bc | `1.07.1-3ubuntu4` |
| cpio | `2.15+dfsg-1ubuntu2` |
| busybox-static | `1:1.36.1-6ubuntu3.1` |
| e2fsprogs | `1.47.0-2.4~exp1ubuntu4.1` (mke2fs 1.47.0) |
| xz-utils | `5.6.1+really5.4.5-1ubuntu0.3` |

GNU Make 4.3.

## 2. Kernel (pinned, reproducible build)

* **Pinned version: Linux 6.12.9** (`cdn.kernel.org/pub/linux/kernel/v6.x/`),
  matching the `~6.12.x` the design doc references. This is roadmap item 1
  ("pin the kernel build").
* Config recipe (exactly as the vendored fragment intends):

  ```sh
  make defconfig                                                   # x86_64 base
  ./scripts/kconfig/merge_config.sh -m .config docker/vm/kernel.config
  make olddefconfig
  make -j4 bzImage
  ```

* **Build time: 450 s (7.5 min)** on 4 cores, `-j4`. Output: 14 MiB bzImage.
* Final `.config` verification (all `=y`, confirmed present after
  `olddefconfig`): `CONFIG_ANDROID_BINDER_IPC`, `CONFIG_ANDROID_BINDERFS`,
  `CONFIG_PSI`, `CONFIG_BPF_SYSCALL`, `CONFIG_BPF_JIT`, `CONFIG_VIRTIO_BLK`,
  `CONFIG_VIRTIO_PCI`, `CONFIG_VIRTIO_CONSOLE`, `CONFIG_OVERLAY_FS`,
  `CONFIG_EXT4_FS`, `CONFIG_CGROUP_BPF`, `CONFIG_USER_NS`,
  `CONFIG_DEVTMPFS_MOUNT`, `CONFIG_MEMFD_CREATE` — all set.

### Correction to `docker/vm/kernel.config`

* **`CONFIG_ANDROID=y` is stale and was removed.** On modern kernels there is
  no `CONFIG_ANDROID` symbol — the umbrella config was dropped and
  `drivers/android/Kconfig` now opens straight on `menu "Android"` with
  `ANDROID_BINDER_IPC` as a top-level entry. Verified absent from the 6.12.9
  `.config` even after requesting it (silent no-op). `ANDROID_BINDER_IPC`
  enables binder on its own. The fragment now carries a comment explaining
  this instead of the dead line.

No other fragment options needed correcting — binder, binderfs, PSI, bpf, and
the virtio set all took effect as written.

## 3. Minimal rootfs (Stage-A: busybox + init, no docker yet)

Per §4.2/§4.3 ordering, a deliberately tiny rootfs to validate boot + binderfs
*before* layering docker/redroid (Stage B):

* `busybox-static` (host's `/bin/busybox`, 1.36.1) at `/bin/busybox`, applets
  self-installed at boot via `busybox --install -s`.
* `/init` (PID 1): mounts `proc`, `sysfs`, `devtmpfs`, `devpts`, `tmpfs`
  (`/run`), `cgroup2` (`/sys/fs/cgroup`), then `mount -t binder binder
  /dev/binderfs`, prints confirmation markers, and `poweroff -f` (so automated
  runs self-terminate; an interactive build would `exec sh` here instead).
* Packed as a 256 MiB raw ext4 image with `mke2fs -q -t ext4 -d <tree>` — no
  loop mount, no root needed, exactly the §4.2 technique.

## 4. Working QEMU invocation (TCG) + boot result

```sh
qemu-system-x86_64 \
  -M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max -smp 4 -m 8192 \
  -nographic -display none -no-reboot \
  -kernel bzImage \
  -drive file=minroot.img,format=raw,if=virtio \
  -append "console=ttyS0 root=/dev/vda rw init=/init panic=1"
```

This is the §4.4 invocation verbatim and it boots cleanly — no kernel
warnings, BUGs, or oops. `virtio_blk` brings up `/dev/vda`, ext4 mounts r/w.

### binderfs confirmation (the whole point)

```
BINDERFS_MOUNT_OK
crw------- 1 0 0 247, 1  binder
crw------- 1 0 0 247, 0  binder-control
crw------- 1 0 0 247, 2  hwbinder
crw------- 1 0 0 247, 3  vndbinder
drwxr-xr-x 2 0 0          features/
--- /proc/filesystems ---
nodev   binder
```

All three devices from `CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"`
appear, plus `binder-control` (the `BINDER_CTL_ADD` dynamic-device endpoint
that Android's `servicemanager` setup relies on) and the `features/` dir.

`PSI_OK`: `/proc/pressure/{cpu,io,memory}` all present (the lmkd-stability
lever from §5). cgroup2 controllers: `cpuset cpu io memory hugetlb pids rdma
misc`.

## 5. Measured timings (pure TCG)

Kernel→init→binderfs-ready, measured *inside* the guest via `/proc/uptime` at
the `init` markers; host wall is the full QEMU launch→poweroff cycle.

| Invocation | guest uptime to init-ready | host wall (boot+poweroff) | binderfs |
| --- | --- | --- | --- |
| `tcg,thread=multi -smp 4` | 3.46–3.65 s | 4.65–4.85 s | OK |
| `tcg,thread=single -smp 4` | 3.99–4.06 s | 5.17–5.29 s | OK |
| `tcg,thread=multi -smp 2` | 3.13 s | 4.28 s | OK |
| `tcg,thread=multi -smp 1` | 3.00–3.42 s | 4.54 s | OK |

This matches the doc's "minimal busybox boot under TCG: ~3 s."

### Perf-lever direction (honest reading)

* **MTTCG helps when vCPU count is fixed:** at `-smp 4`, `thread=multi` is
  ~0.4–0.5 s faster than `thread=single` (3.46–3.65 s vs 3.99–4.06 s).
  Confirms `thread=multi` is the right default — the §4.4 claim holds.
* **More vCPUs did NOT help *this* trivial boot:** `-smp 1/2` were slightly
  *faster* than `-smp 4` (3.0–3.1 s vs 3.5 s) because a busybox boot has almost
  no parallel guest work, so extra-vCPU SMP bring-up + cross-vCPU sync is pure
  overhead. The `-smp 4` payoff is expected to appear only under a parallel
  workload (ART/Zygote/system_server during the real redroid boot) — i.e. it
  is a **Stage B** measurement, not visible on a bare-init boot. Do not read
  this as "drop to -smp 1"; read it as "the SMP lever needs a real workload to
  show its value."

## 6. Artifacts (scratch — never committed)

* Kernel source: `/home/user/vm-rnd/linux-6.12.9/`
* **bzImage: `/home/user/vm-rnd/bzImage`** (14 MiB, Linux 6.12.9 SMP)
* Final kernel `.config`: `/home/user/vm-rnd/linux-6.12.9/.config`
* **Minimal rootfs: `/home/user/vm-rnd/minroot.img`** (256 MiB ext4)
* Serial console logs: `/home/user/vm-rnd/boot-*.log`

## 7. Blockers / notes

* `/usr/bin/time` is not installed by default on the host — the first build
  invocation failed with `rc=127` before `make` even ran. Re-run with shell
  `date` arithmetic for timing. (Not a recipe issue; a host-tooling note.)
* No hard walls hit. Kernel built first try (after the `time` fix); rootfs
  booted first try; binderfs mounted first try.

## 8. Readiness for Stage B

The bzImage + minimal rootfs are a known-good base. Stage B (add the Docker
static bundle + `guest-init.sh` + a redroid 11.0.0 image and drive it to
`sys.boot_completed=1`) can build on `/home/user/vm-rnd/bzImage`. The kernel
already has every config the full stack needs (bpf syscall for runc, cgroup2,
overlay, PSI for lmkd, memfd for ashmem-less Android 11+), so no kernel rebuild
should be required to proceed — only swapping the minimal rootfs for the full
`build-rootfs.sh` output.

---

# Micro-VM R&D log — Stage B (full stack: docker + redroid Android 11 under TCG)

!!! success "Status: Stage B validated (2026-06-15) — full redroid boot, ADB partially"
    The full stack — busybox + a static docker/containerd/runc bundle + a
    **baked-in** redroid Android 11 image + `guest-init.sh` as PID 1 — was built
    on top of the Stage A `bzImage` and booted under **pure TCG** on the same
    binderless, KVM-less host. redroid reached **`sys.boot_completed=1` in ~100 s**
    (reproducibly, across 10 boots), fully **offline** (no internet in the guest).
    An SMP sweep on the *real* redroid boot quantified the MTTCG/SMP payoff that
    Stage A's trivial busybox boot could not show. The host→guest **ADB** path was
    diagnosed hop-by-hop: every layer except docker's port-publish machinery works;
    the corrected, docker-bypassing networking model (socat relay into redroid's
    netns) is recorded below, including the open last-hop hardening item.

## B.1 Image acquisition (exactly how, fully offline guest)

The guest boots redroid with **`--network none`** (see B.4) and has **no
internet**, so the image must be present on disk before first boot.

* **Pulled on the host with a docker daemon.** The host had the `docker` CLI +
  `containerd` but no running daemon; we started one (`dockerd &` — runs fine on
  this kernel, it just can't *run* redroid for lack of binder) and
  `docker pull redroid/redroid:11.0.0-latest`. **Correction:** the plain tag
  `redroid/redroid:11.0.0` does **not exist** on Docker Hub — the valid tags are
  `11.0.0-latest`, `11.0.0-<date>`, etc. `guest-init.sh`/`build-rootfs.sh` use
  `11.0.0-latest` (matching the design doc).
* **Baked into the guest `/var/lib/docker`, not `docker load`-ed at boot.** We
  `docker save`d the image (815 MiB tar), then **loaded it into a staging
  `/var/lib/docker` using the *same* static docker bundle version the guest runs
  (27.5.1)** so the `overlay2` on-disk layout is byte-compatible with the guest's
  dockerd. That staged data-root (2.0 GiB) is `cp -a`'d into the rootfs tree.
  Result: in the guest, **`dockerd` becomes ready in ~1 s** and the image is
  already present — *no* in-guest `docker load` (which would be minutes under TCG).
  If a host has no docker daemon to pull with, `build-rootfs.sh` accepts a
  pre-made tarball via `REDROID_TAR=` (e.g. from `skopeo copy docker://… docker-archive:…`).

## B.2 Rootfs corrections (`build-rootfs.sh`)

The vendored builder was close but would not have produced a bootable stack.
Corrections made and validated:

| # | Issue in the vendored script | Fix |
| --- | --- | --- |
| 1 | No redroid image baked in → guest needs internet / a slow in-guest `docker load` | Bake the image into `/var/lib/docker` via a staging dockerd of the **same** static bundle version (overlay2 layout matches) |
| 2 | dockerd's bridge driver needs an **`iptables` binary**; the static docker bundle ships none → `failed to create NAT chain DOCKER: iptables not found` | Stage `iptables-legacy` (the kernel has the **legacy xt** backend, `CONFIG_NF_TABLES` is **off**) + its shared libs + the `ld-linux` loader |
| 3 | busybox applet list was a tiny hand-picked set (missing `poweroff`, `nsenter`, `netstat`, `udhcpc`, `ip`, `ps`, …) | Ship the single busybox binary; `guest-init` runs `busybox --install -s` at boot to lay down **all** applets |
| 4 | `socat` (the ADB relay, see B.4) not present | Stage `socat` + its libs |
| 5 | 16 GiB image (wasteful) | 8 GiB is ample (2.1 GiB image + overlay scratch) |
| 6 | busybox fetched from busybox.net 1.35.0 | Use the host's known-good busybox-static (1.36.x); overridable via `BUSYBOX_BIN` |

The mke2fs `-d <tree>` technique (Stage A) was kept; repacking the 8 GiB image
takes ~10–20 s, and `debugfs -w` edits `/init` in place for fast iteration.

## B.3 Guest boot sequence (`guest-init.sh`) — what actually worked

The §4.3 contract booted **first try** with no §5-class blockers on the boot
path itself (the Stage A kernel already had bpf-syscall, cgroup2, overlay, PSI,
memfd). The exact, validated ordering:

1. mount proc/sysfs/devtmpfs(+EBUSY-tolerant, kernel auto-mounts it)/devpts/
   tmpfs(`/dev/shm`,`/run`,`/var/run`)/cgroup2/binderfs; `ip link set lo up`.
2. `containerd` standalone → socket up in **~2 s**.
3. `dockerd --containerd=… --iptables=false --bridge=none` → ready in **~1 s**
   (image pre-baked; no load).
4. `docker rm -f redroid` (stale cleanup) → `docker run -d --privileged
   --name redroid --network none redroid/redroid:11.0.0-latest
   androidboot.redroid_gpu_mode=guest ro.adb.secure=0
   service.adb.tcp.port=5555 persist.adb.tcp.port=5555`.
5. poll `docker inspect .State.Status == running` (fail-fast) + `docker exec
   redroid getprop sys.boot_completed`.

Evidence: Android `init` brings up `surfaceflinger`, `bootanim`, `system_server`,
`zygote`, `adbd`; `sys.boot_completed=1` and `sys.bootstat.first_boot_completed=1`
set; `bootanim` exits cleanly. No lmkd flapping (PSI present from Stage A).

## B.4 Host→guest ADB networking — the critical finding

This is the VmDeviceBackend's whole purpose, so it was driven to a precise,
hop-by-hop diagnosis. **The QEMU user-net `hostfwd=tcp::<port>-:5555` assumption
in the Python `build_qemu_argv` is correct** — host:port reaches the guest main
netns :5555 (verified: a raw TCP connect to the host-forwarded port succeeds).
The hard part is the *last hop*, guest → redroid's adbd. What was learned:

* **redroid's adbd is USB-gadget-only by default.** `persist.sys.usb.config=adb`,
  no `service.adb.tcp.port` → adbd opens no TCP listener. The image's adbd
  binary *does* support TCP (`strings`: `using tcp port=%d`,
  `service.adb.tcp.port`). Setting `service.adb.tcp.port=5555` + `stop/start adbd`
  makes adbd bind **`:::5555` (IPv6 wildcard)**; with `net.ipv6.bindv6only=0` it
  accepts IPv4 too (a direct CNXN probe to `127.0.0.1:5555` *and* `[::1]:5555`
  inside the container both get a valid `CNXN` reply — adbd is healthy).
  `ro.adb.secure=0` is already set in the image, so no host adb key is needed.
* **`--network none` is the only redroid networking mode that boots cleanly.**
  Tested all three:
  * `--network none` → **boots in ~100 s, stable** (adbd in an isolated netns).
  * `--network host` → **netd/zygote crash-loop** (zygote SIGKILL'd every ~44 s,
    never reaches boot_completed) — the guest main netns has no usable address
    for Android's netd to manage. A §5-class blocker; do **not** use host net.
  * `-p 5555:5555` (docker publish) → boots, the whole publish chain is wired
    (`docker port` shows `0.0.0.0:5555`, **docker-proxy** binds the guest main
    netns, iptables NAT `DNAT … to:172.17.0.2:5555` present) **but the byte path
    fails**: a real ADB `CNXN` through docker-proxy gets **`Connection reset by
    peer`**. Per-hop isolation with a static CNXN probe (`adbprobe`) pinned it:

    | Hop | Path | Result |
    | --- | --- | --- |
    | A | container → adbd `[::1]:5555` | **CNXN OK** |
    | A2 | container → adbd `127.0.0.1:5555` | **CNXN OK** (dual-stack) |
    | B | guest in-netns → in-container socat `:15555` → adbd | **CNXN OK** |
    | C | guest main-netns → **docker-proxy** `:5555` | connects, then **RESET** |

    So adbd and an in-netns socat relay both work; **docker-proxy's userspace
    forward is the broken hop** on this minimal kernel.
* **The pure-DNAT fallback needs a kernel feature we don't have.**
  `dockerd --userland-proxy=false` (which would drop docker-proxy and rely on
  iptables DNAT alone) **refuses to start**: it needs
  `/proc/sys/net/bridge/bridge-nf-call-iptables`, i.e. **`CONFIG_BRIDGE_NETFILTER`
  (`br_netfilter`)**, absent from the Stage A kernel; it also tried (and failed)
  to create an `ip6tables nat` table (no `IP6_NF_NAT` in-kernel). Setting
  `--ip6tables=false` clears the ip6 error and dockerd *does* come up with
  `--userland-proxy=false`, but the IPv4 DNAT publish still did not deliver to
  adbd from the host under TCG in the time available.

* **Corrected networking model (docker-bypassing socat relay).** Because docker's
  port machinery is unusable here, the validated direction is to keep redroid on
  `--network none` (stable boot) and bridge ADB *outside* docker: a **socat relay
  that `nsenter`s into redroid's network namespace** and forwards the QEMU
  hostfwd target (guest `:5555`) to adbd. `guest-init.sh` now does exactly this.
  The in-netns hop (B above) is proven; the cross-process socat chaining
  (`socat TCP4-LISTEN … EXEC:"nsenter -n socat - TCP6:[::1]:5555"`) connected but
  did not yet relay bytes end-to-end in the last runs — **this is the one open
  hardening item** (likely needs `nsenter -t … -n` plus an explicit
  `socat … TCP6:[::1]:5555` with `fork`, or a long-lived pair of cooperating
  socats rather than per-connection `EXEC`). It is plumbing, not physics: adbd is
  reachable, the relay just needs to be made robust.

### Implication for the Python `build_qemu_argv`

No change to the hostfwd is required: `hostfwd=tcp::<adb_port>-:5555` (host port
→ guest main netns :5555) is the correct target — the guest-side socat relay
listens there. The backend should **not** assume docker `-p`/`--network host`
will surface adbd; the relay model in `guest-init.sh` is the contract. If a
later iteration prefers docker-native publishing, the kernel config must gain
`CONFIG_BRIDGE_NETFILTER=y` + `CONFIG_IP6_NF_IPTABLES/NAT=y` (then
`--userland-proxy=false` works and the relay can be dropped).

## B.5 Performance — real redroid boot + SMP/MTTCG sweep

Wall-clock to `sys.boot_completed=1`, pure TCG, `thread=multi` (MTTCG), 8 GiB,
redroid `--network none`. `boot_seconds` is measured inside the guest from the
`docker run` to `boot_completed`; `host_wall` is the full QEMU launch→poweroff.

| `-smp` | boot_seconds (guest) | host wall |
| --- | --- | --- |
| 2 | **180 s** | 198 s |
| 4 | **~100 s** (99–103 across runs) | 120 s |
| 8 | **113 s** | 134 s |

* **This is the headline number:** ~100 s to `boot_completed` at `-smp 4`,
  matching (slightly beating) the design doc's "~150 s under TCG" estimate.
* **The SMP/MTTCG payoff Stage A could not show is now visible.** Stage A's
  trivial busybox boot got *slower* with more vCPUs (pure SMP bring-up overhead,
  no parallel work). The **real redroid boot scales the other way**: `-smp 4`
  (= the host's physical core count) is **~1.8× faster than `-smp 2`** because
  ART/Zygote/`system_server` boot is genuinely parallel. `-smp 8` regresses
  slightly vs 4 (8 vCPU threads oversubscribe 4 physical cores → cross-thread
  TCG sync overhead). **Conclusion: pin `-smp` to the host's physical core count;
  `thread=multi` remains the right default.** On a KVM host (rank 3) this boot
  should approach native redroid speed.

## B.6 Artifacts (scratch — never committed)

* Full rootfs tree: `/home/user/vm-rnd/fullroot/` (busybox + static docker +
  iptables-legacy + socat + baked `/var/lib/docker`).
* **Full rootfs image: `/home/user/vm-rnd/rootdisk.img`** (8 GiB ext4).
* Redroid image tarball: `/home/user/vm-rnd/redroid-11.tar` (815 MiB);
  staged data-root: `/home/user/vm-rnd/stage-dockerroot/` (2.0 GiB, overlay2).
* Static CNXN probe used for hop isolation: `/home/user/vm-rnd/adbprobe[.c]`.
* Serial logs: `/home/user/vm-rnd/stageB-boot*.log` (boot1 = first full boot,
  `--network none`, success; boot12 = the per-hop ADB isolation; boot13/14 =
  DNAT attempts; smp{2,4,8} = the sweep).

## B.7 Honest status / open items

* ✅ docker (containerd + dockerd) comes up in-guest in ~3 s total (baked image).
* ✅ redroid Android 11 reaches `sys.boot_completed=1` reproducibly (~100 s).
* ✅ adbd reachable on TCP *inside* the container (IPv4 and IPv6 CNXN verified).
* ✅ SMP/MTTCG payoff quantified (`-smp 4` optimal on a 4-core host).
* ⚠️ **Host→guest ADB end-to-end not yet confirmed.** adbd is reachable and the
  hostfwd target is correct; the remaining work is hardening the socat
  netns-bridge relay (or adding `CONFIG_BRIDGE_NETFILTER` to enable docker-native
  publishing). docker's own `-p`/`--network host` paths are ruled out on this
  kernel (docker-proxy reset / netd crash-loop) — documented above.
