"""
Environment-driven overrides for beetroot.

Settings are read **strictly from the process environment** (no
``.env`` auto-load). All variables are prefixed with ``BEETROOT_``.

Examples::

    BEETROOT_DOCKER_BIN=/usr/local/bin/docker  # override docker binary
    BEETROOT_FRIDA_ARCH=android-arm64          # frida-server arch for ARM VMs
    BEETROOT_HTTP_TIMEOUT=60                   # urllib timeout in seconds

The :class:`Settings` instance is constructed once at import time and is
frozen — any code that wants to override a setting in a test should
do so by setting the env var **before** import, or by replacing the
module-level :data:`settings` object via ``monkeypatch.setattr(<module>,
"settings", Settings(...))``. Direct attribute assignment raises
``ValidationError`` (the frozen flag exists precisely to make
"settings is the source of truth" load-bearing).

v0.3 carried ``env_file=".env"`` in ``SettingsConfigDict``, which
auto-loaded the *current working directory's* .env file at every
``Settings()`` instantiation. Inside an instance directory the
per-instance .env (which is consumed by Docker compose, not Beetroot)
carries keys like ``INSTANCE_NAME``, ``ADB_PORT`` etc. — none of which
match ``BEETROOT_*`` — but the discovery walk + extra-key warnings
tripped on the missing match. v0.4 dropped ``env_file`` entirely
(T2 — Agent 3 §1.5, Agent 4 §4 Issue 1) so Beetroot's CLI overrides
are decoupled from the instance .env contract.
"""

from __future__ import annotations

import pydantic
from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class InvalidSettingsError(RuntimeError):
    """
    Raised when a ``BEETROOT_*`` environment variable fails validation.

    ``Settings()`` is constructed at import time, *before* ``cli.main()``'s
    ``try``/``except`` error boundary is reached, so a raw
    ``pydantic.ValidationError`` (e.g. a non-numeric ``BEETROOT_HTTP_TIMEOUT``)
    would otherwise escape as an unhandled traceback that bricks even
    ``beetroot --help`` (#197). Wrapping construction in this domain error and
    catching it in ``cli.main()`` maps a malformed setting to the friendly
    ``error: ...`` + exit 1 contract the rest of the CLI upholds.
    """


class Settings(BaseSettings):
    """
    Runtime overrides sourced from ``BEETROOT_*`` environment variables.

    The forwarded-to-container variables (``BEETROOT_MAGISK_DB`` /
    ``BEETROOT_MODULES_DIR`` / ``BEETROOT_FRIDA_BIN`` /
    ``BEETROOT_BUILD_CONTEXT``) are declared here so ``extra="forbid"``
    doesn't trip over them when a researcher exports them in their
    shell. The CLI itself doesn't read them — they're plumbed through
    the rendered ``.env`` into the container's helper scripts — but
    accepting them at the settings layer means the host-side
    ``BEETROOT_*`` namespace stays internally consistent.

    Attributes:
        docker_bin: Path or name of the Docker binary (default: ``docker``).
        frida_arch: frida-server architecture suffix
            (default: ``android-x86_64``).
        http_timeout: Timeout in seconds for HTTP downloads (default: ``30``).
        magisk_db: Container path to Magisk's sqlite DB (forwarded to
            ``docker/magisk-config.sh``).
        modules_dir: Container path to the Magisk module staging dir
            (forwarded to ``docker/flash-modules.sh``).
        frida_bin: Container path to the bind-mounted ``frida-server``
            (forwarded to ``docker/launch-frida.sh``).
        build_context: ``docker compose`` project directory used during
            ``beetroot build`` (forwarded into the bundled compose
            template's ``${BEETROOT_BUILD_CONTEXT}`` substitution).
        qemu_bin: Path or name of the QEMU system emulator binary used by
            the ``binder: vm`` micro-VM backend (default:
            ``qemu-system-x86_64``).
        qemu_img_bin: Path or name of the ``qemu-img`` binary used by the
            ``vm.boot_cache`` warm-start path to create the qcow2 overlay and
            inspect its snapshots (default: ``qemu-img``).
        vm_kernel: Default host path to the guest ``bzImage`` for the
            micro-VM backend when an instance's config doesn't pin one
            (empty = require an explicit ``vm.kernel`` in ``beetroot.yaml``).
        vm_rootfs: Default host path to the guest ext4 rootfs image for the
            micro-VM backend when an instance's config doesn't pin one
            (empty = require an explicit ``vm.rootfs`` in ``beetroot.yaml``).
        vm_adb_connect_timeout: Seconds ``VmDeviceBackend.up()`` polls
            ``adb connect`` against the freshly-launched guest before giving
            up (default: ``60``). The guest restarts adbd to enable TCP a few
            seconds *after* ``sys.boot_completed=1``, so the first connect
            races that late bind; ``up`` retries with backoff until the
            endpoint accepts or this deadline elapses. This flat default is the
            KVM (near-native) budget — under TCG software emulation, where a
            cold boot to first ADB is minutes (issue #160), the deadline is
            auto-extended to a boot-completed-scale floor so a slow first boot
            doesn't abort ``up`` before the guest exposes ADB. Bump it to raise
            the floor further on an unusually slow host.
    """

    model_config = SettingsConfigDict(
        env_prefix="BEETROOT_",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    docker_bin: str = "docker"
    frida_arch: str = "android-x86_64"
    http_timeout: PositiveInt = 30
    magisk_db: str = "/data/adb/magisk.db"
    modules_dir: str = "/data/adb/modules_update"
    frida_bin: str = "/data/local/tmp/frida-server"
    build_context: str = ""
    qemu_bin: str = "qemu-system-x86_64"
    qemu_img_bin: str = "qemu-img"
    vm_kernel: str = ""
    vm_rootfs: str = ""
    vm_adb_connect_timeout: PositiveInt = 60


def _build_settings() -> Settings:
    """
    Construct :class:`Settings`, mapping a validation failure to a domain error.

    A malformed ``BEETROOT_*`` env var (e.g. a non-numeric ``BEETROOT_HTTP_TIMEOUT``)
    otherwise raises a raw ``pydantic.ValidationError``. Re-raising as
    :class:`InvalidSettingsError` lets ``cli.main()`` emit the friendly
    ``error: ...`` + exit 1 line instead (#197).

    Returns:
        A validated :class:`Settings` instance.

    Raises:
        InvalidSettingsError: If any ``BEETROOT_*`` env var fails validation.
    """
    try:
        return Settings()
    except pydantic.ValidationError as e:
        raise InvalidSettingsError(f"invalid BEETROOT_* environment variable: {e}") from e


class _LazySettings:
    """
    Import-safe proxy that defers :class:`Settings` construction to first use.

    Consumers bind this proxy via ``from .settings import settings`` at *their*
    import time, but the actual env-var validation only runs when an attribute is
    first read — which happens at CLI *runtime*, inside ``cli.main()``'s error
    boundary. This is what lets a malformed ``BEETROOT_*`` var map to the friendly
    ``error: ...`` + exit 1 line instead of a raw traceback that bricks even
    ``beetroot --help`` at import (#197). The resolved instance is cached, so the
    env is read at most once per process.
    """

    __slots__ = ("_resolved",)

    def __init__(self) -> None:
        self._resolved: Settings | None = None

    def _get(self) -> Settings:
        """
        Return the cached :class:`Settings`, building it on first access.
        """
        if self._resolved is None:
            self._resolved = _build_settings()
        return self._resolved

    def __getattr__(self, name: str) -> object:
        # __slots__ + the leading-underscore guard keep this from recursing on
        # ``self._resolved``; every other attribute routes to the real Settings.
        return getattr(self._get(), name)


# The runtime object is the lazy proxy (import-safe), but every attribute it
# forwards is a real ``Settings`` field, so it's typed as ``Settings`` for
# consumers — mypy sees ``settings.http_timeout: int`` etc.
settings: Settings = _LazySettings()  # type: ignore[assignment]  # proxy forwards to Settings
