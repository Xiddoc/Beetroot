"""
beetroot.yaml schema, loading, and .env rendering.

Only ``android.version`` is required; every other field has a sensible default
and can be omitted entirely from an instance YAML or preset.  Optional
top-level sections: ``display``, ``resources``, ``frida``, ``modules``,
``stealth``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from . import paths

_VALID_ANDROID_VERSIONS = {11, 12, 13, 14}


class Display(BaseModel):
    """
    Display settings for the virtual Android screen.

    Attributes:
        width: Horizontal resolution in pixels.
        height: Vertical resolution in pixels.
        fps: Frame rate limit.
        gpu_mode: GPU rendering mode passed to redroid (e.g. ``host``).
    """

    width: int = 540
    height: int = 960
    fps: int = 3
    gpu_mode: str = "host"


class Resources(BaseModel):
    """
    Docker resource caps for the container.

    Attributes:
        mem: Hard memory limit (e.g. ``3g``).
        cpus: CPU cap as a float.
        shm: Shared-memory size.
        mem_reservation: Optional soft memory floor.
        memswap_limit: Optional total memory + swap cap.
        pids_limit: Maximum number of PIDs the container can spawn.
    """

    mem: str = "3g"
    cpus: float = 2.0
    shm: str = "256m"
    mem_reservation: str | None = None
    memswap_limit: str | None = None
    pids_limit: int = 4096


class Frida(BaseModel):
    """
    Frida-server version pinning for an instance.

    Attributes:
        version: The frida release tag to download (e.g. ``16.4.10``).
    """

    version: str = "16.4.10"


class Module(BaseModel):
    """
    A single Magisk module entry from beetroot.yaml.

    Exactly one of ``url`` or ``path`` must be set. ``sha256`` is optional
    but recommended when fetching from a URL.

    Attributes:
        url: HTTPS URL to download the module zip from.
        path: Repo-relative path to a local zip file.
        sha256: Expected hex digest for integrity verification.
    """

    url: str | None = None
    path: str | None = None
    sha256: str | None = None

    @override
    def model_post_init(self, _ctx: Any) -> None:
        if not self.url and not self.path:
            raise ValueError("module entry must set either `url` or `path`")
        if self.url and self.path:
            raise ValueError("module entry sets both `url` and `path` — pick one")


class Stealth(BaseModel):
    """
    Root-hiding (denylist) configuration.

    Attributes:
        denylist: Package names added to Magisk's denylist at boot.
    """

    denylist: list[str] = Field(default_factory=list)


class Android(BaseModel):
    """
    Android version and GApps flavour selection.

    Attributes:
        version: Android major version (11, 12, 13, or 14).
        gapps: GApps bundle to bake into the base image.
    """

    version: int = 14
    gapps: Literal["none", "lite", "full", "mindthegapps"] = "lite"

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v not in _VALID_ANDROID_VERSIONS:
            raise ValueError(
                f"android.version {v!r} is not supported — valid values: "
                + ", ".join(str(x) for x in sorted(_VALID_ANDROID_VERSIONS))
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _reject_base_image(cls, data: object) -> object:
        if isinstance(data, dict) and "base_image" in data:
            raise ValueError(
                "android.base_image is no longer supported — replace with "
                "`android.version: 14`. See CHANGELOG."
            )
        return data


_GAPPS_SLUG = {
    "none": "",
    "lite": "_litegapps",
    "full": "_gapps",
    "mindthegapps": "_mindthegapps",
}


def base_image_tag(android: Android) -> str:
    """
    Derive the redroid base-image tag from version + gapps flavour.

    Args:
        android: The Android section of an InstanceConfig.

    Returns:
        The Docker image tag, e.g.
        ``redroid/redroid:14.0.0_litegapps_houdini_magisk``.
    """
    return f"redroid/redroid:{android.version}.0.0{_GAPPS_SLUG[android.gapps]}_houdini_magisk"


class InstanceConfig(BaseModel):
    """
    The schema of ``instances/<name>/beetroot.yaml``.

    ``name`` is omitted from the schema because it lives in the directory
    name itself — single source of truth, can't drift.

    Attributes:
        android: Android version and GApps flavour.
        display: Virtual screen geometry and frame rate.
        resources: Docker resource caps.
        frida: Frida-server version pin; ``None`` disables frida entirely.
        modules: Magisk modules to flash at boot.
        stealth: Denylist / root-hiding settings.
    """

    android: Android = Field(default_factory=Android)
    display: Display = Field(default_factory=Display)
    resources: Resources = Field(default_factory=Resources)
    frida: Frida | None = Field(default_factory=Frida)
    modules: list[Module] = Field(default_factory=list)
    stealth: Stealth = Field(default_factory=Stealth)


def load_yaml(path: Path) -> InstanceConfig:
    """
    Load and validate an InstanceConfig from a YAML file.

    An empty file is treated as an all-defaults config.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        A validated InstanceConfig populated from the file.
    """
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    return InstanceConfig.model_validate(raw)


def load_instance(name: str) -> InstanceConfig:
    """
    Load the config for a named instance from its beetroot.yaml.

    Args:
        name: Instance name (directory under ``instances/``).

    Returns:
        The validated InstanceConfig for that instance.
    """
    return load_yaml(paths.instance_yaml(name))


def load_preset(preset_name: str) -> InstanceConfig:
    """
    Load a named preset from the ``presets/`` directory.

    Args:
        preset_name: Basename of the preset file without the ``.yaml``
            extension (e.g. ``default``).

    Returns:
        The validated InstanceConfig for the preset.

    Raises:
        FileNotFoundError: If no matching ``.yaml`` file exists in
            ``presets/``.
    """
    p = paths.presets_dir() / f"{preset_name}.yaml"
    if not p.exists():
        raise FileNotFoundError(
            f"preset {preset_name!r} not found at {p} — "
            f"available: {sorted(p.stem for p in paths.presets_dir().glob('*.yaml'))}"
        )
    return load_yaml(p)


def write_yaml(path: Path, cfg: InstanceConfig) -> None:
    """
    Serialise an InstanceConfig to a YAML file, creating parent dirs.

    Args:
        path: Destination path (parent directories are created if absent).
        cfg: The config to serialise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))


def render_env(name: str, cfg: InstanceConfig, ports: dict[str, int]) -> str:
    """
    Render the .env file that compose reads via --env-file.

    Every ``${VAR}`` substitution in ``compose.yaml`` must have a
    corresponding line here.

    Args:
        name: Instance name used as the compose project name.
        cfg: The instance configuration.
        ports: Port mapping produced by ``ports.ports_for_index``.

    Returns:
        The rendered ``.env`` content as a newline-terminated string.
    """
    lines = [
        f"INSTANCE_NAME={name}",
        f"BASE_IMAGE={base_image_tag(cfg.android)}",
        f"ADB_PORT={ports['adb']}",
        f"FRIDA_PORT={ports['frida']}",
        f"FRIDA_PORT2={ports['frida2']}",
        f"MEM_LIMIT={cfg.resources.mem}",
        f"CPUS={cfg.resources.cpus}",
        f"SHM_SIZE={cfg.resources.shm}",
        f"PIDS_LIMIT={cfg.resources.pids_limit}",
        f"DISPLAY_WIDTH={cfg.display.width}",
        f"DISPLAY_HEIGHT={cfg.display.height}",
        f"DISPLAY_FPS={cfg.display.fps}",
        f"DISPLAY_GPU={cfg.display.gpu_mode}",
    ]
    if cfg.resources.mem_reservation is not None:
        lines.append(f"MEM_RESERVATION={cfg.resources.mem_reservation}")
    if cfg.resources.memswap_limit is not None:
        lines.append(f"MEMSWAP_LIMIT={cfg.resources.memswap_limit}")
    return "\n".join(lines) + "\n"
