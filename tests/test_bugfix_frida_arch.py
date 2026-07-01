"""Regression tests for #189 — frida-server arch auto-detects from the host.

Before the fix, the arch was hardcoded to ``settings.frida_arch``
(``android-x86_64``) with no host-arch probe, so an aarch64 redroid host/auto
instance staged an x86_64 ELF that never launched in the ARM guest. Now the arch
is backend-aware: ``binder: vm`` is pinned to x86_64, host/auto detects from
``platform.machine()``, and an explicit ``BEETROOT_FRIDA_ARCH`` always wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import frida_download
from beetroot.settings import settings


@pytest.fixture
def instance_root(isolated_registry: Path, tmp_path: Path) -> Path:
    """An empty instance directory under the isolated XDG tree."""
    root = tmp_path / "alpha"
    root.mkdir()
    return root


def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the lazy settings proxy to re-read env after a setenv/delenv."""
    monkeypatch.setattr("beetroot.settings.settings._resolved", None)


@pytest.mark.parametrize("binder", ["auto", "host", "vm"])
def test_explicit_env_arch_always_wins(binder: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEETROOT_FRIDA_ARCH", "android-arm")
    _reset_settings(monkeypatch)
    monkeypatch.setattr("beetroot.frida_download.platform.machine", lambda: "aarch64")
    assert frida_download.resolve_frida_arch(binder) == "android-arm"


@pytest.mark.parametrize("machine", ["x86_64", "amd64", "aarch64", "arm64", "riscv64"])
def test_vm_is_always_x86_64(machine: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEETROOT_FRIDA_ARCH", raising=False)
    _reset_settings(monkeypatch)
    monkeypatch.setattr("beetroot.frida_download.platform.machine", lambda: machine)
    assert frida_download.resolve_frida_arch("vm") == "android-x86_64"


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("aarch64", "android-arm64"),
        ("arm64", "android-arm64"),
        ("x86_64", "android-x86_64"),
        ("amd64", "android-x86_64"),
    ],
)
def test_host_auto_detects_from_machine(
    machine: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BEETROOT_FRIDA_ARCH", raising=False)
    _reset_settings(monkeypatch)
    monkeypatch.setattr("beetroot.frida_download.platform.machine", lambda: machine)
    assert frida_download.resolve_frida_arch("host") == expected
    assert frida_download.resolve_frida_arch("auto") == expected


def test_unknown_machine_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEETROOT_FRIDA_ARCH", raising=False)
    _reset_settings(monkeypatch)
    monkeypatch.setattr("beetroot.frida_download.platform.machine", lambda: "sparc64")
    # No mapping → the ``settings.frida_arch`` default (never worse than before).
    assert frida_download.resolve_frida_arch("auto") == settings.frida_arch


def test_cached_binary_arch_in_filename() -> None:
    p = frida_download.cached_binary("16.4.10", arch="android-arm64")
    assert p.name == "frida-server-16.4.10-android-arm64"


def test_release_url_arch_suffix() -> None:
    url = frida_download.release_url("16.4.10", arch="android-arm64")
    assert url.endswith("frida-server-16.4.10-android-arm64.xz")


def test_stage_for_instance_threads_arm64_arch(
    instance_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BEETROOT_FRIDA_ARCH", raising=False)
    _reset_settings(monkeypatch)
    monkeypatch.setattr("beetroot.frida_download.platform.machine", lambda: "aarch64")

    seen: dict[str, object] = {}

    def _fake_download(version: str, *, expected_sha256: str | None = None) -> Path:
        # ``stage_for_instance`` publishes the resolved arch via the context var.
        arch = frida_download._active_arch.get()
        seen["arch"] = arch
        out = frida_download.cached_binary(version, arch=arch)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-arm64")
        out.chmod(0o755)
        return out

    monkeypatch.setattr("beetroot.frida_download.download", _fake_download)
    monkeypatch.setattr("beetroot.frida_download.host_frida_tools_version", lambda: None)

    dst = frida_download.stage_for_instance(instance_root, "16.4.10", binder="host")
    assert seen["arch"] == "android-arm64"
    assert dst.read_bytes() == b"fake-arm64"
