#!/usr/bin/env python3
r"""
A/B cold-boot timer for the ``binder: vm`` micro-VM (issue #83).

Boots the Beetroot guest kernel + rootfs under QEMU repeatedly, toggling the
two entropy levers under investigation — ``-device virtio-rng-pci`` and the
``random.trust_cpu=on`` kernel-cmdline flag — and records the wall-clock cold
boot time for each run. Designed to *isolate the RNG delta*: every run starts
from a fresh qcow2 overlay over the same raw rootfs (so a prior boot's
``/var/lib/docker`` churn can't skew the next), and arms are interleaved by the
caller so monotonic host-contention drift cancels across the A/B (the same
methodology the vm-rnd-log Stage D cmdline A/B used).

This deliberately mirrors — but does not import — ``beetroot.vm.qemu``'s
``build_qemu_argv`` so it can construct a *baseline* argv with the levers
removed (the shipped builder always includes them now). Keep the non-lever
flags in sync with ``build_qemu_argv`` if that recipe changes.

Boot completion is detected from the guest's own serial marker
(``[guest-init] sys.boot_completed=1 ... boot_seconds=N``), so the recorded
host wall is launch -> the guest reporting Android up — a true cold boot, not a
post-boot adb-reconnect lag.

Usage::

    scripts/vm_boot_ab.py --kernel bzImage --rootfs rootdisk.img \
        --rng --trust-cpu --runs 3 --out samples.json

A standalone, dependency-free reproducibility harness referenced by the
issue-#83 report in ``docs/design/vm-rnd-log.md`` (Stage E).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_GUEST_ADB_PORT = 5555
# The guest-init marker that announces Android is up, carrying the
# guest-measured boot seconds (docker run -> sys.boot_completed=1).
_BOOT_MARKER = re.compile(r"sys\.boot_completed=1.*boot_seconds=(\d+)")
# A guest panic / fatal init error — fail the run fast instead of waiting out
# the whole timeout on a wedged boot.
_FATAL = re.compile(r"Kernel panic|\[guest-init\] FATAL")


def build_argv(  # noqa: PLR0913  # each knob is a distinct QEMU invocation parameter
    *,
    kernel: Path,
    rootfs: Path,
    smp: int,
    memory_mib: int,
    host_adb_port: int,
    rng: bool,
    trust_cpu: bool,
) -> list[str]:
    """
    Build the QEMU argv, toggling the two issue-#83 entropy levers.

    Mirrors ``beetroot.vm.qemu.build_qemu_argv`` (TCG arm: MTTCG + ``-cpu
    max``) but lets the caller drop the RNG device and the ``trust_cpu``
    cmdline flag to measure a baseline.

    Args:
        kernel: Host path to the guest ``bzImage``.
        rootfs: Host path to the (qcow2 overlay) root disk.
        smp: Guest vCPU count.
        memory_mib: Guest RAM in MiB.
        host_adb_port: Host loopback port the guest adbd is forwarded to.
        rng: Attach ``-device virtio-rng-pci`` when True.
        trust_cpu: Append ``random.trust_cpu=on`` to the cmdline when True.

    Returns:
        The full ``qemu-system-x86_64`` argv.
    """
    hostfwd = f"hostfwd=tcp:127.0.0.1:{host_adb_port}-:{_GUEST_ADB_PORT}"
    cmdline = "console=ttyS0 root=/dev/vda rw init=/init panic=1 mitigations=off"
    if trust_cpu:
        cmdline += " random.trust_cpu=on"
    argv = [
        "qemu-system-x86_64",
        "-M",
        "q35",
        "-accel",
        "tcg,thread=multi,tb-size=1024",
        "-cpu",
        "max",
        "-smp",
        str(smp),
        "-m",
        str(memory_mib),
        "-nographic",
        "-display",
        "none",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-drive",
        f"file={rootfs},format=qcow2,if=virtio",
        "-netdev",
        f"user,id=net0,{hostfwd}",
        "-device",
        "virtio-net-pci,netdev=net0",
    ]
    if rng:
        argv += ["-device", "virtio-rng-pci"]
    argv += ["-append", cmdline]
    return argv


def _fresh_overlay(rootfs: Path, scratch: Path) -> Path:
    """
    Create a fresh qcow2 overlay over ``rootfs`` so each run starts pristine.

    Args:
        rootfs: The raw backing image (never written to).
        scratch: Path for the per-run qcow2 overlay (overwritten if present).

    Returns:
        The overlay path.
    """
    scratch.unlink(missing_ok=True)
    cmd = ["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(rootfs.resolve()), "-F", "raw"]
    subprocess.run([*cmd, str(scratch)], check=True)  # noqa: S603  # fixed argv; qemu-img on PATH
    return scratch


def one_boot(  # noqa: PLR0913  # each knob is a distinct launch parameter
    *,
    kernel: Path,
    rootfs: Path,
    smp: int,
    memory_mib: int,
    host_adb_port: int,
    rng: bool,
    trust_cpu: bool,
    timeout: float,
    log_path: Path,
    scratch: Path,
) -> tuple[float, int]:
    """
    Cold-boot the guest once and time launch -> the guest boot_completed marker.

    Args:
        kernel: Guest ``bzImage``.
        rootfs: Raw backing rootfs (an overlay is taken per run).
        smp: vCPU count.
        memory_mib: Guest RAM in MiB.
        host_adb_port: Forwarded host adb port (kept unique per concurrent run).
        rng: Attach the virtio-rng device.
        trust_cpu: Append ``random.trust_cpu=on``.
        timeout: Seconds to wait for the marker before giving up.
        log_path: Where the QEMU serial console is written (and polled).
        scratch: Per-run qcow2 overlay path.

    Returns:
        ``(host_wall_seconds, guest_boot_seconds)``.

    Raises:
        TimeoutError: If the marker does not appear within ``timeout``.
        RuntimeError: If the guest panics or guest-init reports FATAL.
    """
    overlay = _fresh_overlay(rootfs, scratch)
    argv = build_argv(
        kernel=kernel,
        rootfs=overlay,
        smp=smp,
        memory_mib=memory_mib,
        host_adb_port=host_adb_port,
        rng=rng,
        trust_cpu=trust_cpu,
    )
    start = time.monotonic()
    with log_path.open("wb") as log:
        proc = subprocess.Popen(  # noqa: S603  # argv built locally from CLI-supplied paths
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return _await_marker(proc, log_path, start, timeout)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _await_marker(
    proc: subprocess.Popen[bytes], log_path: Path, start: float, timeout: float
) -> tuple[float, int]:
    """
    Poll the serial log until the boot marker, a fatal line, or the timeout.

    Args:
        proc: The running QEMU process.
        log_path: The serial log being written by ``proc``.
        start: ``time.monotonic()`` captured at launch.
        timeout: Seconds to wait.

    Returns:
        ``(host_wall_seconds, guest_boot_seconds)``.

    Raises:
        TimeoutError: If the marker never appears.
        RuntimeError: On a guest panic / FATAL init line.
    """
    deadline = start + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"QEMU exited early (rc={proc.returncode}); see {log_path}")
        text = log_path.read_text(errors="replace")
        m = _BOOT_MARKER.search(text)
        if m:
            return time.monotonic() - start, int(m.group(1))
        if _FATAL.search(text):
            raise RuntimeError(f"guest panic / FATAL during boot; see {log_path}")
        time.sleep(2)
    raise TimeoutError(f"no boot_completed marker within {timeout:.0f}s; see {log_path}")


def main(argv: list[str] | None = None) -> int:
    """
    Run the configured number of cold boots and append samples to a JSON file.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` if any boot failed.
    """
    p = argparse.ArgumentParser(description="A/B cold-boot timer for binder: vm (issue #83)")
    p.add_argument("--kernel", type=Path, required=True)
    p.add_argument("--rootfs", type=Path, required=True)
    p.add_argument("--rng", action="store_true", help="attach -device virtio-rng-pci")
    p.add_argument("--trust-cpu", action="store_true", help="append random.trust_cpu=on")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--smp", type=int, default=4)
    p.add_argument("--memory-mib", type=int, default=8192)
    p.add_argument("--host-adb-port", type=int, default=5555)
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--out", type=Path, default=Path("rng-samples.json"))
    p.add_argument("--workdir", type=Path, default=Path("vm-boot-ab-logs"))
    args = p.parse_args(argv)

    args.workdir.mkdir(parents=True, exist_ok=True)
    arm = f"rng={int(args.rng)},trust_cpu={int(args.trust_cpu)}"
    samples: list[dict[str, object]] = json.loads(args.out.read_text()) if args.out.exists() else []
    failed = False
    for i in range(1, args.runs + 1):
        log_path = args.workdir / f"boot-{arm}-{i}.log"
        scratch = args.workdir / f"overlay-{args.host_adb_port}.qcow2"
        print(f"[{arm}] run {i}/{args.runs} ...", flush=True)
        try:
            host_wall, guest_boot = one_boot(
                kernel=args.kernel,
                rootfs=args.rootfs,
                smp=args.smp,
                memory_mib=args.memory_mib,
                host_adb_port=args.host_adb_port,
                rng=args.rng,
                trust_cpu=args.trust_cpu,
                timeout=args.timeout,
                log_path=log_path,
                scratch=scratch,
            )
        except (TimeoutError, RuntimeError) as exc:
            print(f"[{arm}] run {i} FAILED: {exc}", file=sys.stderr)
            failed = True
            continue
        sample = {
            "arm": arm,
            "rng": args.rng,
            "trust_cpu": args.trust_cpu,
            "run": i,
            "host_wall_seconds": round(host_wall, 1),
            "guest_boot_seconds": guest_boot,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        samples.append(sample)
        args.out.write_text(json.dumps(samples, indent=2) + "\n")
        print(
            f"[{arm}] run {i}: host_wall={host_wall:.1f}s guest_boot={guest_boot}s -> {args.out}",
            flush=True,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
