# Device Backends: Design Doc

!!! info "Status: design only"
    This is a v0.3 design document. v0.3 ships a single implicit backend
    (`Instance` itself, wrapping a Redroid container managed via
    `docker compose`). The `AdbDeviceBackend` described here is scheduled
    for v0.4 — no code in this repo currently implements it. See
    [§6 v0.4+ implementation roadmap](#6-v04-implementation-roadmap)
    for the ordered list of PRs that will execute against this spec.

This doc lands the rationale, Protocol surface, and concrete-backend
plan for the device-backend abstraction Beetroot will introduce in
v0.4. The goal is the same as
[the stealth posture design doc](stealth-posture.md): give the v0.4
implementer a written spec to build against, and prevent ongoing
themes from baking in new assumptions that v0.4 will have to undo
(every `docker compose` call inside a code path that wants to be
backend-agnostic is a future migration cost).

## 1. Why a backend abstraction

v0.3 ships **one** way to drive Magisk-on-Android: the Beetroot
container, brought up via `docker compose`. That covers the most
common research case — researchers who want a disposable rooted phone
for an afternoon of Frida hooking — but it leaves two real workflows
on the floor:

* **A researcher's existing rooted phone.** Plenty of mobile-security
  practitioners already have a Pixel with Magisk installed and a USB
  cable on the desk. Today they have to choose between
  "use my real device with bare adb" and "use Beetroot and lose all
  the device-specific state." A `DeviceBackend` Protocol lets the
  same `frida_cli`, `add_module`, `shell` verbs target either.
* **A remote device farm.** Same Protocol, different implementation:
  the backend hands `frida-server` over `adb push` after `adb
  -H <farm-host> connect`. The orchestration layer doesn't change.

The constraint is that v0.3's `Instance` class already exposes the
right verbs (`shell`, `frida_cli`, `install_frida`, `add_module`,
plus `adb_address` / `frida_address` properties). The abstraction
exists implicitly. v0.4's job is to extract the **subset** that
generalises across backends into a Protocol, keep the
Redroid-container-specific parts on `Instance`, and add a sibling
`AdbDeviceBackend` that satisfies the Protocol via `adb`.

## 2. The `DeviceBackend` Protocol

The Protocol is defined in `src/beetroot/api.py` and re-exported from
the top-level package. It uses `@runtime_checkable` so callers can do
`isinstance(x, DeviceBackend)` for ad-hoc structural checks (the v0.3
test suite already exercises this against `Instance`).

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class DeviceBackend(Protocol):
    """
    Abstraction for a Magisk-rooted Android device that Beetroot can drive.
    """

    @property
    def adb_address(self) -> str:
        """Return the host:port (or adb serial) that `adb connect` targets."""
        ...

    @property
    def frida_address(self) -> str:
        """Return the host:port Frida control endpoint."""
        ...

    @property
    def is_available(self) -> bool:
        """Return True iff the backend is reachable right now."""
        ...

    def install_frida(self, version: str) -> None:
        """Make a frida-server of the requested version available on the device."""
        ...
```

This is the **lowest-common-denominator** surface: enough to attach
Frida, identify the canonical addresses, and check whether the
backend is reachable. Capability methods that don't generalise
(compose `up`/`down`, Magisk-DB stealth writes, container overlay
manipulation) live on `Instance` and stay off the Protocol — see
[§4 Capability methods that aren't universal](#4-capability-methods-that-arent-universal).

## 3. Concrete backends

### 3.1 `RedroidBackend` — v0.3's `Instance`

In v0.3 the `Instance` class **is** the Redroid backend; it satisfies
the Protocol directly without a wrapper. The Protocol methods map to
existing implementation:

| Protocol member         | `Instance` implementation                                                  |
|-------------------------|----------------------------------------------------------------------------|
| `adb_address`           | `f"localhost:{self.ports['adb']}"`                                         |
| `frida_address`         | `f"localhost:{self.ports['frida']}"`                                       |
| `is_available`          | `self.status == "running"` (live `docker compose ps`)                      |
| `install_frida(version)`| `frida_download.stage_for_instance(self.root, version)` (bind-mount path)        |

All four Protocol members are supported on `RedroidBackend`. The
Magisk-DB writes that gate stealth (`stealth.rc` plus the `denylist`
push in `entrypoint.sh`) are container-side and run on every
`Instance.up`; the backend doesn't need to manage them explicitly.

For v0.4, the cleanest path is **to extract** an explicit
`RedroidBackend(instance: Instance)` adapter that forwards to the
underlying `Instance`. That gives `AdbDeviceBackend` a sibling class
to keep symmetry, even though the adapter is one-line forwards.
Whether the adapter lives in `api.py` or in a new
`backends/` sub-package is a v0.4 implementation choice — keep
`Instance` as the public construction site either way.

### 3.2 `AdbDeviceBackend(serial: str)` — v0.4

Wraps an arbitrary adb-connected device. Construction takes the adb
serial (the value `adb devices` prints — e.g. `emulator-5554` or
`R3CN20XYZAB`), nothing more. No compose involvement, no `instances/`
directory, no `beetroot.yaml` (the device is the source of truth for
its own state).

| Protocol member         | `AdbDeviceBackend` implementation                                          |
|-------------------------|----------------------------------------------------------------------------|
| `adb_address`           | `self._serial` (the constructor argument, verbatim)                        |
| `frida_address`         | `f"localhost:{self._frida_local_port}"` — set up via `adb forward tcp:`    |
| `is_available`          | `self._serial in <parse `adb devices` output>`                             |
| `install_frida(version)`| `adb push` cached binary to `/data/local/tmp/frida-server`; `adb shell chmod 755`; `adb shell su -c '/data/local/tmp/frida-server &'` |

The `frida_address` property is non-trivial: Frida binds inside the
device on a port that's not directly reachable from the host, so the
backend sets up an `adb forward tcp:<host_port> tcp:27042` and reports
`localhost:<host_port>`. The host port can be allocated via the same
stride-of-10 allocator that `Manager.allocate_port_index()` uses for
`Instance` — that keeps "I run two `AdbDeviceBackend`s on the same
host" from colliding.

The cached binary in `install_frida` is the same
`$XDG_CACHE_HOME/beetroot/frida/<filename>` blob that
`frida_download.download()` already produces; no duplicate cache.

## 4. Capability methods that aren't universal

These methods live on `Instance` (and on the future
`RedroidBackend` adapter) but **do not** appear on the
`DeviceBackend` Protocol. Backends that can implement them do so
locally; backends that can't raise `BackendCapabilityError` (a new
exception type introduced alongside `AdbDeviceBackend` in v0.4).

* **`apply_stealth_config()` — Magisk DB write.** Beetroot's Redroid
  container writes Magisk's `denylist` and `Zygisk` settings directly
  to `/data/adb/magisk.db` from `entrypoint.sh` at boot. On a
  researcher's existing phone, this is the researcher's call to make
  through the Magisk app; Beetroot is not in the business of mutating
  the user's installed Magisk state. `AdbDeviceBackend` raises
  `BackendCapabilityError` if asked.
* **`shell()` — interactive shell.** `RedroidBackend` invokes
  `adb connect localhost:<port>` then `adb -s localhost:<port> shell`
  (today's `Instance.shell` implementation). `AdbDeviceBackend` is
  simpler: skip the `adb connect` (USB serials don't need it) and run
  `adb -s <serial> shell` directly.
* **`add_module(source, sha256=None)` — install a Magisk module.**
  `RedroidBackend` adds the module to `beetroot.yaml` and stages it
  into the per-instance `modules/` bind mount; the next `up` flashes
  it via `magisk --install-module` from `entrypoint.sh`. The
  `AdbDeviceBackend` story has **two valid implementations**, both
  worth offering:
    * **Refuse** — `AdbDeviceBackend.add_module()` raises
      `BackendCapabilityError("install modules via the Magisk app")`.
      This is the safe default for the v0.4 PR.
    * **Route through `adb push`** — copy the zip to
      `/sdcard/Download/<name>.zip` and surface a one-line user
      instruction (`"now flash the module from the Magisk app
      Modules tab → Install from storage"`). A future enhancement
      could use Magisk's `/data/adb/modules/` install path directly,
      but that requires `su` and lives downstream of the safe v0.4
      shape.
* **`up()` / `down()` / `restart()` / `apply()` / `destroy()` —
  lifecycle.** `RedroidBackend` is the only thing that has a container
  to manage. The `AdbDeviceBackend` analogue would be "power-cycle
  the device" / "factory-reset the device" — that's a fundamentally
  different operation and shouldn't share method names. v0.4 leaves
  `AdbDeviceBackend` with no lifecycle methods; the device is
  always-on from Beetroot's perspective.

## 5. What's emulator-only

These features are listed in the
[stealth posture design doc](stealth-posture.md) but are worth
calling out here as **`RedroidBackend`-only** capabilities:

* **Per-build path randomization.** The stealth-posture work plans to
  randomize the in-container paths for `frida-server`, `flash_dir`,
  and `magisk` itself per Beetroot image build. This only works on
  images that **we** build — `RedroidBackend` benefits, but
  `AdbDeviceBackend` cannot influence the layout of the user's
  installed Magisk + system image.
* **Container overlay layer manipulation.** v0.4's stealth work may
  emit a tar layer that swaps in randomised file metadata
  (timestamps, attribute order) on top of the redroid base. There is
  no overlay layer on a USB-connected device — that knob doesn't
  apply.
* **Frida Gadget mode via Zygisk.** This one **works on both
  backends** — Frida Gadget is a Magisk/Zygisk module, not an
  emulator feature — but `AdbDeviceBackend` requires the user to
  install the Gadget Magisk module on the device by hand. The CLI
  can ship the module zip; the user accepts the install prompt in
  the Magisk app.

See the stealth posture doc's
[capability matrix](stealth-posture.md#7-v04-implementation-roadmap)
for the cross-reference.

## 6. v0.4+ implementation roadmap

Ordered list of concrete PRs. Each PR should land independently with
its own tests; later PRs depend on the surface earlier ones expose.

* **PR1: `AdbDeviceBackend` scaffolding.** Add the class in
  `src/beetroot/backends/adb.py` (introducing the new sub-package).
  Implement `__init__(serial)`, `adb_address`, `frida_address`
  (raises `NotImplementedError` for now — wired in PR2), and
  `is_available` (parses `adb devices`). The
  `BackendCapabilityError` exception type lands here too. Tests stub
  out `subprocess.run` so the suite stays offline.
* **PR2: `install_frida()` via `adb push`.** Reuses
  `frida_download.download()` to fetch the binary into the existing host
  cache, then `adb -s <serial> push <cached> /data/local/tmp/`,
  `adb shell chmod 755`, and `adb shell su -c
  '/data/local/tmp/frida-server &'`. Sets up the
  `adb forward tcp:<host> tcp:27042` and wires up `frida_address`.
* **PR3: `shell()` via `adb shell`.** Mirror `Instance.shell` but
  skip the `adb connect` step (USB serials don't need it). Returns
  the `adb shell` exit code so research scripts can chain.
* **PR4: `add_module()` via `adb push` + user instruction.** Lands
  the "safe" variant — copy to `/sdcard/Download/`, print a one-line
  instruction. The "route through Magisk's `/data/adb/modules/`
  directly" path is left as a follow-up; the PR description should
  link to a tracking issue.
* **PR5: CLI integration — `beetroot adopt <serial>`.** New verb
  registers an `AdbDeviceBackend` in the user-global registry so
  subsequent `beetroot shell <name>` / `beetroot frida <name>` /
  `beetroot module <name>` dispatch to it. The registry schema needs
  a `kind: "redroid" | "adb"` discriminator field — register that
  schema bump in `CHANGELOG.md` against `api_version: 3`. The CLI's
  resolver returns a `DeviceBackend` (the Protocol type) and every
  verb that doesn't need `Instance`-only methods narrows to that.

After PR5, the `Manager.list()` API will return a heterogeneous list
of `DeviceBackend` objects (both `RedroidBackend`-via-`Instance` and
`AdbDeviceBackend`). Callers that need lifecycle methods narrow with
`isinstance(b, Instance)`.

## 7. Out of scope

* **Rooting the device.** `AdbDeviceBackend` assumes Magisk is
  pre-installed by the user; Beetroot is not going to flash a boot
  image. The CLI should print a one-line link to the official
  [Magisk install guide](https://topjohnwu.github.io/Magisk/install.html)
  when `is_available` returns True but `adb shell su -c 'id'` fails.
* **MDM bypass.** Devices enrolled in a corporate MDM may refuse
  `adb` access entirely; that's a policy decision and not a v0.4
  concern.
* **Hardware-backed attestation.** Bypassing key attestation on real
  hardware (TEE-backed `keymaster` evidence that the bootloader is
  unlocked) is a fundamentally different threat model from
  Beetroot's. Cross-ref the
  [stealth-posture doc](stealth-posture.md) for what *is* in scope.

## See also

* [API Reference](../reference/api.md) — the actual `DeviceBackend`
  Protocol definition in `beetroot.api`.
* [Stealth posture design doc](stealth-posture.md) — the
  capability matrix for what stealth tricks work on which backend.
