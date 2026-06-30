"""Tests for the BEETROOT_* env-var override surface.

T2 Agent 3 1.5 / Agent 4: ``Settings`` used to carry
``env_file=".env"`` in its ``SettingsConfigDict``, which auto-loaded
the *current working directory's* .env file at every
``Settings()`` instantiation. Inside an instance directory the
per-instance .env (which is consumed by Docker compose, NOT
Beetroot) carries keys like ``INSTANCE_NAME=foo`` — values Beetroot
must not pick up. Worse, running the CLI from an instance dir whose
.env had any malformed line would crash the import chain.

These tests pin the post-T2 contract: ``Settings`` reads ONLY from
``os.environ``; the cwd's .env is irrelevant.
"""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from beetroot import settings


def test_settings_ignores_cwd_dot_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The current working directory's .env is the per-instance compose
    # env file, NOT a Beetroot CLI config. v0.3 auto-loaded it via
    # ``env_file=".env"`` and would have at least walked the file
    # parsing every line. v0.4 ignores the file entirely.
    (tmp_path / ".env").write_text(
        "INSTANCE_NAME=foo\nADB_PORT=5555\nBEETROOT_DOCKER_BIN=/should-not-leak\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEETROOT_DOCKER_BIN", raising=False)

    s = settings.Settings()
    # ``.env`` is NOT consulted; the in-process default wins.
    assert s.docker_bin == "docker"


def test_settings_still_reads_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Belt-and-suspenders: dropping env_file must NOT have broken the
    # real ``os.environ`` lookup.
    monkeypatch.setenv("BEETROOT_DOCKER_BIN", "/usr/local/bin/docker")
    monkeypatch.setenv("BEETROOT_HTTP_TIMEOUT", "60")
    s = settings.Settings()
    assert s.docker_bin == "/usr/local/bin/docker"
    assert s.http_timeout == 60


def test_settings_construction_inside_instance_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A regression test that mirrors the exact failure mode: an
    # instance directory whose .env has keys that don't match any
    # ``BEETROOT_*`` field. Pre-T2 the pydantic-settings extra-field
    # path would have parsed and rejected (or warned on) each one.
    # Post-T2 ``Settings()`` simply doesn't read the file.
    (tmp_path / ".env").write_text(
        "INSTANCE_NAME=alpha\n"
        "ADB_PORT=5555\n"
        "FRIDA_PORT=27042\n"
        "FRIDA_PORT_CONTROL=27043\n"
        "BEETROOT_DENYLIST_PACKAGES=com.foo,com.bar\n"
    )
    monkeypatch.chdir(tmp_path)
    # Construction must not raise.
    s = settings.Settings()
    assert s.docker_bin == "docker"


def test_settings_vm_defaults() -> None:
    s = settings.Settings()
    assert s.qemu_bin == "qemu-system-x86_64"
    assert s.vm_kernel == ""
    assert s.vm_rootfs == ""


def test_settings_vm_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEETROOT_QEMU_BIN", "/opt/qemu")
    monkeypatch.setenv("BEETROOT_VM_KERNEL", "/img/bzImage")
    monkeypatch.setenv("BEETROOT_VM_ROOTFS", "/img/rootdisk.img")
    s = settings.Settings()
    assert s.qemu_bin == "/opt/qemu"
    assert s.vm_kernel == "/img/bzImage"
    assert s.vm_rootfs == "/img/rootdisk.img"


def test_timeout_defaults_are_positive() -> None:
    s = settings.Settings()
    assert s.http_timeout == 30
    assert s.vm_adb_connect_timeout == 60


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_http_timeout_rejected(bad: int) -> None:
    # A zero/negative timeout flows into urllib.urlopen(timeout=...) and
    # turns every download into an instant failure (#196); reject it at
    # construction instead.
    with pytest.raises(pydantic.ValidationError):
        settings.Settings(http_timeout=bad)


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_vm_adb_connect_timeout_rejected(bad: int) -> None:
    # A zero/negative deadline disables every adb-connect retry in
    # VmDeviceBackend.up() (#196); reject it at construction.
    with pytest.raises(pydantic.ValidationError):
        settings.Settings(vm_adb_connect_timeout=bad)
