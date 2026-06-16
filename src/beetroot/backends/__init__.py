"""
Backend registry: maps a backend ``kind`` discriminator to a concrete class.

A "backend" is any class that satisfies the :class:`beetroot.api.DeviceBackend`
Protocol AND exposes a ``from_meta(name: str, backend_config) -> Self``
classmethod (used by :meth:`beetroot.api.Manager.resolve` to construct the
backend from a registry row's :class:`beetroot.registry.BackendConfigBase`).

In-tree backends register themselves programmatically at import time
(see :func:`_register_builtin_backends`); third-party backends register
via the ``[project.entry-points."beetroot.backends"]`` group in their
``pyproject.toml``. Third parties also call
:func:`beetroot.registry.register_backend_config` to add their
:class:`~beetroot.registry.BackendConfigBase` subclass to the open registry
union — that is what makes their rows survive read/write cycles.

T1 ships only the redroid backend; T5 adds ``adb.py`` (the
:class:`AdbDevice` backend) and registers it as ``"adb"``.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beetroot.api import DeviceBackend


_BACKEND_REGISTRY: dict[str, type[DeviceBackend]] = {}
_ENTRY_POINTS_LOADED = False


class BackendRegistrationError(ValueError):
    """
    Raised on duplicate or invalid backend registration.
    """


def register_backend(kind: str, cls: type[DeviceBackend]) -> None:
    """
    Register a backend class under a ``kind`` discriminator.

    The class is expected to satisfy :class:`beetroot.api.DeviceBackend`
    AND expose a ``from_meta(name, backend_config) -> Self`` classmethod
    (used by :meth:`beetroot.api.Manager.resolve`). The ``from_meta``
    requirement is checked via ``hasattr`` at registration time so a
    silently-broken third-party backend surfaces the error at registration
    time rather than at dispatch time.

    Entry-point kind collisions (two installed packages both declaring
    ``kind="foo"``) raise :class:`BackendRegistrationError` loudly instead
    of silently discarding the second registration — a silent discard would
    leave the user with a non-obvious "wrong backend loaded" bug that only
    manifests at runtime dispatch.

    Args:
        kind: The discriminator value (e.g. ``"redroid"``, ``"adb"``,
            ``"cloud-xyz"``). Must be unique across the process.
        cls: The concrete backend class.

    Raises:
        BackendRegistrationError: If ``kind`` is already registered or
            if ``cls`` lacks the required ``from_meta`` classmethod.
    """
    if kind in _BACKEND_REGISTRY:
        raise BackendRegistrationError(
            f"backend kind {kind!r} is already registered to "
            f"{_BACKEND_REGISTRY[kind].__name__}; cannot overwrite with "
            f"{cls.__name__}"
        )
    if not hasattr(cls, "from_meta"):
        raise BackendRegistrationError(
            f"backend class {cls.__name__} for kind {kind!r} is missing "
            "the required ``from_meta(name, backend_config)`` classmethod"
        )
    _BACKEND_REGISTRY[kind] = cls


def get_backend(kind: str) -> type[DeviceBackend]:
    """
    Look up a backend class by ``kind``, loading entry points on first call.

    Args:
        kind: The discriminator value.

    Returns:
        The registered backend class.

    Raises:
        KeyError: If no backend is registered for ``kind`` (after the
            entry-point load attempt).
    """
    _load_entry_point_backends()
    return _BACKEND_REGISTRY[kind]


def registered_kinds() -> list[str]:
    """
    Return the sorted list of currently-registered backend kinds.
    """
    _load_entry_point_backends()
    return sorted(_BACKEND_REGISTRY)


def reset_for_testing() -> None:
    """
    Clear the backend registry and reset the entry-point-loaded flag.

    **Test-only seam.** Allows a test to start from a clean registry
    state and re-register exactly the backends it needs, without relying
    on the autouse ``_snapshot_backend_registry`` fixture's timing.  Do
    NOT call this in production code.
    """
    global _ENTRY_POINTS_LOADED  # noqa: PLW0603
    _BACKEND_REGISTRY.clear()
    _ENTRY_POINTS_LOADED = False


def _load_entry_point_backends() -> None:
    """
    Discover and register third-party backends via the entry-point group.

    Each entry point's name becomes the ``kind`` discriminator and the
    pointed-at object becomes the class. Loaded exactly once per
    process; subsequent calls are no-ops.

    Entry-point KIND collisions raise :class:`BackendRegistrationError`
    so a broken third-party package doesn't silently take over a
    built-in backend kind.
    """
    global _ENTRY_POINTS_LOADED  # noqa: PLW0603
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for ep in entry_points(group="beetroot.backends"):
        cls = ep.load()
        if ep.name in _BACKEND_REGISTRY:
            if _BACKEND_REGISTRY[ep.name] is cls:
                continue  # Same class already registered — idempotent, skip.
            # Different class for same kind: loud collision, not silent skip.
            raise BackendRegistrationError(
                f"entry-point kind {ep.name!r} is already registered to "
                f"{_BACKEND_REGISTRY[ep.name].__name__}; "
                f"cannot overwrite with {cls.__name__} from entry-point"
            )
        register_backend(ep.name, cls)


def _register_builtin_backends() -> None:
    """
    Register the in-tree backends. Called once at module import.
    """
    # Local import keeps this module free of the api import cycle at
    # collection time. ``api.py`` imports nothing from this package.
    from beetroot.api import Instance  # noqa: PLC0415

    if "redroid" not in _BACKEND_REGISTRY:
        register_backend("redroid", Instance)
    # AdbDevice (T5) lives in a sibling module. Importing it for its
    # side-effect (the module-level ``register_backend("adb", ...)``
    # call at the bottom of ``adb.py``) registers it once per process.
    # The ``in`` guard keeps a second ``_register_builtin_backends()``
    # call (e.g. a test that resets the registry) from raising
    # ``BackendRegistrationError``.
    if "adb" not in _BACKEND_REGISTRY:
        # Local-import is intentional — module-level would create an
        # import cycle with adb.py's ``from beetroot.backends import
        # register_backend``. The ``importlib.import_module`` form
        # avoids a top-level ``from`` import that ruff's PLC0415 (no
        # local-import) would flag. Imported for its side-effect
        # (``register_backend("adb", AdbDevice)`` at the bottom of
        # ``adb.py``).
        import importlib  # noqa: PLC0415  # local-import is intentional; see above

        importlib.import_module("beetroot.backends.adb")
    # VmDeviceBackend (issue #44) — same side-effect-import idiom as adb
    # above: importing the module runs its module-level
    # ``register_backend("vm", VmDeviceBackend)``.
    if "vm" not in _BACKEND_REGISTRY:
        import importlib  # noqa: PLC0415  # local-import is intentional; see above

        importlib.import_module("beetroot.backends.vm")


_register_builtin_backends()
