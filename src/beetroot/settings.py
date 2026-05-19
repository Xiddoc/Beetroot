"""
Environment-driven overrides for beetroot.

Settings are read **strictly from the process environment** (no
``.env`` auto-load). All variables are prefixed with ``BEETROOT_``.

Examples::

    BEETROOT_DOCKER_BIN=/usr/local/bin/docker  # override docker binary
    BEETROOT_FRIDA_ARCH=android-arm64          # frida-server arch for ARM VMs
    BEETROOT_HTTP_TIMEOUT=60                   # urllib timeout in seconds

T2 Agent 3 1.5 / Agent 4: v0.3 had ``env_file=".env"`` in
``SettingsConfigDict``, which auto-loaded the *current working
directory's* .env file at every ``Settings()`` instantiation. Inside
an instance directory the per-instance .env (which is consumed by
Docker compose, not Beetroot) carries keys like ``INSTANCE_NAME``,
``ADB_PORT`` etc. — none of which match ``BEETROOT_*`` — but the
discovery walk + extra-key warnings tripped on the missing match.
Dropping ``env_file`` decouples Beetroot's CLI overrides from the
instance .env contract entirely.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Runtime overrides sourced from ``BEETROOT_*`` environment variables.

    Attributes:
        docker_bin: Path or name of the Docker binary (default: ``docker``).
        frida_arch: frida-server architecture suffix
            (default: ``android-x86_64``).
        http_timeout: Timeout in seconds for HTTP downloads (default: ``30``).
    """

    model_config = SettingsConfigDict(
        env_prefix="BEETROOT_",
        extra="ignore",
    )

    docker_bin: str = "docker"
    frida_arch: str = "android-x86_64"
    http_timeout: int = 30


settings = Settings()
