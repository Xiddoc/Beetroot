# Binderless Hosts: redroid via QEMU/TCG micro-VM

!!! success "Status: proof-of-concept validated (2026-06)"
    redroid (Android 11) was booted to `sys.boot_completed=1` inside a
    QEMU **TCG** guest (no KVM, no host binder) on a locked-down
    Firecracker host. This doc records the *why*, the reproducible
    recipe, the debugging log, and the proposed backend/fallback
    design. Nothing here has shipped in the CLI yet — it is the spec a
    future `vm` backend should build against. See also
    [Running in CI / without kernel access](../guides/running-in-ci.md)
    and [Device backends](device-backends.md).

## 1. The problem

Beetroot's Redroid backend runs Android **userspace** directly on the
**host's** Linux kernel (that is the whole point of a container — there
is no second kernel). Android's IPC is **binder**, a kernel driver.
So Redroid can only run where the host kernel provides binder:

* `CONFIG_ANDROID_BINDER_IPC=y` (built in), **or**
* a loadable `binder_linux` module (Waydroid/anbox-modules DKMS), **or**
* nothing — and then Redroid cannot run at all.

On a host with **no binder and no way to add it**, Beetroot is dead in
the water. Concretely this includes:

* hardened CI runners and managed cloud sandboxes booted with
  `nomodule` or signed-module enforcement;
* the Claude-Code-on-the-web execution environment this PoC was built
  in: a **Firecracker microVM**, kernel cmdline contains `nomodule`,
  `# CONFIG_ANDROID_BINDER_IPC is not set`, **no `/dev/kvm`**.

In that environment every "just load the module" path is closed:
`init_module(2)` returns `ENOSYS`, there is no `/lib/modules`, and
`mount -t binder` fails with `unknown filesystem type`.

!!! info "Why not implement binder in userspace?"
    A userspace binder shim (CUSE/FUSE, or seccomp-unotify +
    userfaultfd intercepting the `ioctl`/`mmap` ABI) is *possible* —
    gVisor's Sentry once had one — but it is a multi-month research
    project, risks Android's watchdog timeouts, and gVisor **removed**
    theirs as an unmaintained burden. We chose the pragmatic path:
    **bring our own kernel** in a VM.

## 2. The capability ladder

From fastest/cheapest to slowest, the ways to give Redroid a binder:

| Rank | Mechanism | Speed | Works when |
| --- | --- | --- | --- |
| 1 | Host kernel has `CONFIG_ANDROID_BINDER_IPC=y` | native | host already supports it |
| 2 | Load out-of-tree `binder_linux` (DKMS) | native | modules allowed + headers present |
| 3 | Nested VM w/ binder kernel, **KVM**-accelerated | near-native | `/dev/kvm` present |
| 4 | Nested VM w/ binder kernel, **TCG** (pure emulation) | ~5–20× slower | always (last resort) |

Ranks 1–2 are "use the host." Ranks 3–4 are "bring our own kernel."
This PoC proves **rank 4**, the hardest case — if that works, rank 3
(same thing + KVM) is strictly easier and faster.

## 3. What we proved

### 3.1 Binder is usable under TCG (not just present)

A guest kernel built with binder, booted under TCG, exposes binderfs
and — critically — routes a **real cross-process transaction**:

* dynamic device creation via `binder-control` (`BINDER_CTL_ADD`) ✅
* a server process became the **context manager** (handle 0 — the role
  `servicemanager` plays at Android boot) ✅
* a client sent `BC_TRANSACTION` → driver → server `BR_TRANSACTION`,
  server `BC_REPLY` → client `BR_REPLY`, payloads verified ✅
  (`BINDER_VERSION` protocol 8)

### 3.2 Full redroid boot under TCG

```
Host: Firecracker VM — nomodule, no /dev/kvm, no host binder
 └─ QEMU 8.2 TCG (thread=multi, -cpu max, -smp 4, -m 8192)
     └─ Linux 6.12.9 (custom; binder + binderfs + BPF + cgroup/overlay/virtio built-in)
         └─ containerd + dockerd (static 27.5.1)
             └─ docker run --privileged redroid/redroid:11.0.0-latest
                 └─ sys.boot_completed = 1   @ t≈150s
```

Evidence from `logcat`: `system_server`, `ActivityManager` starting app
processes, `Zygote` forking children, `SurfaceFlinger`+`hwcomposer`
VSYNC, `adbd`/JDWP. **Full boot ≈ 2.5 min under pure emulation.**

## 4. Reproducible recipe

All artifacts were assembled under `/root/binder-poc` in the PoC. The
durable knowledge is the **kernel config delta**, the **guest init
contract**, and the **QEMU invocation**.

### 4.1 Kernel (build on the host; runs as the guest kernel)

Start from `make defconfig` (x86_64), then enable — **all built-in
(`=y`), never `=m`**, because the guest rootfs ships no modules:

```
# Binder (the whole point)
CONFIG_ANDROID=y
CONFIG_ANDROID_BINDER_IPC=y
CONFIG_ANDROID_BINDERFS=y
CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"

# Container runtime (Docker/containerd/runc)
CONFIG_NAMESPACES=y CONFIG_USER_NS=y CONFIG_PID_NS=y CONFIG_NET_NS=y
CONFIG_CGROUPS=y CONFIG_MEMCG=y CONFIG_BLK_CGROUP=y CONFIG_CPUSETS=y
CONFIG_CGROUP_PIDS=y CONFIG_CGROUP_DEVICE=y CONFIG_CGROUP_BPF=y
CONFIG_OVERLAY_FS=y CONFIG_EXT4_FS=y
CONFIG_BRIDGE=y CONFIG_VETH=y CONFIG_TUN=y
CONFIG_NETFILTER=y CONFIG_NF_NAT=y CONFIG_IP_NF_IPTABLES=y CONFIG_IP_NF_NAT=y
CONFIG_NETFILTER_XT_TARGET_MASQUERADE=y CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=y

# bpf() syscall — REQUIRED: cgroup-v2 device control is eBPF-only.
# Without this, runc dies: bpf_prog_query(BPF_CGROUP_DEVICE) ENOSYS.
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y

# Guest I/O
CONFIG_VIRTIO_BLK=y CONFIG_VIRTIO_NET=y CONFIG_VIRTIO_PCI=y CONFIG_VIRTIO_CONSOLE=y
CONFIG_DEVTMPFS=y CONFIG_DEVTMPFS_MOUNT=y CONFIG_BLK_DEV_INITRD=y

# Stability (see §5 lmkd): pressure-stall info for lmkd / low-memory-killer
CONFIG_PSI=y
```

!!! warning "ashmem is gone — Android 11+ only"
    The `ashmem` driver was removed from mainline in Linux 5.18.
    Android ≤10 needs `/dev/ashmem`; **Android 11+** falls back to
    `memfd_create` (`CONFIG_MEMFD_CREATE=y`). On a modern kernel you
    must use a **redroid 11.0.0+** image. (We tried 8.1 first — it
    cannot boot on 6.12.)

### 4.2 Guest rootfs

Lightweight by design (fast TCG boot): busybox-static + the Docker
**static binary bundle** (`dockerd`, `containerd`, `containerd-shim-runc-v2`,
`runc`, `docker`, `ctr`, `docker-proxy`, `docker-init`). Packed into a
raw ext4 image with `mke2fs -d <tree>` (no loop mount needed); the
image is editable after the fact with `debugfs -w -R "write …"` +
`sif <inode> mode 0100755`.

### 4.3 Guest `init` contract (PID 1)

Ordered, with the hard-won correctness notes inline:

1. mount `proc`, `sysfs`, `devtmpfs`, `devpts`, `tmpfs` on `/dev/shm`,
   **`tmpfs` on `/run` AND `/var/run`** (the rootfs `/var/run` is *not*
   a symlink to `/run`; a stale `docker.pid` there makes dockerd refuse
   to start), `cgroup2` on `/sys/fs/cgroup`, `binder` on `/dev/binderfs`.
2. **Start `containerd` standalone first**, wait for
   `/run/containerd/containerd.sock` (generous timeout — TCG is slow).
   Do *not* rely on dockerd's managed containerd: its 15 s startup
   timeout is blown under TCG.
3. Start `dockerd --containerd=/run/containerd/containerd.sock
   --iptables=false --bridge=none`. Wait on **real readiness**
   (`docker version` returns a server version), not socket existence.
4. `docker rm -f redroid` (clear any stale container from a prior boot
   — `/var/lib/docker` is persistent).
5. `docker run -d --privileged --name redroid --network none
   redroid/redroid:11.0.0-latest androidboot.redroid_gpu_mode=guest …`
   * `--name` must be **≥2 chars** (`r` is rejected by docker's regex).
   * `--network none` pairs with `--bridge=none` (no iptables needed).
   * `gpu_mode=guest` = software rendering (no GPU under TCG).
6. Poll `docker inspect -f '{{.State.Status}}'` (**fail fast** if the
   container is not `running`) and `getprop sys.boot_completed`.

### 4.4 QEMU invocation

```sh
qemu-system-x86_64 \
  -M q35 -accel tcg,thread=multi,tb-size=1024 -cpu max -smp 4 -m 8192 \
  -nographic -display none -no-reboot \
  -kernel bzImage \
  -drive file=rootdisk.img,format=raw,if=virtio \
  -append "console=ttyS0 root=/dev/vda rw init=/init panic=1"
```

`thread=multi` (MTTCG) is the single biggest TCG lever — one host
thread per guest vCPU. `-cpu max` exposes SSE4/AVX that ART/bionic
expect. On a host **with** `/dev/kvm`, swap `-accel tcg,thread=multi`
for `-accel kvm` for near-native speed (rank 3).

## 5. Debugging log (every blocker was plumbing, not physics)

This is the most reusable knowledge — the failure→fix chain:

| # | Symptom | Root cause | Fix |
| --- | --- | --- | --- |
| 1 | host can't load binder | `nomodule`, `init_module`=ENOSYS | build guest kernel w/ binder `=y` |
| 2 | "is binder even usable under TCG?" | unknown | proved cross-process transaction |
| 3 | `docker run` → "See 'docker run --help'" | error text truncated by `tail -1`; **`--name r` too short** (docker names need ≥2 chars) | rename `redroid`; never `tail` the error |
| 4 | dockerd: "timeout waiting for containerd to start" | managed-containerd 15 s timeout blown under TCG | run containerd standalone, wait on its socket |
| 5 | dockerd: "delete /var/run/docker.pid: PID N still running" | `/var/run` persistent, stale pidfile, PID reused | tmpfs over `/var/run` |
| 6 | runc: `bpf_prog_query(BPF_CGROUP_DEVICE) ENOSYS` | cgroup-v2 device ctrl is eBPF-only; `CONFIG_BPF_SYSCALL` off | enable `CONFIG_BPF_SYSCALL` |
| 7 | "container name /redroid already in use" | prior boot left a `created` container on disk | `docker rm -f` before run |
| → | **`sys.boot_completed=1`** | — | ✅ |

!!! note "Known wrinkle: lmkd flapping"
    Post-boot, `lmkd` (low-memory-killer daemon) crash-loops
    (*"critical process 'lmkd' exited 4 times"*) because it wants
    pressure-stall info. `boot_completed` is still reached, but left
    unaddressed lmkd restarts can eventually trigger an Android reboot.
    **`CONFIG_PSI=y`** (added in §4.1) is the fix to validate next for a
    *stable* idle, not just a successful boot.

!!! tip "Debugging methodology that worked"
    * **Never `tail -1` an error stream** — it hid the real docker
      message behind "See 'docker run --help'." for three iterations.
    * **Fail fast**: a poll loop that waits 30 min on a dead container
      *looks* like a TCG hang and wastes a whole VM cycle. Check
      `State.Status != running` and bail with `docker logs`.
    * **Cache across iterations**: the redroid image lives in the
      persistent `/var/lib/docker`, so skip `docker load` after the
      first boot — iterations drop from ~25 min to ~3 min.
    * **Edit the image in place** with `debugfs`, don't rebuild the
      16 GB disk to change one script.

## 6. Performance

* Minimal kernel + busybox boot under TCG: ~3 s.
* Full redroid Android 11 boot to `boot_completed`: **~150 s** under
  TCG (`-smp 4`, MTTCG, 8 GiB). Android *feels* the emulation
  (`ActivityManager: Slow operation: 5142ms`) but completes.
* The watchdog/ANR boot-loop risk that was feared **did not
  materialize** for boot. With KVM (rank 3) this should approach
  native redroid speed.

## 7. Proposed Beetroot integration (the backend + fallback design)

This is the design question this PoC unblocks: **how should Beetroot
choose between using the host's binder and falling back to the VM?**

### 7.1 Principle

> Silent automation is a gift when it is **cheap and correct**; it is a
> **trap** when it is expensive. Make the fast paths invisible; make the
> expensive path a deliberate, loud choice — never a silent surprise.

A fully-automatic ladder that quietly falls all the way to TCG would
"work" but leave a developer staring at a 2½-minute boot with no idea
why it is 20× slower than their colleague's. That is exactly the
frustration to design out.

### 7.2 Recommended model: auto-detect, auto-use cheap paths, gate the expensive one

Add a config key (see [config reference](../reference/config.md)):

```yaml
redroid_backend: auto   # auto | native | module | vm
vm_accel: auto          # auto | kvm | tcg   (only consulted for `vm`)
```

`auto` resolution order:

1. **native** — host kernel has binder → use it. (transparent, fast)
2. **module** — `binder_linux` is loadable → load it and use it.
   (transparent, fast, near-native)
3. **STOP.** If neither works, **do not silently start a VM.**
   `beetroot up` fails fast with an actionable error:

   ```
   error: this host has no kernel binder and the binder_linux module
   could not be loaded (nomodule / no matching headers).
   Redroid needs binder. Options:
     • enable CONFIG_ANDROID_BINDER_IPC / load binder_linux on the host, or
     • opt into the emulated micro-VM backend:  redroid_backend: vm
       (slow without KVM on this host: /dev/kvm absent → expect ~Nm boots)
   Run `beetroot doctor` for the full capability report.
   ```

The VM backend is **opt-in** (`redroid_backend: vm`). The cost is
real, so the choice is explicit. But once chosen it is *not* nagged on
every command — it is configured.

### 7.3 Make the expensive path loud *when engaged*

When `redroid_backend: vm` is active, `beetroot up` prints a one-time,
unmissable banner:

```
▶ backend: emulated micro-VM (no host binder)
  acceleration: TCG (software) — /dev/kvm not available
  first boot is slow (~2–3 min); this is expected, not a hang.
  pin `vm_accel: kvm` on a host with nested virt for near-native speed.
```

and `beetroot doctor` always reports the resolved backend + *why*:

```
binder:        absent (CONFIG_ANDROID_BINDER_IPC not set)
binder_linux:  not loadable (nomodule)
/dev/kvm:      absent
→ selected backend: vm (tcg)   [reason: no host binder; KVM unavailable]
```

### 7.4 Why not fully-manual?

Forcing every developer to hand-pick a backend punishes the 95 % whose
host *does* have binder (ranks 1–2) with needless config. Detection is
cheap and reliable (read `/proc/filesystems`, `/sys/…`, `modprobe -n`,
`stat /dev/kvm`), so the cheap-and-correct paths should "just work".

### 7.5 Why not fully-automatic?

Because rank 4 (TCG) has a 5–20× cost. Auto-engaging it silently trades
a clear, one-time error message for recurring, mysterious slowness —
the worst trade in UX terms. The boundary is drawn exactly at the
expensive step.

### 7.6 Summary

| Path | Cost | Engagement |
| --- | --- | --- |
| native binder | free | automatic, silent |
| load `binder_linux` | ~free | automatic, silent |
| VM + KVM | small | opt-in (`vm`), auto-accelerated, banner once |
| VM + TCG | large | opt-in (`vm`), loud banner, `doctor` explains |

## 8. Roadmap

1. **Stabilize** — rebuild with `CONFIG_PSI=y`, confirm a clean
   multi-minute idle (no lmkd flapping); pin a kernel version + config
   fragment in-tree.
2. **Package** — ship a prebuilt guest kernel + minimal rootfs builder
   (or a `beetroot build --vm-kernel` step wrapping the kernel build).
3. **`VmDeviceBackend`** — implement against the
   [`DeviceBackend` Protocol](device-backends.md): `up` boots the
   micro-VM and forwards ADB; `down` powers it off; ports map through
   to the guest's redroid `5555`.
4. **`auto` resolver + `doctor`** — the detection ladder and messaging
   in §7.
5. **KVM fast path** — detect `/dev/kvm`, prefer `-accel kvm`.
