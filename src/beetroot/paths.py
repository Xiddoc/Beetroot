"""
Single source of truth for filesystem layout.

The path model is Docker-inspired: an "instance" is any directory on disk
containing a ``beetroot.yaml`` file. There is no central ``instances/``
directory — instances live wherever the user puts them. The CLI discovers
the current instance the way ``git`` discovers a repo: it walks up from
the cwd looking for the marker file (``beetroot.yaml``).

Global state (the cross-instance registry and download caches) lives under
the user's platform-appropriate config / cache directories. We delegate
to :mod:`platformdirs` so the same code does the right thing on Linux
(``$XDG_CONFIG_HOME`` / ``$XDG_CACHE_HOME``), macOS (``~/Library/...``),
and Windows (``%APPDATA%`` / ``%LOCALAPPDATA%``). The XDG env vars are
honoured automatically by platformdirs on Linux.
"""
from __future__ import annotations

import importlib.resources
from pathlib import Path

from platformdirs import user_cache_path, user_config_path

_INSTANCE_MARKER = "beetroot.yaml"
_APP_NAME = "beetroot"


class InstanceRootNotFoundError(FileNotFoundError):
    """Raised when no ``beetroot.yaml`` marker is found in cwd or its ancestors."""


def instance_root(start: Path | None = None) -> Path:
    """
    Find the current Beetroot instance root by walking up from ``start``.

    An instance root is the nearest ancestor directory containing a
    ``beetroot.yaml`` file. This is the same discovery model used by git
    (``.git`` marker) and pip/uv (``pyproject.toml`` marker).

    Args:
        start: Directory to start the search from. Defaults to ``Path.cwd()``.

    Returns:
        The absolute path to the instance root.

    Raises:
        InstanceRootNotFoundError: If no ``beetroot.yaml`` is found in the
            start directory or any of its ancestors. The error message
            tells the user to ``cd`` into an instance directory.
    """
    cur = (start if start is not None else Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / _INSTANCE_MARKER).is_file():
            return parent
    raise InstanceRootNotFoundError(
        f"Beetroot instance root not found: no {_INSTANCE_MARKER} in {cur} or any "
        "ancestor directory. Run `beetroot` from an instance directory (one "
        "containing beetroot.yaml), or create a new instance there with "
        "`beetroot create`."
    )


def instance_yaml(root: Path) -> Path:
    """Return ``<root>/beetroot.yaml`` — the instance config file."""
    return root / "beetroot.yaml"


def instance_env(root: Path) -> Path:
    """Return ``<root>/.env`` — the compose env file rendered by the CLI."""
    return root / ".env"


def instance_data(root: Path) -> Path:
    """Return ``<root>/data/`` — bind-mounted to ``/data`` inside the container."""
    return root / "data"


def instance_modules(root: Path) -> Path:
    """Return ``<root>/modules/`` — bind-mounted read-only to ``/flash_dir``."""
    return root / "modules"


def instance_frida(root: Path) -> Path:
    """Return ``<root>/frida-server`` — the staged Frida binary for this instance."""
    return root / "frida-server"


def bundled_compose_file() -> Path:
    """
    Return the path to the ``compose.yaml`` shipped inside the package.

    The compose template is bundled under ``beetroot.templates`` so the CLI
    works identically whether installed editable (``uv sync``) or as a
    tool (``uv tool install``); there is no copy of ``compose.yaml`` at
    the project root anymore.

    Uses :func:`importlib.resources.as_file` (T2 Agent 2 B-8) so a
    wheel install — where the resource lives inside a zip and has no
    real filesystem path — gets a materialised copy under the user's
    cache dir that ``docker compose -f`` can actually read. The
    extracted copy is cached per-process: subsequent calls return the
    same path. The cache key is the resource's bytes (digest), so a
    package upgrade that ships a new template invalidates the cache
    on first read of the new bytes. For an editable / source install
    where ``files()`` already returns a real path, ``as_file`` returns
    that path unchanged and no copy is made.

    Returns:
        Absolute path to a readable compose.yaml on disk.
    """
    return _materialise_bundled_compose()


_BUNDLED_COMPOSE_CACHE: Path | None = None


def _materialise_bundled_compose() -> Path:
    """
    Return a stable on-disk copy of the bundled compose.yaml.

    The result is cached at the module level for the program's
    lifetime — ``as_file()`` would otherwise create a fresh extraction
    inside its context manager every call, defeating the per-process
    caching contract documented above. T3 will swap the cache dir for
    ``platformdirs.user_cache_path("beetroot") / "templates"``; for
    now :func:`user_cache_dir` covers the same path.
    """
    global _BUNDLED_COMPOSE_CACHE  # noqa: PLW0603
    if _BUNDLED_COMPOSE_CACHE is not None and _BUNDLED_COMPOSE_CACHE.exists():
        return _BUNDLED_COMPOSE_CACHE
    ref = importlib.resources.files("beetroot.templates").joinpath(
        "compose.yaml"
    )
    with importlib.resources.as_file(ref) as resolved:
        if resolved.is_file():
            # Editable / source install: ``resolved`` is a real
            # filesystem path; using it directly preserves
            # backward-compat with callers that ``read_text()`` it.
            # We still cache the path so subsequent calls don't redo
            # the importlib lookup.
            _BUNDLED_COMPOSE_CACHE = Path(resolved)
            return _BUNDLED_COMPOSE_CACHE
        # Zip-install path: copy the bytes into the user cache so
        # ``docker compose -f`` can read them. ``as_file``'s
        # extracted-tempdir is gone the moment the context manager
        # exits; we need a path that outlives this call.
        contents = resolved.read_bytes()
    cache_target = user_cache_dir("templates") / "compose.yaml"
    cache_target.parent.mkdir(parents=True, exist_ok=True)
    if not cache_target.exists() or cache_target.read_bytes() != contents:
        cache_target.write_bytes(contents)
    _BUNDLED_COMPOSE_CACHE = cache_target
    return _BUNDLED_COMPOSE_CACHE


def user_config_dir() -> Path:
    """
    Return the user-global Beetroot config directory.

    On Linux this resolves to ``$XDG_CONFIG_HOME/beetroot`` (defaulting
    to ``~/.config/beetroot``); on macOS / Windows :mod:`platformdirs`
    picks the platform-appropriate location. Use :func:`user_registry_file`
    for the registry-file path specifically.

    Returns:
        Absolute path to the per-user Beetroot config dir.
    """
    return user_config_path(_APP_NAME)


def user_registry_file() -> Path:
    """
    Return the absolute path to the cross-instance registry file.

    Lives at ``<user_config_dir>/instances.json``. The directory is
    resolved by :mod:`platformdirs` so the same code does the right
    thing on Linux / macOS / Windows; on Linux ``$XDG_CONFIG_HOME``
    is honoured automatically. Note this is a *user-global* registry —
    every instance on the host is listed here, regardless of where on
    disk its directory lives.

    Returns:
        Absolute path to the registry JSON file.
    """
    return user_config_dir() / "instances.json"


def user_cache_dir(subdir: str) -> Path:
    """
    Return a per-subsystem subdirectory under the user's Beetroot cache.

    Lives under ``<platformdirs-cache>/beetroot/<subdir>`` — on Linux
    this is ``$XDG_CACHE_HOME/beetroot/<subdir>``, defaulting to
    ``~/.cache/beetroot/<subdir>``. Used for the Frida binary cache
    and the Magisk module download cache, both shared across instances
    to avoid re-downloading the same blobs.

    Args:
        subdir: A subsystem name (e.g. ``"frida"``, ``"modules"``).

    Returns:
        Absolute path to the requested cache subdirectory.
    """
    return user_cache_path(_APP_NAME) / subdir
