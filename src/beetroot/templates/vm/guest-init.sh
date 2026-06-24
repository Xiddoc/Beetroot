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
# ADB reachability (the VmDeviceBackend's whole point) — PROVEN end-to-end in
# Stage C (host `adb shell getprop sys.boot_completed` returns 1; see §C of
# vm-rnd-log.md for the full transcript). The working model:
#   * redroid's adbd is USB-gadget-only by default; TCP needs the prop
#     `service.adb.tcp.port=5555` set AND adbd restarted. adbd then binds the
#     IPv6 wildcard (:::5555) and (with net.ipv6.bindv6only=0) accepts IPv4 too.
#   * `docker run --network none` is the ONLY redroid networking mode that boots
#     cleanly here: `--network host` crash-loops netd/zygote (guest main netns
#     has no usable address), and docker's `-p` publish path is broken on this
#     minimal kernel (docker-proxy RESETs the relayed connection; the pure-DNAT
#     `--userland-proxy=false` fallback needs CONFIG_BRIDGE_NETFILTER, which the
#     kernel lacks). See vm-rnd-log.md §B for the per-hop isolation evidence.
#   * So ADB is bridged with socat OUTSIDE docker's port machinery: an outer
#     socat in the guest MAIN netns listens on the QEMU user-net hostfwd target
#     (guest :5555); each connection spawns an inner socat that nsenter's into
#     redroid's netns and connects to adbd. TWO things made this carry bytes:
#     (a) the EXEC wrapper-script fix below (socat's EXEC: chokes on commas in
#     an inline command), and (b) bringing up eth0 with the user-net address
#     10.0.2.15 — the hostfwd delivers to the guest's eth0 IP, not loopback, so
#     a down eth0 silently dropped every host SYN. Both are handled here.
#   * The Python `build_qemu_argv` hostfwd `tcp:127.0.0.1:<port>-:5555` is
#     correct for the guest side; no Python change is needed (the relay + eth0
#     bring-up are entirely in this init).

set -u

# Path of the baked-image marker build_rootfs writes (issue #82): the plain
# upstream redroid image whose layers are baked into /var/lib/docker, so the
# guest boots whatever Android version was baked rather than a hardcoded
# default. resolve_redroid_image() (run first in main, after log() is defined)
# consults it; an explicit REDROID_IMAGE env still wins.
_BAKED_IMAGE_FILE="/etc/beetroot/redroid-image"

# Legacy fallback for a *pre-#82* rootfs that predates the marker. This is the
# historical default ON PURPOSE — such a rootfs baked its 11.0.0 image into
# /var/lib/docker, so falling back to any other tag would 404 at `docker run`.
# It is deliberately NOT config.DEFAULT_ANDROID_VERSION: a *current* rootfs
# always carries the marker, so reaching this fallback on a freshly-built rootfs
# is a build bug. resolve_redroid_image() logs a loud WARN naming it rather than
# silently resurrecting the "boots Android 11" bug (#82/#97).
_LEGACY_FALLBACK_IMAGE="redroid/redroid:11.0.0-latest"
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

resolve_redroid_image() {
    # Decide which redroid image to boot, in precedence order:
    #   1. an explicit REDROID_IMAGE env override (testing / manual runs);
    #   2. the baked-image marker build_rootfs wrote (the normal path — boots
    #      exactly the Android version the rootfs was built for, issue #82);
    #   3. the legacy fallback, with a LOUD warning.
    #
    # The whole point of #82 is "never silently boot a different Android than
    # the rootfs was built for." A current rootfs always carries the marker, so
    # a missing/empty marker here means the rootfs is pre-#82, hand-assembled,
    # or its bake was interrupted — a condition we want to hear about, not paper
    # over by quietly resurrecting Android 11 (issue #97). So the fallback warns
    # prominently and names the image it is about to boot.
    if [ -n "${REDROID_IMAGE:-}" ]; then
        log "using REDROID_IMAGE override: $REDROID_IMAGE"
        return 0
    fi
    if [ -s "$_BAKED_IMAGE_FILE" ]; then
        REDROID_IMAGE="$(cat "$_BAKED_IMAGE_FILE")"
        if [ -n "$REDROID_IMAGE" ]; then
            log "using baked redroid image from $_BAKED_IMAGE_FILE: $REDROID_IMAGE"
            return 0
        fi
    fi
    REDROID_IMAGE="$_LEGACY_FALLBACK_IMAGE"
    log "WARN: no usable baked-image marker at $_BAKED_IMAGE_FILE (missing or" \
        "empty); falling back to $REDROID_IMAGE. The rootfs may be stale or" \
        "mis-built — a freshly-built rootfs always carries this marker, so if" \
        "this is one, that is a bug (issue #97)."
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
    ip link set lo up 2>/dev/null || true
    bring_up_eth0
}

bring_up_eth0() {
    # The QEMU user-net hostfwd (tcp:127.0.0.1:<host>-:5555) delivers host
    # traffic to the GUEST'S eth0 ADDRESS (the user-net default 10.0.2.15), NOT
    # to guest loopback. If eth0 is left down the SYN is silently dropped and the
    # host `adb connect` times out even though the in-guest relay is healthy.
    # This was the true last-hop blocker (the relay itself was already fine).
    #
    # QEMU's built-in user-net (SLIRP) DHCP hands out 10.0.2.15/24, gw 10.0.2.2.
    # We assign it statically — deterministic, no udhcpc lease wait, and the
    # relay only needs eth0 to have *an* address the hostfwd target can reach.
    _eth="$(ip -o link 2>/dev/null | awk -F': ' '/^[0-9]+: e/{print $2; exit}')"
    [ -n "$_eth" ] || _eth=eth0
    log "bringing up $_eth (static 10.0.2.15/24 for QEMU user-net hostfwd)"
    ip link set "$_eth" up 2>/dev/null || true
    ip addr add 10.0.2.15/24 dev "$_eth" 2>/dev/null || true
    ip route add default via 10.0.2.2 dev "$_eth" 2>/dev/null || true
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
    # Persist redroid's /data across VM reboots by bind-mounting a directory on
    # the (persistent) guest rootfs as the container's /data. Without this the
    # rm -f + fresh `docker run` below discards /data every boot — the Magisk
    # DB, MAGISKBIN, and installed modules reset — so a flash → reboot →
    # activate flow (Zygisk modules such as LSPosed need that second boot, where
    # magiskd reads the persisted zygisk=1 at post-fs-data and injects zygote)
    # could never complete. The mount is overridable via BEETROOT_GUEST_DATA_DIR.
    _data_dir="${BEETROOT_GUEST_DATA_DIR:-/var/lib/redroid-data}"
    mkdir -p "$_data_dir"
    log "running $REDROID_IMAGE (--network none, /data persisted at $_data_dir)"
    # --name must be >=2 chars (docker rejects 'r'); --network none is the only
    # mode that boots cleanly here (see header). The extra key=value args become
    # Android system properties at the earliest boot stage: ro.adb.secure=0 (no
    # adb auth) and service/persist.adb.tcp.port so adbd opens TCP.
    docker run -d --privileged --name redroid --network none \
        -v "$_data_dir:/data" \
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
    # redroid runs with --network none, so its adbd lives in an isolated netns
    # with only `lo`. docker's own port-publish is broken on this minimal kernel
    # (docker-proxy RESETs; --userland-proxy=false needs CONFIG_BRIDGE_NETFILTER),
    # so we bridge ADB with socat OUTSIDE docker's port machinery.
    #
    # The relay is a single socat in the guest MAIN netns listening on the QEMU
    # user-net hostfwd target (guest :${ADB_TCP_PORT}); for each accepted
    # connection it spawns an inner socat that nsenter's into redroid's netns and
    # connects to adbd on loopback. adbd binds the IPv6 wildcard (:::5555) and,
    # with bindv6only=0, accepts IPv4 too; we target 127.0.0.1 inside the netns,
    # which the Stage B hop-isolation proved replies with a valid CNXN.
    #
    # CRITICAL: socat's EXEC: address parses commas as option separators, so the
    # inner command line (`nsenter -t PID -n socat STDIO TCP4:...`) must NOT be
    # passed inline — commas in any socat option there trigger `EXEC: wrong
    # number of parameters` (the bug that stalled every Stage B relay run).
    # Instead we write the inner command to a parameterless wrapper script and
    # EXEC that. Fixing this made the relay carry a full ADB CNXN end-to-end
    # (Stage C, vm-rnd-log.md §C).
    _rpid="$(docker inspect -f '{{.State.Pid}}' redroid 2>/dev/null)"
    [ -n "$_rpid" ] || {
        log "WARN: could not resolve redroid pid; ADB relay not started"
        return 1
    }
    log "starting ADB relay: guest :${ADB_TCP_PORT} -> nsenter(redroid netns, pid $_rpid) -> adbd"

    # Parameterless wrapper: one inner socat per connection, entering the
    # container netns and bridging stdio<->adbd. socat EXEC hands the accepted
    # socket to the child on fd 0/1, so STDIO there IS the host-side connection.
    # Target IPv4 loopback (dual-stack adbd accepts it) to avoid v6-scope quirks.
    # Write to /run (a tmpfs we mount unconditionally) rather than assuming a
    # /usr/local/bin exists in the rootfs — and mkdir BEFORE the heredoc.
    _inner=/run/adb-relay-inner.sh
    mkdir -p /run
    cat >"$_inner" <<EOF
#!/bin/sh
exec nsenter -t ${_rpid} -n socat STDIO "TCP4:127.0.0.1:${ADB_TCP_PORT}"
EOF
    chmod 0755 "$_inner"

    # reuseaddr: survive relay restarts. fork: one child per connection.
    # EXEC defaults to fork+pipe, handing the accepted socket to a dedicated
    # nsenter'd socat.
    socat "TCP4-LISTEN:${ADB_TCP_PORT},fork,reuseaddr" \
        "EXEC:$_inner" \
        >/var/log/adb-relay.log 2>&1 &
    sleep 2

    verify_adb_relay
}

verify_adb_relay() {
    # In-guest end-to-end self-probe of the full relay chain
    # (guest main-netns :PORT -> outer socat -> nsenter -> inner socat -> adbd).
    # adbd does NOT banner unprompted: the client must send a CNXN first, then
    # adbd replies CNXN. So a meaningful probe must SPEAK the handshake — the
    # optional static /usr/bin/adbprobe (an R&D tool, see vm-rnd-log.md §C) does
    # exactly that and a 'first4=CNXN' reply proves the byte path carries ADB
    # both ways. This is the in-guest mirror of the host-side `adb connect` proof.
    # Best-effort: if adbprobe is not staged, skip silently (the relay still runs).
    log "guest listeners: $(netstat -ltn 2>/dev/null | grep ":${ADB_TCP_PORT} " | head -1)"
    if [ -x /usr/bin/adbprobe ]; then
        _out="$(/usr/bin/adbprobe 4 127.0.0.1 "${ADB_TCP_PORT}" 2>&1)"
        case "$_out" in
        *"first4=CNXN"*)
            log "ADB_RELAY_OK: self-probe through relay got adbd CNXN ($_out)"
            ;;
        *)
            log "ADB_RELAY_WARN: self-probe through relay: $_out; see /var/log/adb-relay.log"
            tail -8 /var/log/adb-relay.log 2>/dev/null | sed 's/^/  relay: /'
            ;;
        esac
    fi
}

main() {
    resolve_redroid_image
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
