"""Single source of truth for filesystem layout."""
from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Return the android-emulator/ directory — the repo this CLI manages."""
    # src/beetroot/paths.py -> src/beetroot -> src -> repo
    return Path(__file__).resolve().parents[2]


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
