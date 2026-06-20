# Design Notes

This section collects Beetroot's design documents — **some
forward-looking, some recording the design behind already-shipped
subsystems** (the device-backend abstraction and the binderless-host
micro-VM both ship today). Each doc lands a threat model, scope, and
ordered implementation roadmap so a future contributor can pick up the
work without re-deriving the design, and notes inline which parts have
landed.

A doc that describes a fully-shipped subsystem eventually graduates out
of this section — its content is rewritten as a regular reference page
and the design doc is archived from the nav.

## Current design docs

- **[Stealth posture](stealth-posture.md)** — threat model, current
  fingerprint inventory, mitigation playbook, and v0.4 PR roadmap for
  hiding Beetroot's container-specific indicators (Frida path, custom
  init.rc, `/flash_dir`) from GMS / Play Integrity / DroidGuard.
- **[Device backends](device-backends.md)** — rationale, Protocol
  surface, and v0.4 PR roadmap for the `DeviceBackend` abstraction
  that lets Beetroot drive an `adb`-connected real-world Magisk phone
  (`AdbDeviceBackend`) alongside the v0.3 Redroid container backend.
- **[Binderless hosts (QEMU/TCG)](binderless-hosts-qemu-tcg.md)** —
  validated proof-of-concept and proposed `vm` backend for running
  redroid on a host whose kernel lacks binder, by booting it inside a
  QEMU micro-VM that brings its own binder-enabled kernel. Includes the
  reproducible recipe, the full debugging log, and the auto-detect /
  fallback design (use host binder when present; opt into the slow
  emulated path explicitly).
- **[VM cold-boot performance (#83)](vm-cold-boot-perf.md)** — measured
  cold-boot report for the `binder: vm` TCG path: the proposed RNG levers
  (`random.trust_cpu=on`, `-device virtio-rng-pci`) are **neutral** (the
  CRNG already seeds in ~0.15 s), the QEMU `savevm`/`loadvm` warm-start is a
  **~20× win** (200 s → ~10 s, validated), and the Android version is itself a
  ~40 % cold-boot dial under TCG.
