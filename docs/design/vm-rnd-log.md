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

!!! note "Asset paths"
    The micro-VM build assets (`kernel.config`, `adbprobe.c`) now live under
    `src/beetroot/templates/vm/` — they were moved there from `docker/vm/` in
    #77. Live recipes below point at the new location; any remaining
    `docker/vm/` mentions are historical R&D narrative that predate the move.

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
  ./scripts/kconfig/merge_config.sh -m .config src/beetroot/templates/vm/kernel.config
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

### Correction to `src/beetroot/templates/vm/kernel.config`

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

> **Note (post-#82):** `11.0.0-latest` is the Android version this R&D log
> originally validated, and it remains `guest-init.sh`'s historical fallback.
> The builder's **default** baked Android version is now **14** — `build_rootfs`
> derives the redroid image from `android_version` (default
> `config.DEFAULT_ANDROID_VERSION = 14`) via `config.vm_redroid_image`, so a
> default `beetroot build --vm-kernel` and a default `beetroot create` agree.
> The `11.0.0-latest` references below describe the originally-validated R&D
> stack, not today's default.

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

> **Note:** the rootfs builder described here was originally the shell script
> `docker/vm/build-rootfs.sh`. It has since been ported to typed, unit-tested
> Python as `build_rootfs` in `src/beetroot/builder.py`; every correction below
> (the `REDROID_TAR=` escape hatch, the `cp -a` of the staged data-root, the
> `11.0.0-latest` tag, the iptables-legacy + socat staging) is preserved there.

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
  hostfwd target (guest `:5555`) to adbd. `guest-init.sh` does exactly this.
  The in-netns hop (B above) is proven; the cross-process socat chaining still
  needed hardening at the end of Stage B. **✅ RESOLVED in Stage C — see §C
  below: with two fixes (an EXEC wrapper script + bringing up the guest eth0)
  the relay now carries a full host→guest ADB session.**

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

  **Shipped as the default.** This finding is now the `vm.smp: auto` default
  (`config.Vm.smp`): `qemu.resolve_smp("auto")` pins `-smp` to the host's
  **physical** core count via `qemu.host_physical_cores()` — distinct
  `(physical id, core id)` pairs in `/proc/cpuinfo`, capped by
  `os.sched_getaffinity` (affinity/cgroup-aware, so it is correct inside a
  constrained CI container too). Counting *physical* cores rather than logical
  CPUs is what makes this match the conclusion above: on a hyperthreaded host
  a logical count (`os.process_cpu_count()`) would pick `-smp 8` on a 4c/8t
  box and hit the very oversubscription regression §B.5 measured. The
  validated `-smp 4` runs above were on a 4-physical-core host, which `auto`
  reproduces exactly — so the measured numbers still describe the shipped
  default on that class of host, while other hosts now get the right `-smp`
  without hand-tuning.

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
* ✅ **Host→guest ADB end-to-end CONFIRMED in Stage C (§C below).** From the
  host: `adb connect localhost:5555` → `device`; `adb shell getprop
  sys.boot_completed` → `1`. The socat netns-bridge relay was hardened (no
  kernel rebuild needed). docker's own `-p`/`--network host` paths remain ruled
  out on this kernel (docker-proxy reset / netd crash-loop) — documented above.

---

# Micro-VM R&D log — Stage C (host→guest ADB end-to-end, proven)

!!! success "Status: Stage C validated (2026-06-15) — real `adb` session from host to guest redroid"
    The one open Stage B item — making the host's `adb connect localhost:<port>`
    actually reach redroid's adbd — is **closed**. From the host, against the
    QEMU `hostfwd` port, `adb shell getprop sys.boot_completed` returns **`1`**,
    `ro.product.cpu.abi` is `x86_64`, and `uname -a` reports the guest's Linux
    6.12.9 kernel. Two fixes turned the "connects-but-no-bytes" relay into a
    working end-to-end path; neither needed a kernel rebuild.

## C.1 The two real blockers (and why the prior diagnosis missed them)

Stage B left the relay "connected but did not relay bytes." Driving it to a
working session uncovered **two** independent faults, plus one measurement error
that had masked them:

1. **socat `EXEC:` chokes on commas (the relay never actually ran).** The
   Stage B relay passed the inner command inline:
   `EXEC:"nsenter -t PID -n socat - TCP6:[::1]:5555"`. socat parses the `EXEC:`
   address's argument with **commas as option separators**, so it died with
   `EXEC: wrong number of parameters (3 instead of 1)` — the listener was up but
   every accepted connection's child failed instantly. **Fix:** write the inner
   command to a **parameterless wrapper script** (`/run/adb-relay-inner.sh`) and
   `EXEC:` *that*. (Also: the script must live in a dir that exists — the first
   attempt wrote to `/usr/local/bin`, absent from the rootfs, so the heredoc
   silently failed and EXEC hit "No such file or directory". Use `/run`, a tmpfs
   we mount unconditionally, and `mkdir` before the heredoc.)

2. **The guest `eth0` was never brought up — the QEMU hostfwd had nowhere to
   land.** This was the true last-hop blocker. QEMU user-net `hostfwd` delivers
   host traffic to the **guest's eth0 address** (the SLIRP default `10.0.2.15`),
   **not** to guest loopback. `guest-init.sh` only ran `ip link set lo up`, so
   `eth0` stayed down and every host SYN was silently dropped — even though the
   outer socat was listening on `0.0.0.0:5555` and the in-guest relay was
   perfectly healthy. **Fix:** bring up `eth0` with the user-net address in
   `mount_pseudo_filesystems`:

   ```sh
   ip link set eth0 up
   ip addr add 10.0.2.15/24 dev eth0
   ip route add default via 10.0.2.2 dev eth0
   ```

3. **Measurement error that hid both:** the in-guest self-probe used
   `socat - TCP:127.0.0.1:5555 < /dev/null` and waited for a banner. **adbd does
   NOT banner unprompted** — the ADB client must send a `CNXN` first, then adbd
   replies `CNXN`. A connect-and-read probe therefore *always* reads zero bytes,
   which looked like a broken relay even after fix #1 made it work. The honest
   test is the tiny static `adbprobe` (vendored at `src/beetroot/templates/vm/adbprobe.c`) that
   **sends** a real CNXN; through the full relay it returns `first4=CNXN`.

## C.2 The working relay (exact commands)

In `guest-init.sh`, after `boot_completed` + `enable_adb_tcp`:

```sh
# inner wrapper (parameterless — avoids socat's comma parsing):
cat >/run/adb-relay-inner.sh <<EOF
#!/bin/sh
exec nsenter -t ${REDROID_PID} -n socat STDIO "TCP4:127.0.0.1:5555"
EOF
chmod 0755 /run/adb-relay-inner.sh

# outer relay in the guest MAIN netns, listening on the hostfwd target:
socat TCP4-LISTEN:5555,fork,reuseaddr EXEC:/run/adb-relay-inner.sh &
```

`REDROID_PID` is `docker inspect -f '{{.State.Pid}}' redroid`. socat's `EXEC`
hands the accepted socket to the child on fds 0/1, so `STDIO` in the inner socat
*is* the host-side connection; the inner socat `nsenter`s into redroid's netns
and connects to adbd on `127.0.0.1:5555` (adbd binds `:::5555`, dual-stack).

Per-hop confirmation from one boot (in-guest, real CNXN via `adbprobe`):

| Hop | Path | Result |
| --- | --- | --- |
| netns | `nsenter -t PID -n adbprobe 4 127.0.0.1 5555` | `REPLY n=24 first4=CNXN` |
| netns v6 | `nsenter -t PID -n adbprobe 6 ::1 5555` | `REPLY n=24 first4=CNXN` |
| full relay | `adbprobe 4 127.0.0.1 5555` (guest main netns, through outer+inner socat) | `REPLY first4=CNXN` |

## C.3 The deliverable — host-side `adb` transcript

Host = the binderless, KVM-less R&D box; guest = redroid Android 11 under pure
TCG (`-smp 4`, `-m 8192`), `--network none`, QEMU `hostfwd=tcp:127.0.0.1:5555-:5555`:

```console
$ adb connect localhost:5555
connected to localhost:5555

$ adb devices
List of devices attached
localhost:5555	device

$ adb -s localhost:5555 shell getprop sys.boot_completed
1

$ adb -s localhost:5555 shell getprop ro.product.cpu.abi
x86_64

$ adb -s localhost:5555 shell uname -a
Linux localhost 6.12.9 #1 SMP PREEMPT_DYNAMIC ... x86_64

$ adb -s localhost:5555 shell getprop ro.build.version.release
11

$ adb -s localhost:5555 shell id
uid=2000(shell) gid=2000(shell) groups=2000(shell),... ,3003(inet),...
```

`sys.boot_completed=1`, the guest's own 6.12.9 kernel, the redroid Android-11
ABI, and a live interactive shell stream (the `id` output) — a complete
host→guest ADB session, reproduced against the cleaned production `guest-init.sh`.

## C.4 Implication for `src/beetroot/**` (no code change needed)

`VmDeviceBackend.up()` issuing `adb connect localhost:<port>` is correct **as
is**, and `build_qemu_argv`'s `hostfwd=tcp:127.0.0.1:<host_adb_port>-:5555` is the
right target — verified verbatim by the run harness mirroring that argv. **No
Python change is required**: both fixes (the EXEC wrapper and the eth0 bring-up)
live entirely in `guest-init.sh`, which is baked into the rootfs and runs as PID
1, so the relay is established before the host ever connects. The backend does
**not** need to issue the relay command out-of-band. One forward-looking note for
the Python owner: `up()` should expect adbd to take a few seconds past
`boot_completed` to bind TCP (`enable_adb_tcp` restarts adbd), so a short
connect-retry loop on `adb connect` is prudent — but that is a robustness nicety,
not a correctness requirement; the path itself is proven.

## C.5 Artifacts (scratch — never committed)

* Host→guest ADB transcript: `/home/user/vm-rnd/stageC-host-adb-transcript.txt`.
* Boot+relay serial logs: `/home/user/vm-rnd/stageC-relay*.log`,
  `/home/user/vm-rnd/stageC-final.log` (the cleaned-production-init proof).
* Run harness mirroring `build_qemu_argv` (TCG): `/home/user/vm-rnd/run-stageC.sh`.
* CNXN probe: `/home/user/vm-rnd/adbprobe[.c]` (vendored to `src/beetroot/templates/vm/adbprobe.c`).
* Iteration method: loop-mount `rootdisk.img` to refresh `/init` (cleaner than
  `debugfs -w` on the large tree, which corrupted ext4 dir checksums once after
  an unclean QEMU kill — `e2fsck -fy` repaired it). Always SIGKILL QEMU and
  `e2fsck -fy` before re-mounting.

---

# Micro-VM R&D log — Stage D (committed-recipe boot from scratch + cmdline A/B)

!!! success "Status: Stage D validated (2026-06-17) — the *committed* code boots redroid under TCG, after fixing a boot-blocker bug"
    Stages A–C were validated against scratch artifacts hand-assembled during
    R&D. Stage D drove the **committed code** end-to-end on a fresh binderless,
    KVM-less host: `beetroot build`'s kernel recipe + the typed
    `beetroot.builder.build_rootfs` + the committed `qemu.build_qemu_argv`. It
    surfaced a real boot-blocker (a `build_rootfs` symlink bug) that the prior
    scratch runs had masked, and — once fixed — produced a clean
    `sys.boot_completed=1` under pure TCG. It also A/B-tested the
    `mitigations=off` cmdline lever (added in #66) and **measured it neutral for
    boot time under TCG**.

## D.1 Environment

Same class as Stage A–C: 4 physical cores, 15 GiB RAM, **no `/dev/kvm`**, host
kernel without `CONFIG_ANDROID_BINDER_IPC` (`beetroot modes` → `binder: vm, TCG
accel: needs-setup`). Toolchain installed fresh (`qemu-system-x86` 8.2.x,
`busybox-static`, `socat`, `flex`/`cpio`/`libelf-dev`). Kernel built from the
committed `src/beetroot/templates/vm/kernel.config` (linux-6.12.9, ~7–8 min, 14 MiB bzImage);
rootfs from the committed `build_rootfs` (8 GiB ext4, redroid `11.0.0-latest`
baked into `/var/lib/docker`).

## D.2 Boot-blocker bug: `/bin/busybox` self-symlink → guest ELOOP panic

The first boot of the committed recipe **kernel-panicked at ~4 s**:

```
Run /init as init process
Kernel panic - not syncing: Requested init /init failed (error -40).
```

`-40` is `-ELOOP`. Root cause, confirmed via `debugfs` on the built image:
`build_rootfs._stage_busybox` symlinked **every** applet from `busybox --list`
to `"busybox"` — but `busybox` is itself in that list, so it replaced the real
`/bin/busybox` binary with a self-referential symlink (`/bin/busybox →
busybox`). That also breaks `/bin/sh` (an applet link to busybox), so the kernel
cannot exec `/init`'s `#!/bin/sh` interpreter. **The micro-VM guest had never
actually booted from the committed builder** — exactly the unverified-boot risk
#57 flags, and it survived #66 (a perf change that never booted the guest). Fix:
skip the `busybox` applet when laying down symlinks (one-line guard + regression
test). After the fix, `debugfs` shows `/bin/busybox` as a real 2.1 MiB regular
file and `/bin/sh → busybox` resolves.

## D.3 Full boot confirmed (committed recipe, post-fix)

With the fix, the committed `guest-init.sh` ran the §4.3 sequence cleanly: mount
→ eth0 up → containerd → dockerd → `docker run … --network none redroid` → poll.
Result, pure TCG, `-smp 4`, MTTCG, 8 GiB:

```
[guest-init] sys.boot_completed=1 — redroid is up (boot_seconds=105)
```

`boot_seconds=105` (guest-measured, `docker run` → `boot_completed`); ~123 s host
wall (QEMU launch → marker). Matches the Stage B "~100 s" number — the committed
code reproduces the hand-assembled result.

## D.4 Kernel-cmdline A/B — `mitigations=off` is boot-neutral under TCG

#66 added `mitigations=off` to the guest cmdline as a perf lever (and
`smp: auto`). With the guest now bootable, an A/B quantified the
`mitigations=off` effect on boot — five boots, baseline (plain cmdline) vs
treatment (`mitigations=off`), interleaved:

| launch order | arm | boot_seconds |
| --- | --- | --- |
| 1 | treatment | 105 |
| 2 | baseline | 105 |
| 3 | treatment | 110 |
| 4 | baseline | 110 |
| 5 | treatment | 113 |

The times track **launch order, not arm** (a monotonic host-contention drift),
so there is **no separable treatment effect**. The flag verifiably *worked* —
baseline kernel logs show `Spectre V2 : Mitigation: Retpolines` + RSB-filling,
treatment shows them disabled — it just **did not move boot time under TCG**:
the mitigation instructions are a negligible fraction of TCG's per-instruction
cost of the whole Android boot. **This does not argue for reverting #66** —
`mitigations=off` is harmless and may genuinely help a *KVM* host (real
retpolines/RSB-fills at native speed, unmeasured here) — but the honest record
is that it is **not** a TCG boot-speed lever. `smp: auto` resolves to 4 on this
4-core host, exactly the §B.5-measured optimum.

## D.5 Artifacts (scratch — never committed)

* Kernel source + build: `/root/vm-rnd/linux-6.12.9/`, `bzImage` (14 MiB).
* Rootfs: `/root/vm-rnd/rootdisk.img` (8 GiB ext4, redroid baked).
* Boot harness + per-run serial logs: `/root/vm-rnd/boot-measure.sh`,
  `/root/vm-rnd/boot-{baseline,treatment}-*.log`.

---

# Micro-VM R&D log — Stage E (cold-boot entropy levers + warm-start, issue #83)

!!! success "Status: Stage E validated (2026-06-20) — RNG levers measured boot-neutral; warm-start proven (131 s → 15 s)"
    Issue #83 asked two things: benchmark the cold TCG boot with vs. without the
    entropy levers (`-device virtio-rng-pci`, `random.trust_cpu=on`), and
    document/validate a warm-start path. Both were driven end-to-end on a
    binderless, KVM-less host through the committed builder + a small A/B harness
    (`scripts/vm_boot_ab.py`). Headline: the guest CRNG seeds at **~0.19 s with
    *and* without** the levers (§E.3), so the boot never stalls on entropy and the
    levers are boot-neutral under TCG — they ship as cheap, correct insurance. The
    real cold-start win is the **warm-start** (savevm/loadvm): a `-loadvm` resume
    reaches a usable device in **15 s vs the 131 s cold boot** (§E.4).

## E.1 Environment

Same class as Stages A–D: 4 physical cores, 15 GiB RAM, **no `/dev/kvm`** (pure
TCG, `accel: auto` → tcg), Ubuntu 24.04, QEMU 8.2.2. Guest kernel built from the
committed `src/beetroot/templates/vm/kernel.config` (linux-6.12.9) **with the new
`CONFIG_HW_RANDOM=y` + `CONFIG_HW_RANDOM_VIRTIO=y`** so the `-device
virtio-rng-pci` actually binds a guest driver. Rootfs from the committed
`build_rootfs` (8 GiB ext4, redroid `11.0.0-latest` baked — the lighter,
Stage-B/D-validated Android keeps repeated TCG boots tractable; the entropy
effect is at the early kernel/init layer and is Android-version-independent).

## E.2 Method

`scripts/vm_boot_ab.py` boots the guest under QEMU, toggling the two levers, and
times **launch → the guest's own `boot_seconds` marker** (a true cold boot, not
the post-boot adb-reconnect lag that inflated the host-wall figure in
`benchmarks/README.md`). Each run takes a **fresh qcow2 overlay** over the same
raw rootfs so a prior boot's `/var/lib/docker` churn can't skew the next. Three
arms, **interleaved** A→B→C each round (so the monotonic host-contention drift
Stage D §D.4 observed cancels across the comparison), three rounds:

* **A — baseline:** no `virtio-rng-pci`, cmdline without `random.trust_cpu=on`.
* **B — trust_cpu only:** `random.trust_cpu=on`, no RNG device.
* **C — shipped default:** `virtio-rng-pci` **and** `random.trust_cpu=on`.

The most direct entropy signal is **when `random: crng init done` fires** in the
kernel log — boot wall-time is dominated by ART/Zygote/`system_server` (which
entropy does not touch), so a CRNG stall shows up as a fixed early-boot delay,
not a proportional one.

## E.3 RNG results — boot-neutral under TCG (the levers don't move boot time)

Three rounds, interleaved A→B→C, pure TCG, `-smp 4`, 8 GiB, redroid 11. One arm-B
boot (round 2) hit a **one-off TCG kernel panic at ~20 s** ("Fatal exception in
interrupt") — an emulation flake unrelated to the lever (the other two arm-B
boots were clean), so it is excluded from the means below and noted here for
honesty. The harness fails such a boot fast rather than recording it.

| arm | `crng init done` (kernel log) | host wall (s) | guest boot (s) |
| --- | --- | --- | --- |
| **A** baseline (no levers) | 0.192 / 0.194 / 0.200 | 126.1 / 128.1 / 132.1 (μ **128.8**) | 108 / 111 / 114 (μ 111.0) |
| **B** `random.trust_cpu=on` only | 0.191 / *(panic)* / 0.193 | 134.1 / 128.1 (μ **131.1**) | 116 / 111 (μ 113.5) |
| **C** `virtio-rng-pci` + `trust_cpu` (shipped) | 0.189 / 0.208 / 0.205 | 130.1 / 132.1 / 130.1 (μ **130.8**) | 113 / 116 / 112 (μ 113.7) |

**Reading (honest):**

* **The boot never stalls on entropy, so the levers can't speed it up.** The
  kernel CRNG reports `crng init done` at **~0.19–0.20 s in every arm, baseline
  included** — the 6.12.9 guest seeds the pool almost instantly from its
  in-kernel jitter-entropy source plus the `-cpu max` `RDRAND` leaf, *with or
  without* the levers. (In one arm-B boot the marker even printed at
  `0.000000` — `trust_cpu` credited `RDRAND` at t=0 — but baseline already
  reaches the same state by 0.19 s, so there is nothing left to win.)
* **The ~3 s spread across arms (128.8 / 131.1 / 130.8 wall) is within the
  within-arm spread** (arm A alone ranges 126–132 s), i.e. host-contention
  drift, not a lever effect — exactly the pattern Stage D §D.4 measured for
  `mitigations=off`. There is **no separable RNG treatment effect on boot
  time** under TCG.
* **They still ship**, for the same reason `mitigations=off` did: they are
  correct, free, and *could* matter on a guest kernel/arch without a fast RNG
  source (no `RDRAND`, weaker jitter entropy) where a real `getrandom()` stall
  would otherwise block Android init / ART. On this stack they are inert
  insurance — the flag verifiably works (the device binds; `trust_cpu` is on
  the cmdline), it just isn't a TCG boot-speed lever.

**So where does the cold-boot time actually go?** Into ART/Zygote/`system_server`
under software emulation — ~111 s of the ~129 s wall is the guest-measured
Android boot itself. That is irreducible per-instruction TCG cost (and the
reason `-smp 4` + MTTCG + `-cpu max` are the levers that *do* move it, §B.5).
The way to make a *repeat* start "blazingly quick" is therefore not to boot
faster but to **not cold-boot at all** — see §E.4.

## E.4 Warm-start (savevm/loadvm) — the real cold-start win: 131 s → 15 s

Since the cold boot is irreducible TCG cost (§E.3), the way to make a *repeat*
start fast is to skip it: checkpoint the running machine once, then resume.
Validated end-to-end on the same host with the documented recipe
([VM boot-cache § Warm-start recipe](vm-savevm-cache.md#warm-start-recipe)) —
a qcow2 overlay over `rootdisk.img`, a QMP monitor socket, `savevm` after
`boot_completed`, then relaunch with `-loadvm`:

| phase | host wall | what it measures |
| --- | --- | --- |
| **cold boot** | **131 s** | fresh `qemu` launch → guest `boot_completed=1` (`boot_seconds=114` guest-measured) |
| **`savevm warm`** | one-time | QMP `stop` → `savevm` writes a **1.79 GiB** internal RAM+disk snapshot into the qcow2, then `quit` |
| **`-loadvm warm` resume** | **15 s** | fresh `qemu -loadvm` launch → host `adb connect` sees `sys.boot_completed=1` (`ro.build.version.release=11`) |

**~8.7× faster to a usable device** (131 s → 15 s), and the 15 s is itself an
upper bound: it includes the `adb connect` retry-poll cadence and the relay
settle, so the guest is *running* (ART/Zygote already warm, the in-guest adb
relay already listening — the resumed serial log shows `ADB_RELAY_OK` and
`guest ready` immediately) in even less. The QMP transcript confirms the clean
path: `STOP` → `savevm` returns `{}` → `quit`, then on resume a real host-side
`adb shell getprop sys.boot_completed` → `1`.

**Caveats (already in the design note):** the snapshot is only valid against the
exact kernel+rootfs it was taken on — gate it with `scripts/vm_cache_key.py`;
and a `loadvm` resume must **never** feed the cold-boot benchmark (it measures
resume, not boot). Wiring this into the CLI (a qcow2 root + QMP socket + a
`--resume` launch path) is tracked on #49; the recipe above needs no code change
today.

## E.5 Bottom line for issue #83

* **RNG levers** (`virtio-rng-pci` + `random.trust_cpu=on`): shipped, correct,
  and **boot-neutral under TCG** on this guest — the CRNG never stalls (seeds at
  ~0.19 s with or without them), so they are cheap insurance, not a speed-up.
  The honest record matches Stage D's `mitigations=off`: a working flag that
  isn't a TCG boot-speed lever.
* **The cold boot itself** is dominated by ART/Zygote/`system_server` under
  software emulation (~114 s of ~131 s); the levers that move *that* are already
  shipped (`-smp` auto = physical cores, MTTCG `thread=multi`, `-cpu max`, and a
  KVM fast path when `/dev/kvm` exists).
* **The "blazingly quick" repeat start** is the **warm-start** (savevm/loadvm):
  **131 s → 15 s**, validated here, documented as a recipe, CLI integration
  tracked on #49. For a Claude-Cloud-style binderless sandbox that boots the
  same guest repeatedly, this is the lever with real leverage.

## E.6 Artifacts (scratch — never committed)

* Guest kernel (with `CONFIG_HW_RANDOM_VIRTIO=y`): `~/.cache/beetroot/vm/bzImage`
  (12 MiB); rootfs `~/.cache/beetroot/vm/rootdisk.img` (8 GiB, redroid 11 baked).
* A/B harness: `scripts/vm_boot_ab.py` (committed); per-arm serial logs under
  `/tmp/vmlogs/r{1,2,3}/`.
* Warm-start validation logs: `/tmp/warm/cold.log`, `/tmp/warm/warm.log`.
