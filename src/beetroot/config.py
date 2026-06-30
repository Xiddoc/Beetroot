"""
beetroot.yaml schema, loading, and .env rendering.

Only ``android.version`` is required; every other field has a sensible
default and can be omitted entirely from an instance YAML. Optional
top-level sections: ``display``, ``resources``, ``frida``, ``modules``,
``magisk``, ``ports``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Self, override

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from . import console

if TYPE_CHECKING:
    from collections.abc import Sequence

    from . import ports as ports_mod

SUPPORTED_API_VERSION: Final = 8

# Additive auto-bump: old YAMLs that hard-pinned one of these versions are
# silently upgraded to SUPPORTED_API_VERSION on load with a one-line stderr
# warning, *unless* they also carry a key that a non-additive bump renamed (the
# stealth / gpu_mode / gapps handling below), in which case a migration error
# fires instead. Persistence happens organically on the next ``beetroot apply``
# (which calls :func:`write_yaml`).
#
# next (api_version 7 → 8): generalised ``ports`` from a fixed mapping of three
#   well-known host overrides (``ports: {adb, frida, frida_control}``) into a
#   **list** of named guest→host mappings (``ports: [{service, guest, host}]``)
#   supporting arbitrary services and explicit guest ports (issue #108). The
#   default seeds the three well-known services (adb 5555, frida 27042,
#   frida_control 27043, all host=None → stride-allocated). An old mapping-form
#   ``ports`` is translated losslessly into the seeded list with the well-known
#   host overrides applied (one-line note, then auto-bumps); a mapping carrying
#   any key that is NOT adb/frida/frida_control raises a migration error naming
#   the new list shape. A YAML pinning api_version 7 with a list-form (or absent)
#   ``ports`` auto-bumps silently.
# next (api_version 6 → 7): split ``android.gapps`` into an intent vocabulary
#   (none/minimal/full) plus an optional ``android.gapps_vendor`` escape hatch
#   (litegapps/opengapps/mindthegapps). ``gapps: none`` and ``gapps: full`` keep
#   working unchanged; a YAML that wrote the now-vendor values ``gapps: lite`` or
#   ``gapps: mindthegapps`` gets a migration error (mirrors the gpu_mode rename).
# v0.6 → next (api_version 5 → 6): added the top-level ``lifecycle: ephemeral |
#   durable`` intent field (default ``durable``, strictly additive — old YAMLs
#   without it default to durable, today's contract). Bumps silently.
# v0.6 → next (api_version 4 → 5): renamed ``display.gpu_mode`` to
#   ``display.rendering`` with an intent vocabulary (gpu/software/auto). A YAML
#   that does NOT use ``display.gpu_mode`` bumps silently; one that DOES gets a
#   migration error (mirrors the 3 → 4 stealth handling).
# v0.4 → v0.4 (api_version 2 → 3): added ``stealth.denylist`` per-package
#   regex validator (strictly additive).
# v0.3 → v0.4 (api_version 1 → 2): added opt-in frida block (strictly
#   additive; old YAMLs without a frida block default to frida=None).
_AUTO_BUMPABLE_API_VERSIONS: Final = frozenset({1, 2, 3, 4, 5, 6, 7})

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

# The single source of truth for the supported Android major versions. To add a
# new version, see "Adding a new Android version" in AGENTS.md — the human-
# readable enumerations elsewhere ("11, 12, 13, or 14") are kept in sync by
# ``tests/test_android_version_extensibility.py``, which fails CI on drift
# (issue #98).
_VALID_ANDROID_VERSIONS = {11, 12, 13, 14}

# Single source of truth for the Android major version a fresh ``beetroot
# create`` defaults to. Reused by the :class:`Android` schema default AND by
# the micro-VM rootfs baker (:func:`beetroot.builder.build_vm_kernel`) so the
# redroid image baked into the guest matches what an all-defaults instance
# expects — see issue #82. Bumping this changes both the config default and
# the default VM redroid base in lock-step.
DEFAULT_ANDROID_VERSION: Final = 14


def validate_android_version(v: int) -> int:
    """
    Validate an Android major version against the supported set.

    Single source of truth for the supported-version check, reused by the
    :class:`Android` schema validator AND by callers outside the config model
    (e.g. ``beetroot build --vm-kernel --android-version N``) that need to
    fail fast before kicking off expensive work.

    Args:
        v: The Android major version to validate.

    Returns:
        ``v`` unchanged when it is one of the supported versions.

    Raises:
        ValueError: If ``v`` is not one of the supported Android versions.
    """
    if v not in _VALID_ANDROID_VERSIONS:
        raise ValueError(
            f"android.version {v!r} is not supported — valid values: "
            + ", ".join(str(x) for x in sorted(_VALID_ANDROID_VERSIONS))
        )
    return v


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

# Symbolic ``frida.version`` values resolved to a concrete tag at staging time
# by :mod:`beetroot.frida_download` (issue #105):
#   * ``auto`` (the default) — match the host's installed ``frida-tools``
#     version so the staged server and the client you'll attach with agree on
#     major+minor; falls back to ``latest`` when ``frida-tools`` isn't installed.
#   * ``latest`` — resolve to the current upstream release at download time.
# A pinned ``major.minor.patch`` still works exactly as before (reproducible).
FRIDA_AUTO: Final = "auto"
FRIDA_LATEST: Final = "latest"
_FRIDA_SYMBOLIC_VERSIONS: Final = frozenset({FRIDA_AUTO, FRIDA_LATEST})


def is_pinned_frida_version(v: str) -> bool:
    """
    Return ``True`` if ``v`` is a concrete ``major.minor.patch`` Frida tag.

    The single source of truth for the pinned-tag shape, shared by the
    :class:`Frida` validator and :mod:`beetroot.frida_download`'s resolver so
    the two never disagree on what counts as "already concrete".

    Args:
        v: The candidate version string.

    Returns:
        ``True`` for a concrete tag (e.g. ``16.4.10``); ``False`` for the
        symbolic ``auto`` / ``latest`` or any malformed value.
    """
    return bool(_FRIDA_VERSION_RE.match(v))


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


# Where DRM render nodes live; their presence means the host has a GPU the
# privileged container can render through. Used by ``rendering: auto`` to pick
# ``gpu`` vs ``software`` instead of blindly assuming a host GPU exists.
_RENDER_NODE_DIR: Final = Path("/dev/dri")
_RENDER_NODE_PATTERN: Final = "renderD*"

# Maps the intent-named ``rendering`` value to redroid's own ``gpu_mode``
# vocabulary (the string that goes into ``androidboot.redroid_gpu_mode``).
# ``auto`` is resolved separately (it probes the host), so it isn't a key here.
_RENDERING_TO_REDROID: Final = {"gpu": "host", "software": "guest"}


def _host_has_render_node() -> bool:
    """Return ``True`` if the host exposes a DRM render node (a usable GPU)."""
    return any(_RENDER_NODE_DIR.glob(_RENDER_NODE_PATTERN))


def resolve_rendering(rendering: str) -> str:
    """
    Map a ``display.rendering`` intent to redroid's ``gpu_mode`` string.

    ``gpu`` → ``host`` (render via the host GPU), ``software`` → ``guest``
    (SwiftShader software rendering), and ``auto`` probes the host for a DRM
    render node — picking ``host`` when one exists, else ``guest`` — so a
    headless / GPU-less box renders in software instead of silently misbehaving.

    Args:
        rendering: The validated ``display.rendering`` value.

    Returns:
        The redroid ``gpu_mode`` string (``host`` or ``guest``).
    """
    if rendering in _RENDERING_TO_REDROID:
        return _RENDERING_TO_REDROID[rendering]
    return "host" if _host_has_render_node() else "guest"


class Display(BaseModel):
    """
    Display settings for the virtual Android screen.

    Attributes:
        width: Horizontal resolution in pixels (must be > 0).
        height: Vertical resolution in pixels (must be > 0).
        fps: Frame rate limit (must be > 0).
        rendering: How redroid renders the framebuffer — the speed-vs-portability
            axis, expressed as intent rather than redroid's ``host``/``guest``
            vocabulary. ``gpu`` renders via the host GPU (fast, assumes a
            GPU-capable host); ``software`` uses SwiftShader (always works,
            slower); ``auto`` (the default) probes for a host render node and
            picks ``gpu`` when present, else ``software`` — so a headless box
            doesn't silently assume a GPU. Mapped to redroid's ``gpu_mode`` via
            :func:`resolve_rendering` at ``.env`` render time.
    """

    width: int = Field(default=540, gt=0)
    height: int = Field(default=960, gt=0)
    fps: int = Field(default=3, gt=0)
    rendering: Literal["gpu", "software", "auto"] = "auto"

    @model_validator(mode="before")
    @classmethod
    def _reject_gpu_mode(cls, data: object) -> object:
        if isinstance(data, dict) and "gpu_mode" in data:
            raise ValueError(
                "display.gpu_mode was renamed to display.rendering in api_version 5. "
                "Replace it with `rendering: gpu` (was gpu_mode: host), "
                "`rendering: software` (was gpu_mode: guest), or `rendering: auto`, "
                f"and set `api_version: {SUPPORTED_API_VERSION}`. "
                "See CHANGELOG.md for the migration."
            )
        return data


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
        mem: Hard memory limit (e.g. ``3g``). Docker size format. This is the
            Docker container cap, authoritative for redroid (``binder:
            auto``/``host``); for ``binder: vm`` the guest RAM is
            :attr:`Vm.memory_mib` (the QEMU ``-m``). Both knobs are
            intentionally kept (issue #104) — collapsing into one field is
            deferred.
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
    Frida-server version selection for an instance.

    Attributes:
        version: Which frida-server release to stage. One of:
            ``auto`` (the default) — match the host's installed
            ``frida-tools`` version, falling back to ``latest`` when it
            isn't installed; ``latest`` — the current upstream release,
            resolved at download time; or a pinned ``major.minor.patch``
            tag (e.g. ``16.4.10``) for reproducibility. ``auto`` / ``latest``
            are resolved to a concrete tag by
            :mod:`beetroot.frida_download` at staging time; a malformed
            pinned tag (``"16.4"``, ``"16.4.10-rc1"``) raises a
            ValidationError at load time rather than 404-ing on the CDN.
        sha256: Optional expected hex digest of the decompressed
            frida-server binary. ``frida_download.download`` verifies
            the digest against the cached binary when set and raises
            ``ValueError`` on mismatch (defends against a hostile
            mirror replacing the upstream release). Only meaningful with a
            pinned ``version`` — a digest can't match the moving target
            ``auto`` / ``latest`` resolve to, so that combination is
            rejected at load time. Lowercase or mixed-case hex are both
            accepted; comparison is case-insensitive.
    """

    version: str = FRIDA_AUTO
    sha256: str | None = None

    @field_validator("version")
    @classmethod
    def _check_version_shape(cls, v: str) -> str:
        if v in _FRIDA_SYMBOLIC_VERSIONS or is_pinned_frida_version(v):
            return v
        raise ValueError(
            f"frida.version {v!r} is not '{FRIDA_AUTO}', '{FRIDA_LATEST}', or a "
            "major.minor.patch tag (e.g. '16.4.10'). Frida releases at "
            "https://github.com/frida/frida/releases follow the pinned shape; "
            "typos surface 404s at download time otherwise."
        )

    @model_validator(mode="after")
    def _reject_sha256_with_symbolic_version(self) -> Self:
        if self.sha256 is not None and self.version in _FRIDA_SYMBOLIC_VERSIONS:
            raise ValueError(
                f"frida.sha256 pins the digest of one specific build, so it "
                f"requires a pinned frida.version; '{self.version}' resolves to a "
                "moving target. Pin a major.minor.patch version, or drop sha256."
            )
        return self


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


# GApps intent — "what the user gets", the axis 95% of users touch.
GappsIntent = Literal["none", "minimal", "full"]
# GApps vendor — "how we bake it", the optional compatibility escape hatch.
# These name the actual distributions the ``beetroot build`` patcher knows how
# to install.
GappsVendor = Literal["litegapps", "opengapps", "mindthegapps"]

# The redroid base-image tag slug each vendor contributes. The single source of
# truth shared by :func:`base_image_tag` and the ``beetroot build`` patcher's
# flag table (``builder.GAPPS_VENDOR_FLAGS``), so a vendor's tag and its
# patcher flag never drift apart.
_VENDOR_SLUG: Final[dict[GappsVendor, str]] = {
    "litegapps": "_litegapps",
    "opengapps": "_gapps",
    "mindthegapps": "_mindthegapps",
}

# The default vendor each non-``none`` intent resolves to when the user doesn't
# pin one explicitly. ``minimal`` → LiteGApps (a slim Play Services), ``full``
# → OpenGApps full suite. These preserve the historical tags exactly: the old
# ``gapps: lite`` baked LiteGApps and the old ``gapps: full`` baked OpenGApps.
_INTENT_DEFAULT_VENDOR: Final[dict[str, GappsVendor]] = {
    "minimal": "litegapps",
    "full": "opengapps",
}

# The two pre-split ``gapps`` values that named a vendor rather than an intent.
# A YAML still carrying one of these gets a migration error pointing at the
# new intent + ``gapps_vendor`` shape (mirrors the gpu_mode → rendering rename).
_LEGACY_GAPPS_VENDOR_VALUES: Final = {"lite": "litegapps", "mindthegapps": "mindthegapps"}


class Android(BaseModel):
    """
    Android version and GApps selection.

    GApps is split across two axes (issue #107): ``gapps`` is the **intent**
    (what the user gets — nothing / a minimal Play Services / the full suite),
    and ``gapps_vendor`` is an optional **escape hatch** for the compatibility
    case where an app prefers a specific distribution. Most users only set
    ``gapps``; Beetroot picks a sensible vendor for the chosen intent.

    Attributes:
        version: Android major version (11, 12, 13, or 14).
        gapps: GApps intent — ``none`` (no Play Services), ``minimal`` (a slim
            Play Services, the default), or ``full`` (the full suite). Resolved
            to a concrete vendor via :func:`resolve_gapps_vendor`.
        gapps_vendor: Optional vendor override (``litegapps`` / ``opengapps`` /
            ``mindthegapps``). ``None`` (the default) lets the intent pick the
            vendor. Setting it pins a specific distribution for app
            compatibility; it must not be combined with ``gapps: none`` (naming
            a vendor while asking for no GApps is contradictory).
    """

    version: int = DEFAULT_ANDROID_VERSION
    gapps: GappsIntent = "minimal"
    gapps_vendor: GappsVendor | None = None

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        return validate_android_version(v)

    @model_validator(mode="before")
    @classmethod
    def _reject_base_image(cls, data: object) -> object:
        if isinstance(data, dict) and "base_image" in data:
            raise ValueError(
                "android.base_image is no longer supported — replace with "
                "`android.version: 14`. See CHANGELOG."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_gapps_vendor_value(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("gapps") in _LEGACY_GAPPS_VENDOR_VALUES:
            old = data["gapps"]
            vendor = _LEGACY_GAPPS_VENDOR_VALUES[old]
            intent = "full" if old == "mindthegapps" else "minimal"
            raise ValueError(
                f"android.gapps: {old!r} was split into an intent + vendor in "
                f"api_version {SUPPORTED_API_VERSION} (issue #107). {old!r} named a "
                f"vendor, not an intent. Replace it with `gapps: {intent}` plus "
                f"`gapps_vendor: {vendor}` (identical base image), and set "
                f"`api_version: {SUPPORTED_API_VERSION}`. See CHANGELOG.md."
            )
        return data

    @model_validator(mode="after")
    def _reject_vendor_with_none(self) -> Self:
        if self.gapps == "none" and self.gapps_vendor is not None:
            raise ValueError(
                f"android.gapps_vendor: {self.gapps_vendor!r} names a GApps "
                "distribution, but android.gapps: none asks for no GApps at all. "
                "Drop gapps_vendor, or set gapps to minimal/full."
            )
        return self


def resolve_gapps_vendor(android: Android) -> GappsVendor | None:
    """
    Resolve the effective GApps vendor for an Android config.

    An explicit ``gapps_vendor`` always wins. Otherwise the intent picks the
    vendor: ``none`` → ``None`` (no GApps), ``minimal`` → LiteGApps, ``full``
    → OpenGApps.

    Args:
        android: The Android section of an InstanceConfig.

    Returns:
        The vendor name (a key of :data:`_VENDOR_SLUG`), or ``None`` when the
        intent is ``none``.
    """
    if android.gapps_vendor is not None:
        return android.gapps_vendor
    if android.gapps == "none":
        return None
    return _INTENT_DEFAULT_VENDOR[android.gapps]


def base_image_tag(android: Android) -> str:
    """
    Derive the redroid base-image tag from version + resolved GApps vendor.

    Args:
        android: The Android section of an InstanceConfig.

    Returns:
        The Docker image tag, e.g.
        ``redroid/redroid:14.0.0_litegapps_houdini_magisk``.
    """
    vendor = resolve_gapps_vendor(android)
    slug = "" if vendor is None else _VENDOR_SLUG[vendor]
    return f"redroid/redroid:{android.version}.0.0{slug}_houdini_magisk"


def vm_redroid_image(version: int) -> str:
    """
    Derive the *plain* redroid image the micro-VM guest bakes for an Android version.

    Unlike :func:`base_image_tag` (which names the Magisk + GApps + Houdini
    layered base image built by ``beetroot build``), the ``binder: vm`` guest
    runs an unmodified upstream redroid image pulled straight from Docker Hub.
    Those tags carry a ``-latest`` suffix (e.g. ``11.0.0-latest``); the bare
    ``X.0.0`` tag does not exist on Docker Hub — see
    ``docs/design/vm-rnd-log.md``.

    Args:
        version: Android major version (11, 12, 13, or 14).

    Returns:
        The plain redroid image reference, e.g. ``redroid/redroid:14.0.0-latest``.
    """
    return f"redroid/redroid:{version}.0.0-latest"


# The three services Beetroot has always known about and stride-allocates by
# name. ``well_known`` (in ports.py) and the back-compat mapping translation
# below both key off this set; the values are the guest ports the redroid
# container exposes (ADB 5555, Frida data 27042, Frida control 27043).
WELL_KNOWN_SERVICES: Final[dict[str, int]] = {
    "adb": 5555,
    "frida": 27042,
    "frida_control": 27043,
}


def _default_port_mappings() -> list[PortMapping]:
    """
    Seed the three well-known services as auto-allocated mappings.

    Each seeded entry pins the well-known guest port and leaves ``host``
    unset so the stride-of-10 allocator assigns it from the instance index.
    """
    return [
        PortMapping(service=service, guest=guest) for service, guest in WELL_KNOWN_SERVICES.items()
    ]


class PortMapping(BaseModel):
    """
    A single guest→host port mapping for an instance.

    Generalises the pre-v8 fixed ``ports: {adb, frida, frida_control}``
    block (issue #108) into an arbitrary, named guest→host mapping. The
    three well-known services (``adb`` / ``frida`` / ``frida_control``) are
    seeded by default and stride-allocated on the instance index when
    ``host`` is left unset; any other entry whose ``host`` is unset is
    auto-allocated from a dedicated extra-pool band (see
    :func:`beetroot.ports.resolve_ports`).

    Attributes:
        service: Optional label. ``adb`` / ``frida`` / ``frida_control``
            are the well-known names the stride allocator and the
            ``adb_address`` / ``frida_address`` accessors key off; any other
            string is a free-form label for an arbitrary mapping. ``None``
            is allowed for an unlabelled arbitrary mapping.
        guest: The container-side (guest) port this mapping exposes
            (1..65535). Required.
        host: The host-side port. ``None`` (the default) auto-allocates —
            a stride base for a well-known service, an extra-pool slot
            otherwise. An explicit value pins the host port (1..65535).
    """

    service: str | None = None
    guest: int
    host: int | None = None

    @field_validator("guest")
    @classmethod
    def _check_guest_range(cls, v: int) -> int:
        if not (_MIN_PORT <= v <= _MAX_PORT):
            raise ValueError(f"ports guest {v} out of range (must be {_MIN_PORT}..{_MAX_PORT})")
        return v

    @field_validator("host")
    @classmethod
    def _check_host_range(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if not (_MIN_PORT <= v <= _MAX_PORT):
            raise ValueError(f"ports host {v} out of range (must be {_MIN_PORT}..{_MAX_PORT})")
        return v


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
        smp: Number of guest vCPUs (``-smp``). Either an explicit integer
            (must be >= 1) or ``"auto"`` (the default), which pins ``-smp``
            to the host's *physical* core count at launch (HyperThread
            siblings collapsed, capped by CPU affinity so a cgroup-limited
            CI runner is respected). The vm-rnd-log §B.5 sweep showed the
            real redroid boot scales with vCPUs up to the host core count and
            regresses past it (oversubscription → cross-thread TCG sync
            overhead); counting *physical* cores avoids the regression a
            logical-CPU count would hit on a hyperthreaded host. Pin an
            explicit value to leave host cores free or to override.
        memory_mib: Guest RAM in MiB (``-m``). Must be >= 256. Default 8192.
            Authoritative for ``binder: vm``; :attr:`Resources.mem` is the
            Docker cap used by ``binder: auto``/``host``. Both knobs kept by
            decision in #104.
        boot_cache: Opt into the warm-start boot cache (default ``False``).
            When ``True``, the first ``beetroot up`` cold-boots through a
            qcow2 overlay and checkpoints the running machine state with QEMU
            ``savevm``; every subsequent ``up`` *resumes* that checkpoint
            (``-loadvm``) instead of cold-booting — ~10 s vs ~minutes under
            TCG (issue #49/#83). The checkpoint lives in the instance
            directory (``vm-overlay.qcow2``) and auto-invalidates when the
            kernel/rootfs changes — a digest of both is recorded beside it, so
            the next ``up`` after a ``build --vm-kernel`` cold-boots once to
            re-cache (issue #126); delete it by hand to reset otherwise. Resume
            reverts the guest to the checkpoint each time, so it is a fast
            *known-good boot*, not a persistence mechanism. Requires ``qemu-img``.
    """

    kernel: str | None = None
    rootfs: str | None = None
    accel: Literal["auto", "kvm", "tcg"] = "auto"
    smp: int | Literal["auto"] = "auto"
    memory_mib: int = Field(default=8192, ge=_MIN_MEMORY_MIB)
    boot_cache: bool = False

    @field_validator("smp")
    @classmethod
    def _validate_smp(cls, value: int | Literal["auto"]) -> int | Literal["auto"]:
        """
        Accept ``"auto"`` or an explicit vCPU count >= ``_MIN_SMP``.

        ``Field(ge=...)`` can't express "either a literal string or a bounded
        int", so the lower bound on the explicit-integer case is enforced
        here instead (mirroring the old ``Field(ge=_MIN_SMP)`` rejection of
        ``smp: 0`` / negatives, while letting ``smp: auto`` through).
        """
        if value == "auto":
            return value
        if value < _MIN_SMP:
            raise ValueError(f"vm.smp must be >= {_MIN_SMP} or 'auto', got {value!r}")
        return value


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
        lifecycle: Whether this instance's ``/data`` is meant to **survive**
            (``durable``, the default — a long-lived "research phone") or is
            **throwaway** (``ephemeral`` — CI/E2E, comparative fleets, reset
            between runs). This is a **label + guardrails, not a runtime
            persistence switch**: ``beetroot down`` never wipes ``/data`` for
            either value; only ``destroy`` / ``reset`` (and a ``vm.boot_cache``
            warm resume) drop it. The label drives intent-aware behaviour —
            ``destroy`` escalates its confirmation for a ``durable`` instance,
            and an ``ephemeral`` instance opts into ``vm.boot_cache``'s
            revert-on-resume **quietly** (the #123 advisory is suppressed,
            because a reset each boot is exactly what ``ephemeral`` asked for).
            ``durable`` preserves today's contract exactly.
        android: Android version and GApps flavour.
        display: Virtual screen geometry and frame rate.
        resources: Docker resource caps.
        frida: Frida-server version pin; ``None`` (the default) disables
            frida entirely. Declare an explicit ``frida:`` block to opt in.
        modules: Magisk modules to flash at boot.
        magisk: Magisk denylist / root-hiding settings.
        ports: List of guest→host :class:`PortMapping` entries. Defaults to
            the three well-known services (``adb`` / ``frida`` /
            ``frida_control``) with auto-allocated host ports. Entries whose
            ``host`` is unset fall back to the stride-of-10 allocator (for a
            well-known service) or a dedicated extra-pool band (for an
            arbitrary service) on the instance's index. The old mapping form
            (``ports: {adb: ...}``) is migrated to this list on load.
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
    lifecycle: Literal["ephemeral", "durable"] = "durable"
    android: Android = Field(default_factory=Android)
    display: Display = Field(default_factory=Display)
    resources: Resources = Field(default_factory=Resources)
    frida: Frida | None = None
    modules: list[Module] = Field(default_factory=list)
    magisk: Magisk = Field(default_factory=Magisk)
    ports: list[PortMapping] = Field(default_factory=_default_port_mappings)
    binder: Literal["auto", "host", "vm"] = "auto"
    vm: Vm = Field(default_factory=Vm)

    @model_validator(mode="before")
    @classmethod
    def _reject_stealth_key(cls, data: object) -> object:
        if isinstance(data, dict) and "stealth" in data:
            raise ValueError(
                "The 'stealth:' key was removed in api_version 4. "
                "Move 'stealth.denylist' to 'magisk.denylist' and set "
                f"'api_version' to {SUPPORTED_API_VERSION}. "
                "See CHANGELOG.md for the migration."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_ports_mapping(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw_ports = data.get("ports")
        if not isinstance(raw_ports, dict):
            # Already a list (new form), absent, or some other shape pydantic
            # will reject downstream — nothing to migrate.
            return data
        # An empty *mapping* means "use defaults": drop the key entirely so the
        # ``_default_port_mappings`` default_factory seeds the three well-known
        # services. (A NEW-form explicit ``ports: []`` is a deliberate "forward
        # nothing" and is left untouched above — it never reaches this branch.)
        if not raw_ports:
            return {k: v for k, v in data.items() if k != "ports"}
        unknown = sorted(k for k in raw_ports if k not in WELL_KNOWN_SERVICES)
        if unknown:
            raise ValueError(
                f"ports mapping carries non-well-known key(s) {unknown} that the "
                f"pre-v{SUPPORTED_API_VERSION} dict form could never express. The "
                f"ports schema is now a list of {{service, guest, host}} mappings "
                f"(api_version {SUPPORTED_API_VERSION}, issue #108) — e.g.\n"
                "  ports:\n"
                "    - {service: adb, guest: 5555, host: 9000}\n"
                "    - {guest: 8080, host: 9090}\n"
                "Rewrite the block as a list and set "
                f"api_version: {SUPPORTED_API_VERSION}. See CHANGELOG.md."
            )
        # Translate the well-known host overrides into the seeded list form.
        migrated = [
            {"service": service, "guest": guest, "host": raw_ports.get(service)}
            for service, guest in WELL_KNOWN_SERVICES.items()
        ]
        console.note(
            "migrated legacy ports mapping to the api_version "
            f"{SUPPORTED_API_VERSION} list form; run 'beetroot apply' to rewrite "
            "the YAML."
        )
        return {**data, "ports": migrated}

    @model_validator(mode="after")
    def _check_ports_distinct(self) -> Self:
        services = [m.service for m in self.ports if m.service is not None]
        if len(services) != len(set(services)):
            dupes = sorted({s for s in services if services.count(s) > 1})
            raise ValueError(f"ports has duplicate service name(s): {dupes}")
        guests = [m.guest for m in self.ports]
        if len(guests) != len(set(guests)):
            dupes_g = sorted({g for g in guests if guests.count(g) > 1})
            raise ValueError(f"ports has duplicate guest port(s): {dupes_g}")
        hosts = [m.host for m in self.ports if m.host is not None]
        if len(hosts) != len(set(hosts)):
            dupes_h = sorted({h for h in hosts if hosts.count(h) > 1})
            raise ValueError(f"ports has duplicate explicit host port(s): {dupes_h}")
        return self

    @model_validator(mode="after")
    def _check_required_addressing_services(self) -> Self:
        services = {m.service for m in self.ports if m.service is not None}
        if "adb" not in services:
            raise ValueError(
                "ports must include a mapping with service: adb — every backend "
                "derives its adb_address (and the doctor adb.connect row) from it. "
                "Add `- {service: adb, guest: 5555}` (host optional) to the ports list."
            )
        if self.frida is not None and "frida" not in services:
            raise ValueError(
                "ports must include a mapping with service: frida when a frida: "
                "block is configured — frida_address is derived from it. Add "
                "`- {service: frida, guest: 27042}` (host optional), or drop the "
                "frida: block."
            )
        return self

    @model_validator(mode="after")
    def _check_api_version(self) -> Self:
        if self.api_version != SUPPORTED_API_VERSION:
            raise ValueError(
                f"beetroot.yaml api_version: {self.api_version} is not supported by this "
                f"Beetroot release (expects api_version: {SUPPORTED_API_VERSION}). See "
                f"CHANGELOG.md for the migration."
            )
        return self


def inert_fields(cfg: InstanceConfig) -> list[str]:
    """
    Return human-readable descriptions of beetroot.yaml fields the backend ignores.

    Expresses the field→backend applicability matrix structurally (issue
    #104): given a fully-loaded config, returns one entry per set-but-inert
    field for the ACTIVE backend, each naming the field and why it no-ops.
    Empty list means every set field is honoured. The caller turns a non-empty
    list into a single apply-time advisory.

    Only ``binder: vm`` has inert fields today: it boots an UNMODIFIED
    upstream redroid image (:func:`vm_redroid_image`) with no GApps / Magisk /
    Houdini / Frida layer, so the layered-image knobs (``android.gapps``,
    ``magisk.denylist``) and the whole ``frida:`` block are inert, and only
    adb is forwarded so arbitrary ``ports:`` mappings are dropped (issue #44/
    #108). ``binder: auto``/``host`` honour all of these → empty list.

    Args:
        cfg: The fully-loaded instance config to inspect.

    Returns:
        One human-readable string per set-but-inert field for ``cfg``'s
        active backend; empty when every set field is honoured.
    """
    inert: list[str] = []
    if cfg.binder != "vm":
        return inert
    if cfg.android.gapps != "none":
        inert.append(
            f"android.gapps: {cfg.android.gapps} (the guest boots plain "
            "redroid with no GApps — Play Services will be absent)"
        )
    if cfg.frida is not None:
        inert.append(
            "frida (the network-isolated guest can't reach a frida-server; "
            "the frida verbs are unsupported on binder: vm)"
        )
    if cfg.magisk.denylist and cfg.magisk.denylist != Magisk().denylist:
        inert.append(
            "magisk.denylist (the guest runs plain redroid with no Magisk, "
            "so the denylist is never applied)"
        )
    arbitrary = [m for m in cfg.ports if m.service not in WELL_KNOWN_SERVICES]
    if arbitrary:
        inert.append(
            "arbitrary ports: entries (only adb is forwarded under binder: vm; "
            "extra guest→host mappings are ignored)"
        )
    return inert


def warn_inert_fields(cfg: InstanceConfig, name: str) -> None:
    """
    Emit the single apply-time advisory naming every set-but-inert field.

    Builds the one-shot ``console.note`` from :func:`inert_fields` (issue
    #104) so the message text and the field→backend applicability matrix are
    single-sourced here, in code. Called by every ``apply`` path that can
    change or first observe an instance's effective backend: the redroid
    ``Instance.apply`` (which flips a hand-edited ``binder: vm`` config to the
    VM backend kind — the canonical create-redroid → edit-to-vm → apply flow,
    where the registry still says ``redroid`` so ``Manager.resolve`` never
    reaches the VM backend) and
    :meth:`beetroot.backends.vm.VmDeviceBackend._warn_on_inert_vm_config`
    (re-``apply`` of an already-VM instance). A no-op when nothing is inert
    (every redroid config, and a VM config that sets no layered-image knobs),
    so it is safe to call unconditionally at apply time. Non-fatal note.

    Args:
        cfg: The fully-loaded instance config to inspect.
        name: The instance name, woven into the advisory for context.
    """
    inert = inert_fields(cfg)
    if not inert:
        return
    console.note(
        f"warning: instance {name!r} uses binder: vm, which boots an "
        "unmodified upstream redroid image. These beetroot.yaml settings have "
        "no effect under binder: vm: " + "; ".join(inert) + "."
    )


def load_yaml(path: Path) -> InstanceConfig:
    """
    Load and validate an InstanceConfig from a YAML file.

    An empty file is treated as an all-defaults config.

    **Auto-bump (legacy versions):** YAMLs that pinned one of the
    versions in :data:`_AUTO_BUMPABLE_API_VERSIONS` are upgraded to
    :data:`SUPPORTED_API_VERSION` on load with a one-line stderr warning —
    *unless* they also carry a key that a non-additive bump renamed (see
    below), in which case the migration error fires instead. The bump is
    persisted organically on the next ``beetroot apply``.

    **Migration error (non-additive renames):** ``stealth.denylist`` moved to
    ``magisk.denylist`` in api_version 4, ``display.gpu_mode`` became
    ``display.rendering`` in api_version 5, and ``android.gapps``'s vendor
    values (``lite`` / ``mindthegapps``) were split into an intent +
    ``android.gapps_vendor`` in api_version 7. A YAML that still contains a
    ``stealth:`` section, a ``display.gpu_mode`` key, or a vendor-named
    ``android.gapps`` raises a clear, actionable error naming the renamed field
    rather than silently mis-parsing.

    **Lossless migration (silent / note):** the old mapping-form ``ports``
    (``ports: {adb: 9000}``) was generalised to a list of ``{service, guest,
    host}`` mappings in api_version 8 (issue #108). A well-known mapping is
    translated into the seeded list with the host overrides applied (a
    one-line note) and the version auto-bumps; a mapping with a non-well-known
    key raises a migration error naming the new list shape.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        A validated InstanceConfig populated from the file.

    Raises:
        pydantic.ValidationError: If the YAML is invalid, contains a renamed
            key (``stealth:`` → ``magisk:`` in v4, ``display.gpu_mode`` →
            ``display.rendering`` in v5, a vendor-named ``android.gapps`` →
            ``gapps`` intent + ``gapps_vendor`` in v7), or carries an
            unsupported ``api_version``.
    """
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    if isinstance(raw, dict) and raw.get("api_version") in _AUTO_BUMPABLE_API_VERSIONS:
        # Skip the auto-bump notice when the YAML also contains a key a
        # non-additive bump renamed (``stealth:`` → magisk in v4, or
        # ``display.gpu_mode`` → rendering in v5) — those trigger a migration
        # error below, and printing "auto-upgraded, run apply" before the error
        # is contradictory. The migration error is the only message to show.
        renamed_key_present = (
            "stealth" in raw
            or (isinstance(raw.get("display"), dict) and "gpu_mode" in raw["display"])
            or (
                isinstance(raw.get("android"), dict)
                and raw["android"].get("gapps") in _LEGACY_GAPPS_VENDOR_VALUES
            )
            # A populated old-form ports mapping carrying a non-well-known key
            # triggers the migration error in ``_migrate_legacy_ports_mapping``;
            # printing "auto-upgraded, run apply" before that error is
            # contradictory, so suppress the note here (mirrors gpu_mode/gapps).
            or (
                isinstance(raw.get("ports"), dict)
                and any(k not in WELL_KNOWN_SERVICES for k in raw["ports"])
            )
        )
        if not renamed_key_present:
            # Dedup the warning by absolute path. ``beetroot ls`` over N
            # legacy instances would otherwise print N copies of the line,
            # and a single ``register bravo`` triple-prints because
            # ``all_resolved_ports`` cascades into the same load twice.
            resolved = path.resolve()
            old_version = raw["api_version"]
            if resolved not in _API_VERSION_BUMP_WARNED:
                console.note(
                    f"auto-upgraded api_version {old_version} → "
                    f"{SUPPORTED_API_VERSION} in {path}; run 'beetroot apply' "
                    f"to rewrite the YAML."
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
    stealth_paths: dict[str, str] | None = None,
) -> str:
    """
    Render the .env file that compose reads via --env-file.

    Every ``${VAR}`` substitution in ``compose.yaml`` must have a
    corresponding line here. Ports are intentionally NOT emitted here:
    since v8 the port list is variable-length (issue #108) and a flat
    ``.env`` can't expand into multiple compose YAML list items, so ports
    live in the per-instance ``compose.override.yaml`` rendered by
    :func:`render_compose_ports_override` instead.

    Args:
        name: Instance name used as the compose project name.
        cfg: The instance configuration.
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
        f"MEM_LIMIT={cfg.resources.mem}",
        f"CPUS={cfg.resources.cpus}",
        f"SHM_SIZE={cfg.resources.shared_mem}",
        f"PIDS_LIMIT={cfg.resources.pids_limit}",
        f"DISPLAY_WIDTH={cfg.display.width}",
        f"DISPLAY_HEIGHT={cfg.display.height}",
        f"DISPLAY_FPS={cfg.display.fps}",
        f"DISPLAY_GPU={resolve_rendering(cfg.display.rendering)}",
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


# The compose service name the bundled template defines (and the override
# file must target). Kept here as the single source of truth for the
# override renderer.
_COMPOSE_SERVICE_NAME: Final = "phone"


def render_compose_ports_override(resolved: Sequence[ports_mod.ResolvedPort]) -> str:
    """
    Render the per-instance ``compose.override.yaml`` carrying the port list.

    A flat ``.env`` can't expand into a variable-length compose ``ports:``
    list, so since v8 (issue #108) the resolved host→guest mappings are
    written to a per-instance override file that the CLI layers on top of
    the bundled template with a second ``-f``. The output targets the
    bundled template's ``phone`` service and is deterministic (entries in
    the resolved order) so re-staging an unchanged config produces an
    identical file.

    Args:
        resolved: The resolved port list produced by
            :func:`beetroot.ports.resolve_ports`.

    Returns:
        The YAML override document as a newline-terminated string.
    """
    lines = [
        "services:",
        f"  {_COMPOSE_SERVICE_NAME}:",
        "    ports:",
    ]
    lines.extend(f'      - "{rp.host}:{rp.guest}"' for rp in resolved)
    return "\n".join(lines) + "\n"
