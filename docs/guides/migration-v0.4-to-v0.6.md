# Migrating from v0.4 to v0.6

Beetroot v0.6 is the stability-and-cleanup release on the road to v1.0:
it freezes the CLI and Python API after a round of intentional pre-1.0
breaking changes. It is the first hop since v0.4 that touches a
user-visible contract — **v0.5 shipped no schema change and no breaking
changes** (`api_version` stayed `3`, v0.4 instances and registries worked
unchanged), so upgrading from v0.5 is the same as upgrading from v0.4 and
this guide covers both.

If you'd rather scan the changes than walk through them, the headline
list lives at the top of
[`CHANGELOG.md`](https://github.com/Xiddoc/beetroot/blob/main/CHANGELOG.md#v060--2026-05-20)
under **Breaking changes**. This page expands each bullet into the exact
command or edit you run.

## 1. Schema bump: `api_version: 3` → `api_version: 4` (non-additive)

v0.6 raised `SUPPORTED_API_VERSION` to `4` (the current release is `5`; see
[`CHANGELOG.md`](https://github.com/Xiddoc/beetroot/blob/main/CHANGELOG.md)),
and unlike the additive `2` → `3` hop in v0.4, this one **renames a field**, so
it is not a silent auto-bump.
The top-level `stealth:` key is gone; its only populated field,
`stealth.denylist`, moved under `magisk.denylist`.

A `beetroot.yaml` that still contains a `stealth:` block is **rejected at
load** with a migration hint — it is not silently dropped. Edit it:

```yaml
# Before (api_version 3)
api_version: 3
stealth:
  denylist:
    - com.example.target

# After (api_version 4)
api_version: 4
magisk:
  denylist:
    - com.example.target
```

YAMLs that merely omit `api_version`, or pin `1` / `2` / `3` *without* a
`stealth:` block, auto-bump on load to the current `api_version` (now `5`;
the additive path), so a config that never used `stealth.denylist` needs no
edit. Persist the bump with `beetroot apply <name>`.

## 2. The `env` verb was removed

`beetroot env` (and `env --all`) is gone. Use the structured endpoints
that replaced it:

```bash
# Before
beetroot env alpha
beetroot env --all alpha

# After — machine-readable instance data as JSON
beetroot status alpha --json
```

For interactive use, `beetroot shell <name>` and `beetroot frida <name>`
cover what the human-readable `env` output was used for. See
[`status` in the CLI reference](../reference/cli.md#status).

## 3. Second Frida port renamed to `frida_control`

The second of Frida's two consecutive ports — its RPC/command channel —
is now spelled `frida_control` in `beetroot.yaml`, and the rendered
compose env var is `FRIDA_PORT_CONTROL`. The data port is unchanged.

```yaml
# After
frida:
  version: "16.4.10"
  port: 27042
  frida_control: 27043
```

Existing instances re-render the new env var on the next
`beetroot apply <name>`. If you parse the generated `.env` directly,
update your tooling to read `FRIDA_PORT_CONTROL`. The stride-of-10 port
layout itself is unchanged — see [Port allocation](../reference/ports.md).

## 4. `restore --as` → `--name`

`beetroot restore` now takes `--name` for the target instance name. The
old `--as` is kept as a **hidden alias for one release** and will be
removed, so update scripts now:

```bash
# Before
beetroot restore alpha.tar.zst --as alpha-fork

# After
beetroot restore alpha.tar.zst --name alpha-fork
```

## 5. `DeviceBackend` Protocol redesigned (programmatic users)

Third-party backends are now a first-class, stable contract. If you ship
a custom backend, the signatures changed:

- `frida_cli` takes a `Sequence` (was positional `*args`).
- `install_frida(version=None)` — version is now an explicit keyword.
- `shell(args=...)` — command passed as a keyword `Sequence`.
- `from_meta` takes a `BackendConfigBase`.
- Capabilities are **opt-in sub-protocols** —
  `Lifecycle` / `ModuleInstaller` / `HealthCheckable` / `Snapshottable`.
  A backend implements only what it supports rather than stubbing the
  whole surface.

See [Adding a backend](adding-a-backend.md) for the current ~30-LOC
recipe against the redesigned Protocol.

## 6. `Manager` and `registry` API changes (programmatic users)

```python
# Before
from beetroot import Manager, registry

instances = Manager.list()
registry.add(name, absolute_path=path, index=idx)

# After
from beetroot import Manager, registry

instances = Manager.list_instances()   # renamed
all_backends = Manager.all()           # new — resolved backends
backend = Manager.get(name)            # now returns a resolved backend
index = registry.add_allocating(name, backend=...)  # registry.add removed
```

`Instance.destroy(yes=False)` now **raises** instead of prompting — the
confirmation prompt moved to the CLI layer, so the OOP API never blocks on
stdin. Pass `yes=True` (or go through `beetroot destroy`, which prompts)
to tear an instance down programmatically.

## 7. Behaviour fixes you may notice

These aren't config edits, but they change observable behaviour:

- A sha256 mismatch on a downloaded frida-server or module zip no longer
  leaves a poisoned cache entry that re-fails forever; the bad file is
  discarded and re-fetched.
- Adopted adb devices' Frida ports are now counted in cross-backend
  collision detection, and `status` reports an adb device's real
  allocated Frida endpoint (was hardcoded `localhost:27042`).
- `up` / `down` / `restart --all` skip non-container backends instead of
  aborting the whole batch.
- `beetroot shell <name> -c '<cmd>'` now passes the command through.
- `ls --json` no longer prints advisories to stdout.

## 8. Verify with `beetroot ls` + `beetroot doctor`

```bash
beetroot ls
```

```
NAME    KIND     IDX  ADB             FRIDA            STATUS         PATH
alpha   redroid  0    localhost:5555  localhost:27042  running        /home/you/alpha
```

```bash
beetroot doctor alpha
```

Confirms `magisk.zygisk = 1`, the GMS denylist enrolment, and Frida
reachability survived the upgrade. If `doctor` flags the denylist, double
-check that you moved `stealth.denylist` to `magisk.denylist` (§1) and ran
`beetroot apply alpha`.

## Troubleshooting

**`ValidationError` / migration hint mentioning `stealth:` at load.**
§1 — your YAML still has a top-level `stealth:` block. Move
`stealth.denylist` under `magisk.denylist` and set `api_version: 5`.

**`No such command 'env'`.** §2 — the `env` verb was removed. Use
`beetroot status <name> --json` for machine-readable data.

**Scripts reading `FRIDA_PORT` from the generated `.env` see nothing for
the control port.** §3 — the control port env var is now
`FRIDA_PORT_CONTROL`. Re-run `beetroot apply <name>` to re-render `.env`.

**`AttributeError: module 'beetroot.registry' has no attribute 'add'` /
`Manager.list`.** §6 — `registry.add` → `registry.add_allocating`,
`Manager.list` → `Manager.list_instances`.
