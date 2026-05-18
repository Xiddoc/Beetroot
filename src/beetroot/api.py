"""
High-level OOP wrapper around Beetroot's procedural modules.

The procedural modules (:mod:`beetroot.compose`, :mod:`beetroot.config`,
:mod:`beetroot.frida_dl`, :mod:`beetroot.modules_dl`, :mod:`beetroot.paths`,
:mod:`beetroot.ports`, :mod:`beetroot.registry`, :mod:`beetroot.snapshot`,
:mod:`beetroot.builder`) remain the load-bearing implementation. This
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
  (``list``, ``get``, ``allocate_port_index``).
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

import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import compose, config, frida_dl, modules_dl, paths, ports, registry
from . import snapshot as _snapshot_mod

_MINIMAL_BEETROOT_YAML = "api_version: 2\nandroid:\n  version: 14\n"


class InstanceNotFoundError(LookupError):
    """Raised when an instance name is not in the registry."""


class FridaNotInstalledError(RuntimeError):
    """Raised when ``Instance.frida_cli`` is called without the host ``frida`` CLI on PATH."""


class AdbNotInstalledError(RuntimeError):
    """Raised when ``Instance.shell`` is called without the host ``adb`` CLI on PATH."""


@runtime_checkable
class DeviceBackend(Protocol):
    """
    Abstraction for a Magisk-rooted Android device that Beetroot can drive.

    v0.3 ships a single implicit backend: a Redroid container managed via
    ``docker compose`` (the entire :class:`Instance` class). v0.4 will
    introduce an ``AdbDeviceBackend`` that targets a researcher's
    real-world rooted phone over ADB; see
    ``docs/design/device-backends.md`` for the implementation roadmap.

    This Protocol is the lowest-common-denominator surface both backends
    expose: enough to attach Frida, look up the canonical addresses, and
    check whether the backend is reachable. Operations that don't
    generalise across backends (compose-layer routines like ``up`` /
    ``down``, Magisk-DB stealth writes, container overlay manipulation)
    are kept off the Protocol on purpose — see the design doc for the
    list of capability methods and how non-supporting backends should
    raise.
    """

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
        if registry.get(name) is not None:
            raise ValueError(f"instance {name!r} already exists in registry")
        target_root = (path if path is not None else Path(name)).resolve()
        yaml_path = paths.instance_yaml(target_root)
        if yaml_path.exists():
            raise FileExistsError(
                f"{yaml_path} already exists — use Instance.register(path) to adopt it"
            )

        effective_cfg = cfg if cfg is not None else config.InstanceConfig()

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
        try:
            _check_port_collisions(name, new_ports)
        except ValueError:
            registry.remove(name)
            raise

        inst = cls(name=name, root=target_root, cfg=effective_cfg)
        inst._stage()
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
        if registry.get(resolved_name) is not None:
            raise ValueError(f"instance {resolved_name!r} already in registry")
        cfg = config.load_yaml(yaml_path)
        # Atomic allocation + registration under one file lock.
        index = registry.add_allocating(resolved_name, target_root)
        new_ports = ports.resolve_ports(index, cfg.ports)
        try:
            _check_port_collisions(resolved_name, new_ports)
        except ValueError:
            registry.remove(resolved_name)
            raise
        inst = cls(name=resolved_name, root=target_root, cfg=cfg)
        # Stage .env + frida-server + modules now so a follow-up
        # `beetroot up <name>` works without an intermediate
        # `beetroot apply`. Mirrors what Instance.create does.
        inst._stage()
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
            InstanceNotFoundError: If ``name`` is not in the registry.
        """
        meta = registry.get(name)
        if meta is None:
            raise InstanceNotFoundError(
                f"no instance named {name!r}; try Manager.list()"
            )
        root = Path(meta["absolute_path"])
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
            if Path(meta["absolute_path"]).resolve() == resolved:
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
        return int(self._meta()["index"])

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
    def status(self) -> str:
        """Live one-word container status (``running``, ``exited``, ``not-created``)."""
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
        compose_error: compose.ComposeError | None = None
        try:
            compose.down(self._name, self._root, volumes=True)
        except compose.ComposeError as e:
            compose_error = e
        if self._root.exists():
            shutil.rmtree(self._root)
        registry.remove(self._name)
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
        subprocess.run(["adb", "connect", target], check=False)
        res = subprocess.run(["adb", "-s", target, "shell"], check=False)
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
        frida_dl.stage_for_instance(self._root, version)

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
        res = subprocess.run(cmd, check=False)
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

        Args:
            source: Either an ``http(s)://`` URL or an instance-relative
                path to a ``.zip`` module.
            sha256: Optional expected hex digest for integrity checking.

        Notes:
            The container will not pick up the new module until the next
            :meth:`restart`.
        """
        if source.startswith(("http://", "https://")):
            self._cfg.modules.append(config.Module(url=source, sha256=sha256))
        else:
            self._cfg.modules.append(config.Module(path=source, sha256=sha256))
        config.write_yaml(paths.instance_yaml(self._root), self._cfg)
        modules_dl.stage_for_instance(self._root, self._cfg)

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

    def _meta(self) -> dict[str, Any]:
        meta = registry.get(self._name)
        if meta is None:
            raise InstanceNotFoundError(
                f"instance {self._name!r} disappeared from the registry"
            )
        return meta

    def _stage(self) -> None:
        """Render .env, stage frida-server, stage modules. Idempotent."""
        new_ports = ports.resolve_ports(self.index, self._cfg.ports)
        paths.instance_data(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_modules(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_env(self._root).write_text(
            config.render_env(self._name, self._cfg, new_ports)
        )
        if self._cfg.frida is not None:
            frida_dl.stage_for_instance(self._root, self._cfg.frida.version)
        else:
            frida_dl.stage_empty(self._root)
        modules_dl.stage_for_instance(self._root, self._cfg)


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
    def list() -> list[Instance]:
        """
        Return every registered instance, sorted by name.

        Returns:
            A list of :class:`Instance` objects, one per registered name.
        """
        return [Instance.load(name) for name in sorted(registry.list_instances())]

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
    def allocate_port_index() -> int:
        """
        Return the lowest non-negative port index not in current use.

        The index is not reserved — callers must claim it by registering
        an instance before the next allocator call, or it'll be returned
        to the next caller too.

        Returns:
            The next available port index.
        """
        return ports.lowest_free_index(registry.used_indices())
