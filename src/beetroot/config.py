"""
beetroot.yaml schema, loading, and .env rendering.

Only ``android.version`` is required; every other field has a sensible
default and can be omitted entirely from an instance YAML. Optional
top-level sections: ``display``, ``resources``, ``frida``, ``modules``,
``stealth``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final, Literal, Self, override

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

SUPPORTED_API_VERSION: Final = 2

_VALID_ANDROID_VERSIONS = {11, 12, 13, 14}

_MIN_PORT: Final = 1
_MAX_PORT: Final = 65535


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
        shared_mem: Shared-memory size (Docker ``shm_size``).
        mem_reservation: Optional soft memory floor.
        memswap_limit: Optional total memory + swap cap.
        pids_limit: Maximum number of PIDs the container can spawn.
    """

    mem: str = "3g"
    cpus: float = 2.0
    shared_mem: str = "256m"
    mem_reservation: str | None = None
    memswap_limit: str | None = None
    pids_limit: int = 4096

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_shm(cls, data: object) -> object:
        if isinstance(data, dict) and "shm" in data:
            raise ValueError(
                "resources.shm is no longer supported — rename to resources.shared_mem. "
                "See CHANGELOG.md for the migration."
            )
        return data


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
        path: Path to a local zip file, resolved relative to the instance
            directory (the directory containing this beetroot.yaml).
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


class Ports(BaseModel):
    """
    Optional per-instance port overrides.

    Fields are independently optional — set only the ones you want to pin;
    the rest fall back to the stride-of-10 allocator on the instance's
    index. The ``frida_control`` field overrides the second Frida port
    (``frida2`` in the resolved port dict, the control channel that sits
    one above the data port in the default stride).

    Attributes:
        adb: Host port for ADB. Stride default: ``5555 + index*10``.
        frida: Host port for Frida data. Stride default: ``27042 + index*10``.
        frida_control: Host port for Frida control. Stride default:
            ``27043 + index*10``.
    """

    adb: int | None = None
    frida: int | None = None
    frida_control: int | None = None

    @field_validator("adb", "frida", "frida_control")
    @classmethod
    def _check_port_range(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if not (_MIN_PORT <= v <= _MAX_PORT):
            raise ValueError(
                f"port {v} out of range (must be {_MIN_PORT}..{_MAX_PORT})"
            )
        return v

    @model_validator(mode="after")
    def _check_distinct(self) -> Self:
        values = [v for v in (self.adb, self.frida, self.frida_control) if v is not None]
        if len(values) != len(set(values)):
            raise ValueError(
                "ports.adb / ports.frida / ports.frida_control must be distinct"
            )
        return self


class InstanceConfig(BaseModel):
    """
    The schema of an instance directory's ``beetroot.yaml``.

    Instance ``name`` is not part of the schema — it's the registry key
    that maps a name to this directory's absolute path.

    Each Beetroot release supports exactly one ``api_version``; mismatched
    values fail loud with a pointer to the migration story in
    ``CHANGELOG.md``.

    Attributes:
        api_version: Schema version this YAML targets. Must equal
            :data:`SUPPORTED_API_VERSION`.
        android: Android version and GApps flavour.
        display: Virtual screen geometry and frame rate.
        resources: Docker resource caps.
        frida: Frida-server version pin; ``None`` (the default) disables
            frida entirely. Declare an explicit ``frida:`` block to opt in.
        modules: Magisk modules to flash at boot.
        stealth: Denylist / root-hiding settings.
        ports: Optional per-instance port overrides. Absent fields fall
            back to the stride-of-10 allocator on the instance's index.
    """

    api_version: int = SUPPORTED_API_VERSION
    android: Android = Field(default_factory=Android)
    display: Display = Field(default_factory=Display)
    resources: Resources = Field(default_factory=Resources)
    frida: Frida | None = None
    modules: list[Module] = Field(default_factory=list)
    stealth: Stealth = Field(default_factory=Stealth)
    ports: Ports = Field(default_factory=Ports)

    @model_validator(mode="after")
    def _check_api_version(self) -> Self:
        if self.api_version != SUPPORTED_API_VERSION:
            raise ValueError(
                f"beetroot.yaml api_version: {self.api_version} is not supported by this "
                f"Beetroot release (expects api_version: {SUPPORTED_API_VERSION}). See "
                f"CHANGELOG.md for the migration."
            )
        return self


def load_yaml(path: Path) -> InstanceConfig:
    """
    Load and validate an InstanceConfig from a YAML file.

    An empty file is treated as an all-defaults config. A v0.2 YAML
    that pinned ``api_version: 1`` is auto-bumped to the current
    :data:`SUPPORTED_API_VERSION` with a one-line stderr warning,
    because v0.2 → v2 is strictly additive (no fields renamed). The
    bump is persisted organically on the next ``beetroot apply``
    (which calls :func:`write_yaml`).

    Args:
        path: Absolute path to the YAML file.

    Returns:
        A validated InstanceConfig populated from the file.
    """
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    if isinstance(raw, dict) and raw.get("api_version") == 1:
        print(
            f"[beetroot] auto-upgraded api_version 1 → {SUPPORTED_API_VERSION} "
            f"in {path}; run 'beetroot apply' to rewrite the YAML.",
            file=sys.stderr,
        )
        raw["api_version"] = SUPPORTED_API_VERSION
    return InstanceConfig.model_validate(raw)


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
        ports: Resolved port mapping produced by ``ports.resolve_ports``.

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
        f"SHM_SIZE={cfg.resources.shared_mem}",
        f"PIDS_LIMIT={cfg.resources.pids_limit}",
        f"DISPLAY_WIDTH={cfg.display.width}",
        f"DISPLAY_HEIGHT={cfg.display.height}",
        f"DISPLAY_FPS={cfg.display.fps}",
        f"DISPLAY_GPU={cfg.display.gpu_mode}",
        # v0.4 stealth-posture overrides — emitted empty by default
        # so the bundled compose template's ${VAR:-} fallback is
        # the source of truth. v0.4 sets these from the manifest
        # path_layout. Keeping them in render_env keeps the
        # compose-template / render_env contract symmetric (see
        # tests/test_compose_template_envs.py).
        "BEETROOT_MAGISK_DB=",
        "BEETROOT_MODULES_DIR=",
        "BEETROOT_FRIDA_BIN=",
    ]
    if cfg.resources.mem_reservation is not None:
        lines.append(f"MEM_RESERVATION={cfg.resources.mem_reservation}")
    if cfg.resources.memswap_limit is not None:
        lines.append(f"MEMSWAP_LIMIT={cfg.resources.memswap_limit}")
    return "\n".join(lines) + "\n"
