# Adding a Backend

Beetroot's v0.4 `DeviceBackend` Protocol is designed for ~30-LOC
third-party backends. If you have a target environment that exposes
Android over its own shell — a cloud-emulator service, a custom
network-adb gateway, a corp-mandated remote-device farm — you can
ship a backend that plugs into every Protocol-driven `beetroot` verb
without forking the project.

This guide walks through a working `CloudBackend` example end-to-end.
The architectural rationale (why the Protocol surface is shaped the
way it is, why some verbs are off-Protocol on purpose, the
implementation roadmap) lives in the
[Device backends design doc](../design/device-backends.md); the API
reference lives at [`beetroot.api`](../reference/api.md). This page
is the recipe.

## 1. Why add a backend

The in-tree backends cover two scenarios:

* **`Instance`** — a Beetroot-managed Redroid container brought up
  via `docker compose`. The default. Fully self-hosted, fully owned
  by Beetroot.
* **`AdbDevice`** — any rooted Android device the host can reach via
  `adb`: a real phone on USB, an emulator started outside Beetroot,
  an `adb connect`-ed network device.

Real-world scenarios that need a third backend:

* **Cloud emulator services** (Genymotion Cloud, AWS Device Farm,
  BrowserStack, Sauce Labs) — each one ships its own remote-shell
  transport. The Protocol surface (`shell`, `frida_cli`, `install_frida`,
  `is_available`) maps cleanly onto whatever the service's CLI
  exposes.
* **Custom network-adb gateways** — an SSH tunnel to a lab adb
  daemon, a mTLS-gated proxy in front of a fleet of devices, a
  pre-shared-key handshake to an in-house orchestrator. The host
  `adb` CLI is often the wrong transport — a thin shim that wraps
  the gateway's own CLI plugs straight in.
* **Bare-metal phone farms** — a researcher with a rack of 20 rooted
  Pixels mounted in a USB hub plus a Raspberry Pi running a custom
  scheduler. `AdbDevice` adopts them one at a time; a `RackBackend`
  could batch-adopt the whole rack.

If your scenario fits the universal Protocol surface (shell + Frida +
reachability), a third backend is the right hammer. If you need
fundamentally different verbs (e.g. "trigger a factory reset"), you're
looking at a separate tool.

## 2. The `DeviceBackend` Protocol surface

Every backend must implement these eight members. The Protocol is
`@runtime_checkable`, so `isinstance(your_backend, DeviceBackend)` is
a meaningful test (and the synthetic third-backend test asserts it).

```python
from typing import Protocol, runtime_checkable

from beetroot import registry


@runtime_checkable
class DeviceBackend(Protocol):
    @property
    def name(self) -> str:
        """Return the registry name for this backend."""
        ...

    @property
    def kind(self) -> str:
        """Return the backend kind discriminator (e.g. "cloud-xyz")."""
        ...

    @property
    def adb_address(self) -> str:
        """Return the host:port (or backend-specific identifier) that
        adb / native shell targets."""
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
        """Make a frida-server of the requested version available."""
        ...

    def shell(self) -> int:
        """Open an interactive shell into the device; return exit code."""
        ...

    def frida_cli(self, args: list[str]) -> int:
        """Invoke the host frida CLI against this backend; return exit code."""
        ...

    @classmethod
    def from_meta(
        cls, name: str, backend: registry.BackendConfig,
    ) -> "DeviceBackend":
        """Construct a backend from a registry meta's backend config."""
        ...
```

Three details that often surprise first-time backend authors:

* **`kind` is typed `str`, not `Literal[...]`** — so third-party
  backends can declare their own discriminator without forking the
  Protocol.
* **`from_meta` is a classmethod ON the Protocol.** The backend
  registry's dispatcher (`Manager.resolve`) calls it. The argument is
  typed `registry.BackendConfig` (the in-tree discriminated union),
  but third-party backends typically narrow the parameter to their
  own pydantic model via `isinstance` + `TypeError` — see the
  `CloudBackend` example below.
* **Verbs that don't generalise (lifecycle: `up` / `down` /
  `apply` / `destroy` / `snapshot`) are NOT on the Protocol.** Third-
  party backends don't need to implement them. The CLI narrows via
  `isinstance(b, Instance)` for those verbs and raises
  `BackendCapabilityError` for everything else (exit code 2).

## 3. The pydantic `BackendConfig` model

Every backend owns its own pydantic config model with a unique
`kind: Literal[...]` discriminator and whatever connection params
the backend needs. The discriminator name is what shows up in the
registry's JSON:

```python
from pydantic import BaseModel, ConfigDict, SecretStr
from typing import Literal


class CloudBackendConfig(BaseModel):
    """Config for the synthetic cloud-emulator backend."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    kind: Literal["cloud-xyz"] = "cloud-xyz"
    endpoint: str           # https://<service>.example/api
    api_token: SecretStr    # held in memory only; never written to disk
```

Use `extra="forbid"` and a `Literal` discriminator — both are
load-bearing. `extra="forbid"` rejects typo'd fields at registry-load
time; the `Literal` makes the discriminator match the registry's
`backend.kind` key.

Pick a `kind` value that's namespaced under your package name to
avoid collisions with other backends (e.g. `"cloud-xyz"`,
`"farmco/v2"`, `"acme.bare-metal"`).

## 4. The class implementation

A complete third backend in ~30 LOC. This example talks to a fake
`cloud-cli` shell helper via `subprocess.run`; substitute whatever
your real transport is.

```python
import subprocess
from beetroot import frida_download


class CloudBackend:
    """Drive a cloud-emulator service via its own shell CLI."""

    def __init__(self, name: str, config: CloudBackendConfig) -> None:
        self._name = name
        self._config = config

    @classmethod
    def from_meta(cls, name: str, backend: object) -> "CloudBackend":
        # ``backend`` is typed ``object`` because the in-tree
        # discriminated union doesn't include third-party arms — the
        # third-party model validates against its own pydantic class,
        # so we duck-type-narrow here. Raise ``TypeError`` (not
        # ``ValueError``) so the registry dispatcher's error path is
        # distinguishable from a missing-name error.
        if not isinstance(backend, CloudBackendConfig):
            raise TypeError(
                f"CloudBackend expected CloudBackendConfig, got "
                f"{type(backend).__name__}",
            )
        return cls(name, backend)

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return "cloud-xyz"

    @property
    def adb_address(self) -> str:
        return self._config.endpoint

    @property
    def frida_address(self) -> str:
        # The cloud service forwards :27042 locally; substitute your
        # service's real Frida endpoint.
        return f"{self._config.endpoint}:27042"

    @property
    def is_available(self) -> bool:
        res = subprocess.run(
            ["cloud-cli", "status", "--endpoint", self._config.endpoint],
            check=False, capture_output=True, text=True,
        )
        return res.returncode == 0

    def install_frida(self, version: str) -> None:
        cached = frida_download.download(version)
        subprocess.run(
            ["cloud-cli", "push", str(cached), "--to", "/data/local/tmp/frida-server"],
            check=True,
        )
        subprocess.run(
            ["cloud-cli", "exec", "--",
             "chmod", "755", "/data/local/tmp/frida-server"],
            check=True,
        )

    def shell(self) -> int:
        return subprocess.run(
            ["cloud-cli", "shell", "--endpoint", self._config.endpoint],
            check=False,
        ).returncode

    def frida_cli(self, args: list[str]) -> int:
        return subprocess.run(
            ["frida", "-H", self.frida_address, *args],
            check=False,
        ).returncode
```

That's 30 lines of meaningful code (give or take whitespace and
docstrings you'll want to add). Three things to notice:

1. **The class doesn't inherit from anything.** The Protocol is
   structural; satisfying its surface is enough.
2. **No lifecycle methods.** If you call `beetroot up <cloud-name>`,
   `cli._resolve_redroid_for_backend` raises
   `BackendCapabilityError("up not supported by cloud-xyz backend")`
   and `cli.main` exits with code 2.
3. **No registry mutation.** The backend reads its own config and
   talks to the remote service; it never writes to the registry.
   Registry rows are created by `beetroot adopt`-equivalent verbs
   (in v0.4, that means a programmatic call to
   `registry.add_allocating(name, backend=CloudBackendConfig(...))`
   — see the "What works now vs deferred" callout below).

## 5. Entry-point registration

Third-party packages register their backend via the
`[project.entry-points."beetroot.backends"]` group in `pyproject.toml`:

```toml
[project.entry-points."beetroot.backends"]
cloud-xyz = "your_pkg.backend:CloudBackend"
```

The entry-point **name** is the `kind` discriminator (must match the
`kind: Literal[...]` on your `BackendConfig`); the **value** is the
dotted path to the concrete class. Beetroot loads entry-point
backends lazily on the first `Manager.resolve` call (or eagerly via
`backends.registered_kinds()`).

For tests + scratch code that don't have a packaged distribution
on hand, register in-process instead:

```python
from beetroot import backends

backends.register_backend("cloud-xyz", CloudBackend)
```

The two paths are equivalent at runtime — they both populate the
same `_BACKEND_REGISTRY` dict that `Manager.resolve` looks up.

## 6. What works in v0.4 vs deferred to v0.5

This split matters for anyone planning to ship a third-party backend
against v0.4.

**Works now (v0.4):**

* In-process `register_backend("cloud-xyz", CloudBackend)` — the
  class is registered, `Manager.resolve(name)` dispatches via the
  lookup table, and the CLI verbs that go through the Protocol
  (`shell`, `frida`, `env`, `status`, `doctor`) work uniformly via
  `Manager.resolve(name).shell()` etc.
* Entry-point registration via
  `[project.entry-points."beetroot.backends"]` — the lazy loader in
  `backends._load_entry_point_backends` discovers and registers
  every entry-point in the group on first lookup.
* The backend's own pydantic config round-trips through JSON via
  plain `model_dump_json` / `model_validate_json` (the third-party
  class owns the schema).

**Deferred to v0.5: third-party `kind` values round-tripping through
`RegistryFile` JSON validation.**

v0.4's `RegistryFile` validates the full registry document through
the in-tree `BackendConfig = Annotated[RedroidBackendConfig |
AdbBackendConfig, Field(discriminator="kind")]` union. A third-party
`kind` value in a saved registry row (`backend.kind: "cloud-xyz"`)
currently raises `ValidationError` on the next `_read`. **This means
in v0.4, a third-party backend can be used in-process but its
registry rows can't survive a Beetroot restart.**

Concrete workarounds for v0.4:

* **Re-register on each Beetroot invocation** from a startup-time
  side script that calls `register_backend(...)` and then
  `registry.add_allocating(...)` against a fresh registry.
* **Carry the registry shape externally** — your backend package can
  read its own per-instance state from its own config file and only
  use Beetroot's registry as a name-to-port-index allocator.

**v0.5 plan:** add a registry-side extension hook so third-party
`BackendConfig` subclasses can be registered for JSON discrimination.
The expected shape is something like:

```python
from beetroot import registry

registry.register_backend_config(CloudBackendConfig)
# After this, RegistryFile JSON validation accepts kind: "cloud-xyz"
# rows and validates them against CloudBackendConfig.
```

— but the exact API will be settled when v0.5 ships. Until then,
the v0.4 recipe is "in-process Protocol dispatch works, on-disk
round-trip needs to wait or be worked around".

## 7. Test your backend

The pattern is laid out in
[`tests/test_backend_extension.py`](https://github.com/Xiddoc/Beetroot/blob/main/tests/test_backend_extension.py) —
the synthetic third-backend test that grades the entire recipe at
every CI run. Extend the pattern in your own package:

```python
# In your_pkg/tests/test_cloud_backend.py
import subprocess
from collections.abc import Iterator

import pytest
from beetroot import api, backends

from your_pkg.backend import CloudBackend, CloudBackendConfig


@pytest.fixture
def _cloud_registered() -> Iterator[None]:
    backends.register_backend("cloud-xyz", CloudBackend)
    try:
        yield
    finally:
        backends._BACKEND_REGISTRY.pop("cloud-xyz", None)


class TestCloudBackendStandalone:
    def test_satisfies_protocol(self) -> None:
        b = CloudBackend(
            "lab-1",
            CloudBackendConfig(
                endpoint="https://cloud.example/api",
                api_token="secret",  # noqa: S106  # test fixture
            ),
        )
        assert isinstance(b, api.DeviceBackend)

    @pytest.mark.usefixtures("_cloud_registered")
    def test_resolve_returns_cloud_backend(self) -> None:
        cls = backends.get_backend("cloud-xyz")
        assert cls is CloudBackend

    @pytest.mark.usefixtures("_cloud_registered")
    def test_lifecycle_verb_raises_capability_error(self) -> None:
        from beetroot import cli

        b = CloudBackend(
            "lab-1",
            CloudBackendConfig(
                endpoint="https://cloud.example/api",
                api_token="secret",  # noqa: S106  # test fixture
            ),
        )
        with pytest.raises(api.BackendCapabilityError, match="up"):
            cli._resolve_redroid_for_backend(b, verb="up")
```

Three assertions are enough to grade the contract:

1. **Protocol satisfaction.** `isinstance(b, DeviceBackend)` confirms
   you implemented every required member.
2. **Registry round-trip.** `backends.get_backend("cloud-xyz") is
   CloudBackend` confirms registration took effect.
3. **Lifecycle verb rejection.** `BackendCapabilityError` confirms
   your backend won't accidentally be called for redroid-only verbs.

Add your own per-method tests for `shell` / `frida_cli` /
`install_frida` / `is_available` against a mocked subprocess (the
in-tree `tests/test_adb_device.py` is a good template for the
subprocess-mocking pattern).

## See also

* [Device backends design doc](../design/device-backends.md) — the
  rationale + the v0.3 Protocol-evolution roadmap.
* [API Reference](../reference/api.md) — the canonical
  `DeviceBackend` Protocol definition + the v0.4 backend-registry
  surface.
* [Migrating from v0.3 to v0.4](migration-v0.3-to-v0.4.md) — covers
  the schema bump, the new discriminator shape, and the v0.5
  known-limitations list.
