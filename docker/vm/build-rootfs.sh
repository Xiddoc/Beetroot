#!/bin/sh
# build-rootfs.sh — assemble the Beetroot micro-VM guest ext4 rootfs.
#
# Lightweight by design (fast TCG boot): busybox-static + the Docker static
# binary bundle + a STATIC iptables-legacy + socat (the host-ADB relay; see
# guest-init.sh) + the redroid image baked into /var/lib/docker, packed into a
# raw ext4 image with `mke2fs -d` (no loop mount needed). guest-init.sh is
# installed as /init (PID 1). This is the §4.2 recipe from
# docs/design/binderless-hosts-qemu-tcg.md, corrected against the Stage B
# build log in docs/design/vm-rnd-log.md.
#
# Invoked by `beetroot build --vm-kernel`. Run on the build host (needs mke2fs
# from e2fsprogs, curl, tar, and a way to obtain the redroid image — a docker
# daemon to pull+save, or a pre-saved tarball via REDROID_TAR). shfmt -i 4 clean.
#
# IMPORTANT corrections proven in Stage B (see vm-rnd-log.md §B):
#   * The redroid image MUST be baked into the guest's /var/lib/docker so the
#     guest boots fully offline (--network none / no internet). Staging it with
#     the SAME static docker bundle version the guest runs keeps the overlay2
#     on-disk layout byte-compatible — dockerd then comes up in ~1s with no
#     in-guest `docker load`.
#   * The redroid tag is `11.0.0-latest` (plain `11.0.0` does not exist).
#   * dockerd's bridge driver needs an `iptables` BINARY; the static docker
#     bundle does not ship one. We stage iptables-legacy (the kernel has the
#     legacy xt backend, NOT nftables) + its shared libs. Without it dockerd
#     dies: "failed to create NAT chain DOCKER: iptables not found".
#   * busybox must expose `nsenter`, `netstat`, `udhcpc`, `poweroff` — install
#     all applets at boot via `busybox --install -s` (guest-init does this),
#     so we only need to drop the busybox binary here.

set -eu

OUT_IMAGE="${1:-rootdisk.img}"
# 8 GiB: the 2.1 GiB redroid image + overlay2 scratch + container rootfs during
# the redroid run. 16 GiB (the original default) also works but wastes space.
IMAGE_SIZE_MB="${IMAGE_SIZE_MB:-8192}"
# The host's busybox-static (Ubuntu 1.36.x) is known-good and already used in
# Stage A; override BUSYBOX_BIN to ship a different one.
BUSYBOX_BIN="${BUSYBOX_BIN:-/usr/bin/busybox}"
DOCKER_VERSION="${DOCKER_VERSION:-27.5.1}"
DOCKER_URL="${DOCKER_URL:-https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz}"
# redroid 11.0.0 (ashmem-less; needs CONFIG_MEMFD_CREATE on the 6.12 kernel).
REDROID_IMAGE="${REDROID_IMAGE:-redroid/redroid:11.0.0-latest}"
# Pre-saved `docker save` tarball of REDROID_IMAGE. If empty, we pull+save with
# the host docker daemon (DOCKER_BIN). If the host has no daemon, set REDROID_TAR
# to a tarball produced elsewhere (e.g. `skopeo copy docker://… docker-archive:…`).
REDROID_TAR="${REDROID_TAR:-}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
WORK="$(mktemp -d)"
ROOT="$WORK/root"
DBIN="$WORK/docker/docker" # extracted static bundle dir

cleanup() {
    # Stop the staging dockerd if we started one.
    [ -f "$WORK/stage.pid" ] && kill "$(cat "$WORK/stage.pid")" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

log() {
    echo "[build-rootfs] $*"
}

fetch_static_bundle() {
    log "fetching Docker static bundle $DOCKER_VERSION"
    curl -fsSL "$DOCKER_URL" -o "$WORK/docker.tgz"
    mkdir -p "$WORK/docker"
    tar -xzf "$WORK/docker.tgz" -C "$WORK/docker"
}

stage_docker_root() {
    # Produce a /var/lib/docker pre-populated with the redroid image, using the
    # SAME static bundle (overlay2 layout matches what the guest dockerd reads).
    _stage="$WORK/dockerroot"
    mkdir -p "$_stage"

    if [ -z "$REDROID_TAR" ]; then
        log "pulling $REDROID_IMAGE with host docker ($DOCKER_BIN) and saving to tarball"
        "$DOCKER_BIN" pull "$REDROID_IMAGE"
        REDROID_TAR="$WORK/redroid.tar"
        "$DOCKER_BIN" save "$REDROID_IMAGE" -o "$REDROID_TAR"
    fi

    log "loading $REDROID_IMAGE into a staging dockerd (static $DOCKER_VERSION)"
    "$DBIN/dockerd" \
        --data-root="$_stage" \
        --host="unix://$WORK/stage.sock" \
        --exec-root="$WORK/stage-exec" \
        --pidfile="$WORK/stage.pid" \
        --iptables=false --bridge=none \
        >"$WORK/stage-dockerd.log" 2>&1 &
    # Wait for the staging daemon to be ready.
    _i=0
    while ! "$DBIN/docker" --host="unix://$WORK/stage.sock" info >/dev/null 2>&1; do
        _i=$((_i + 1))
        [ "$_i" -ge 60 ] && {
            tail -20 "$WORK/stage-dockerd.log"
            echo "[build-rootfs] staging dockerd did not start" >&2
            exit 1
        }
        sleep 1
    done
    "$DBIN/docker" --host="unix://$WORK/stage.sock" load -i "$REDROID_TAR"
    kill "$(cat "$WORK/stage.pid")" 2>/dev/null || true
    rm -f "$WORK/stage.pid"
    sleep 3
    echo "$_stage"
}

build_tree() {
    log "assembling rootfs tree in $ROOT"
    mkdir -p "$ROOT"/bin "$ROOT"/sbin "$ROOT"/proc "$ROOT"/sys "$ROOT"/dev \
        "$ROOT"/run "$ROOT"/var/run "$ROOT"/var/log "$ROOT"/var/lib \
        "$ROOT"/sys/fs/cgroup "$ROOT"/dev/binderfs "$ROOT"/etc "$ROOT"/tmp \
        "$ROOT"/usr/sbin "$ROOT"/lib/x86_64-linux-gnu "$ROOT"/lib64 \
        "$ROOT"/usr/lib/x86_64-linux-gnu
    chmod 1777 "$ROOT"/tmp

    log "installing busybox ($BUSYBOX_BIN)"
    cp "$BUSYBOX_BIN" "$ROOT/bin/busybox"
    chmod 0755 "$ROOT/bin/busybox"
    # guest-init runs `busybox --install -s` to lay down ALL applet symlinks
    # (sh, mount, poweroff, nsenter, netstat, udhcpc, ip, ...) at boot, so we
    # only ship the single binary here.

    log "installing Docker static bundle binaries"
    for bin in dockerd containerd containerd-shim-runc-v2 runc docker ctr \
        docker-proxy docker-init; do
        cp "$DBIN/$bin" "$ROOT/bin/$bin"
        chmod 0755 "$ROOT/bin/$bin"
    done

    log "staging iptables-legacy (+ libs) — dockerd's bridge driver needs it"
    # The 6.12 kernel config carries the LEGACY xt backend (CONFIG_IP_NF_*),
    # NOT nftables (CONFIG_NF_TABLES is off), so point plain `iptables` at the
    # legacy multi-binary.
    cp /usr/sbin/xtables-legacy-multi "$ROOT/usr/sbin/"
    for n in iptables iptables-save iptables-restore ip6tables ip6tables-save ip6tables-restore; do
        ln -sf xtables-legacy-multi "$ROOT/usr/sbin/$n"
        ln -sf xtables-legacy-multi "$ROOT/usr/sbin/$n-legacy"
    done
    for lib in $(ldd /usr/sbin/xtables-legacy-multi | awk '{print $3}' | grep -E '^/'); do
        cp -L "$lib" "$ROOT/lib/x86_64-linux-gnu/" 2>/dev/null || true
    done
    cp -L /lib64/ld-linux-x86-64.so.2 "$ROOT/lib64/" 2>/dev/null || true

    log "staging socat (+ libs) — the host-ADB relay (see guest-init.sh / log §B)"
    cp /usr/bin/socat "$ROOT/bin/socat"
    chmod 0755 "$ROOT/bin/socat"
    for lib in $(ldd /usr/bin/socat | awk '{print $3}' | grep -E '^/'); do
        cp -L "$lib" "$ROOT/lib/x86_64-linux-gnu/" 2>/dev/null || true
    done

    log "baking the redroid image into /var/lib/docker (offline boot)"
    fetch_static_bundle_done=1
    _dockerroot="$(stage_docker_root)"
    cp -a "$_dockerroot" "$ROOT/var/lib/docker"

    log "installing guest-init.sh as /init"
    cp "$SCRIPT_DIR/guest-init.sh" "$ROOT/init"
    chmod 0755 "$ROOT/init"
}

pack_image() {
    log "packing $OUT_IMAGE (${IMAGE_SIZE_MB} MiB ext4)"
    # mke2fs -d builds the image directly from the tree — no loop mount, no root
    # needed. The image is editable afterwards with debugfs -w (handy for fast
    # iteration on /init without repacking the whole disk).
    mke2fs -q -t ext4 -d "$ROOT" "$OUT_IMAGE" "${IMAGE_SIZE_MB}M"
    log "done: $OUT_IMAGE"
}

fetch_static_bundle
build_tree
pack_image
