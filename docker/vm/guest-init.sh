#!/bin/sh
# guest-init.sh — PID 1 inside the Beetroot micro-VM guest.
#
# Implements the §4.3 init contract from
# docs/design/binderless-hosts-qemu-tcg.md: mount the pseudo-filesystems,
# start containerd standalone (NOT dockerd's managed one — its 15s startup
# timeout is blown under TCG), start dockerd waiting on real readiness, then
# run redroid and poll for sys.boot_completed=1. Every blocker in the PoC
# debugging log was plumbing, not physics; the ordering below encodes the
# hard-won fixes (stale pidfile, container-name length, fail-fast on a dead
# container, bpf syscall, etc.).
#
# Runs under busybox sh in the guest rootfs — keep it POSIX. shfmt -i 4 clean.

set -eu

REDROID_IMAGE="${REDROID_IMAGE:-redroid/redroid:11.0.0-latest}"
CONTAINERD_SOCK="/run/containerd/containerd.sock"
# TCG is slow: generous timeouts so a slow-but-progressing boot is not
# mistaken for a hang. Override via the kernel cmdline / env if needed.
CONTAINERD_TIMEOUT="${CONTAINERD_TIMEOUT:-120}"
DOCKERD_TIMEOUT="${DOCKERD_TIMEOUT:-120}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-600}"

log() {
    echo "[guest-init] $*"
}

die() {
    log "FATAL: $*"
    exit 1
}

mount_pseudo_filesystems() {
    log "mounting pseudo-filesystems"
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    mkdir -p /dev/pts /dev/shm
    mount -t devpts devpts /dev/pts
    mount -t tmpfs tmpfs /dev/shm
    # /run AND /var/run as tmpfs: the rootfs /var/run is NOT a symlink to
    # /run, and a stale docker.pid there makes dockerd refuse to start.
    mount -t tmpfs tmpfs /run
    mount -t tmpfs tmpfs /var/run
    mount -t cgroup2 cgroup2 /sys/fs/cgroup
    mkdir -p /dev/binderfs
    mount -t binder binder /dev/binderfs
}

wait_for_socket() {
    # wait_for_socket <path> <timeout-seconds>
    _path="$1"
    _deadline=$(($(date +%s) + $2))
    while [ ! -S "$_path" ]; do
        [ "$(date +%s)" -ge "$_deadline" ] && return 1
        sleep 1
    done
    return 0
}

start_containerd() {
    log "starting containerd (standalone)"
    containerd >/var/log/containerd.log 2>&1 &
    wait_for_socket "$CONTAINERD_SOCK" "$CONTAINERD_TIMEOUT" ||
        die "containerd socket $CONTAINERD_SOCK did not appear in ${CONTAINERD_TIMEOUT}s"
}

start_dockerd() {
    log "starting dockerd"
    dockerd \
        --containerd="$CONTAINERD_SOCK" \
        --iptables=false \
        --bridge=none \
        >/var/log/dockerd.log 2>&1 &
    # Wait on REAL readiness (a server version), not just socket existence.
    _deadline=$(($(date +%s) + DOCKERD_TIMEOUT))
    while ! docker version >/dev/null 2>&1; do
        [ "$(date +%s)" -ge "$_deadline" ] &&
            die "dockerd did not become ready in ${DOCKERD_TIMEOUT}s"
        sleep 1
    done
}

run_redroid() {
    # Clear any stale container from a prior boot (/var/lib/docker persists).
    docker rm -f redroid >/dev/null 2>&1 || true
    log "running $REDROID_IMAGE"
    # --name must be >=2 chars (docker rejects 'r'); --network none pairs
    # with --bridge=none; gpu_mode=guest = software rendering (no GPU/TCG).
    docker run -d --privileged --name redroid --network none \
        "$REDROID_IMAGE" androidboot.redroid_gpu_mode=guest
}

wait_for_boot() {
    log "waiting for sys.boot_completed=1 (slow under TCG; not a hang)"
    _deadline=$(($(date +%s) + BOOT_TIMEOUT))
    while :; do
        _state="$(docker inspect -f '{{.State.Status}}' redroid 2>/dev/null || echo missing)"
        # Fail fast: a dead container looks like a TCG hang otherwise.
        [ "$_state" = "running" ] || die "redroid container is '$_state', not running"
        if docker exec redroid getprop sys.boot_completed 2>/dev/null | grep -q 1; then
            log "sys.boot_completed=1 — redroid is up"
            return 0
        fi
        [ "$(date +%s)" -ge "$_deadline" ] && die "boot did not complete in ${BOOT_TIMEOUT}s"
        sleep 2
    done
}

main() {
    mount_pseudo_filesystems
    start_containerd
    start_dockerd
    run_redroid
    wait_for_boot
    # Keep PID 1 alive so the VM stays up and adbd remains reachable.
    log "guest ready; idling as PID 1"
    while :; do
        sleep 3600
    done
}

main "$@"
