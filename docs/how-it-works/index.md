# How It Works

Under-the-hood explanations of Beetroot's design decisions.

## In this section

- **[Architecture](architecture.md)** — one bundled compose template, project-per-instance isolation, Magisk stealth via direct DB writes.
- **[Boot Flow](boot-flow.md)** — how Android init, `stealth.rc`, and `entrypoint.sh` connect; why there's no Docker `ENTRYPOINT`.
- **[Boot Scripts](boot-scripts.md)** — per-helper contracts for the three POSIX shell scripts `entrypoint.sh` sources at boot.
- **[Filesystem Layout](filesystem.md)** — what lives where in an instance dir, in the user XDG dirs, and inside the wheel.
