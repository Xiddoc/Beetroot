# Migration

Beetroot is pre-1.0, so each minor release is allowed to break the
`beetroot.yaml` schema, the CLI surface, or the Python API. This page is
the index of every **schema migration** — one walkthrough per version
hop that changed a user-visible contract. Start at the guide for the
release you're upgrading *from* and work forward.

## How the schema version works

Every `beetroot.yaml` carries a top-level `api_version: int`
(`SUPPORTED_API_VERSION` in `src/beetroot/config.py`, currently **8**).
Two kinds of bump can happen:

- **Additive bump** (e.g. `2` → `3`): new fields, no renames or removals.
  YAMLs that omit `api_version` — or pin an older additive version —
  **auto-bump on load** with a one-line stderr warning, and persist on the
  next `beetroot apply`. "Do nothing" keeps working.
- **Non-additive bump** (e.g. `3` → `4`): a field was renamed or removed.
  A YAML pinning the old shape is **rejected at load** with an actionable
  migration error naming the changed field. You must edit the YAML.

The cross-instance registry (`instances.json`) carries its own schema
version and follows the same auto-bump / backup-and-re-emit pattern; the
per-hop guides below call out when a registry migration is involved.

See the [Configuration reference](../reference/config.md) for the live
field-by-field schema, and [`CHANGELOG.md`](https://github.com/Xiddoc/beetroot/blob/main/CHANGELOG.md)
for the headline breaking-change list of each release.

## Schema migrations

- **[Migrating from v0.2 to v0.3](migration-v0.2-to-v0.3.md)** — end-to-end
  upgrade walkthrough: registry relocation, the `api_version: 1` → `2`
  bump, `setup` → `build`, the removed preset flag, and the new opt-in
  Frida default.
- **[Migrating from v0.3 to v0.4](migration-v0.3-to-v0.4.md)** — the
  pydantic-typed registry, the `AdbDevice` backend, the `adopt` / `status`
  / `doctor` verbs, and the additive `api_version: 2` → `3` bump.
- **[Migrating from v0.4 to v0.6](migration-v0.4-to-v0.6.md)** — v0.5
  changed no schema (it's a no-op upgrade), so the next breaking step is
  v0.6: the non-additive `api_version: 3` → `4` bump (`stealth.denylist`
  → `magisk.denylist`), the removed `env` verb, the `frida_control` port
  rename, and the redesigned `DeviceBackend` Protocol.

### Since v0.6: the `api_version: 6` → `7` → `8` hops

Two breaking schema bumps have landed since the v0.4 → v0.6 guide above.
Neither has a dedicated page yet — the walkthroughs live here, and the
field-by-field detail is in the [Configuration
reference](../reference/config.md).

- **`api_version: 6` → `7` — the GApps intent/vendor split.**
  `android.gapps` was an open-ended string that mixed *intent* (how much
  GApps you want) with *vendor* (which distribution provides it). It now
  carries only the **intent** vocabulary — `none`, `minimal`, or `full`
  — and an optional `android.gapps_vendor` (`litegapps` / `opengapps` /
  `mindthegapps`) pins the distribution. `gapps: none` and `gapps: full`
  keep working unchanged and auto-bump silently. A YAML that wrote a
  *vendor* value directly — `gapps: lite` or `gapps: mindthegapps` — is
  **rejected at load** with a migration error naming the field (the same
  shape as the v4 `stealth:` and v5 `gpu_mode` renames). Rewrite it as an
  intent plus, if you need that specific distribution, the
  `gapps_vendor` escape hatch — e.g. `gapps: minimal` +
  `gapps_vendor: mindthegapps` — then `beetroot apply <name>`.
- **`api_version: 7` → `8` — `ports` mapping → list (issue #108).** The
  `ports:` key was a fixed mapping of three well-known host overrides
  (`ports: {adb, frida, frida_control}`). It is now a **list** of named
  guest→host mappings (`ports: [{service, guest, host}]`) so a single
  instance can publish arbitrary services and explicit guest ports. The
  default seeds the three well-known services (adb 5555, frida 27042,
  frida_control 27043). An old mapping that names only `adb` / `frida` /
  `frida_control` is translated losslessly into the seeded list (a
  one-line note, then auto-bumps); a mapping carrying any *other* key is
  **rejected at load** with a migration error naming the new list shape.
  Rewrite each `host:` override as a list entry — e.g.
  `ports: [{service: adb, guest: 5555, host: 6000}]` — then
  `beetroot apply <name>`.
