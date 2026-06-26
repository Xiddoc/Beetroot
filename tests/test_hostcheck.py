"""Tests for :mod:`beetroot.hostcheck` — the host binder-capability probe.

The whole module opts out of the autouse ``_assume_binder_ready`` stub
(see ``conftest.py``) so these tests exercise the *real* probe logic
rather than the pinned-``ready`` test default.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from beetroot import hostcheck

pytestmark = pytest.mark.real_binder


class TestClassify:
    def test_dev_nodes_present_is_ready(self) -> None:
        status = hostcheck._classify(dev_present=True, binderfs=False, kconfig=None)
        assert status.state == "ready"
        assert status.available is True
        assert status.remedy == ""

    def test_binderfs_present_is_ready(self) -> None:
        # binderfs alone is enough — redroid mounts it at boot.
        status = hostcheck._classify(dev_present=False, binderfs=True, kconfig="not-set")
        assert status.state == "ready"
        assert status.available is True

    @pytest.mark.parametrize("kconfig", ["y", "m"])
    def test_module_available_but_unloaded_is_loadable(self, kconfig: str) -> None:
        status = hostcheck._classify(
            dev_present=False,
            binderfs=False,
            kconfig=kconfig,  # type: ignore[arg-type]
        )
        assert status.state == "loadable"
        assert status.available is False
        assert "modprobe binder_linux" in status.remedy
        assert kconfig in status.reason

    def test_compiled_out_is_unsupported(self) -> None:
        status = hostcheck._classify(dev_present=False, binderfs=False, kconfig="not-set")
        assert status.state == "unsupported"
        assert status.available is False
        assert "beetroot adopt" in status.remedy

    def test_unreadable_config_is_unknown(self) -> None:
        status = hostcheck._classify(dev_present=False, binderfs=False, kconfig=None)
        assert status.state == "unknown"
        assert status.available is False


class TestDevBinderPresent:
    def test_true_when_any_node_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            hostcheck, "_BINDER_DEVICE_NODES", (Path("/definitely/nope"), Path("/proc"))
        )
        assert hostcheck._dev_binder_present() is True

    def test_false_when_no_node_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            hostcheck, "_BINDER_DEVICE_NODES", (Path("/definitely/nope"), Path("/also/nope"))
        )
        assert hostcheck._dev_binder_present() is False


class TestBinderfsSupported:
    def test_true_when_listed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        procfs = tmp_path / "filesystems"
        procfs.write_text("nodev\ttmpfs\nnodev\tbinder\n\text4\n")
        monkeypatch.setattr(hostcheck, "_PROC_FILESYSTEMS", procfs)
        assert hostcheck._binderfs_supported() is True

    def test_false_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        procfs = tmp_path / "filesystems"
        # Includes a blank line to exercise the ``fields`` empty guard.
        procfs.write_text("nodev\ttmpfs\n\n\text4\n")
        monkeypatch.setattr(hostcheck, "_PROC_FILESYSTEMS", procfs)
        assert hostcheck._binderfs_supported() is False

    def test_false_when_unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostcheck, "_PROC_FILESYSTEMS", tmp_path / "missing")
        assert hostcheck._binderfs_supported() is False


class TestKernelConfig:
    def _no_proc_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(hostcheck, "_PROC_CONFIG_GZ", tmp_path / "config.gz.missing")

    def test_reads_gzipped_proc_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gz = tmp_path / "config.gz"
        gz.write_bytes(gzip.compress(b"CONFIG_ANDROID_BINDER_IPC=y\n"))
        monkeypatch.setattr(hostcheck, "_PROC_CONFIG_GZ", gz)
        assert hostcheck._kernel_config_binder() == "y"

    def test_falls_back_to_boot_config_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_proc_config(monkeypatch, tmp_path)
        boot = tmp_path / "boot-config"
        boot.write_text("# some\nCONFIG_ANDROID_BINDER_IPC=m\n# more\n")
        monkeypatch.setattr(hostcheck, "_boot_config_path", lambda: boot)
        assert hostcheck._kernel_config_binder() == "m"

    def test_detects_explicit_not_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_proc_config(monkeypatch, tmp_path)
        boot = tmp_path / "boot-config"
        boot.write_text("# CONFIG_ANDROID_BINDER_IPC is not set\n")
        monkeypatch.setattr(hostcheck, "_boot_config_path", lambda: boot)
        assert hostcheck._kernel_config_binder() == "not-set"

    def test_none_when_absent_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_proc_config(monkeypatch, tmp_path)
        boot = tmp_path / "boot-config"
        boot.write_text("CONFIG_SOMETHING_ELSE=y\n")
        monkeypatch.setattr(hostcheck, "_boot_config_path", lambda: boot)
        assert hostcheck._kernel_config_binder() is None

    def test_none_when_nothing_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_proc_config(monkeypatch, tmp_path)
        monkeypatch.setattr(hostcheck, "_boot_config_path", lambda: tmp_path / "missing")
        assert hostcheck._kernel_config_lines() == []
        assert hostcheck._kernel_config_binder() is None

    def test_none_when_value_not_y_or_m(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: the key is present with an unexpected value — neither
        # an enable nor the ``is not set`` comment, so we keep scanning.
        self._no_proc_config(monkeypatch, tmp_path)
        boot = tmp_path / "boot-config"
        boot.write_text('CONFIG_ANDROID_BINDER_IPC=""\n')
        monkeypatch.setattr(hostcheck, "_boot_config_path", lambda: boot)
        assert hostcheck._kernel_config_binder() is None

    def test_boot_config_path_tracks_kernel_release(self) -> None:
        # Exercises the real (un-monkeypatched) path builder.
        import platform

        path = hostcheck._boot_config_path()
        assert path.name == f"config-{platform.uname().release}"
        assert str(path).startswith("/boot/")


class TestBinderStatusIntegration:
    def test_ready_when_dev_node_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostcheck, "_dev_binder_present", lambda: True)
        monkeypatch.setattr(hostcheck, "_binderfs_supported", lambda: False)
        monkeypatch.setattr(hostcheck, "_kernel_config_binder", lambda: None)
        assert hostcheck.binder_status().state == "ready"

    def test_unsupported_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostcheck, "_dev_binder_present", lambda: False)
        monkeypatch.setattr(hostcheck, "_binderfs_supported", lambda: False)
        monkeypatch.setattr(hostcheck, "_kernel_config_binder", lambda: "not-set")
        status = hostcheck.binder_status()
        assert status.state == "unsupported"
        assert status.available is False

    def test_status_is_frozen(self) -> None:
        import pydantic

        status = hostcheck.BinderStatus(state="ready", reason="x", remedy="")
        with pytest.raises(pydantic.ValidationError):
            status.state = "unsupported"  # type: ignore[misc]  # asserting frozen behaviour


class TestPlanBinderRuntime:
    """Pure policy fold: configured binder mode + host status -> plan."""

    def _status(
        self, state: hostcheck.BinderState, *, reason: str = "r", remedy: str = "rem"
    ) -> hostcheck.BinderStatus:
        return hostcheck.BinderStatus(state=state, reason=reason, remedy=remedy)

    def test_vm_mode_always_returns_vm_even_on_ready_host(self) -> None:
        # vm is an explicit opt-in: honoured regardless of host capability.
        plan = hostcheck.plan_binder_runtime("vm", self._status("ready", remedy=""))
        assert plan.action == "vm"
        assert "binderless-hosts-qemu-tcg.md" in plan.reason
        assert plan.remedy == ""

    def test_ready_host_proceeds_under_auto(self) -> None:
        plan = hostcheck.plan_binder_runtime("auto", self._status("ready", remedy=""))
        assert plan.action == "proceed"
        assert plan.remedy == ""

    def test_ready_host_proceeds_under_host(self) -> None:
        plan = hostcheck.plan_binder_runtime("host", self._status("ready", remedy=""))
        assert plan.action == "proceed"

    def test_host_mode_blocks_when_unavailable(self) -> None:
        plan = hostcheck.plan_binder_runtime(
            "host", self._status("unsupported", reason="compiled out", remedy="use adb")
        )
        assert plan.action == "block"
        assert plan.reason == "compiled out"
        assert plan.remedy == "use adb"

    def test_auto_mode_warns_and_appends_vm_hint(self) -> None:
        plan = hostcheck.plan_binder_runtime(
            "auto", self._status("loadable", reason="not loaded", remedy="modprobe it")
        )
        assert plan.action == "warn"
        assert plan.reason == "not loaded"
        assert "modprobe it" in plan.remedy
        assert "binder: vm" in plan.remedy

    def test_auto_mode_warn_with_empty_status_remedy_still_hints_vm(self) -> None:
        plan = hostcheck.plan_binder_runtime(
            "auto", self._status("unknown", reason="cannot tell", remedy="")
        )
        assert plan.action == "warn"
        assert "binder: vm" in plan.remedy
        assert not plan.remedy.startswith(".")
