# API Reference

Auto-generated documentation for the `beetroot` Python package. Two
audiences are served here:

* **Researchers driving Beetroot from Python.** Start with the high-level
  OOP layer in `beetroot.api` — `Instance`, `Manager`, and the
  `DeviceBackend` Protocol are re-exported from the top-level package so
  `from beetroot import Instance` Just Works. The Protocol is the same
  one fleshed out in the [Device backends design doc](../design/device-backends.md).
* **Contributors editing the CLI.** The procedural modules (`compose`,
  `config`, `ports`, `registry`, `frida_download`, `modules_download`, `paths`,
  `snapshot`, `builder`) remain the load-bearing implementation —
  `api.py` composes them, doesn't replace them. The CLI's Typer verbs
  delegate to `Instance` / `Manager`; the procedural modules stay
  importable.

## `beetroot.api` — the OOP surface

The recommended entry point for programmatic users. Each `Instance`
binds a registry name to an on-disk root and a parsed
`InstanceConfig`. `Manager` exposes the cross-instance operations
(`list`, `get`, `resolve`, `list_orphans`). `DeviceBackend` is the
Protocol both `Instance` (Redroid-via-compose) and v0.4's `AdbDevice`
satisfy. `Manager.resolve(name)` dispatches via the backend registry
in `beetroot.backends`; verbs that don't generalise across backends
(`up`, `down`, `apply`, `snapshot`) raise `BackendCapabilityError`
when called on a backend that doesn't expose them.

Adding a new backend (e.g. a cloud-emulator service that talks via
its own shell instead of adb) takes about 30 LOC + one entry-point
line — see the [Adding a backend guide](../guides/adding-a-backend.md)
for the step-by-step recipe (and the [Device backends design doc](../design/device-backends.md)
for the rationale).

### Surfaces introduced in v0.4

The v0.3 OOP surface (`Instance`, `Manager`, `DeviceBackend`,
`InstanceNotFoundError`, `FridaNotInstalledError`,
`AdbNotInstalledError`) is preserved bit-for-bit. v0.4 adds:

* **`AdbDevice`** (in `beetroot.backends.adb`) — sibling to `Instance`
  for rooted-Android-device backends driven over the host `adb` CLI.
  Import as `from beetroot.backends.adb import AdbDevice`. Satisfies
  the expanded `DeviceBackend` Protocol; registers itself as
  `kind="adb"` in the backend registry at module import time.
* **Expanded `DeviceBackend` Protocol** — now has `name: str` (read-only
  property), `kind: str` (the backend discriminator, e.g.
  `"redroid"` / `"adb"`), `shell() -> int`, `frida_cli(args) -> int`,
  and a `from_meta(name, backend_config)` classmethod used by the
  backend-registry dispatcher. The Protocol stays `@runtime_checkable`
  so `isinstance(b, DeviceBackend)` still works.
* **`BackendCapabilityError(RuntimeError)`** — raised by lifecycle
  verbs (`up`, `down`, `restart`, `apply`, `destroy`, `snapshot`) when
  called on a backend that doesn't honour them (typically: any
  non-`Instance` backend). The CLI catches it and renders a friendly
  `error: ...` line + exit code `2` (distinct from "instance not
  found" → exit `1`).
* **`Manager.resolve(name) -> DeviceBackend`** — dispatches to the
  concrete backend class via the backend registry. The return type is
  the Protocol, so callers narrow with `isinstance(b, Instance)` for
  Redroid-specific operations. This is the polymorphic entry point
  most v0.4+ programmatic code wants.
* **`register_backend(kind, cls)`** (in `beetroot.backends`) — register
  an in-process third-party backend. Third-party packages typically
  prefer the `[project.entry-points."beetroot.backends"]` mechanism
  instead (loaded lazily on first `Manager.resolve` call), but the
  in-process registration is what tests use and what the synthetic
  third-backend test exercises.
* **`registry.BackendConfig`** — `Annotated[RedroidBackendConfig |
  AdbBackendConfig, Field(discriminator="kind")]`. The
  discriminated-union shape of the `backend` field on
  `registry.InstanceMeta`. In-tree concrete subclasses:
  `registry.RedroidBackendConfig(absolute_path, stealth_paths)` and
  `registry.AdbBackendConfig(serial)`. Third-party backends define
  their own `BackendConfig` subclass with a unique `kind: Literal[...]`
  discriminator — see the [Adding a backend guide](../guides/adding-a-backend.md)
  for the in-process / entry-point registration split and the v0.4 →
  v0.6 JSON-discriminator round-trip limitation.
* **`CheckResult`** — frozen pydantic model with `status: Literal["pass",
  "fail", "skip"]` and optional `reason: str | None`. Returned from
  `Instance.health()` / `AdbDevice.health()` keyed by check name.
* **`Instance.health() -> dict[str, CheckResult]`** — redroid-backed
  health surface that `beetroot doctor` consumes. NOT part of the
  `DeviceBackend` Protocol — callers narrow with `isinstance(b,
  Instance)` (or call `AdbDevice.health()` after narrowing the other
  way). See the design rationale at the top of the
  [Device backends design doc](../design/device-backends.md).
* **`AdbDevice.health() -> dict[str, CheckResult]`** — adb-backed
  equivalent. Returns the same check-name vocabulary minus
  `compose.status` (no container to inspect). Delegates to the free
  function `api.adb_device_health(device)`, which is preserved as a
  back-compat shim for pre-T7 programmatic callers.
* **`registry.set_stealth_paths(name, blob)`** — write a `dict[str,
  str]` into the named instance's `RedroidBackendConfig.stealth_paths`
  slot (T4 plumbing for v0.6's stealth-path PR1). Locked + atomic-
  replaced via the same `_write` pattern the rest of `registry.py`
  uses. Rejects unknown names and adb-kind rows.

::: beetroot.api

## `beetroot.cli`

::: beetroot.cli

## `beetroot.config`

::: beetroot.config

## `beetroot.ports`

::: beetroot.ports

## `beetroot.registry`

::: beetroot.registry

## `beetroot.compose`

::: beetroot.compose

## `beetroot.frida_download`

::: beetroot.frida_download

## `beetroot.modules_download`

::: beetroot.modules_download

## `beetroot.snapshot`

::: beetroot.snapshot

## `beetroot.backends`

::: beetroot.backends

## `beetroot.backends.adb` — the `AdbDevice` backend

T5's real-device backend. Drives a rooted Android device (real phone, third-party emulator, `adb connect`-ed network device) via the host `adb` CLI. Satisfies the `DeviceBackend` Protocol so every universal CLI verb (`shell`, `frida`, `module`, `status`) works uniformly against an adopted instance; lifecycle verbs (`up`, `down`, `restart`, `apply`, `destroy`, `snapshot`) raise `BackendCapabilityError` cleanly because there's no on-disk container to manage.

```python
from beetroot.backends.adb import AdbDevice
```

The class registers itself as `kind="adb"` at module import time so `Manager.resolve("phone")` returns an `AdbDevice` for any registry row with `backend.kind == "adb"`.

::: beetroot.backends.adb

## `beetroot.builder`

::: beetroot.builder

## `beetroot.paths`

::: beetroot.paths
