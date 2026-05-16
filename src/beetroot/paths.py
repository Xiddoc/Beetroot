"""Single source of truth for filesystem layout."""
from __future__ import annotations

from pathlib import Path

_MARKER = "compose.yaml"


class ProjectRootNotFoundError(FileNotFoundError):
    """Raised when no compose.yaml marker is found in cwd or its ancestors."""


def repo_root(start: Path | None = None) -> Path:
    """
    Find the Beetroot project root by walking up from ``start`` (default cwd).

    The project root is the nearest ancestor directory containing a
    ``compose.yaml`` file. This is the same discovery model used by git
    (``.git`` marker) and pip/uv (``pyproject.toml`` marker).

    Args:
        start: Directory to start the search from. Defaults to ``Path.cwd()``.

    Returns:
        The absolute path to the project root.

    Raises:
        ProjectRootNotFoundError: If no ``compose.yaml`` is found in the
            start directory or any of its ancestors. The error message
            tells the user to ``cd`` into a project directory.
    """
    cur = (start if start is not None else Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / _MARKER).is_file():
            return parent
    raise ProjectRootNotFoundError(
        f"Beetroot project root not found: no {_MARKER} in {cur} or any "
        "ancestor directory. Run `beetroot` from a project directory "
        "(one containing compose.yaml), or initialize a new project there."
    )


def instances_dir() -> Path:
    """Return the ``instances/`` directory that holds all instance subdirs."""
    return repo_root() / "instances"


def instance_dir(name: str) -> Path:
    """Return ``instances/<name>/`` — the root directory for one instance."""
    return instances_dir() / name


def instance_data(name: str) -> Path:
    """Return ``instances/<name>/data/`` — bind-mounted to ``/data`` inside the container."""
    return instance_dir(name) / "data"


def instance_modules(name: str) -> Path:
    """Return ``instances/<name>/modules/`` — bind-mounted read-only to ``/flash_dir``."""
    return instance_dir(name) / "modules"


def instance_frida(name: str) -> Path:
    """Return ``instances/<name>/frida-server`` — the staged frida binary."""
    return instance_dir(name) / "frida-server"


def instance_yaml(name: str) -> Path:
    """Return ``instances/<name>/beetroot.yaml`` — the instance config file."""
    return instance_dir(name) / "beetroot.yaml"


def instance_env(name: str) -> Path:
    """Return ``instances/<name>/.env`` — the compose env file rendered by the CLI."""
    return instance_dir(name) / ".env"


def registry_file() -> Path:
    """Return the path to ``instances.json`` — the instance registry."""
    return repo_root() / "instances.json"


def presets_dir() -> Path:
    """Return the ``presets/`` directory containing bundled beetroot.yaml templates."""
    return repo_root() / "presets"


def compose_file() -> Path:
    """Return the path to ``compose.yaml`` in the repo root."""
    return repo_root() / "compose.yaml"


def frida_cache_dir() -> Path:
    """Return the shared cache for downloaded frida-server binaries, indexed by version."""
    return repo_root() / ".cache" / "frida"
