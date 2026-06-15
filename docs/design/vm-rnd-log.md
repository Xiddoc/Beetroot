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
