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
line — see the [Device backends design doc](../design/device-backends.md)
for the full recipe.

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

T5's real-device backend. Drives a rooted Android device (real phone, third-party emulator, `adb connect`-ed network device) via the host `adb` CLI. Satisfies the `DeviceBackend` Protocol so every universal CLI verb (`shell`, `frida`, `module`, `env`) works uniformly against an adopted instance; lifecycle verbs (`up`, `down`, `restart`, `apply`, `destroy`, `snapshot`) raise `BackendCapabilityError` cleanly because there's no on-disk container to manage.

```python
from beetroot.backends.adb import AdbDevice
```

The class registers itself as `kind="adb"` at module import time so `Manager.resolve("phone")` returns an `AdbDevice` for any registry row with `backend.kind == "adb"`.

::: beetroot.backends.adb

## `beetroot.builder`

::: beetroot.builder

## `beetroot.paths`

::: beetroot.paths
