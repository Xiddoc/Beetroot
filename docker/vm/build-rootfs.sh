#!/bin/sh
# build-rootfs.sh — assemble the Beetroot micro-VM guest ext4 rootfs.
#
# Lightweight by design (fast TCG boot): busybox-static + the Docker static
# binary bundle, packed into a raw ext4 image with `mke2fs -d` (no loop mount
# needed). guest-init.sh is installed as /init (PID 1). This is the §4.2
# recipe from docs/design/binderless-hosts-qemu-tcg.md.
#
# Invoked by `beetroot build --vm-kernel`. Run on the build host (needs
# mke2fs from e2fsprogs, curl, and tar). shfmt -i 4 clean.

set -eu

OUT_IMAGE="${1:-rootdisk.img}"
IMAGE_SIZE_MB="${IMAGE_SIZE_MB:-16384}"
BUSYBOX_URL="${BUSYBOX_URL:-https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox}"
DOCKER_VERSION="${DOCKER_VERSION:-27.5.1}"
DOCKER_URL="${DOCKER_URL:-https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz}"

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
WORK="$(mktemp -d)"
ROOT="$WORK/root"

cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT

log() {
    echo "[build-rootfs] $*"
}

build_tree() {
    log "assembling rootfs tree in $ROOT"
    mkdir -p "$ROOT"/bin "$ROOT"/sbin "$ROOT"/proc "$ROOT"/sys "$ROOT"/dev \
        "$ROOT"/run "$ROOT"/var/run "$ROOT"/var/log "$ROOT"/var/lib/docker \
        "$ROOT"/sys/fs/cgroup "$ROOT"/dev/binderfs "$ROOT"/etc

    log "fetching busybox-static"
    curl -fsSL "$BUSYBOX_URL" -o "$ROOT/bin/busybox"
    chmod 0755 "$ROOT/bin/busybox"
    # Install the common applet symlinks busybox provides.
    for applet in sh mount umount mkdir sleep date grep echo cat ln rm; do
        ln -sf busybox "$ROOT/bin/$applet"
    done

    log "fetching Docker static bundle $DOCKER_VERSION"
    curl -fsSL "$DOCKER_URL" -o "$WORK/docker.tgz"
    tar -xzf "$WORK/docker.tgz" -C "$WORK"
    for bin in dockerd containerd containerd-shim-runc-v2 runc docker ctr \
        docker-proxy docker-init; do
        cp "$WORK/docker/$bin" "$ROOT/bin/$bin"
        chmod 0755 "$ROOT/bin/$bin"
    done

    log "installing guest-init.sh as /init"
    cp "$SCRIPT_DIR/guest-init.sh" "$ROOT/init"
    chmod 0755 "$ROOT/init"
}

pack_image() {
    log "packing $OUT_IMAGE (${IMAGE_SIZE_MB} MiB ext4)"
    # mke2fs -d builds the image directly from the tree — no loop mount,
    # no root needed. The image is editable afterwards with debugfs -w.
    mke2fs -q -t ext4 -d "$ROOT" "$OUT_IMAGE" "${IMAGE_SIZE_MB}M"
    log "done: $OUT_IMAGE"
}

build_tree
pack_image
