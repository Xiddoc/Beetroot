"""
beetroot.yaml schema, loading, and .env rendering.

Only ``android.version`` is required; every other field has a sensible
default and can be omitted entirely from an instance YAML. Optional
top-level sections: ``display``, ``resources``, ``frida``, ``modules``,
``magisk``, ``ports``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final, Literal, Self, override

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

SUPPORTED_API_VERSION: Final = 4

# Additive auto-bump: old YAMLs that hard-pinned one of these versions are
# silently upgraded to SUPPORTED_API_VERSION on load with a one-line stderr
# warning. The bumps are strictly additive — no fields renamed, only new
# optional fields / validators added — so these YAMLs remain valid once the
# field is rewritten. Persistence happens organically on the next
# ``beetroot apply`` (which calls :func:`write_yaml`).
#
# v0.4 → v0.4 (api_version 2 → 3): added ``stealth.denylist`` per-package
#   regex validator (strictly additive).
# v0.3 → v0.4 (api_version 1 → 2): added opt-in frida block (strictly
#   additive; old YAMLs without a frida block default to frida=None).
_AUTO_BUMPABLE_API_VERSIONS: Final = frozenset({1, 2, 3})

# Non-additive versions that require an explicit migration rather than a
# silent auto-bump. If a YAML pins one of these and a migration path exists,
# load_yaml raises a clear, actionable migration error naming the renamed /
# removed fields.
#
# api_version 3 → 4: ``stealth.denylist`` moved to ``magisk.denylist``.
# The ``stealth:`` key is now rejected with a migration hint pointing at
# CHANGELOG.md. YAMLs that merely omit ``api_version`` (default=current)
# are unaffected — only those that explicitly wrote ``api_version: 3`` and
# also used ``stealth:`` are covered by this path.
_MIGRATION_REQUIRED_VERSIONS: Final = frozenset[int]()  # none yet beyond auto-bumpable

_VALID_ANDROID_VERSIONS = {11, 12, 13, 14}

_MIN_PORT: Final = 1
_MAX_PORT: Final = 65535

# Magisk/stealth denylist packages must look like a normal Android package
# id: alphanumerics, dots, and underscores only. Pre-validated at
# config-load time as SQL-injection prophylaxis for the wire-up of
# the denylist through ``magisk-config.sh``'s sqlite REPLACE INTO.
_DENYLIST_PKG_RE: Final = re.compile(r"^[a-zA-Z0-9._]+$")

# Frida release tags follow the major.minor.patch shape upstream.
# Pre-validated so a typo in ``frida.version`` (e.g. ``"16.4"`` or
# ``"16.4.10-rc1"``) surfaces at config-load time rather than as a
# 404 from the cdn at download time. (T2 Agent 1.)
_FRIDA_VERSION_RE: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

# Docker size format: a number (optionally with decimal) followed by an
# optional SI/binary suffix. Matches "3g", "512m", "1.5G", "256k", "1024"
# (bare bytes). Rejects free-form strings like "3gb" or "512 mb" that
# docker compose silently misinterprets or rejects opaquely at runtime.
_DOCKER_SIZE_RE: Final = re.compile(r"^\d+(\.\d+)?[bkmgtBKMGT]?$")

# Module-level set of YAML paths we've already printed the "auto-bumped
# api_version" warning for in this process. Without the dedup,
# ``beetroot ls`` over 5 legacy instances prints 5+ warning lines; a
# single ``register bravo`` triple-prints because ``all_resolved_ports``
# cascades into the same load twice. CR #2 finding A2.
_API_VERSION_BUMP_WARNED: set[Path] = set()


class Display(BaseModel):
    """
    Display settings for the virtual Android screen.

    Attributes:
        width: Horizontal resolution in pixels (must be > 0).
        height: Vertical resolution in pixels (must be > 0).
        fps: Frame rate limit (must be > 0).
        gpu_mode: GPU rendering mode passed to redroid (e.g. ``host``).
    """

    width: int = Field(default=540, gt=0)
    height: int = Field(default=960, gt=0)
    fps: int = Field(default=3, gt=0)
    gpu_mode: str = "host"


def _check_docker_size(field_name: str, v: str) -> str:
    if not _DOCKER_SIZE_RE.match(v):
        raise ValueError(
            f"{field_name} {v!r} is not a valid Docker size format. "
            "Use a number followed by a suffix: b, k, m, g, t (case-insensitive). "
            "Examples: '3g', '512m', '1.5G'. "
            "Typos like '3gb' or '512 mb' fail opaquely at 'docker compose up' — "
            "catching them at load time keeps the error actionable."
        )
    return v


class Resources(BaseModel):
    """
    Docker resource caps for the container.

    Attributes:
        mem: Hard memory limit (e.g. ``3g``). Docker size format.
        cpus: CPU cap as a float.
        shared_mem: Shared-memory size (Docker ``shm_size``). Docker size format.
        mem_reservation: Optional soft memory floor. Docker size format.
        memswap_limit: Optional total memory + swap cap. Docker size format.
        pids_limit: Maximum number of PIDs the container can spawn.
    """

    mem: str = "3g"
    cpus: float = 2.0
    shared_mem: str = "256m"
    mem_reservation: str | None = None
    memswap_limit: str | None = None
    pids_limit: int = 4096

    @field_validator("mem", "shared_mem")
    @classmethod
    def _check_size_required(cls, v: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _check_docker_size(f"resources.{field_name}", v)

    @field_validator("mem_reservation", "memswap_limit")
    @classmethod
    def _check_size_optional(cls, v: str | None, info: object) -> str | None:
        if v is None:
            return v
        field_name = getattr(info, "field_name", "field")
        return _check_docker_size(f"resources.{field_name}", v)

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
            Must match ``major.minor.patch`` (the upstream Frida tag
            grammar); typos like ``"16.4"`` or ``"16.4.10-rc1"`` raise
            a ValidationError at load time rather than 404-ing on the
            CDN at ``frida_download.download`` time.
        sha256: Optional expected hex digest of the decompressed
            frida-server binary. ``frida_download.download`` verifies
            the digest against the cached binary when set and raises
            ``ValueError`` on mismatch (defends against a hostile
            mirror replacing the upstream release). Lowercase or
            mixed-case hex are both accepted; comparison is
            case-insensitive.
    """

    version: str = "16.4.10"
    sha256: str | None = None

    @field_validator("version")
    @classmethod
    def _check_version_shape(cls, v: str) -> str:
        if not _FRIDA_VERSION_RE.match(v):
            raise ValueError(
                f"frida.version {v!r} is not a major.minor.patch tag "
                "(e.g. '16.4.10'). Frida releases at "
                "https://github.com/frida/frida/releases follow this "
                "shape; typos surface 404s at download time otherwise."
            )
        return v


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
    def model_post_init(self, _ctx: object) -> None:
        """
        Validate that exactly one of ``url`` or ``path`` is set.

        Args:
            _ctx: Pydantic post-init context (unused).

        Raises:
            ValueError: If neither or both of ``url`` and ``path`` are set,
                or if ``url`` uses a non-http(s) scheme.
        """
        if not self.url and not self.path:
            raise ValueError("module entry must set either `url` or `path`")
        if self.url and self.path:
            raise ValueError("module entry sets both `url` and `path` — pick one")
        # Defence-in-depth: refuse non-http(s) module URLs at validation
        # time. Without this, a malicious beetroot.yaml with
        # ``url: file:///etc/passwd`` would silently exfiltrate that
        # file into the module cache and stage it as a module zip.
        # ``modules_download._fetch_url`` re-checks the same prefix at the
        # call site so a third-party script can't bypass it either.
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError(
                f"module url {self.url!r} uses an unsupported scheme; "
                "only http:// and https:// are allowed"
            )


_DEFAULT_DENYLIST: Final = (
    "com.google.android.gms",
    "com.google.android.gms.unstable",
)


class Magisk(BaseModel):
    """
    Magisk configuration, including the boot-time denylist.

    Attributes:
        denylist: Package names added to Magisk's denylist at boot. Each
            entry must match the Android package-id grammar
            (``[a-zA-Z0-9._]+``) — see :data:`_DENYLIST_PKG_RE`. The
            grammar is enforced at validation time so
            ``magisk-config.sh`` can compose the entries into a SQLite
            REPLACE-INTO statement without escaping; any shape that
            wouldn't be a valid package name today is assumed to be
            either a typo or an injection attempt.
            Defaults to the GMS package pair (the v0.3 helper enrolled
            these unconditionally; the config move keeps the default
            behaviour identical while putting the user in control).
    """

    denylist: list[str] = Field(default_factory=lambda: list(_DEFAULT_DENYLIST))

    @field_validator("denylist")
    @classmethod
    def _check_packages(cls, value: list[str]) -> list[str]:
        for pkg in value:
            if not _DENYLIST_PKG_RE.match(pkg):
                raise ValueError(
                    f"magisk.denylist entry {pkg!r} is not a valid Android "
                    "package id (must match [a-zA-Z0-9._]+)"
                )
        return value


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
    index.

    Attributes:
        adb: Host port for ADB. Stride default: ``5555 + index*10``.
        frida: Host port for Frida data. Stride default: ``27042 + index*10``.
        frida_control: Host port for Frida control (RPC/command channel,
            one above the data port). Stride default: ``27043 + index*10``.
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
            raise ValueError(f"port {v} out of range (must be {_MIN_PORT}..{_MAX_PORT})")
        return v

    @model_validator(mode="after")
    def _check_distinct(self) -> Self:
        values = [v for v in (self.adb, self.frida, self.frida_control) if v is not None]
        if len(values) != len(set(values)):
            raise ValueError("ports.adb / ports.frida / ports.frida_control must be distinct")
        return self


_MIN_SMP: Final = 1
_MIN_MEMORY_MIB: Final = 256


class Vm(BaseModel):
    """
    QEMU micro-VM backend settings, consulted only when ``binder: vm``.

    The micro-VM boots a Beetroot-built guest kernel (``kernel``, a
    ``bzImage`` with binder + binderfs compiled in) on top of an ext4
    rootfs (``rootfs``) that auto-starts redroid inside Docker — the
    proof-of-concept proven in ``docs/design/binderless-hosts-qemu-tcg.md``.
    The host's own kernel binder driver is irrelevant in this mode: the
    guest ships its own.

    Both ``kernel`` and ``rootfs`` are optional in the schema so an empty
    ``vm:`` block (or none at all) is valid; the launcher falls back to the
    ``BEETROOT_VM_KERNEL`` / ``BEETROOT_VM_ROOTFS`` environment defaults
    (see :mod:`beetroot.settings`) and only errors at ``up`` time when
    neither the config nor the env supplies a path.

    Attributes:
        kernel: Host path to the guest ``bzImage``. ``None`` defers to the
            ``BEETROOT_VM_KERNEL`` setting.
        rootfs: Host path to the guest ext4 root image. ``None`` defers to
            the ``BEETROOT_VM_ROOTFS`` setting.
        accel: QEMU accelerator. ``"auto"`` (default) probes ``/dev/kvm``
            and prefers KVM, falling back to TCG; ``"kvm"`` / ``"tcg"``
            force the choice. Explicit ``"kvm"`` on a host without
            ``/dev/kvm`` is a hard error (no silent slow fallback).
        smp: Number of guest vCPUs (``-smp``). Must be >= 1. Default 4.
        memory_mib: Guest RAM in MiB (``-m``). Must be >= 256. Default 8192.
    """

    kernel: str | None = None
    rootfs: str | None = None
    accel: Literal["auto", "kvm", "tcg"] = "auto"
    smp: int = Field(default=4, ge=_MIN_SMP)
    memory_mib: int = Field(default=8192, ge=_MIN_MEMORY_MIB)


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
        magisk: Magisk denylist / root-hiding settings.
        ports: Optional per-instance port overrides. Absent fields fall
            back to the stride-of-10 allocator on the instance's index.
        binder: How redroid obtains the kernel ``binder`` driver it needs
            to boot. ``"auto"`` (default) uses the host kernel's binder
            and *warns* (without aborting) when the host can't provide it
            — preserving the historical behaviour. ``"host"`` is the
            strict variant: ``beetroot up`` refuses to start unless the
            host binder is ready (useful in CI, where a container that
            silently never boots Android is worse than a fast failure).
            ``"vm"`` opts into running redroid inside an emulated QEMU
            micro-VM that ships its own binder-enabled kernel — the path
            for hosts with no host binder at all. The micro-VM *engine*
            now ships: selecting ``"vm"`` boots redroid inside the QEMU
            micro-VM (``VmDeviceBackend``); on a host with ``/dev/kvm`` it
            is near-native, and under TCG it is slow but functional. See
            ``docs/design/binderless-hosts-qemu-tcg.md``. Never silently
            falls back to the slow emulated path — that choice is always
            explicit.
        vm: QEMU micro-VM tunables (kernel/rootfs paths, accelerator,
            vCPUs, memory). Consulted only when ``binder == "vm"``; ignored
            otherwise. Defaults to an all-defaults :class:`Vm` block so a
            YAML can opt into ``binder: vm`` without a ``vm:`` section and
            still rely on the ``BEETROOT_VM_*`` env defaults.
    """

    api_version: int = SUPPORTED_API_VERSION
    android: Android = Field(default_factory=Android)
    display: Display = Field(default_factory=Display)
    resources: Resources = Field(default_factory=Resources)
    frida: Frida | None = None
    modules: list[Module] = Field(default_factory=list)
    magisk: Magisk = Field(default_factory=Magisk)
    ports: Ports = Field(default_factory=Ports)
    binder: Literal["auto", "host", "vm"] = "auto"
    vm: Vm = Field(default_factory=Vm)

    @model_validator(mode="before")
    @classmethod
    def _reject_stealth_key(cls, data: object) -> object:
        if isinstance(data, dict) and "stealth" in data:
            raise ValueError(
                "The 'stealth:' key was removed in api_version 4. "
                "Move 'stealth.denylist' to 'magisk.denylist' and update "
                "'api_version' to 4. See CHANGELOG.md for the migration."
            )
        return data

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

    An empty file is treated as an all-defaults config.

    **Auto-bump (additive versions):** YAMLs that pinned one of the
    versions in :data:`_AUTO_BUMPABLE_API_VERSIONS` are upgraded to
    :data:`SUPPORTED_API_VERSION` on load with a one-line stderr warning,
    because those bumps are strictly additive (no fields renamed). The bump
    is persisted organically on the next ``beetroot apply``.

    **Migration error (non-additive versions):** api_version 3 used
    ``stealth.denylist``; that key was moved to ``magisk.denylist`` in
    api_version 4. A YAML that still contains a ``stealth:`` section raises
    a clear, actionable error naming the renamed field rather than silently
    mis-parsing.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        A validated InstanceConfig populated from the file.

    Raises:
        pydantic.ValidationError: If the YAML is invalid, contains
            ``stealth:`` (renamed to ``magisk:`` in api_version 4), or
            carries an unsupported ``api_version``.
    """
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    if isinstance(raw, dict) and raw.get("api_version") in _AUTO_BUMPABLE_API_VERSIONS:
        # Skip the auto-bump notice when the YAML also contains a ``stealth:``
        # key — that triggers a non-additive migration error below, and printing
        # "auto-upgraded, run apply" before the error is contradictory. The
        # migration error itself is the only message the user should see.
        if "stealth" not in raw:
            # Dedup the warning by absolute path. ``beetroot ls`` over N
            # legacy instances would otherwise print N copies of the line,
            # and a single ``register bravo`` triple-prints because
            # ``all_resolved_ports`` cascades into the same load twice.
            resolved = path.resolve()
            old_version = raw["api_version"]
            if resolved not in _API_VERSION_BUMP_WARNED:
                print(  # noqa: T201  # stderr migration hint — typer.echo is unavailable from non-CLI callers
                    f"[beetroot] auto-upgraded api_version {old_version} → "
                    f"{SUPPORTED_API_VERSION} in {path}; run 'beetroot apply' "
                    f"to rewrite the YAML.",
                    file=sys.stderr,
                )
                _API_VERSION_BUMP_WARNED.add(resolved)
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


_STEALTH_PATH_DEFAULTS: Final = {
    "magisk_db": "/data/adb/magisk.db",
    "modules_dir": "/data/adb/modules_update",
    "frida_bin": "/data/local/tmp/frida-server",
}

# Map ``stealth_paths`` keys to the ``BEETROOT_*`` env-var names the
# bundled compose template + boot helpers consume. The key vocabulary
# (``magisk_db`` / ``modules_dir`` / ``frida_bin``) matches the
# snapshot manifest's ``path_layout`` field naming so a v0.6 snapshot
# round-trips through restore without any per-key renaming.
_STEALTH_PATH_ENV_KEYS: Final = {
    "magisk_db": "BEETROOT_MAGISK_DB",
    "modules_dir": "BEETROOT_MODULES_DIR",
    "frida_bin": "BEETROOT_FRIDA_BIN",
}


def render_env(
    name: str,
    cfg: InstanceConfig,
    ports: dict[str, int],
    stealth_paths: dict[str, str] | None = None,
) -> str:
    """
    Render the .env file that compose reads via --env-file.

    Every ``${VAR}`` substitution in ``compose.yaml`` must have a
    corresponding line here.

    Args:
        name: Instance name used as the compose project name.
        cfg: The instance configuration.
        ports: Resolved port mapping produced by ``ports.resolve_ports``.
            Must contain keys ``adb``, ``frida``, and ``frida_control``.
        stealth_paths: Optional per-instance override blob (T4) carrying
            the ``magisk_db`` / ``modules_dir`` / ``frida_bin`` keys.
            Each key present here overrides the corresponding
            ``BEETROOT_*`` default; absent keys fall back to the
            well-known v0.4 defaults
            (``/data/adb/magisk.db`` / ``/data/adb/modules_update`` /
            ``/data/local/tmp/frida-server``). ``None`` and ``{}``
            both mean "use defaults" — the helper merges either form
            against ``_STEALTH_PATH_DEFAULTS`` so callers can pass the
            ``RedroidBackendConfig.stealth_paths`` blob verbatim.
            Unknown keys are silently ignored (so a v0.6-shaped blob
            carrying a future ``stealth_module_id`` key restores
            cleanly against a v0.4 ``render_env``).

    Returns:
        The rendered ``.env`` content as a newline-terminated string.
    """
    resolved_paths = {**_STEALTH_PATH_DEFAULTS, **(stealth_paths or {})}
    lines = [
        f"INSTANCE_NAME={name}",
        f"BASE_IMAGE={base_image_tag(cfg.android)}",
        f"ADB_PORT={ports['adb']}",
        f"FRIDA_PORT={ports['frida']}",
        f"FRIDA_PORT_CONTROL={ports['frida_control']}",
        f"MEM_LIMIT={cfg.resources.mem}",
        f"CPUS={cfg.resources.cpus}",
        f"SHM_SIZE={cfg.resources.shared_mem}",
        f"PIDS_LIMIT={cfg.resources.pids_limit}",
        f"DISPLAY_WIDTH={cfg.display.width}",
        f"DISPLAY_HEIGHT={cfg.display.height}",
        f"DISPLAY_FPS={cfg.display.fps}",
        f"DISPLAY_GPU={cfg.display.gpu_mode}",
        # Encoded as a comma-separated list because toybox sh has no array
        # support — the helper iterates over ``IFS=,``. Per-package shape is
        # already validated by the ``Magisk._check_packages`` regex, so we
        # can safely join with a delimiter that's not in the package-id grammar.
        f"BEETROOT_DENYLIST_PACKAGES={','.join(cfg.magisk.denylist)}",
        # v0.4 stealth-posture overrides — emitted with the known-safe
        # defaults. render_env is the single source of truth instead of the
        # YAML's ${VAR:-default} fallback. A future release's stealth work
        # flips the default in ``Instance.create``'s generator once stealth
        # research validates a safe layout.
        f"BEETROOT_MAGISK_DB={resolved_paths['magisk_db']}",
        f"BEETROOT_MODULES_DIR={resolved_paths['modules_dir']}",
        f"BEETROOT_FRIDA_BIN={resolved_paths['frida_bin']}",
    ]
    if cfg.resources.mem_reservation is not None:
        lines.append(f"MEM_RESERVATION={cfg.resources.mem_reservation}")
    if cfg.resources.memswap_limit is not None:
        lines.append(f"MEMSWAP_LIMIT={cfg.resources.memswap_limit}")
    return "\n".join(lines) + "\n"
