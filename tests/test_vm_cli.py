"""Tests for the CLI wiring of the binder: vm micro-VM backend (issue #44)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, config, registry
from beetroot.backends import vm as vm_backend
from beetroot.builder import VmArtifacts
from beetroot.settings import Settings
from beetroot.vm import qemu

runner = CliRunner()


# ---------------------------------------------------------------------------
# beetroot build --vm-kernel
# ---------------------------------------------------------------------------


class TestBuildVmKernel:
    def test_vm_kernel_flag_builds_artifacts(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_vm_kernel") as mock_b:
            mock_b.return_value = VmArtifacts(
                kernel=Path("/c/bzImage"), rootfs=Path("/c/rootdisk.img")
            )
            result = runner.invoke(cli.app, ["build", "--vm-kernel"])
        assert result.exit_code == 0, result.stderr
        mock_b.assert_called_once_with()
        assert "/c/bzImage" in result.stdout
        assert "/c/rootdisk.img" in result.stdout

    def test_vm_kernel_failure_surfaces_error(self, cli_root: Path) -> None:
        from beetroot.builder import BootstrapError

        with patch("beetroot.cli.builder.build_vm_kernel", side_effect=BootstrapError("boom")):
            result = runner.invoke(cli.app, ["build", "--vm-kernel"])
        assert result.exit_code == 1
        assert "boom" in result.stderr

    def test_default_build_is_unaffected(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bi:
            mock_bi.return_value = "redroid/redroid:14.0.0_litegapps_houdini_magisk"
            result = runner.invoke(cli.app, ["build"])
        assert result.exit_code == 0
        mock_bi.assert_called_once_with(gapps="lite")


# ---------------------------------------------------------------------------
# create + apply → vm dispatch
# ---------------------------------------------------------------------------


def _stage_vm_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kernel = tmp_path / "bzImage"
    rootfs = tmp_path / "rootdisk.img"
    kernel.write_bytes(b"k")
    rootfs.write_bytes(b"r")
    monkeypatch.setattr(
        vm_backend, "settings", Settings(vm_kernel=str(kernel), vm_rootfs=str(rootfs))
    )


class TestVmDispatch:
    def test_create_with_vm_binder_registers_vm_kind(self, cli_root: Path) -> None:
        # Write a vm config, register it via adopt (register adopts on-disk yaml).
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        meta = registry.get("vm1")
        assert meta is not None
        assert meta.backend.kind == "vm"
        # Manager.resolve dispatches to the VM backend.
        backend = api.Manager.resolve("vm1")
        assert isinstance(backend, vm_backend.VmDeviceBackend)

    def test_apply_flips_redroid_to_vm(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        # Hand-edit to vm, then apply reconciles the registry kind.
        config.write_yaml(cli_root / "alpha" / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        meta = registry.get("alpha")
        assert meta is not None
        assert meta.backend.kind == "vm"


class TestVmUp:
    def test_up_tcg_prints_loud_banner_and_launches(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_vm_artifacts(monkeypatch, cli_root)
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        launched: list[list[str]] = []

        def _start(_self: object, argv: list[str]) -> int:
            launched.append(argv)
            return 1

        monkeypatch.setattr(qemu.QemuProcess, "start", _start)
        result = runner.invoke(cli.app, ["up", "vm1"])
        assert result.exit_code == 0, result.stderr
        assert "TCG (software)" in result.stderr
        assert "5-20x" in result.stderr
        assert launched  # QEMU was launched
        assert "vm1 up" in result.stdout

    def test_up_kvm_prints_quiet_banner(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_vm_artifacts(monkeypatch, cli_root)
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "kvm")
        monkeypatch.setattr(qemu.QemuProcess, "start", lambda _self, argv: 1)
        result = runner.invoke(cli.app, ["up", "vm1"])
        assert result.exit_code == 0, result.stderr
        assert "KVM (near-native)" in result.stderr
        assert "5-20x" not in result.stderr

    def test_up_explicit_kvm_without_dev_kvm_errors_before_launch(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(
            root / "beetroot.yaml",
            config.InstanceConfig(binder="vm", vm={"accel": "kvm"}),  # type: ignore[arg-type]
        )
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "_dev_kvm_usable", lambda: False)

        def _no_launch(_self: object, _argv: list[str]) -> int:
            raise AssertionError("QEMU must not launch when kvm is demanded but absent")

        monkeypatch.setattr(qemu.QemuProcess, "start", _no_launch)
        result = runner.invoke(cli.app, ["up", "vm1"])
        assert result.exit_code == 1
        assert "/dev/kvm is absent" in result.stderr

    def test_up_missing_artifacts_surfaces_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vm_backend, "settings", Settings(vm_kernel="", vm_rootfs=""))
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        result = runner.invoke(cli.app, ["up", "vm1"])
        assert result.exit_code == 1
        assert "no VM kernel configured" in result.stderr

    def test_up_redroid_with_vm_yaml_demands_apply(self, cli_root: Path) -> None:
        # create registers redroid; hand-edit yaml to vm without apply.
        runner.invoke(cli.app, ["create", "alpha"])
        config.write_yaml(cli_root / "alpha" / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        with patch("subprocess.run") as mock_run:
            result = runner.invoke(cli.app, ["up", "alpha"])
        assert result.exit_code == 1
        assert "beetroot apply alpha" in result.stderr
        assert not mock_run.called


# ---------------------------------------------------------------------------
# doctor + status for vm
# ---------------------------------------------------------------------------


class TestVmDoctorStatus:
    def _make_vm(self, cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")

    def test_doctor_runs_vm_checks(self, cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._make_vm(cli_root, monkeypatch)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: True)
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        result = runner.invoke(cli.app, ["doctor", "vm1"])
        # vm.process passes; vm.accel passes (tcg with note). adb/magisk rows
        # skip because adb isn't really reachable, so exit may be 0 or fail-count.
        assert "vm.process: pass" in result.stdout
        assert "vm.accel: pass" in result.stdout

    def test_status_emits_vm_row(self, cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._make_vm(cli_root, monkeypatch)
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: False)
        result = runner.invoke(cli.app, ["status", "vm1"])
        assert result.exit_code == 0, result.stderr
        row = json.loads(result.stdout)
        assert row["kind"] == "vm"
        assert row["adb_address"] == "localhost:5555"
        assert "serial" not in row
