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

from pydantic_settings import BaseSettings, SettingsConfigDict


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
        vm_kernel: Default host path to the guest ``bzImage`` for the
            micro-VM backend when an instance's config doesn't pin one
            (empty = require an explicit ``vm.kernel`` in ``beetroot.yaml``).
        vm_rootfs: Default host path to the guest ext4 rootfs image for the
            micro-VM backend when an instance's config doesn't pin one
            (empty = require an explicit ``vm.rootfs`` in ``beetroot.yaml``).
    """

    model_config = SettingsConfigDict(
        env_prefix="BEETROOT_",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    docker_bin: str = "docker"
    frida_arch: str = "android-x86_64"
    http_timeout: int = 30
    magisk_db: str = "/data/adb/magisk.db"
    modules_dir: str = "/data/adb/modules_update"
    frida_bin: str = "/data/local/tmp/frida-server"
    build_context: str = ""
    qemu_bin: str = "qemu-system-x86_64"
    vm_kernel: str = ""
    vm_rootfs: str = ""


settings = Settings()
