"""
High-level OOP wrapper around Beetroot's procedural modules.

The procedural modules (:mod:`beetroot.compose`, :mod:`beetroot.config`,
:mod:`beetroot.frida_download`, :mod:`beetroot.modules_download`,
:mod:`beetroot.paths`, :mod:`beetroot.ports`, :mod:`beetroot.registry`,
:mod:`beetroot.snapshot`, :mod:`beetroot.builder`) remain the load-bearing
implementation. This
module composes them behind a small object-oriented surface so researchers
can drive Beetroot from Python with ``from beetroot import Instance``
without learning the cross-module function vocabulary.

Two end-user classes are exported alongside a Protocol that formalises
the device backend abstraction (see ``docs/design/device-backends.md``
for the v0.4 roadmap that fleshes the Protocol out into multiple
backends):

* :class:`Instance` — a single research phone, identified by its on-disk
  directory and its registry name. Owns the per-instance lifecycle
  (``up`` / ``down`` / ``apply`` / ``destroy``) plus operations
  (``shell``, ``frida_cli``, ``add_module``, ``snapshot``).
* :class:`Manager` — aggregate operations over the global registry
  (``list``, ``get``, ``resolve``).
* :class:`DeviceBackend` — the Protocol that v0.3's implicit
  Redroid-via-compose backend satisfies and that v0.4's
  ``AdbDeviceBackend`` will satisfy too.

The CLI verbs in :mod:`beetroot.cli` delegate to these classes: the
Typer command bodies are 1-5 lines that construct an ``Instance`` or
call a ``Manager`` static method. The verbs themselves stay as
module-level Typer commands (not bound methods) because Typer captures
the function reference at import time.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict

from . import compose, config, frida_download, hostcheck, modules_download, paths, ports, registry
from . import snapshot as _snapshot_mod

# Derived from ``config.SUPPORTED_API_VERSION`` rather than hardcoded so the
# pinned version can never drift behind the schema. A fresh create must load
# without triggering the auto-bump warning in ``config.load_yaml``.
_MINIMAL_BEETROOT_YAML = (
    f"api_version: {config.SUPPORTED_API_VERSION}\n"
    f"android:\n  version: {config.DEFAULT_ANDROID_VERSION}\n"
)

# ``adb devices`` lines are ``<serial>\t<state>`` (two whitespace-separated
# columns). Anything with fewer columns is a header line or blank —
# not a device row.
_MIN_ADB_DEVICES_COLUMNS: Final = 2

# Instance names are used as Docker compose project names (which
# enforce ``[a-z0-9_-]+``) AND as filesystem-segment defaults for the
# instance directory. Pre-validate at the OOP boundary so a typo like
# ``Foo`` or ``alpha bravo`` surfaces with a clear message before any
# side effect runs. (T2 v0.3.1 deferred.)
_INSTANCE_NAME_RE: Final = re.compile(r"^[a-z0-9_-]+$")


def _validate_instance_name(name: str) -> None:
    """
    Raise ``ValueError`` if ``name`` doesn't match the instance-name grammar.
    """
    if not _INSTANCE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"instance name {name!r} is invalid — must match "
            r"[a-z0-9_-]+ (Docker compose project-name grammar). "
            "Lowercase alphanumerics, underscores, and hyphens only."
        )


class InstanceNotFoundError(LookupError):
    """
    Raised when an instance name is not in the registry.
    """


class FridaNotInstalledError(RuntimeError):
    """
    Raised when ``Instance.frida_cli`` is called without the host ``frida`` CLI on PATH.
    """


class AdbNotInstalledError(RuntimeError):
    """
    Raised when ``Instance.shell`` is called without the host ``adb`` CLI on PATH.
    """


class CheckResult(BaseModel):
    """
    One row of a backend health report.

    Returned from :meth:`Instance.health` and :func:`adb_device_health`
    keyed by a check name (e.g. ``"compose.status"``, ``"magisk.zygisk"``).
    ``status`` is a closed three-valued literal so downstream tools
    (``beetroot doctor``, dashboards) can pattern-match without parsing
    free-form strings. ``reason`` is a one-line human-readable hint
    that's surfaced verbatim on the doctor output line for ``fail`` /
    ``skip`` rows (and elided for ``pass``).

    Attributes:
        status: Either ``"pass"``, ``"fail"``, or ``"skip"``.
        reason: Optional one-line explanation. Surfaced on the doctor
            output line for non-``pass`` rows (and ``pass`` rows that
            volunteer extra context).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["pass", "fail", "skip"]
    reason: str | None = None


class BackendCapabilityError(RuntimeError):
    """
    Raised when a :class:`DeviceBackend` can't honour a requested operation.

    The CLI gate :func:`beetroot.cli._require` raises this when a verb
    targets a backend that doesn't implement the required capability
    sub-protocol. The CLI catches it and renders a friendly
    ``error: ...`` line with ``exit 2``.
    """


@runtime_checkable
class DeviceBackend(Protocol):
    """
    Base abstraction for a Magisk-rooted Android device that Beetroot can drive.

    Every backend satisfies this Protocol: enough to identify the backend,
    attach Frida, look up the canonical addresses, check reachability, and
    dispatch the two universal user-facing operations (``shell`` and
    ``frida_cli``).

    Capability sub-protocols (:class:`Lifecycle`, :class:`ModuleInstaller`,
    :class:`HealthCheckable`, :class:`Snapshottable`) are **opt-in** — a
    backend gains a capability by implementing the methods.  The CLI gates on
    ``isinstance(backend, <CapabilityProtocol>)`` via :func:`beetroot.cli._require`
    rather than ``isinstance(b, Instance)`` so third-party backends are
    first-class citizens.

    The ``kind`` property is typed as :class:`str` (not ``Literal[...]``) so
    third-party backends can declare their own discriminator strings without
    forking this Protocol.

    ``from_meta`` returns :class:`~typing.Self` so mypy can prove structural
    conformance for each concrete subclass.
    """

    @property
    def name(self) -> str:
        """
        Return the registry name for this backend.
        """
        ...

    @property
    def kind(self) -> str:
        """
        Return the backend kind discriminator (``"redroid"``, ``"adb"``, …).

        Mirrors the ``kind`` field of the matching
        :class:`~beetroot.registry.BackendConfigBase` subclass.
        """
        ...

    @property
    def adb_address(self) -> str:
        """
        Return the ``host:port`` (or adb serial) that ``adb connect`` targets.
        """
        ...

    @property
    def frida_address(self) -> str:
        """
        Return the ``host:port`` Frida control endpoint.
        """
        ...

    @property
    def is_available(self) -> bool:
        """
        Return True iff the backend is reachable right now (no install/start required).
        """
        ...

    def install_frida(self, version: str | None = None) -> None:
        """
        Make a frida-server available on the device.

        Args:
            version: The frida release tag (e.g. ``16.4.10``).  ``None``
                means "use the backend's default version".  Backends that
                have no meaningful default (e.g. :class:`AdbDevice`) raise
                :class:`ValueError` when ``version`` is ``None``.
        """
        ...

    def shell(self, args: Sequence[str] | None = None) -> int:
        """
        Open a shell into the device; return the subprocess exit code.

        Args:
            args: Optional extra argv tokens forwarded to the underlying
                shell invocation. Pass ``["-c", "id"]`` to run a
                non-interactive command.  ``None`` (the default) opens
                an interactive shell.

        Returns:
            The exit code of the underlying shell subprocess.
        """
        ...

    def frida_cli(self, args: Sequence[str]) -> int:
        """
        Invoke the host ``frida`` CLI against this backend; return the exit code.
        """
        ...

    @classmethod
    def from_meta(
        cls,
        name: str,
        backend: registry.BackendConfigBase,
    ) -> Self:
        """
        Construct a backend instance from a registry meta's backend config.

        Used by :meth:`Manager.resolve` to dispatch via the backend
        registry.  Typing ``backend`` as :class:`~beetroot.registry.BackendConfigBase`
        (not the old in-tree union) lets a third-party ``from_meta``
        ``isinstance``-narrow to its own config under mypy strict.

        Args:
            name: Registry name for the backend.
            backend: The matching registry backend config row — the
                concrete subclass this backend kind owns.

        Returns:
            A constructed backend instance satisfying this Protocol.
        """
        ...


@runtime_checkable
class Lifecycle(Protocol):
    """
    Capability sub-protocol: backends that manage a container or process lifecycle.

    Backends implement this to gain ``up`` / ``down`` / ``restart`` /
    ``apply`` / ``destroy`` CLI verbs.  The :class:`Instance` (redroid)
    backend implements this; the :class:`AdbDevice` backend does not
    (adb-adopted devices are always-on and managed outside Beetroot).
    """

    def up(self) -> None:
        """
        Start the backend.
        """
        ...

    def down(self) -> None:
        """
        Stop the backend (data preserved).
        """
        ...

    def restart(self) -> None:
        """
        Stop then start the backend.
        """
        ...

    def apply(self) -> None:
        """
        Re-load config and re-stage derived files.
        """
        ...

    def destroy(self, *, yes: bool = False) -> None:
        """
        Permanently destroy this backend and its host-side state.

        Args:
            yes: Must be True to proceed. Passing False raises
                ValueError — callers must confirm before destroying.
                The CLI handles the prompt via typer.confirm then
                passes yes=True.
        """
        ...


@runtime_checkable
class LogReader(Protocol):
    """
    Capability sub-protocol: backends that can surface their own logs.

    The :class:`Instance` (redroid) backend tails ``docker compose logs``;
    the :class:`~beetroot.backends.vm.VmDeviceBackend` reads the persisted
    QEMU serial console. Both satisfy this Protocol, so the ``logs`` verb
    gates on it (via :func:`beetroot.cli._require`) rather than on the
    concrete :class:`Instance` type.
    """

    def logs(self, *, follow: bool = False) -> None:
        """
        Tail this backend's logs.

        Args:
            follow: If True, stream continuously instead of printing once.
        """
        ...


@runtime_checkable
class ModuleInstaller(Protocol):
    """
    Capability sub-protocol: backends that can install Magisk modules.

    Backends implement this to gain the ``beetroot module`` verb.
    Both :class:`Instance` (redroid) and :class:`AdbDevice` implement
    this capability; third-party backends can opt in too.
    """

    def add_module(self, source: str, *, sha256: str | None = None) -> None:
        """
        Install a Magisk module.

        Args:
            source: URL or path to the module zip.
            sha256: Optional expected hex digest for integrity checking.
        """
        ...


class ModuleInstallResult(BaseModel):
    """
    Per-module outcome of an auto-install run.

    Returned (one row per requested module, in request order) from
    :meth:`AutoModuleInstaller.auto_install_modules`. A failed module
    never aborts the rest of the batch — callers inspect ``ok`` per row
    and decide the aggregate exit status themselves (the CLI exits
    non-zero if any row failed).

    Attributes:
        source: The host-side source path exactly as the caller passed it.
        ok: True iff the module was pushed and installed successfully.
        detail: One-line human-readable outcome — the on-device install
            target for ``ok`` rows, the error message for failed rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    ok: bool
    detail: str


class DevicePreflightError(RuntimeError):
    """
    Raised when an auto-install device probe fails before (or mid-) batch.

    :meth:`beetroot.backends.adb.AdbDevice.auto_install_modules` probes
    the device before pushing anything (is the device reachable? does
    ``su`` work? is ``magisk`` on the root PATH?) and raises this with a
    single friendly diagnosis instead of emitting N identical failed
    rows. It is also raised mid-batch when an adb call fails and a
    serial-scoped ``adb devices`` re-probe confirms the device is no
    longer available — the remaining modules are skipped because they
    would all fail identically. (Connectivity is always determined by
    that re-probe, never by matching failure text, which can embed
    untrusted host paths or module-controlled stderr.)

    The CLI catches it, reports any per-module rows completed before the
    abort, and renders ``error: <message>`` + exit 1 (the standard
    domain-error shape; capability gating stays exit 2).

    Attributes:
        results: Per-module rows completed before the abort, in request
            order. Empty for pre-flight failures (nothing was attempted).
    """

    def __init__(
        self,
        message: str,
        results: Sequence[ModuleInstallResult] = (),
    ) -> None:
        """
        Bind the friendly diagnosis and any pre-abort per-module rows.

        Args:
            message: The user-facing diagnosis (rendered after
                ``error:`` by the CLI).
            results: Rows for modules processed before the abort.
        """
        super().__init__(message)
        self.results = list(results)


@runtime_checkable
class AutoModuleInstaller(Protocol):
    """
    Capability sub-protocol: backends that can auto-install Magisk modules.

    Opting in gives the backend the ``beetroot module --auto-install``
    path: modules are installed without manual Magisk-app interaction
    (on the adb backend, via ``su -c magisk --install-module``, which
    stages the zip into ``/data/adb/modules_update/<id>/``).
    :class:`beetroot.backends.adb.AdbDevice` implements this capability;
    :class:`Instance` deliberately does not (redroid instances flash
    staged modules at boot — there is nothing to auto-install at runtime).
    """

    def auto_install_modules(
        self,
        sources: Sequence[str],
        *,
        sha256s: Sequence[str | None] | None = None,
    ) -> list[ModuleInstallResult]:
        """
        Install Magisk modules via root, reporting per-module outcomes.

        Args:
            sources: Host paths to local ``.zip`` modules.
            sha256s: Optional per-source expected hex digests, parallel
                to ``sources``. A configured digest is enforced
                fail-closed — a mismatching zip is never pushed.

        Returns:
            One :class:`ModuleInstallResult` per source, in order.
        """
        ...


@runtime_checkable
class HealthCheckable(Protocol):
    """
    Capability sub-protocol: backends that expose health-check diagnostics.

    Backends implement this to gain the ``beetroot doctor`` verb.
    Both :class:`Instance` (redroid) and :class:`AdbDevice` implement
    this capability.
    """

    def health(self) -> dict[str, CheckResult]:
        """
        Run the aggregated health checks for this backend.

        Returns:
            Ordered dict of check name to :class:`CheckResult`.
        """
        ...


@runtime_checkable
class Snapshottable(Protocol):
    """
    Capability sub-protocol: backends that can be snapshotted to an archive.

    Backends implement this to gain the ``beetroot snapshot`` verb.
    Only :class:`Instance` (redroid) currently implements this; adb-backed
    devices have no host-side directory to pack.
    """

    def snapshot(self, dest: Path) -> Path:
        """
        Pack the backend's host-side state into a ``.tar.zst`` archive.

        Args:
            dest: Destination archive path.

        Returns:
            The final archive path (after extension fix-up).
        """
        ...


@runtime_checkable
class Resettable(Protocol):
    """
    Capability sub-protocol: backends that can drop their ``/data`` in place.

    Backends implement this to gain the ``beetroot reset`` verb — a
    destructive "fresh start" that wipes accumulated app/``/data`` state
    while keeping the instance's identity (registry row, port index) and
    its staged tooling (``frida-server`` / ``modules/``). Only
    :class:`Instance` (redroid) currently implements it; ``binder: vm``
    keeps ``/data`` inside the guest (pending the split-data-disk work,
    issue #125) and adb-backed devices have no host-side ``/data`` to wipe,
    so both surface a capability error.
    """

    def reset(self, *, yes: bool = False) -> None:
        """
        Drop the backend's ``/data`` while keeping the instance.

        Args:
            yes: Must be ``True`` to proceed (the destructive op is gated;
                the CLI prompts before passing ``yes=True``).
        """
        ...


class Instance:
    """
    A single Beetroot research phone, identified by its on-disk directory.

    Instances are addressed by name in the global registry; the registry
    is the authoritative source for "does this name exist" and "where on
    disk does it live". The container's runtime state is queried live
    from ``docker compose ps`` — never cached on the instance.

    Construct via the three classmethod constructors:

    * :meth:`create` — make a new instance directory and register it.
    * :meth:`load` — look up an existing registered instance by name.
    * :meth:`from_path` — walk up from a path containing
      ``beetroot.yaml`` and load the matching registry entry.

    The ``__init__`` constructor itself is a low-level "I have all the
    pieces, hand me the object" path and is rarely the right call site
    in research code.
    """

    def __init__(self, name: str, root: Path, cfg: config.InstanceConfig) -> None:
        """
        Bind a name + on-disk root + parsed config into an ``Instance``.

        Most callers use :meth:`create`, :meth:`load`, or
        :meth:`from_path` instead of constructing the object directly.

        Args:
            name: Registry name for this instance.
            root: Absolute path to the instance directory (the one
                containing ``beetroot.yaml``).
            cfg: Parsed instance configuration.
        """
        self._name = name
        self._root = root
        self._cfg = cfg

    # ---- constructors -----------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        path: Path | None = None,
        cfg: config.InstanceConfig | None = None,
        lifecycle: Literal["ephemeral", "durable"] | None = None,
    ) -> Instance:
        """
        Create a new instance directory and register it under ``name``.

        Allocates a fresh port index, writes a minimal ``beetroot.yaml``,
        renders the ``.env``, and stages Frida (or an empty placeholder)
        and modules. The instance is left in the "down" state — call
        :meth:`up` to start it.

        Args:
            name: Registry name to assign.
            path: Directory to create. Defaults to ``./<name>``.
            cfg: Override the default minimal config. Defaults to a
                fresh :class:`config.InstanceConfig`.
            lifecycle: Persistence intent to stamp into the generated minimal
                ``beetroot.yaml`` (``ephemeral`` or ``durable``). Only honoured
                on the default-config path (``cfg is None``); when ``None`` (the
                default) the key is omitted and the instance is ``durable`` by
                schema default. Pass it together with an explicit ``cfg`` and a
                ``ValueError`` is raised — set ``cfg.lifecycle`` instead.

        Returns:
            The newly created and registered :class:`Instance`.

        Raises:
            ValueError: If ``name`` is already in the registry, the resolved
                ports collide with another instance, or ``lifecycle`` is passed
                alongside an explicit ``cfg``.
            FileExistsError: If ``path`` already contains a
                ``beetroot.yaml`` (use :meth:`register` to adopt it).
        """
        _validate_instance_name(name)
        if cfg is not None and lifecycle is not None:
            raise ValueError(
                "pass lifecycle only with the default config (cfg=None); "
                "set cfg.lifecycle on an explicit config instead"
            )
        if registry.get(name) is not None:
            raise ValueError(f"instance {name!r} already exists in registry")
        target_root = (path if path is not None else Path(name)).resolve()
        yaml_path = paths.instance_yaml(target_root)
        if yaml_path.exists():
            raise FileExistsError(
                f"{yaml_path} already exists — use Instance.register(path) to adopt it"
            )

        effective_cfg = (
            cfg if cfg is not None else config.InstanceConfig(lifecycle=lifecycle or "durable")
        )

        # Track whether ``target_root`` existed before this call. If we
        # created it and the rest of the constructor blows up, we
        # ``rmtree`` it on the rollback path so a failed create doesn't
        # leave debris behind. Pre-existing dirs (e.g. ``--from-data``
        # paths the CLI populated before calling us) are NOT removed —
        # blowing those away would silently destroy user data.
        created_dir = not target_root.exists()
        target_root.mkdir(parents=True, exist_ok=True)
        # When the caller didn't pin a config, emit the minimal-readable
        # YAML the CLI relies on (api_version + android.version only) so
        # researchers can hand-edit a small file instead of the full
        # schema-defaulted dump. Explicit configs go through
        # config.write_yaml so the model's own serialisation is honoured.
        if cfg is None:
            # Stamp the lifecycle intent into the committed YAML only when the
            # caller opted in (greppable, explicit); the default omits it and
            # relies on the schema default (durable).
            lifecycle_line = f"lifecycle: {lifecycle}\n" if lifecycle is not None else ""
            yaml_path.write_text(_MINIMAL_BEETROOT_YAML + lifecycle_line)
        else:
            config.write_yaml(yaml_path, effective_cfg)
        # Atomic allocation + registration under one file lock. Two
        # parallel create() calls cannot grab the same stride slot. The
        # ``binder: vm`` opt-in registers the QEMU micro-VM backend kind so
        # ``Manager.resolve`` dispatches to ``VmDeviceBackend`` (issue #44);
        # every other binder mode is the redroid-over-compose backend.
        backend_cfg: registry.BackendConfigBase = (
            registry.VmBackendConfig(absolute_path=str(target_root))
            if effective_cfg.binder == "vm"
            else registry.RedroidBackendConfig(absolute_path=str(target_root))
        )
        index = registry.add_allocating(name, backend=backend_cfg)
        new_ports = ports.resolve_ports(index, effective_cfg.ports)
        inst = cls(name=name, root=target_root, cfg=effective_cfg)
        try:
            _check_port_collisions(name, new_ports)
            inst._stage_local()
        except BaseException:
            _rollback_partial_create(name, target_root, created_dir=created_dir)
            raise
        # Network-touching stage runs OUTSIDE the rollback try/except —
        # a Frida 404 or module HTTP error should not destroy the
        # already-registered local artefacts. The user re-runs
        # ``beetroot apply <name>`` once the network heals. (T2 Agent
        # 2 B-2.)
        _stage_network_soft(inst)
        return inst

    @classmethod
    def register(cls, path: Path, name: str | None = None) -> Instance:
        """
        Adopt an existing instance directory under the global registry.

        Args:
            path: Directory containing a ``beetroot.yaml``.
            name: Registry name. Defaults to the directory's basename.

        Returns:
            The newly registered :class:`Instance`.

        Raises:
            FileNotFoundError: If ``path`` has no ``beetroot.yaml``.
            ValueError: If the chosen name is already registered, or if
                the resolved ports collide with another instance.
        """
        target_root = path.resolve()
        yaml_path = paths.instance_yaml(target_root)
        if not yaml_path.is_file():
            raise FileNotFoundError(f"no beetroot.yaml at {yaml_path}")
        resolved_name = name if name is not None else target_root.name
        _validate_instance_name(resolved_name)
        if registry.get(resolved_name) is not None:
            raise ValueError(f"instance {resolved_name!r} already in registry")
        cfg = config.load_yaml(yaml_path)
        # Atomic allocation + registration under one file lock. ``binder: vm``
        # registers the QEMU micro-VM backend kind (issue #44).
        backend_cfg: registry.BackendConfigBase = (
            registry.VmBackendConfig(absolute_path=str(target_root))
            if cfg.binder == "vm"
            else registry.RedroidBackendConfig(absolute_path=str(target_root))
        )
        index = registry.add_allocating(resolved_name, backend=backend_cfg)
        new_ports = ports.resolve_ports(index, cfg.ports)
        inst = cls(name=resolved_name, root=target_root, cfg=cfg)
        try:
            _check_port_collisions(resolved_name, new_ports)
            # Stage .env + dirs + frida placeholder so a follow-up
            # `beetroot up <name>` works without an intermediate
            # `beetroot apply`. Network artefacts (real Frida binary
            # + module zips) stage outside this try/except — see
            # _stage_network_soft below.
            inst._stage_local()
        except BaseException:
            # ``register`` adopts an existing directory — never delete
            # it on rollback. The user's beetroot.yaml + data/ + any
            # other files they put there must survive a partial
            # registration failure.
            _rollback_partial_create(
                resolved_name,
                target_root,
                created_dir=False,
            )
            raise
        # Network step runs post-commit; soft failure leaves a usable
        # registered instance behind for the user to retry via
        # ``beetroot apply``. (T2 Agent 2 B-2.)
        _stage_network_soft(inst)
        return inst

    @classmethod
    def load(cls, name: str) -> Instance:
        """
        Look up a registered instance by name and load its config.

        Args:
            name: Registry name.

        Returns:
            The :class:`Instance` bound to the registry's recorded path
            and the on-disk ``beetroot.yaml``.

        Raises:
            InstanceNotFoundError: If ``name`` is not in the registry,
                or if it's registered under a non-redroid backend kind
                (which ``Instance`` does not represent — use the
                corresponding backend class from :mod:`beetroot.backends`).
        """
        meta = registry.get(name)
        if meta is None:
            raise InstanceNotFoundError(f"no instance named {name!r}; try Manager.list()")
        if not isinstance(meta.backend, registry.RedroidBackendConfig):
            raise InstanceNotFoundError(
                f"instance {name!r} is a {meta.backend.kind!r} backend; "
                "Instance only represents redroid backends"
            )
        root = Path(meta.backend.absolute_path)
        cfg = config.load_yaml(paths.instance_yaml(root))
        return cls(name=name, root=root, cfg=cfg)

    @classmethod
    def from_meta(
        cls,
        name: str,
        backend: registry.BackendConfigBase,
    ) -> Self:
        """
        Build an :class:`Instance` from a registry meta's backend config.

        Used by the backend-registry dispatcher in :mod:`beetroot.backends`
        so :meth:`Manager.resolve` can construct any backend class
        uniformly given ``(name, BackendConfigBase)``.  Typing ``backend``
        as :class:`~beetroot.registry.BackendConfigBase` (the shared base)
        lets mypy verify the call site without requiring the in-tree union.

        Args:
            name: Registry name.
            backend: The matching backend config.  Must be a
                :class:`~beetroot.registry.RedroidBackendConfig`.

        Returns:
            The hydrated :class:`Instance`.

        Raises:
            InstanceNotFoundError: If ``backend`` is not a
                :class:`~beetroot.registry.RedroidBackendConfig`.
        """
        if not isinstance(backend, registry.RedroidBackendConfig):
            raise InstanceNotFoundError(
                f"instance {name!r} has backend kind {backend.kind!r}; "
                "Instance only represents redroid backends"
            )
        root = Path(backend.absolute_path)
        try:
            cfg = config.load_yaml(paths.instance_yaml(root))
        except FileNotFoundError as exc:
            raise InstanceNotFoundError(
                f"instance {name!r} has no beetroot.yaml at {root}; "
                "it may be an orphan — run `beetroot destroy "
                f"{name}` to clean up"
            ) from exc
        return cls(name=name, root=root, cfg=cfg)

    @classmethod
    def from_path(cls, path: Path) -> Instance:
        """
        Walk up from ``path`` to the nearest ``beetroot.yaml`` and load it.

        The discovered directory must be in the global registry; this
        constructor refuses to make up a name on its own.

        Args:
            path: A path inside (or at the top of) an instance directory.

        Returns:
            The :class:`Instance` bound to the discovered directory.

        Raises:
            paths.InstanceRootNotFoundError: If no ``beetroot.yaml`` is
                found in ``path`` or any of its ancestors.
            InstanceNotFoundError: If the discovered directory isn't
                registered under any name.
        """
        root = paths.instance_root(path)
        resolved = root.resolve()
        for candidate_name, meta in registry.list_instances().items():
            if not isinstance(meta.backend, registry.RedroidBackendConfig):
                continue
            if Path(meta.backend.absolute_path).resolve() == resolved:
                cfg = config.load_yaml(paths.instance_yaml(root))
                return cls(name=candidate_name, root=root, cfg=cfg)
        raise InstanceNotFoundError(
            f"directory {resolved} is not registered; call Instance.register(path) first"
        )

    # ---- introspection ----------------------------------------------------

    @property
    def name(self) -> str:
        """
        Registry name for this instance.
        """
        return self._name

    @property
    def kind(self) -> str:
        """
        Backend discriminator — always ``"redroid"`` for :class:`Instance`.

        Defined here so :class:`Instance` satisfies the
        :class:`DeviceBackend` Protocol's ``kind`` property.
        """
        return "redroid"

    @property
    def root(self) -> Path:
        """
        Absolute path to the instance directory.
        """
        return self._root

    @property
    def config(self) -> config.InstanceConfig:
        """
        The parsed ``beetroot.yaml`` at the time this object was constructed.
        """
        return self._cfg

    @property
    def index(self) -> int:
        """
        The instance's allocated port index (stride-of-10 base).
        """
        return self._meta().index

    @property
    def ports(self) -> dict[str, int]:
        """
        Resolved host ports for this instance (``adb`` / ``frida`` / ``frida2``).
        """
        return ports.resolve_ports(self.index, self._cfg.ports)

    @property
    def adb_address(self) -> str:
        """
        ``localhost:<adb_port>`` — what ``adb connect`` should target.
        """
        return f"localhost:{self.ports['adb']}"

    @property
    def frida_address(self) -> str:
        """
        ``localhost:<frida_port>`` — what ``frida -H`` should target.
        """
        return f"localhost:{self.ports['frida']}"

    @property
    def status(self) -> compose.ComposeStatus:
        """
        Live one-word container status (see :data:`compose.ComposeStatus`).
        """
        return compose.ps_status(self._name, self._root)

    @property
    def is_available(self) -> bool:
        """
        True iff the underlying container is running right now.
        """
        return self.status == "running"

    # ---- lifecycle --------------------------------------------------------

    def up(self) -> None:
        """
        Start the instance with ``docker compose up -d``.
        """
        compose.up(self._name, self._root)

    def down(self) -> None:
        """
        Stop the instance with ``docker compose down`` (data preserved).
        """
        compose.down(self._name, self._root)

    def restart(self) -> None:
        """
        Stop then start the instance in sequence.
        """
        compose.down(self._name, self._root)
        compose.up(self._name, self._root)

    def apply(self) -> None:
        """
        Re-load ``beetroot.yaml`` and re-stage all derived files.

        Re-reads the on-disk config (so external edits are picked up),
        re-validates port-collision, then re-renders ``.env`` and
        re-stages Frida + modules. A subsequent :meth:`restart` is
        required for the container to pick up the new config.

        If the reloaded config now sets ``binder: vm`` (a hand-edit after
        ``create`` registered the redroid kind), the registry row is flipped
        to the QEMU micro-VM backend kind so the next resolution dispatches
        to ``VmDeviceBackend`` (issue #44).

        Raises:
            ValueError: If the re-resolved ports collide with another
                registered instance.
        """
        self._cfg = config.load_yaml(paths.instance_yaml(self._root))
        new_ports = ports.resolve_ports(self.index, self._cfg.ports)
        _check_port_collisions(self._name, new_ports)
        self._stage()
        registry.reconcile_backend_kind(self._name, self._cfg.binder)

    def destroy(self, *, yes: bool = False) -> None:
        """
        Stop the container and permanently delete the instance directory.

        ``compose.down`` errors propagate to the caller — programmatic
        users typically want to see them — but the host-side teardown
        (directory removal + registry deregistration) still runs. The
        CLI wraps this with a friendlier "continuing" message; library
        users can do the same by catching :class:`compose.ComposeError`
        around the call (the host-side state will have already been
        cleaned up by the time the exception fires).

        The library API does NOT prompt on stdin. Callers must pass
        ``yes=True`` to confirm the destructive operation. The CLI
        verb :func:`beetroot.cli.destroy` handles the interactive
        prompt via ``typer.confirm`` and then calls
        ``Instance.destroy(yes=True)``.

        Args:
            yes: Must be ``True`` to proceed. Passing ``False`` (the
                default) raises :class:`ValueError` — the caller is
                responsible for any confirmation prompt before invoking
                this method.

        Raises:
            ValueError: If called with ``yes=False`` (caller must
                confirm before destroying).
            compose.ComposeError: If ``docker compose down`` fails. The
                host-side state is removed regardless before the error
                surfaces.
        """
        if not yes:
            raise ValueError(
                "Instance.destroy() requires yes=True to proceed. "
                "Confirm the destructive operation in the calling code "
                "before invoking this method (the CLI does this via "
                "typer.confirm before calling destroy(yes=True))."
            )
        # Hold an exclusive lock on the instance for the entire
        # teardown — blocks any concurrent ``snapshot()`` from reading
        # the directory while we're rmtree'ing it. ``snapshot`` takes
        # a shared lock so two parallel snapshots are fine, but a
        # snapshot + destroy race would otherwise tear the archive.
        # The lock file lives inside the instance root, so this only
        # works while the root still exists; that's fine because the
        # rmtree happens INSIDE the lock context. (T2 Agent 2 B-12.)
        if self._root.exists():
            with instance_lock(self._root, exclusive=True):
                self._teardown_under_lock()
        else:
            # Root already gone — no need for the lock; just clear the
            # registry row so ``Manager.list_orphans`` stops surfacing
            # this name.
            self._teardown_under_lock()

    def _teardown_under_lock(self) -> None:
        """
        Run ``destroy``'s steps under the assumption the lock is held.
        """
        # Order matters: compose.down → registry.remove → shutil.rmtree.
        # The CLI verb already enforces this order; the OOP path used
        # to do rmtree BEFORE registry.remove, which left a window
        # where a ``^C`` between the two steps stranded a registry row
        # pointing at a now-gone directory (an "orphan" the user could
        # only fix by re-creating the dir then running destroy again,
        # because ``Instance.load`` trips on the missing yaml).
        # Removing the registry row first means a ^C between
        # ``registry.remove`` and the rmtree leaves a tidy registry +
        # stale directory the user can wipe by hand. (T2 Agent 2 B-4.)
        compose_error: compose.ComposeError | None = None
        try:
            compose.down(self._name, self._root, volumes=True)
        except compose.ComposeError as e:
            compose_error = e
        registry.remove(self._name)
        if self._root.exists():
            shutil.rmtree(self._root)
        if compose_error is not None:
            raise compose_error

    def reset(self, *, yes: bool = False) -> None:
        """
        Drop the instance's ``/data`` while keeping the instance and its tooling.

        Stops the container (``compose.down``, idempotent), then wipes and
        recreates the bind-mounted ``data/`` directory. redroid regenerates a
        clean ``/data`` deterministically from the base image on the next
        :meth:`up`. ``frida-server`` and ``modules/`` are staged **outside**
        ``/data`` (see the bundled compose template), so the staged tooling
        survives — this is the explicit, gated counterpart to the silent
        ``boot_cache`` ``/data`` revert (issue #123). Unlike :meth:`destroy`
        the registry row and port index are untouched, so the instance keeps
        its identity.

        The container is left stopped; run :meth:`up` for a fresh ``/data``.
        The library API does NOT prompt on stdin — callers pass ``yes=True``
        (the CLI verb :func:`beetroot.cli.reset` prompts first).

        Args:
            yes: Must be ``True`` to proceed. ``False`` (the default) raises
                :class:`ValueError`.

        Raises:
            ValueError: If called with ``yes=False``.
            compose.ComposeError: If stopping the container fails (the
                ``data/`` directory is left untouched in that case).
        """
        if not yes:
            raise ValueError(
                "Instance.reset() requires yes=True to proceed. Confirm the "
                "destructive operation in the calling code before invoking this "
                "method (the CLI does this via typer.confirm before calling "
                "reset(yes=True))."
            )
        with instance_lock(self._root, exclusive=True):
            # Stop first: wiping the live bind-mounted ``data/`` out from under
            # a running container would corrupt its view of ``/data``. down is
            # idempotent, so this is a no-op when already stopped.
            compose.down(self._name, self._root)
            data = paths.instance_data(self._root)
            if data.exists():
                shutil.rmtree(data)
            data.mkdir(parents=True, exist_ok=True)

    # ---- operations -------------------------------------------------------

    def shell(self, args: Sequence[str] | None = None) -> int:
        """
        Open an ADB shell into the instance.

        Args:
            args: Optional extra tokens appended after ``adb -s <target>
                shell``. Pass ``["-c", "id"]`` to run a non-interactive
                command.  ``None`` (the default) opens an interactive shell.

        Returns:
            The exit code of the ``adb shell`` invocation. Beetroot
            does not raise on non-zero exits — research scripts may
            care about ``adb`` exit codes for their own flow control.

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
        """
        if shutil.which("adb") is None:
            raise AdbNotInstalledError("adb not found on PATH (install android-tools)")
        target = self.adb_address
        subprocess.run(["adb", "connect", target], check=False)  # noqa: S603, S607  # adb is a research CLI we deliberately resolve via PATH
        cmd = ["adb", "-s", target, "shell", *(args or [])]
        res = subprocess.run(cmd, check=False)  # noqa: S603  # same as above
        return int(res.returncode)

    def install_frida(self, version: str | None = None) -> None:
        """
        Stage a frida-server binary of the requested version on the instance.

        Implements the :class:`DeviceBackend` Protocol's
        ``install_frida``. The binary is downloaded into the user-global
        Frida cache (idempotent) and copied into the instance's bind
        mount. A subsequent :meth:`restart` is required for the
        container's ``entrypoint.sh`` to launch the new binary.

        Args:
            version: The frida release tag (e.g. ``16.4.10``).  ``None``
                uses the version pinned in this instance's
                ``beetroot.yaml`` (``cfg.frida.version``).  Raises
                :class:`ValueError` if ``version`` is ``None`` and no
                frida block is configured.

        Raises:
            ValueError: If ``version`` is ``None`` and the instance has
                no ``frida:`` block in its config.
        """
        if version is None:
            if self._cfg.frida is None:
                raise ValueError(
                    f"instance {self._name!r} has no frida: block in its config; "
                    "pass a version explicitly (e.g. install_frida('16.4.10'))"
                )
            version = self._cfg.frida.version
        frida_download.stage_for_instance(self._root, version)

    def frida_cli(self, args: Sequence[str]) -> int:
        """
        Invoke the host ``frida`` CLI against this instance.

        Beetroot prepends ``-H localhost:<frida_port>`` and forwards
        the rest of ``args`` verbatim.

        Args:
            args: Tokens to pass after ``frida -H <addr>`` (e.g.
                ``["-n", "com.app"]``).

        Returns:
            The exit code of the ``frida`` invocation.

        Raises:
            FridaNotInstalledError: If the ``frida`` binary is not on
                PATH (install via the ``[frida]`` extra).
        """
        if shutil.which("frida") is None:
            raise FridaNotInstalledError(
                "frida CLI not found. "
                "Install via `uv tool install 'beetroot[frida]'` "
                "or `uv tool install frida-tools`."
            )
        cmd = ["frida", "-H", self.frida_address, *args]
        res = subprocess.run(cmd, check=False)  # noqa: S603  # frida is a host CLI resolved via PATH; argv validated upstream
        return int(res.returncode)

    def logs(self, *, follow: bool = False) -> None:
        """
        Tail the container logs for this instance.

        Args:
            follow: If True, stream logs continuously (``-f``).
        """
        compose.logs(self._name, self._root, follow=follow)

    def add_module(self, source: str, *, sha256: str | None = None) -> None:
        """
        Append a Magisk module to ``beetroot.yaml`` and re-stage.

        Stages the module zip FIRST (so a download failure or sha256
        mismatch is caught before the YAML grows a half-broken entry),
        then on success mutates the in-memory config + writes the YAML.
        If staging raises, the YAML and in-memory model are left
        unchanged — the user can re-run the verb with a corrected URL
        without manually un-doing a half-applied add.

        Args:
            source: Either an ``http(s)://`` URL or an instance-relative
                path to a ``.zip`` module.
            sha256: Optional expected hex digest for integrity checking.

        Notes:
            The container will not pick up the new module until the next
            :meth:`restart`.
        """
        if source.startswith(("http://", "https://")):
            new_module = config.Module(url=source, sha256=sha256)
        else:
            new_module = config.Module(path=source, sha256=sha256)
        # Stage against a transient config that holds the existing
        # modules PLUS the new entry. ``stage_for_instance`` wipes the
        # ``modules/`` dir and re-stages every entry, so we can't pass
        # just the new module — the existing ones would vanish.
        # Building a one-off InstanceConfig keeps us from mutating
        # ``self._cfg`` until we know the stage succeeded. (T2 Agent 2
        # B-6, Agent 3 1.6.)
        transient = self._cfg.model_copy(update={"modules": [*self._cfg.modules, new_module]})
        modules_download.stage_for_instance(self._root, transient)
        self._cfg = transient
        config.write_yaml(paths.instance_yaml(self._root), self._cfg)

    def snapshot(self, dest: Path) -> Path:
        """
        Pack this instance's host-side state into a ``.tar.zst`` archive.

        Args:
            dest: Destination archive path. ``.tar.zst`` is appended if
                the caller omits it.

        Returns:
            The final archive path (after extension fix-up).

        Raises:
            snapshot.SnapshotError: On packing failure (missing source,
                bad disk write).
        """
        return _snapshot_mod.snapshot(self._root, dest)

    # ---- internals --------------------------------------------------------

    def _meta(self) -> registry.InstanceMeta:
        meta = registry.get(self._name)
        if meta is None:
            raise InstanceNotFoundError(f"instance {self._name!r} disappeared from the registry")
        return meta

    def _stage(self) -> None:
        """
        Stage everything: local artefacts first, then network artefacts.

        Convenience wrapper used by :meth:`apply` (where a single
        try/except over the whole batch is what the user wants — apply
        is interactive, and a network failure should surface).
        :meth:`create` / :meth:`register` / :func:`snapshot.restore`
        call the two halves explicitly so the network half runs
        AFTER the registry commits — a Frida 404 there leaves a usable
        instance behind that the user can retry with ``beetroot apply``.
        """
        self._stage_local()
        self._stage_network()

    def _stage_local(self) -> None:
        """
        Render .env + create empty dirs + place the Frida placeholder.

        Rollback-safe by design: every action is local-only (no
        network), so if any step raises the constructor's
        ``_rollback_partial_create`` can ``rmtree`` the dir without
        risking a half-consumed downloaded artefact. (T2 Agent 2 B-2.)

        T4: the ``.env`` render forwards the per-instance
        ``RedroidBackendConfig.stealth_paths`` blob into ``render_env``
        so a v0.6 snapshot restored against a v0.4 host (where
        :func:`snapshot.restore` already wrote the manifest's
        ``path_layout`` into the registry slot) emits the right
        ``BEETROOT_*`` overrides on the very first ``apply``.
        """
        meta = self._meta()
        backend = meta.backend
        stealth_paths = (
            backend.stealth_paths if isinstance(backend, registry.RedroidBackendConfig) else None
        )
        new_ports = ports.resolve_ports(meta.index, self._cfg.ports)
        paths.instance_data(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_modules(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_env(self._root).write_text(
            config.render_env(
                self._name,
                self._cfg,
                new_ports,
                stealth_paths=stealth_paths,
            )
        )
        # Always place the placeholder, even when frida is configured.
        # ``_stage_network`` overwrites it with the real binary on
        # success; on a network failure the placeholder survives so
        # the bind-mount target exists and the compose ``up`` doesn't
        # fail at mount-resolution time.
        frida_download.stage_empty(self._root)

    def _stage_network(self) -> None:
        """
        Download + stage Frida and modules. May hit the network.

        Runs AFTER the registry write commits — a soft failure here
        (Frida 404, module HTTP error) leaves a usable instance
        behind that the user can retry with ``beetroot apply``
        instead of losing the directory + registry slot to a hard
        rollback. (T2 Agent 2 B-2.)
        """
        if self._cfg.frida is not None:
            frida_download.stage_for_instance(
                self._root,
                self._cfg.frida.version,
                expected_sha256=self._cfg.frida.sha256,
            )
        modules_download.stage_for_instance(self._root, self._cfg)

    # ---- health-check ----------------------------------------------------

    def health(self) -> dict[str, CheckResult]:
        """
        Aggregate the redroid-backed health checks for this instance.

        Returns a mapping ``check_name → CheckResult``. The check names
        that overlap with the adb backend (``frida.handshake``,
        ``magisk.zygisk``, ``magisk.denylist.<pkg>``) use IDENTICAL
        keys to :func:`adb_device_health` so downstream tools can grep
        check rows uniformly across backend kinds.

        Notes:
            ``health()`` is NOT part of the :class:`DeviceBackend`
            Protocol. It's a capability method that not every backend
            supports (third-party cloud backends may not have any
            equivalent), so per the v0.3 device-backend design doc it
            lives on the concrete class rather than the Protocol.
            Callers narrow via ``isinstance(b, Instance)`` (or the
            free-function :func:`adb_device_health` for ADB).

        Returns:
            Ordered dict of check name → :class:`CheckResult`. Insertion
            order matches the doctor verb's intended output order
            (compose first, then connectivity, then Magisk).
        """
        adb_port = self.ports["adb"]
        frida_port = self.ports["frida"]
        checks: dict[str, CheckResult] = {}
        # compose.status: pass iff the container is running. The compose
        # Literal vocabulary is closed (see compose.ComposeStatus); any
        # non-running state surfaces as fail with the literal name as
        # the reason so a user grepping for ``compose.status: fail
        # exited`` can correlate against ``beetroot logs``.
        live_status = self.status
        checks["compose.status"] = (
            CheckResult(status="pass")
            if live_status == "running"
            else CheckResult(status="fail", reason=str(live_status))
        )
        # Host-level: redroid needs the kernel binder driver. A failing
        # row here explains an otherwise-baffling "container running but
        # adb never connects" — the container started but Android never
        # booted (see :mod:`beetroot.hostcheck`).
        checks["host.binder"] = _check_host_binder(self._cfg.binder)
        checks["adb.connect"] = _check_adb_connect(f"localhost:{adb_port}")
        checks["frida.handshake"] = _check_frida_socket(
            "localhost",
            frida_port,
            enabled=self._cfg.frida is not None,
        )
        checks["magisk.zygisk"] = _check_magisk_zygisk_over_adb(f"localhost:{adb_port}")
        denylist = self._cfg.magisk.denylist
        gms_pkg = "com.google.android.gms"
        checks[f"magisk.denylist.{gms_pkg}"] = _check_magisk_denylist_over_adb(
            f"localhost:{adb_port}",
            gms_pkg,
            enrolled=gms_pkg in denylist,
        )
        return checks


_INSTANCE_LOCK_FILENAME = ".beetroot.lock"


@contextlib.contextmanager
def instance_lock(instance_root: Path, *, exclusive: bool) -> Iterator[Path]:
    """
    Acquire an advisory ``fcntl.flock`` on ``<instance_root>/.beetroot.lock``.

    Snapshot acquires a SHARED lock (``LOCK_SH``) — multiple snapshots
    can run in parallel, but a concurrent destroy must wait. Destroy
    acquires an EXCLUSIVE lock (``LOCK_EX``) — blocks every other
    snapshot/destroy on the same instance until the destructive
    operation completes. Without the lock, a destroy that races a
    long-running snapshot could rmtree the directory while the
    snapshot is reading from it, producing a torn archive. (T2 Agent
    2 B-12.)

    The lock file lives inside the instance root so it's naturally
    scoped per-instance — two instances don't share a lock. The lock
    file is created on first acquisition and never deleted; future
    operations re-attach to the same inode. Sibling processes that
    crash holding the lock release it automatically (the kernel drops
    the flock on fd close at process exit).

    Args:
        instance_root: The instance directory.
        exclusive: True for ``LOCK_EX``; False for ``LOCK_SH``.

    Yields:
        The lock-file path (for debugging — callers don't usually
        need it).
    """
    instance_root.mkdir(parents=True, exist_ok=True)
    lock_path = instance_root / _INSTANCE_LOCK_FILENAME
    with lock_path.open("a+") as f:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(f.fileno(), flag)
        try:
            yield lock_path
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _stage_network_soft(inst: Instance) -> None:
    """
    Run the network-touching stage step, swallowing failures with a hint.

    Frida 404s and module HTTP errors after a successful local-stage
    leave the instance registered + the .env rendered + the frida
    placeholder in place — usable in every respect except that the
    Frida binary / module zips aren't there yet. Surfacing the
    failure as a soft "[beetroot] ..." line + a hint to re-run
    ``beetroot apply`` once the network heals is friendlier than
    blowing the instance away on a transient outage. (T2 Agent 2 B-2.)
    """
    try:
        inst._stage_network()  # noqa: SLF001  # module-internal hook; snapshot.restore + Instance.create call the same private surface
    except Exception as e:  # noqa: BLE001  # soft-fail: Frida 404, urllib timeout, sha256 mismatch, etc. all converge here; the *whole point* is to swallow them and surface a hint
        print(  # noqa: T201  # user-visible CLI feedback (the soft-fail hint), not debug output; goes to stderr like every other beetroot[*] line
            f"[beetroot] note: network-stage step failed for "
            f"{inst.name!r} ({e!s}). The instance is registered and "
            f"its .env is rendered; rerun `beetroot apply {inst.name}` "
            f"once the network recovers.",
            file=sys.stderr,
        )


def _rollback_partial_create(name: str, target_root: Path, *, created_dir: bool) -> None:
    """
    Roll back the partial side-effects of a failed instance constructor.

    Removes the registry entry unconditionally (``registry.remove`` is
    a no-op if the row is already gone). If ``created_dir`` is True and
    ``target_root`` still exists, also ``rmtree``s it — otherwise leaves
    the directory alone (the caller adopted it via ``register`` and the
    user's pre-existing files must survive).

    Args:
        name: Registry name to remove.
        target_root: Absolute path to the (possibly-created) instance
            directory.
        created_dir: True iff Beetroot created ``target_root`` in this
            call (and so owns its deletion on the rollback path).
    """
    registry.remove(name)
    if created_dir and target_root.exists():
        shutil.rmtree(target_root)


def _check_port_collisions(name: str, new_ports: dict[str, int]) -> None:
    """
    Raise ``ValueError`` if ``new_ports`` collide with any other instance.

    The CLI wraps this in its own friendly-error formatter; the OOP
    surface raises a plain ``ValueError`` so programmatic callers can
    catch a stdlib exception.
    """
    others = {n: p for n, p in registry.all_resolved_ports().items() if n != name}
    collision = registry.find_port_collision(new_ports, others)
    if collision is None:
        return
    port, other_name, kind = collision
    raise ValueError(
        f"port {port} ({kind}) collides with instance {other_name!r} "
        f"(which also uses {port}). Pin or remove one."
    )


class Manager:
    """
    Aggregate operations over the global instance registry.

    Stateless — every method reads the registry live, so concurrent
    mutations from other processes are picked up on the next call.
    """

    @staticmethod
    def list_instances() -> list[Instance]:
        """
        Return every registered *redroid* instance, sorted by name.

        Adb-kind rows are skipped because :class:`Instance` only
        represents redroid backends — use :meth:`all` to walk every
        backend uniformly via the Protocol. Orphan entries (redroid
        rows whose on-disk directory has been ``rm -rf``'d, or whose
        ``beetroot.yaml`` is now unparsable) are silently skipped —
        without this, a single orphan would crash ``beetroot ls`` and
        prevent the user from cleaning up. Use :meth:`list_orphans`
        to surface them for cleanup.

        Returns:
            A list of :class:`Instance` objects, one per healthy
            registered redroid name.
        """
        out: list[Instance] = []
        for name, meta in sorted(registry.list_instances().items()):
            if not isinstance(meta.backend, registry.RedroidBackendConfig):
                continue
            # Filter orphans by the yaml-exists pre-check rather than
            # ``except FileNotFoundError`` around ``Instance.load``.
            # The bare except used to swallow ANY FileNotFoundError —
            # including permission errors on a parent directory and
            # the unrelated cache-miss FileNotFoundError that arose
            # during T2's pivot to ``platformdirs``. (T2 Agent 2 F-12 /
            # Agent 3 1.7.)
            yaml_path = paths.instance_yaml(Path(meta.backend.absolute_path))
            if not yaml_path.is_file():
                continue
            try:
                out.append(Instance.load(name))
            except Exception:  # noqa: BLE001, S112  # super-set of list_orphans: yaml.YAMLError + pydantic ValidationError + api-version mismatch all converge here; continue is the orphan-skip contract documented in list_orphans
                # YAML present but unparsable — mirrors the orphan
                # contract from ``list_orphans``. Without this, a
                # single corrupted YAML crashes ``beetroot ls`` and
                # the user has no way to surface the row for cleanup.
                # (T2 v0.3.1 deferred.)
                continue
        return out

    @staticmethod
    def all() -> list[DeviceBackend]:
        """
        Return every resolvable registered backend, sorted by name.

        Walks all registered names via :meth:`resolve`, skipping names
        that fail resolution (opaque/unresolvable rows, orphaned redroid
        rows whose yaml is gone, etc.).  Useful for operations that
        span all backend kinds.

        Returns:
            A list of :class:`DeviceBackend` instances, one per
            successfully resolved registry entry, sorted by name.
        """
        out: list[DeviceBackend] = []
        for name in sorted(registry.list_instances()):
            try:
                out.append(Manager.resolve(name))
            except Exception:  # noqa: BLE001, S112  # resolution can fail for orphans, unknown kinds, etc. (InstanceNotFoundError, OSError, etc.) — skip silently
                continue
        return out

    @staticmethod
    def list_orphans() -> list[str]:
        """
        Return names of redroid instances whose on-disk dir is missing OR unparsable.

        An orphan is a redroid-kind registry row pointing at a path
        with no ``beetroot.yaml`` (typically because the user manually
        ``rm -rf``'d the directory without running
        ``beetroot destroy``) OR a ``beetroot.yaml`` that can't be
        parsed any more (e.g. a half-overwritten file, an
        api_version mismatch, or hand-edited junk). v0.3 returned only
        the first kind, so a corrupted YAML left the entry invisible
        to ``Manager.list_instances`` AND to ``Manager.list_orphans`` —
        the user had no surface to clean it up from. (T2 v0.3.1 deferred.)

        Adb-kind rows are not directory-backed so they can never be
        orphans by this definition. Names are returned sorted; the
        cleanup verb is ``beetroot destroy <name> -y``.

        Returns:
            Sorted list of orphan instance names. Empty if every
            registered redroid entry's directory is present and its
            YAML parses.
        """
        orphans: list[str] = []
        for name, meta in registry.list_instances().items():
            if not isinstance(meta.backend, registry.RedroidBackendConfig):
                continue
            yaml_path = paths.instance_yaml(Path(meta.backend.absolute_path))
            if not yaml_path.is_file():
                orphans.append(name)
                continue
            try:
                config.load_yaml(yaml_path)
            except Exception:  # noqa: BLE001  # parse / validation / api_version mismatch all count as orphans; the broad catch is the contract
                # Any parse / validation failure on the YAML counts as
                # an orphan — the row needs cleanup-attention, and
                # ``Manager.list_instances`` already skips it via the
                # InstanceRootNotFoundError filter (which load() emits
                # transitively when the YAML is unreachable). Catch
                # broadly: pydantic ValidationError, yaml.YAMLError,
                # custom api_version mismatches, and any future
                # validation backend all flow through here.
                orphans.append(name)
        return sorted(orphans)

    @staticmethod
    def get(name: str) -> DeviceBackend | None:
        """
        Look up a registered backend by name.

        Returns ``None`` if missing or unresolvable.

        Unlike :meth:`Instance.load`, this method returns any backend kind
        (redroid, adb, or third-party), not just redroid.  Returns ``None``
        if ``name`` is not registered or if the backend cannot be resolved
        (e.g. the package providing an unknown kind is not installed).

        Args:
            name: Registry name.

        Returns:
            A :class:`DeviceBackend`, or ``None`` if ``name`` isn't
            registered or can't be resolved.
        """
        try:
            return Manager.resolve(name)
        except InstanceNotFoundError:
            return None

    @staticmethod
    def resolve(name: str) -> DeviceBackend:
        """
        Look up a registered instance and return its concrete backend.

        Dispatches via the backend registry (see
        :mod:`beetroot.backends`): the ``meta.backend.kind``
        discriminator is mapped to the registered class, which is then
        constructed with ``(name, meta.backend)``.

        Args:
            name: Registry name.

        Returns:
            A backend instance satisfying :class:`DeviceBackend`.

        Raises:
            InstanceNotFoundError: If ``name`` is not in the registry,
                if its ``kind`` is not in the backend registry (install
                the package providing it), or if the backend row is
                opaque (unknown kind).
        """
        meta = registry.get(name)
        if meta is None:
            raise InstanceNotFoundError(f"no instance named {name!r}; try `beetroot ls`")
        backend = meta.backend
        if isinstance(backend, registry.UnresolvedBackendConfig):
            raise InstanceNotFoundError(
                f"instance {name!r} has backend kind {backend.kind!r} which is "
                "not installed; install the package providing that kind and retry."
            )
        from . import backends  # noqa: PLC0415

        try:
            cls = backends.get_backend(backend.kind)
        except KeyError as e:
            raise InstanceNotFoundError(
                f"no backend registered for kind {backend.kind!r}; "
                "install the package providing it (or register it in "
                "process via beetroot.backends.register_backend)."
            ) from e
        return cls.from_meta(name, backend)


def _check_host_binder(mode: hostcheck.BinderMode = "auto") -> CheckResult:
    """
    Map :func:`hostcheck.binder_status` onto a doctor :class:`CheckResult`.

    The instance's configured ``binder`` mode shapes the verdict so the
    doctor row matches what ``beetroot up`` would actually do:

    * ``vm`` → ``skip`` — host binder is irrelevant (the emulated
      micro-VM ships its own), so flagging its absence would mislead.
    * otherwise ``ready`` → ``pass``.
    * ``unknown`` → ``skip`` under ``auto`` (binder couldn't be
      determined — e.g. on macOS — so we don't cry wolf) but ``fail``
      under strict ``host`` (the user demanded host binder and we can't
      confirm it).
    * ``loadable`` / ``unsupported`` → ``fail`` (with the remedy folded
      into the reason so the doctor line is self-contained).

    Args:
        mode: The instance's configured binder mode (``cfg.binder``).
            Defaults to ``"auto"`` so callers that don't care about the
            mode (and the existing unit tests) keep the historical
            behaviour.

    Returns:
        The :class:`CheckResult` for the ``host.binder`` doctor row.
    """
    if mode == "vm":
        return CheckResult(
            status="skip",
            reason="binder: vm — host binder not required (emulated micro-VM provides its own)",
        )
    status = hostcheck.binder_status()
    if status.state == "ready":
        return CheckResult(status="pass")
    if status.state == "unknown" and mode != "host":
        return CheckResult(status="skip", reason=status.reason)
    return CheckResult(
        status="fail",
        reason=f"{status.reason}. {status.remedy} Run `beetroot modes` for this host's options.",
    )


def _check_adb_connect(target: str) -> CheckResult:
    """
    Run ``adb connect <target>`` and report pass/fail.

    Args:
        target: The ``host:port`` argument for ``adb connect``.

    Returns:
        ``pass`` iff the subprocess exited 0 and stderr does NOT
        contain ``failed`` / ``cannot connect`` (adb's ``connect``
        verb exits 0 even on failure in some versions, so we re-scan
        stdout/stderr as a safety net).
    """
    if shutil.which("adb") is None:
        return CheckResult(status="skip", reason="adb not on PATH")
    try:
        res = subprocess.run(  # noqa: S603  # adb is a host CLI resolved via PATH; target arg validated upstream
            ["adb", "connect", target],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(status="fail", reason=str(e))
    combined = f"{res.stdout}\n{res.stderr}".lower()
    if res.returncode != 0 or "cannot connect" in combined or "failed" in combined:
        return CheckResult(
            status="fail",
            reason=f"exit {res.returncode}; {res.stdout.strip() or res.stderr.strip()}",
        )
    return CheckResult(status="pass")


def _check_frida_socket(host: str, port: int, *, enabled: bool) -> CheckResult:
    """
    Confirm a TCP listener exists at ``host:port`` without doing the D-Bus handshake.

    Probes the port with :func:`socket.create_connection` (1s connect
    timeout) instead of shelling out to ``nc`` — ``nc``'s ``-z``/``-w``
    flags are not portable across variants (nmap-ncat, busybox), so an
    in-process socket connect is both faster and dependency-free. The
    D-Bus handshake would require the host ``frida`` CLI; here we only
    assert that *something* is listening so the doctor verb is fast and
    minimally-coupled.

    Args:
        host: Hostname to connect to (usually ``localhost``).
        port: TCP port.
        enabled: ``False`` if ``cfg.frida is None`` (Frida is opt-in
            since v0.3). When ``False`` the check returns ``skip``.

    Returns:
        ``pass`` if the connect succeeds, ``fail`` if it's refused /
        times out / is unreachable, ``skip`` if Frida isn't configured
        for this instance.
    """
    if not enabled:
        return CheckResult(status="skip", reason="frida not configured")
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError:
        return CheckResult(status="fail", reason=f"no listener at {host}:{port}")
    return CheckResult(status="pass")


def _magisk_sqlite_value_over_adb(adb_target: str, sql: str) -> tuple[int, str, str]:
    """
    Run ``adb -s <target> shell magisk --sqlite "<sql>"`` and return the result.

    Returns:
        A ``(returncode, stdout, stderr)`` tuple from the subprocess.
        Stub-mockable via ``subprocess.run`` patching in tests.
    """
    res = subprocess.run(  # noqa: S603  # adb is a host CLI resolved via PATH; sql is composed from constants + validated package names
        ["adb", "-s", adb_target, "shell", "magisk", "--sqlite", sql],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return res.returncode, res.stdout, res.stderr


def _check_magisk_zygisk_over_adb(adb_target: str) -> CheckResult:
    """
    Confirm Magisk's ``zygisk`` setting is 1 via the adb-mediated SQL channel.

    ``magisk --sqlite`` reports the row as ``value=1`` on stdout. We
    look for that literal substring rather than parse the full output,
    because the output shape includes a trailing newline + a possible
    empty row when the key is missing entirely.
    """
    if shutil.which("adb") is None:
        return CheckResult(status="skip", reason="adb not on PATH")
    try:
        rc, stdout, stderr = _magisk_sqlite_value_over_adb(
            adb_target,
            "SELECT value FROM settings WHERE key='zygisk'",
        )
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(status="fail", reason=str(e))
    if rc != 0:
        return CheckResult(
            status="fail",
            reason=f"exit {rc}; {(stderr or stdout).strip()}",
        )
    # Empty stdout = key not in settings → fail. ``value=0`` = disabled
    # → fail with the actual value surfaced so the user can grep.
    text = stdout.strip()
    if "value=1" in text:
        return CheckResult(status="pass")
    if "value=0" in text:
        return CheckResult(status="fail", reason="expected 1, got 0")
    return CheckResult(status="fail", reason=f"unexpected output: {text!r}")


def _check_magisk_denylist_over_adb(
    adb_target: str,
    pkg: str,
    *,
    enrolled: bool,
) -> CheckResult:
    """
    Confirm ``pkg`` is enrolled in Magisk's denylist via the adb SQL channel.

    Args:
        adb_target: The adb serial/endpoint for ``adb -s <target>``.
        pkg: Package id (already validated against the Android
            package-id grammar by :class:`config.Magisk`).
        enrolled: ``False`` if the package isn't in ``cfg.magisk.denylist``.
            When ``False`` the check returns ``skip`` (the user
            explicitly chose not to hide root from this package).

    Returns:
        ``pass`` if the package appears in the ``denylist`` table,
        ``fail`` otherwise, ``skip`` if the config doesn't list it.
    """
    if not enrolled:
        return CheckResult(status="skip", reason=f"{pkg} not in magisk.denylist")
    if shutil.which("adb") is None:
        return CheckResult(status="skip", reason="adb not on PATH")
    try:
        rc, stdout, stderr = _magisk_sqlite_value_over_adb(
            adb_target,
            # Package id is grammar-validated upstream by config.Magisk
            # (only [a-zA-Z0-9._]) so it can't break the SQL quote. The
            # bandit warning is a false positive on that grammar.
            f"SELECT package_name FROM denylist WHERE package_name='{pkg}'",  # noqa: S608
        )
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(status="fail", reason=str(e))
    if rc != 0:
        return CheckResult(
            status="fail",
            reason=f"exit {rc}; {(stderr or stdout).strip()}",
        )
    if pkg in stdout:
        return CheckResult(status="pass")
    return CheckResult(status="fail", reason=f"{pkg} not enrolled")


def _check_adb_serial_listed(serial: str) -> CheckResult:
    r"""
    Confirm ``adb devices`` lists ``serial`` in the ``device`` state.

    The output of ``adb devices`` is one device per line, formatted as
    ``<serial>\t<state>``. We look for the exact ``<serial>\tdevice``
    pair so a half-attached phone in ``offline`` / ``unauthorized``
    state surfaces as fail with the actual state in the reason.
    """
    if shutil.which("adb") is None:
        return CheckResult(status="skip", reason="adb not on PATH")
    try:
        res = subprocess.run(
            ["adb", "devices"],  # noqa: S607  # adb is a host CLI resolved via PATH; argv constant
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(status="fail", reason=str(e))
    if res.returncode != 0:
        return CheckResult(status="fail", reason=f"adb devices exit {res.returncode}")
    # adb devices lines are ``<serial>\t<state>`` — two whitespace-
    # separated columns. _MIN_ADB_DEVICES_COLUMNS is the threshold
    # below which we treat the line as not a device row (headers and
    # blank lines have fewer columns).
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= _MIN_ADB_DEVICES_COLUMNS and parts[0] == serial:
            if parts[1] == "device":
                return CheckResult(status="pass")
            return CheckResult(status="fail", reason=f"state={parts[1]}")
    return CheckResult(status="fail", reason=f"{serial} not listed")


def adb_device_health(device: DeviceBackend) -> dict[str, CheckResult]:
    """
    Health-check the adb-backed equivalent of :meth:`Instance.health`.

    Originally landed as a free function (not a method on
    :class:`AdbDevice`) because T6 landed BEFORE T5's
    :class:`AdbDevice` existed. T7 added :meth:`AdbDevice.health`
    as a real method that delegates back to this function. The free
    function is preserved as the canonical implementation (so the body
    only lives in one place) AND as a back-compat shim for
    pre-T7 programmatic callers that imported
    :func:`adb_device_health` directly. New code on or after T7
    should call ``backend.health()`` — :meth:`AdbDevice.health` is the
    spelling that satisfies the "backends own their own health surface"
    intuition.

    The shared check NAMES (``frida.handshake``, ``magisk.zygisk``,
    ``magisk.denylist.<pkg>``) match :meth:`Instance.health` exactly so
    downstream tools grep uniformly. ``device`` only needs the Protocol
    surface (``adb_address``, ``frida_address``) — no
    AdbDevice-specific methods — so this still works against minimal
    stub backends in tests that don't import :class:`AdbDevice`.

    Args:
        device: A :class:`DeviceBackend` whose ``kind == "adb"``.

    Returns:
        Ordered dict of check name → :class:`CheckResult`. ``compose.status``
        is intentionally absent — there's no container for the adb backend.
    """
    # The adb_address is the serial for the AdbDevice backend (per the
    # DeviceBackend Protocol docstring: ``adb_address`` returns "the
    # host:port (or adb serial) that adb connect targets"). The frida
    # address is ``localhost:<forwarded_port>`` for the ADB-forwarded
    # local port.
    serial = device.adb_address
    frida_host, _, frida_port_str = device.frida_address.partition(":")
    try:
        frida_port = int(frida_port_str)
    except ValueError:
        frida_port = 0
    checks: dict[str, CheckResult] = {}
    checks["adb.serial"] = _check_adb_serial_listed(serial)
    checks["frida.handshake"] = _check_frida_socket(
        frida_host or "localhost",
        frida_port,
        enabled=frida_port > 0,
    )
    checks["magisk.zygisk"] = _check_magisk_zygisk_over_adb(serial)
    gms_pkg = "com.google.android.gms"
    checks[f"magisk.denylist.{gms_pkg}"] = _check_magisk_denylist_over_adb(
        serial,
        gms_pkg,
        enrolled=True,
    )
    return checks


def _allocate_port_index() -> int:
    """
    Return the lowest non-negative port index not in current use.

    Module-private helper; T1 retired the public
    ``Manager.allocate_port_index`` method (per Agent 2 F-4: the index
    is not reserved by this call, so calling it without an immediate
    follow-up ``registry.add`` is a footgun). Use
    :func:`registry.add_allocating` for atomic allocate + register.
    """
    return ports.lowest_free_index(registry.used_indices())
