"""
Environment-driven overrides for beetroot.

Settings are read from the process environment (or a .env file if present).
All variables are prefixed with ``BEETROOT_``.

Examples::

    BEETROOT_DOCKER_BIN=/usr/local/bin/docker  # override docker binary
    BEETROOT_FRIDA_ARCH=android-arm64          # frida-server arch for ARM VMs
    BEETROOT_HTTP_TIMEOUT=60                   # urllib timeout in seconds
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
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    docker_bin: str = "docker"
    frida_arch: str = "android-x86_64"
    http_timeout: int = 30


settings = Settings()
