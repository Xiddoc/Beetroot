# Design Notes

This section collects forward-looking design documents that describe
**how Beetroot will work** rather than how it works today. Each doc
lands a threat model, scope, and ordered implementation roadmap so a
future contributor can pick up the work without re-deriving the
design.

A doc graduates out of this section once its roadmap has shipped — at
that point its content is rewritten as a regular reference page and
the design doc is archived from the nav.

## Current design docs

- **[Stealth posture](stealth-posture.md)** — threat model, current
  fingerprint inventory, mitigation playbook, and v0.4 PR roadmap for
  hiding Beetroot's container-specific indicators (Frida path, custom
  init.rc, `/flash_dir`) from GMS / Play Integrity / DroidGuard.

A planned [device backends](#) design doc (T9) will live here once it
lands, covering the `AdbDeviceBackend` abstraction that lets Beetroot
drive a real rooted phone via ADB instead of (or alongside) the
emulator container.
