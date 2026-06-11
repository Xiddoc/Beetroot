# Device Backends: Design Doc

!!! success "Status: v0.6 backend-contract landed"
    The `AdbDevice` backend shipped in v0.4 and the open-registry
    extension hook shipped in v0.6 — see
    [§6 v0.4+ implementation roadmap](#6-v04-implementation-roadmap)
    for the per-PR status. Researchers wiring up a new backend should
    skip to [the Adding-a-backend guide](../guides/adding-a-backend.md)
    for the step-by-step recipe; this doc remains the rationale +
    Protocol-surface reference.

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
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class DeviceBackend(Protocol):
    """
    Abstraction for a Magisk-rooted Android device that Beetroot can drive.
    """

    @property
    def name(self) -> str:
        """Return the registry name for this backend."""
        ...

    @property
    def kind(self) -> str:
        """Return the backend kind discriminator (e.g. ``"redroid"``)."""
        ...

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

    def install_frida(self, version: str | None = None) -> None:
        """Make a frida-server of the requested version available on the device."""
        ...

    def shell(self) -> int:
        """Open an interactive shell; return exit code."""
        ...

    def frida_cli(self, args: Sequence[str]) -> int:
        """Invoke the host frida CLI against this backend; return exit code."""
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
`localhost:<host_port>`. The host port comes from the same stride-of-10
allocator the redroid backend uses — adb-adopted rows are registered
via `registry.add_allocating(name, backend=AdbBackendConfig(...))`,
so `AdbDevice.from_meta` reads the row's allocated `index` and
computes `ports.ports_for_index(index)["frida"]` for the forward.
That keeps "I run two `AdbDevice`s on the same host" from colliding
(and means an adb-adopted instance with index `N` reaches Frida on
exactly the port a redroid instance with index `N` would have got).

!!! note "v0.4 removed `Manager.allocate_port_index()`"
    The original draft of this doc referenced a public
    `Manager.allocate_port_index()` helper. v0.4 retired it (Agent 2 F-4:
    the index isn't reserved by the call, so calling it without an
    immediate follow-up `registry.add` is a footgun). Use
    `registry.add_allocating(name, backend=...)` for the atomic
    allocate + register; the private `api._allocate_port_index()` exists
    for internal `Manager` callers and is not public surface.

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
      Modules tab → Install from storage"`). This shipped as the
      default; the `su`-driven enhancement shipped later as the
      opt-in `--auto-install` variant (see §6 PR4) — it routes
      through `su -c magisk --install-module`, which stages into
      `/data/adb/modules_update/<id>/`.
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

Ordered list of concrete PRs. Each PR landed independently in v0.4
with its own tests; the per-PR status below is the v0.4-shipped
shape.

* **PR1: `AdbDevice` scaffolding.** **DONE in v0.4 (T5).** Added the
  class in `src/beetroot/backends/adb.py` (introducing the new
  sub-package). The class name dropped the `Backend` suffix
  (`AdbDevice`, not `AdbDeviceBackend`) for symmetry with `Instance`
  — both are concrete classes that satisfy `DeviceBackend`. The
  `__init__(name, config, host_forward_port)` shape takes a typed
  `registry.AdbBackendConfig` instead of a bare serial, because the
  T1 discriminated-union shape carries the config already validated.
  `BackendCapabilityError` landed in T1's expanded `api.py`.
* **PR2: `install_frida()` via `adb push`.** **DONE in v0.4 (T5).**
  Reuses `frida_download.download(version)` for the cached binary,
  then runs the full `adb push` → `chmod 755` → `su -c '... &'` →
  `adb forward tcp:<host> tcp:27042` sequence. `frida_address` is
  `localhost:<host_forward_port>`.
* **PR3: `shell()` via `adb shell`.** **DONE in v0.4 (T5).** Mirrors
  `Instance.shell` but skips the `adb connect` step (USB serials
  don't need it). Returns the `adb shell` exit code verbatim so
  research scripts can chain.
* **PR4: `add_module()` via `adb push` + user instruction.** **DONE
  in v0.4 (T5); auto-install variant DONE post-v0.6 (issue #7).** The
  safe-default variant ships: zip is pushed to
  `/sdcard/Download/<basename>` and the user gets a one-line "install
  via the Magisk app → Modules tab" instruction on stderr. The
  auto-install variant landed as `AdbDevice.auto_install_modules`
  behind the new `AutoModuleInstaller` capability sub-protocol
  (`beetroot module <name> <zip>... --auto-install`): each zip is
  pushed to `/data/local/tmp/` and installed via
  `su -c magisk --install-module`, Magisk's supported non-interactive
  primitive, which stages it into `/data/adb/modules_update/<id>/`
  (raw unpacking into that directory would have required
  reimplementing Magisk's module-id extraction and validation).
  `sha256` digests are enforced fail-closed on this path, and the
  per-module success / failure reporting UX that gated the deferral
  ships with it — one `ok:` / `failed:` line per module, batch
  continues past failures, non-zero exit if any module failed.
* **PR5: CLI integration — `beetroot adopt <serial>`.** **DONE in v0.4
  (T5).** New verb registers an `AdbBackendConfig` row in the
  user-global registry so subsequent `beetroot shell <name>` /
  `beetroot frida <name>` / `beetroot module <name>` dispatch to it
  via `Manager.resolve`. The registry schema's `kind` discriminator
  landed in T1's pydantic foundation (schema bumped to v3). Lifecycle
  verbs that don't generalise narrow via `cli._resolve_redroid` and
  raise `BackendCapabilityError` for the adb backend — caught by
  `cli.main` and rendered as `error: ...` + exit code 2.

After v0.4's PR5, `Manager.all()` returns every resolvable backend
sorted by name (`Manager.list_instances()` — renamed from
`Manager.list()` in v0.6 — remains the redroid-only walker), and the
polymorphic resolver is `Manager.resolve(name)` which dispatches via the
backend registry to `DeviceBackend`-typed objects. `beetroot ls` walks
`Manager.all()`, so adopted adb devices are listed next to redroid
instances. Callers that need
lifecycle methods narrow with `isinstance(b, api.Lifecycle)` (or use
`cli._require(b, api.Lifecycle, "up")` which raises
`BackendCapabilityError` with a clear message).

**v0.6 additions:**

* **Open-union registry.** `BackendConfig` is now the open base class
  `BackendConfigBase` (not a closed `Annotated[... | ...]` union). Third-party
  backends call `registry.register_backend_config(CloudBackendConfig)` and
  the JSON dispatcher recognises their `kind` value from that point on.
* **Opaque row preservation.** Unknown `kind` values are wrapped in
  `UnresolvedBackendConfig` rather than raising `ValidationError`. The raw
  JSON is preserved verbatim so rows written by a plugin-bearing Beetroot
  survive reads by a plugin-free one.
* **Capability sub-protocols.** `Lifecycle`, `ModuleInstaller`,
  `HealthCheckable`, and `Snapshottable` replace the old
  `isinstance(b, Instance)` guard. `cli._require(b, cap, verb)` raises
  `BackendCapabilityError` for backends that don't satisfy the sub-protocol,
  giving a consistent `"<verb> not supported by <kind> backend"` message.
* **`Manager.reset_for_testing()`.** Test-time helper that clears both
  registries (backend class registry + backend config registry) for isolation.

For writing a third-party backend, the step-by-step recipe lives at
[Adding a backend](../guides/adding-a-backend.md).

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

* [Adding a backend](../guides/adding-a-backend.md) — the v0.4
  step-by-step recipe for shipping a third-party backend in ~30 LOC.
* [API Reference](../reference/api.md) — the actual `DeviceBackend`
  Protocol definition in `beetroot.api`.
* [Stealth posture design doc](stealth-posture.md) — the
  capability matrix for what stealth tricks work on which backend.
