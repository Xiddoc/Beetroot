#!/bin/sh
# guest-init.sh — PID 1 inside the Beetroot micro-VM guest.
#
# Implements the §4.3 init contract from
# docs/design/binderless-hosts-qemu-tcg.md, corrected against the Stage B boot
# log in docs/design/vm-rnd-log.md (§B). The boot path (mount → containerd →
# dockerd → redroid → poll boot_completed) is validated: redroid Android 11
# reaches sys.boot_completed=1 in ~100 s under pure TCG (`-smp 4`, MTTCG, 8 GiB),
# fully offline (image baked into /var/lib/docker by build-rootfs.sh).
#
# Runs under busybox sh in the guest rootfs — keep it POSIX. shfmt -i 4 clean.
#
# ADB reachability (the VmDeviceBackend's whole point) — hard-won Stage B notes:
#   * redroid's adbd is USB-gadget-only by default; TCP needs the prop
#     `service.adb.tcp.port=5555` set AND adbd restarted. adbd then binds the
#     IPv6 wildcard (:::5555) and (with net.ipv6.bindv6only=0) accepts IPv4 too.
#   * `docker run --network none` is the ONLY redroid networking mode that boots
#     cleanly here: `--network host` crash-loops netd/zygote (guest main netns
#     has no usable address), and docker's `-p` publish path is broken on this
#     minimal kernel (docker-proxy RESETs the relayed connection; the pure-DNAT
#     `--userland-proxy=false` fallback needs CONFIG_BRIDGE_NETFILTER, which the
#     kernel lacks). See vm-rnd-log.md §B for the per-hop isolation evidence.
#   * Therefore ADB is bridged with socat OUTSIDE docker's port machinery: a
#     relay enters redroid's network namespace (nsenter) and forwards the QEMU
#     user-net hostfwd target (guest :5555) to adbd. CAVEAT: this relay is not
#     yet end-to-end-confirmed from the host under TCG (the in-netns hop to adbd
#     was proven; the cross-process socat chain still needs hardening). The Python
#     `build_qemu_argv` hostfwd `tcp::<port>-:5555` assumption is correct for the
#     guest side; the open item is purely the guest→container last hop.

set -u

REDROID_IMAGE="${REDROID_IMAGE:-redroid/redroid:11.0.0-latest}"
ADB_TCP_PORT="${ADB_TCP_PORT:-5555}"
CONTAINERD_SOCK="/run/containerd/containerd.sock"
# TCG is slow: generous timeouts so a slow-but-progressing boot is not mistaken
# for a hang. Override via env if needed.
CONTAINERD_TIMEOUT="${CONTAINERD_TIMEOUT:-180}"
DOCKERD_TIMEOUT="${DOCKERD_TIMEOUT:-180}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-900}"

log() {
    echo "[guest-init] $*"
}

die() {
    log "FATAL: $*"
    # Self-terminate so an automated run does not hang a whole VM cycle.
    sleep 3
    poweroff -f
}

mount_pseudo_filesystems() {
    log "mounting pseudo-filesystems"
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    # devtmpfs may already be auto-mounted (CONFIG_DEVTMPFS_MOUNT); ignore EBUSY.
    mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
    mkdir -p /dev/pts /dev/shm
    mount -t devpts devpts /dev/pts
    mount -t tmpfs tmpfs /dev/shm
    # /run AND /var/run as tmpfs: the rootfs /var/run is NOT a symlink to /run,
    # and a stale docker.pid there makes dockerd refuse to start.
    mount -t tmpfs tmpfs /run
    mount -t tmpfs tmpfs /var/run
    mount -t cgroup2 cgroup2 /sys/fs/cgroup
    mkdir -p /dev/binderfs
    mount -t binder binder /dev/binderfs || die "binderfs mount failed"
    # lo up is enough for the socat ADB relay; bring it up before docker.
    ip link set lo up 2>/dev/null || true
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
    # --iptables=false --bridge=none pairs with redroid's --network none below:
    # no bridge/NAT setup at all, so dockerd comes up fast and we never touch the
    # broken port-publish path. (The iptables-legacy binary is still staged for
    # the bridge driver should a future config flip to a published-port model.)
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
    log "running $REDROID_IMAGE (--network none)"
    # --name must be >=2 chars (docker rejects 'r'); --network none is the only
    # mode that boots cleanly here (see header). The extra key=value args become
    # Android system properties at the earliest boot stage: ro.adb.secure=0 (no
    # adb auth) and service/persist.adb.tcp.port so adbd opens TCP.
    docker run -d --privileged --name redroid --network none \
        "$REDROID_IMAGE" \
        androidboot.redroid_gpu_mode=guest \
        ro.adb.secure=0 \
        "service.adb.tcp.port=${ADB_TCP_PORT}" \
        "persist.adb.tcp.port=${ADB_TCP_PORT}" ||
        die "docker run failed"
}

wait_for_boot() {
    log "waiting for sys.boot_completed=1 (slow under TCG; not a hang)"
    _deadline=$(($(date +%s) + BOOT_TIMEOUT))
    _start=$(cut -d. -f1 /proc/uptime)
    while :; do
        _state="$(docker inspect -f '{{.State.Status}}' redroid 2>/dev/null || echo missing)"
        # Fail fast: a dead container looks like a TCG hang otherwise.
        if [ "$_state" != "running" ]; then
            docker logs redroid 2>&1 | tail -30
            die "redroid container is '$_state', not running"
        fi
        if docker exec redroid getprop sys.boot_completed 2>/dev/null | grep -q 1; then
            _now=$(cut -d. -f1 /proc/uptime)
            log "sys.boot_completed=1 — redroid is up (boot_seconds=$((_now - _start)))"
            return 0
        fi
        [ "$(date +%s)" -ge "$_deadline" ] && die "boot did not complete in ${BOOT_TIMEOUT}s"
        sleep 2
    done
}

enable_adb_tcp() {
    # adbd defaults to USB-gadget transport; nudge it onto TCP. Setting the prop
    # then restarting adbd makes it bind :::${ADB_TCP_PORT} (IPv6 wildcard, which
    # accepts IPv4 too once bindv6only=0).
    log "enabling adbd TCP on :${ADB_TCP_PORT} inside redroid"
    docker exec redroid sysctl -w net.ipv6.bindv6only=0 >/dev/null 2>&1 || true
    docker exec redroid setprop service.adb.tcp.port "$ADB_TCP_PORT" 2>/dev/null || true
    docker exec redroid stop adbd 2>/dev/null || true
    sleep 1
    docker exec redroid start adbd 2>/dev/null || true
    sleep 4
}

start_adb_relay() {
    # redroid runs with --network none, so its adbd lives in an isolated netns.
    # docker's own port-publish is broken on this kernel (see header), so we
    # bridge ADB with socat: a relay that enters redroid's netns (nsenter -n) and
    # forwards the QEMU user-net hostfwd target (guest :${ADB_TCP_PORT}) to adbd.
    _rpid="$(docker inspect -f '{{.State.Pid}}' redroid 2>/dev/null)"
    [ -n "$_rpid" ] || {
        log "WARN: could not resolve redroid pid; ADB relay not started"
        return 0
    }
    log "starting ADB relay: guest :${ADB_TCP_PORT} -> redroid netns adbd (pid $_rpid)"
    socat "TCP4-LISTEN:${ADB_TCP_PORT},fork,reuseaddr" \
        "EXEC:/bin/nsenter -t $_rpid -n /bin/socat - TCP6:[::1]:${ADB_TCP_PORT}" \
        >/var/log/adb-relay.log 2>&1 &
    sleep 2
}

main() {
    mount_pseudo_filesystems
    start_containerd
    start_dockerd
    run_redroid
    wait_for_boot
    enable_adb_tcp
    start_adb_relay
    # Keep PID 1 alive so the VM stays up and adbd remains reachable.
    log "guest ready; idling as PID 1"
    while :; do
        sleep 3600
    done
}

main "$@"
