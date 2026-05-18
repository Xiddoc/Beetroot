# API Reference

Auto-generated documentation for the `beetroot` Python package. Two
audiences are served here:

* **Researchers driving Beetroot from Python.** Start with the high-level
  OOP layer in `beetroot.api` — `Instance`, `Manager`, and the
  `DeviceBackend` Protocol are re-exported from the top-level package so
  `from beetroot import Instance` Just Works. The Protocol is the same
  one fleshed out in the [Device backends design doc](../design/device-backends.md).
* **Contributors editing the CLI.** The procedural modules (`compose`,
  `config`, `ports`, `registry`, `frida_dl`, `modules_dl`, `paths`,
  `snapshot`, `builder`) remain the load-bearing implementation —
  `api.py` composes them, doesn't replace them. The CLI's Typer verbs
  delegate to `Instance` / `Manager`; the procedural modules stay
  importable.

## `beetroot.api` — the OOP surface

The recommended entry point for programmatic users. Each `Instance`
binds a registry name to an on-disk root and a parsed
`InstanceConfig`. `Manager` exposes the cross-instance operations
(list, get, allocate). `DeviceBackend` is the Protocol both v0.3's
`Instance` (Redroid-via-compose) and v0.4's future `AdbDeviceBackend`
satisfy.

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

## `beetroot.frida_dl`

::: beetroot.frida_dl

## `beetroot.modules_dl`

::: beetroot.modules_dl

## `beetroot.snapshot`

::: beetroot.snapshot

## `beetroot.builder`

::: beetroot.builder

## `beetroot.paths`

::: beetroot.paths
