# Guides

Practical how-tos for common research workflows. These assume you've already completed [Getting Started](../getting-started/index.md) and have at least one instance running.

## In this section

- **[Multiple Instances](multi-instance.md)** — run several research phones in parallel, understand port allocation, coordinate across instances.
- **[Examples](examples.md)** — copy `default.yaml` / `stealth.yaml` / `no-gapps.yaml` / `with-frida.yaml` over a fresh instance, modify YAML, apply changes without rebuilding. Also covers the `adb-device.yaml` (adb-adopted device) and `vm.yaml` (QEMU micro-VM, `binder: vm`) reference configs.
- **[Magisk Modules](modules.md)** — flash Shamiko, LSPosed, or custom hooks via URL or local path; verify with sha256.
- **[Frida](frida.md)** — pin Frida versions per instance, use `beetroot frida` as a wrapper, attach scripts.
- **[Snapshots](snapshots.md)** — pack instance state into a `.tar.zst` archive with `beetroot snapshot`, restore with `beetroot restore`, fork an instance into siblings that run concurrently.
- **[Running in CI / without kernel access](running-in-ci.md)** — why redroid needs the host's binder driver, how to load it on GitHub-hosted runners, and how to drive a remote device over ADB when you have no kernel access at all.
- **[CI integration (reusable workflow)](ci-reusable-workflow.md)** — boot a Beetroot instance in *your* repo's CI with one `uses:` line and run your tests (e.g. a Frida script) against a live rooted Android.
- **[Migrating from v0.2 to v0.3](migration-v0.2-to-v0.3.md)** — end-to-end upgrade walkthrough: registry relocation, `api_version: 2`, `setup` → `build`, the removed preset flag, and the new opt-in Frida default.
- **[Migrating from v0.3 to v0.4](migration-v0.3-to-v0.4.md)** — the pydantic-typed registry, the `AdbDevice` backend, the `adopt` / `status` / `doctor` verbs, and the schema bump (note: the current release is `api_version: 4`, which also moved `stealth.denylist` to `magisk.denylist`).
