"""
Backend registry: maps a backend ``kind`` discriminator to a concrete class.

A "backend" is any class that satisfies the :class:`beetroot.api.DeviceBackend`
Protocol AND exposes a ``from_meta(name: str, backend_config) -> DeviceBackend``
classmethod (used by :meth:`beetroot.api.Manager.resolve` to construct the
backend from a registry row's :class:`beetroot.registry.BackendConfig`).

In-tree backends register themselves programmatically at import time
(see :func:`_register_builtin_backends`); third-party backends register
via the ``[project.entry-points."beetroot.backends"]`` group in their
``pyproject.toml``. The two paths share the same on-disk registry shape
— a third-party backend's config validates against its own pydantic
model (separate from the in-tree discriminated union) and the entry
point only needs to expose the concrete class.

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
    """Raised on duplicate or invalid backend registration."""


def register_backend(kind: str, cls: type[DeviceBackend]) -> None:
    """
    Register a backend class under a ``kind`` discriminator.

    The class is expected to satisfy :class:`beetroot.api.DeviceBackend`
    AND expose a ``from_meta(name, backend_config) -> DeviceBackend``
    classmethod (used by :meth:`beetroot.api.Manager.resolve`). The
    ``from_meta`` requirement is checked via ``hasattr`` at registration
    time so a silently-broken third-party backend surfaces the error at
    registration time rather than at dispatch time. The Protocol
    surface itself is **not** runtime-checked — ``isinstance(cls,
    type[DeviceBackend])`` isn't a meaningful operation (Protocols
    aren't ABCs at the class level), so the contract is duck-typed and
    relies on static type-checking + the per-backend unit tests
    asserting ``isinstance(backend_instance, DeviceBackend)``.

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
    """Return the sorted list of currently-registered backend kinds."""
    _load_entry_point_backends()
    return sorted(_BACKEND_REGISTRY)


def _load_entry_point_backends() -> None:
    """
    Discover and register third-party backends via the entry-point group.

    Each entry point's name becomes the ``kind`` discriminator and the
    pointed-at object becomes the class. Loaded exactly once per
    process; subsequent calls are no-ops.

    Entry-point errors are surfaced as :class:`BackendRegistrationError`
    so a broken third-party package doesn't silently swallow its
    backend.
    """
    global _ENTRY_POINTS_LOADED  # noqa: PLW0603
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for ep in entry_points(group="beetroot.backends"):
        if ep.name in _BACKEND_REGISTRY:
            continue
        cls = ep.load()
        register_backend(ep.name, cls)


def _register_builtin_backends() -> None:
    """Register the in-tree backends. Called once at module import."""
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


_register_builtin_backends()
