#!/usr/bin/env python3
"""
Cold-boot A/B harness for the micro-VM entropy levers (issue #83).

Boots the ``binder: vm`` guest under TCG repeatedly and times the cold path
(QEMU launch → ``sys.boot_completed=1``) for two argv variants:

* ``rng``      — the production argv from :func:`beetroot.vm.qemu.build_qemu_argv`
  (host-backed ``virtio-rng-pci`` + ``random.trust_cpu=on``).
* ``baseline`` — the *same* argv with the two entropy levers stripped out, so
  the only difference between the two arms is the RNG change under test.

For each boot it captures the guest serial console to a temp file and reads two
numbers: the host wall time from launch to the ``sys.boot_completed=1`` console
line, and the guest-reported ``boot_seconds=`` the init script prints on that
same line. It then prints per-variant min/median/max and the median speedup.

This is a one-off investigation tool (the nightly CI lane in
``scripts/bench.py`` is the maintained harness); it lives in ``scripts/`` so the
numbers in ``docs/design/vm-rnd-log.md`` §D can be reproduced. It deliberately
does *not* go through ``beetroot up`` — it drives QEMU directly so it can A/B
the argv without editing config.

Usage::

    uv run python scripts/bench_vm_rng.py \
        --kernel ~/.cache/beetroot/vm/bzImage \
        --rootfs ~/.cache/beetroot/vm/rootdisk.img \
        --runs 3
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Add the in-tree package to the path so the harness runs from a checkout
# without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from beetroot.vm import qemu  # path injected above before import

# Console marker the guest-init prints once Android reports boot_completed.
# Must be unique to the *success* line ("…boot_completed=1 — redroid is up
# (boot_seconds=N)") — matching the bare "boot_completed=1" would false-hit the
# earlier "waiting for sys.boot_completed=1" log line and report an instant boot.
_BOOT_MARKER = "redroid is up"
# Hard ceiling per boot — a wedged TCG guest must not hang the whole sweep.
_BOOT_TIMEOUT_S = 1200.0
# How often to poll the serial log for the boot marker.
_POLL_S = 1.0


@dataclass(frozen=True)
class BootSample:
    """One boot's timings: host wall + guest-reported boot_seconds."""

    host_wall_s: float
    guest_boot_s: int | None


def _baseline_argv(rng_argv: list[str]) -> list[str]:
    """
    Derive the pre-#83 baseline argv by stripping the entropy levers.

    Removes the ``-object rng-builtin.../-device virtio-rng-pci...`` pair and
    the ``random.trust_cpu=on`` cmdline token, leaving everything else
    identical so the A/B isolates exactly the RNG change.
    """
    out: list[str] = []
    i = 0
    while i < len(rng_argv):
        tok = rng_argv[i]
        if tok == "-object" and rng_argv[i + 1].startswith("rng-builtin"):
            i += 2
            continue
        if tok == "-device" and rng_argv[i + 1].startswith("virtio-rng"):
            i += 2
            continue
        if tok.startswith("console=") and "random.trust_cpu=on" in tok:
            out.append(tok.replace(" random.trust_cpu=on", ""))
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _boot_once(argv: list[str], *, label: str) -> BootSample:
    """
    Launch QEMU once with ``argv``, time the boot, then tear the VM down.

    The guest serial console is appended to a temp file; the boot is timed from
    just-before-``Popen`` to the first appearance of :data:`_BOOT_MARKER`.
    """
    fd, path = tempfile.mkstemp(prefix=f"bench-{label}-", suffix=".log")
    os.close(fd)
    console_log = Path(path)
    start = time.monotonic()
    with console_log.open("wb") as sink:
        proc = subprocess.Popen(  # noqa: S603  # argv from build_qemu_argv; trusted
            argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT
        )
        try:
            deadline = start + _BOOT_TIMEOUT_S
            while True:
                text = console_log.read_text(errors="replace")
                if _BOOT_MARKER in text:
                    wall = time.monotonic() - start
                    return BootSample(host_wall_s=wall, guest_boot_s=_parse_boot_seconds(text))
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited early (rc={proc.returncode}); see {console_log}"
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"boot did not complete in {_BOOT_TIMEOUT_S}s; see {console_log}"
                    )
                time.sleep(_POLL_S)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _parse_boot_seconds(console_text: str) -> int | None:
    """Pull the guest-init ``boot_seconds=N`` off the boot_completed line."""
    for line in console_text.splitlines():
        if _BOOT_MARKER in line and "boot_seconds=" in line:
            tail = line.split("boot_seconds=", 1)[1]
            digits = "".join(c for c in tail if c.isdigit())
            if digits:
                return int(digits)
    return None


def _summarize(name: str, samples: list[BootSample]) -> float:
    """Print min/median/max for a variant and return the median host wall."""
    walls = sorted(s.host_wall_s for s in samples)
    guests = [s.guest_boot_s for s in samples if s.guest_boot_s is not None]
    med = statistics.median(walls)
    print(f"\n[{name}]  host wall (s): min={walls[0]:.1f} median={med:.1f} max={walls[-1]:.1f}")
    if guests:
        print(f"          guest boot_seconds: {sorted(guests)}")
    return med


def main() -> int:
    """Parse args, run the interleaved A/B sweep, print the comparison."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kernel", required=True, type=Path)
    ap.add_argument("--rootfs", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=3, help="boots per variant")
    ap.add_argument("--smp", type=int, default=qemu.resolve_smp("auto"))
    ap.add_argument("--memory-mib", type=int, default=8192)
    ap.add_argument("--host-adb-port", type=int, default=5560)
    ap.add_argument(
        "--qemu-bin", default=shutil.which("qemu-system-x86_64") or "qemu-system-x86_64"
    )
    args = ap.parse_args()

    rng_argv = qemu.build_qemu_argv(
        qemu_bin=args.qemu_bin,
        accel="tcg",
        kernel=args.kernel,
        rootfs=args.rootfs,
        smp=args.smp,
        memory_mib=args.memory_mib,
        host_adb_port=args.host_adb_port,
    )
    base_argv = _baseline_argv(rng_argv)
    variants = {"baseline": base_argv, "rng": rng_argv}

    print(f"smp={args.smp} memory_mib={args.memory_mib} runs={args.runs} accel=tcg")
    print("baseline cmdline:", base_argv[base_argv.index("-append") + 1])
    print("rng      cmdline:", rng_argv[rng_argv.index("-append") + 1])

    # Interleave the arms (base, rng, base, rng, …) so a host-load drift over
    # the sweep hits both variants symmetrically rather than biasing one.
    results: dict[str, list[BootSample]] = {"baseline": [], "rng": []}
    for run in range(args.runs):
        for name in ("baseline", "rng"):
            print(f"\n=== run {run + 1}/{args.runs}  variant={name} ===", flush=True)
            sample = _boot_once(variants[name], label=name)
            print(f"  -> host_wall={sample.host_wall_s:.1f}s guest_boot={sample.guest_boot_s}")
            results[name].append(sample)

    base_med = _summarize("baseline", results["baseline"])
    rng_med = _summarize("rng", results["rng"])
    if rng_med > 0:
        print(
            f"\nmedian speedup (baseline/rng): {base_med / rng_med:.2f}x "
            f"({base_med - rng_med:+.1f}s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
