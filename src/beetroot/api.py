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
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from . import compose, config, frida_download, modules_download, paths, ports, registry
from . import snapshot as _snapshot_mod

# Module-level alias for the builtin ``list`` so the two ``list*``
# staticmethods on :class:`Manager` below can use ``_List[...]`` in
# their return annotations. Without the alias, mypy lexically
# resolves ``list[...]`` inside the class body to the
# ``Manager.list`` staticmethod (which doesn't subscript). Using a
# different identifier keeps the call sites readable while sidestepping
# the shadowing.
_List = list

_MINIMAL_BEETROOT_YAML = "api_version: 3\nandroid:\n  version: 14\n"

# Instance names are used as Docker compose project names (which
# enforce ``[a-z0-9_-]+``) AND as filesystem-segment defaults for the
# instance directory. Pre-validate at the OOP boundary so a typo like
# ``Foo`` or ``alpha bravo`` surfaces with a clear message before any
# side effect runs. (T2 v0.3.1 deferred.)
_INSTANCE_NAME_RE: Final = re.compile(r"^[a-z0-9_-]+$")


def _validate_instance_name(name: str) -> None:
    """Raise ``ValueError`` if ``name`` doesn't match the instance-name grammar."""
    if not _INSTANCE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"instance name {name!r} is invalid — must match "
            r"[a-z0-9_-]+ (Docker compose project-name grammar). "
            "Lowercase alphanumerics, underscores, and hyphens only."
        )


class InstanceNotFoundError(LookupError):
    """Raised when an instance name is not in the registry."""


class FridaNotInstalledError(RuntimeError):
    """Raised when ``Instance.frida_cli`` is called without the host ``frida`` CLI on PATH."""


class AdbNotInstalledError(RuntimeError):
    """Raised when ``Instance.shell`` is called without the host ``adb`` CLI on PATH."""


class BackendCapabilityError(RuntimeError):
    """
    Raised when a :class:`DeviceBackend` can't honour a requested operation.

    Backends only expose the universal surface (shell, frida_cli,
    install_frida, presence). Verbs that need backend-specific behaviour
    (``up``, ``down``, ``apply``, ``snapshot``, …) narrow with
    ``isinstance(b, Instance)`` and raise this error if the backend
    isn't capable. The CLI catches it and renders a friendly
    ``error: ...`` line.
    """


@runtime_checkable
class DeviceBackend(Protocol):
    """
    Abstraction for a Magisk-rooted Android device that Beetroot can drive.

    v0.3 ships a single implicit backend: a Redroid container managed via
    ``docker compose`` (the entire :class:`Instance` class). v0.4
    introduces ``AdbDevice`` (T5) targeting a real rooted phone over
    ADB; see ``docs/design/device-backends.md`` for the implementation
    roadmap.

    This Protocol is the lowest-common-denominator surface every backend
    exposes: enough to identify the backend, attach Frida, look up the
    canonical addresses, check reachability, and dispatch the two
    universal user-facing operations (``shell`` and ``frida_cli``).
    Operations that don't generalise across backends (compose-layer
    routines like ``up`` / ``down``, Magisk-DB stealth writes, container
    overlay manipulation) are kept off the Protocol on purpose —
    callers narrow via ``isinstance(b, Instance)`` and raise
    :class:`BackendCapabilityError` from the offending verb if the
    backend isn't capable.

    The ``kind`` property is intentionally typed as :class:`str` (not a
    ``Literal[...]``) so third-party backends that register via
    ``[project.entry-points."beetroot.backends"]`` can declare their
    own discriminator strings (``"cloud-xyz"``, …) without forking this
    Protocol.
    """

    @property
    def name(self) -> str:
        """Return the registry name for this backend."""
        ...

    @property
    def kind(self) -> str:
        """
        Return the backend kind discriminator (``"redroid"``, ``"adb"``, …).

        Mirrors the ``kind`` field of the matching :class:`BackendConfig`
        subclass and so participates in the registry's discriminated union.
        """
        ...

    @property
    def adb_address(self) -> str:
        """Return the ``host:port`` (or adb serial) that ``adb connect`` targets."""
        ...

    @property
    def frida_address(self) -> str:
        """Return the ``host:port`` Frida control endpoint."""
        ...

    @property
    def is_available(self) -> bool:
        """Return True iff the backend is reachable right now (no install/start required)."""
        ...

    def install_frida(self, version: str) -> None:
        """
        Make a frida-server of the requested version available on the device.

        Args:
            version: The frida release tag (e.g. ``16.4.10``).
        """
        ...

    def shell(self) -> int:
        """Open an interactive shell into the device; return the subprocess exit code."""
        ...

    def frida_cli(self, args: list[str]) -> int:
        """Invoke the host ``frida`` CLI against this backend; return the exit code."""
        ...

    @classmethod
    def from_meta(
        cls, name: str, backend: registry.BackendConfig,
    ) -> DeviceBackend:
        """
        Construct a backend instance from a registry meta's backend config.

        Used by :meth:`Manager.resolve` to dispatch via the backend
        registry. The classmethod is part of the Protocol so static
        type-checkers can verify third-party backends expose the
        dispatcher contract.

        Args:
            name: Registry name for the backend.
            backend: The matching :class:`registry.BackendConfig` row's
                backend field — narrowed to the concrete subclass that
                this backend kind owns.

        Returns:
            A constructed backend instance satisfying this Protocol.
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

        Returns:
            The newly created and registered :class:`Instance`.

        Raises:
            ValueError: If ``name`` is already in the registry, or if
                the resolved ports collide with another instance.
            FileExistsError: If ``path`` already contains a
                ``beetroot.yaml`` (use :meth:`register` to adopt it).
        """
        _validate_instance_name(name)
        if registry.get(name) is not None:
            raise ValueError(f"instance {name!r} already exists in registry")
        target_root = (path if path is not None else Path(name)).resolve()
        yaml_path = paths.instance_yaml(target_root)
        if yaml_path.exists():
            raise FileExistsError(
                f"{yaml_path} already exists — use Instance.register(path) to adopt it"
            )

        effective_cfg = cfg if cfg is not None else config.InstanceConfig()

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
            yaml_path.write_text(_MINIMAL_BEETROOT_YAML)
        else:
            config.write_yaml(yaml_path, effective_cfg)
        # Atomic allocation + registration under one file lock. Two
        # parallel create() calls cannot grab the same stride slot.
        index = registry.add_allocating(name, target_root)
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
        # Atomic allocation + registration under one file lock.
        index = registry.add_allocating(resolved_name, target_root)
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
                resolved_name, target_root, created_dir=False,
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
            raise InstanceNotFoundError(
                f"no instance named {name!r}; try Manager.list()"
            )
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
        cls, name: str, backend: registry.BackendConfig,
    ) -> Instance:
        """
        Build an :class:`Instance` from a registry meta's backend config.

        Used by the backend-registry dispatcher in :mod:`beetroot.backends`
        so :meth:`Manager.resolve` can construct any backend class
        uniformly given ``(name, BackendConfig)``.

        Args:
            name: Registry name.
            backend: The matching :class:`registry.RedroidBackendConfig`
                row's backend field.

        Returns:
            The hydrated :class:`Instance`.

        Raises:
            InstanceNotFoundError: If ``backend`` is not a
                :class:`registry.RedroidBackendConfig`.
        """
        if not isinstance(backend, registry.RedroidBackendConfig):
            raise InstanceNotFoundError(
                f"instance {name!r} has backend kind {backend.kind!r}; "
                "Instance only represents redroid backends"
            )
        root = Path(backend.absolute_path)
        cfg = config.load_yaml(paths.instance_yaml(root))
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
            f"directory {resolved} is not registered; "
            "call Instance.register(path) first"
        )

    # ---- introspection ----------------------------------------------------

    @property
    def name(self) -> str:
        """Registry name for this instance."""
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
        """Absolute path to the instance directory."""
        return self._root

    @property
    def config(self) -> config.InstanceConfig:
        """The parsed ``beetroot.yaml`` at the time this object was constructed."""
        return self._cfg

    @property
    def index(self) -> int:
        """The instance's allocated port index (stride-of-10 base)."""
        return self._meta().index

    @property
    def ports(self) -> dict[str, int]:
        """Resolved host ports for this instance (``adb`` / ``frida`` / ``frida2``)."""
        return ports.resolve_ports(self.index, self._cfg.ports)

    @property
    def adb_address(self) -> str:
        """``localhost:<adb_port>`` — what ``adb connect`` should target."""
        return f"localhost:{self.ports['adb']}"

    @property
    def frida_address(self) -> str:
        """``localhost:<frida_port>`` — what ``frida -H`` should target."""
        return f"localhost:{self.ports['frida']}"

    @property
    def status(self) -> compose.ComposeStatus:
        """Live one-word container status (see :data:`compose.ComposeStatus`)."""
        return compose.ps_status(self._name, self._root)

    @property
    def is_available(self) -> bool:
        """True iff the underlying container is running right now."""
        return self.status == "running"

    # ---- lifecycle --------------------------------------------------------

    def up(self) -> None:
        """Start the instance with ``docker compose up -d``."""
        compose.up(self._name, self._root)

    def down(self) -> None:
        """Stop the instance with ``docker compose down`` (data preserved)."""
        compose.down(self._name, self._root)

    def restart(self) -> None:
        """Stop then start the instance in sequence."""
        compose.down(self._name, self._root)
        compose.up(self._name, self._root)

    def apply(self) -> None:
        """
        Re-load ``beetroot.yaml`` and re-stage all derived files.

        Re-reads the on-disk config (so external edits are picked up),
        re-validates port-collision, then re-renders ``.env`` and
        re-stages Frida + modules. A subsequent :meth:`restart` is
        required for the container to pick up the new config.

        Raises:
            ValueError: If the re-resolved ports collide with another
                registered instance.
        """
        self._cfg = config.load_yaml(paths.instance_yaml(self._root))
        new_ports = ports.resolve_ports(self.index, self._cfg.ports)
        _check_port_collisions(self._name, new_ports)
        self._stage()

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

        Args:
            yes: If False (the default), prompt for confirmation on
                stdin. CLI callers pass True after their own prompt; the
                ``yes`` arg here is a safety net for programmatic
                callers that want the same prompt-before-destroy
                behaviour without re-implementing it.

        Raises:
            RuntimeError: If the interactive prompt was declined.
            compose.ComposeError: If ``docker compose down`` fails. The
                host-side state is removed regardless before the error
                surfaces.
        """
        if not yes:
            ans = input(
                f"Destroy {self._name} and delete {self._root}? [y/N] "
            ).strip().lower()
            if ans != "y":
                raise RuntimeError("destroy aborted by user")
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
        """Run ``destroy``'s steps under the assumption the lock is held."""
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

    # ---- operations -------------------------------------------------------

    def shell(self) -> int:
        """
        Open an interactive ADB shell into the instance.

        Returns:
            The exit code of the ``adb shell`` invocation. Beetroot
            does not raise on non-zero exits — research scripts may
            care about ``adb`` exit codes for their own flow control.

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
        """
        if shutil.which("adb") is None:
            raise AdbNotInstalledError(
                "adb not found on PATH (install android-tools)"
            )
        target = self.adb_address
        subprocess.run(["adb", "connect", target], check=False)  # noqa: S603, S607  # adb is a research CLI we deliberately resolve via PATH
        res = subprocess.run(["adb", "-s", target, "shell"], check=False)  # noqa: S603, S607  # same as above
        return int(res.returncode)

    def install_frida(self, version: str) -> None:
        """
        Stage a frida-server binary of the requested version on the instance.

        Implements the :class:`DeviceBackend` Protocol's
        ``install_frida``. The binary is downloaded into the user-global
        Frida cache (idempotent) and copied into the instance's bind
        mount. A subsequent :meth:`restart` is required for the
        container's ``entrypoint.sh`` to launch the new binary.

        Args:
            version: The frida release tag (e.g. ``16.4.10``).
        """
        frida_download.stage_for_instance(self._root, version)

    def frida_cli(self, args: list[str]) -> int:
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
        transient = self._cfg.model_copy(
            update={"modules": [*self._cfg.modules, new_module]}
        )
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
            raise InstanceNotFoundError(
                f"instance {self._name!r} disappeared from the registry"
            )
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
        so a v0.5 snapshot restored against a v0.4 host (where
        :func:`snapshot.restore` already wrote the manifest's
        ``path_layout`` into the registry slot) emits the right
        ``BEETROOT_*`` overrides on the very first ``apply``.
        """
        meta = self._meta()
        backend = meta.backend
        stealth_paths = (
            backend.stealth_paths
            if isinstance(backend, registry.RedroidBackendConfig)
            else None
        )
        new_ports = ports.resolve_ports(meta.index, self._cfg.ports)
        paths.instance_data(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_modules(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_env(self._root).write_text(
            config.render_env(
                self._name, self._cfg, new_ports, stealth_paths=stealth_paths,
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


_INSTANCE_LOCK_FILENAME = ".beetroot.lock"


@contextlib.contextmanager
def instance_lock(
    instance_root: Path, *, exclusive: bool
) -> Iterator[Path]:
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


def _rollback_partial_create(
    name: str, target_root: Path, *, created_dir: bool
) -> None:
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

    # The two list* staticmethods below use a module-level alias for
    # the builtin ``list`` in their return annotations. Without the
    # alias, mypy lexically resolves ``list[...]`` to ``Manager.list``
    # (the staticmethod itself), which doesn't subscript. The alias is
    # declared at module scope just below the imports.

    @staticmethod
    def list() -> _List[Instance]:
        """
        Return every registered *redroid* instance, sorted by name.

        Adb-kind rows are skipped because :class:`Instance` only
        represents redroid backends — use :meth:`resolve` to walk every
        backend uniformly via the Protocol. Orphan entries (redroid
        rows whose on-disk directory has been ``rm -rf``'d, or whose
        ``beetroot.yaml`` is now unparseable) are silently skipped —
        without this, a single orphan would crash ``beetroot ls`` and
        prevent the user from cleaning up. Use :meth:`list_orphans`
        to surface them for cleanup.

        Returns:
            A list of :class:`Instance` objects, one per healthy
            registered redroid name.
        """
        out: _List[Instance] = []
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
                # YAML present but unparseable — mirrors the orphan
                # contract from ``list_orphans``. Without this, a
                # single corrupted YAML crashes ``beetroot ls`` and
                # the user has no way to surface the row for cleanup.
                # (T2 v0.3.1 deferred.)
                continue
        return out

    @staticmethod
    def list_orphans() -> _List[str]:
        """
        Return names of redroid instances whose on-disk dir is missing OR unparseable.

        An orphan is a redroid-kind registry row pointing at a path
        with no ``beetroot.yaml`` (typically because the user manually
        ``rm -rf``'d the directory without running
        ``beetroot destroy``) OR a ``beetroot.yaml`` that can't be
        parsed any more (e.g. a half-overwritten file, an
        api_version mismatch, or hand-edited junk). v0.3 returned only
        the first kind, so a corrupted YAML left the entry invisible
        to ``Manager.list`` AND to ``Manager.list_orphans`` — the
        user had no surface to clean it up from. (T2 v0.3.1 deferred.)

        Adb-kind rows are not directory-backed so they can never be
        orphans by this definition. Names are returned sorted; the
        cleanup verb is ``beetroot destroy <name> -y``.

        Returns:
            Sorted list of orphan instance names. Empty if every
            registered redroid entry's directory is present and its
            YAML parses.
        """
        orphans: _List[str] = []
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
                # ``Manager.list`` already skips it via the
                # InstanceRootNotFoundError filter (which load() emits
                # transitively when the YAML is unreachable). Catch
                # broadly: pydantic ValidationError, yaml.YAMLError,
                # custom api_version mismatches, and any future
                # validation backend all flow through here.
                orphans.append(name)
        return sorted(orphans)

    @staticmethod
    def get(name: str) -> Instance | None:
        """
        Look up a registered instance by name, returning ``None`` if missing.

        Use :meth:`Instance.load` when you want an exception on a miss.

        Args:
            name: Registry name.

        Returns:
            The :class:`Instance`, or ``None`` if ``name`` isn't registered.
        """
        if registry.get(name) is None:
            return None
        return Instance.load(name)

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
                or if its ``kind`` is not in the backend registry.
        """
        meta = registry.get(name)
        if meta is None:
            raise InstanceNotFoundError(
                f"no instance named {name!r}; try Manager.list()"
            )
        from . import backends  # noqa: PLC0415

        try:
            cls = backends.get_backend(meta.backend.kind)
        except KeyError as e:
            raise InstanceNotFoundError(
                f"no backend registered for kind {meta.backend.kind!r}; "
                "install the package providing it (or register it in "
                "process via beetroot.backends.register_backend)."
            ) from e
        return cls.from_meta(name, meta.backend)


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
