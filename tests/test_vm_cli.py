"""Tests for the CLI wiring of the binder: vm micro-VM backend (issue #44)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from beetroot import api, builder, cli, config, console, registry
from beetroot.backends import vm as vm_backend
from beetroot.builder import VmArtifacts
from beetroot.settings import Settings
from beetroot.vm import qemu

runner = CliRunner()


# ---------------------------------------------------------------------------
# beetroot build --vm-kernel
# ---------------------------------------------------------------------------


class TestBuildVmKernel:
    @pytest.fixture(autouse=True)
    def _ready_host(self) -> Iterator[None]:
        # These tests exercise the build *dispatch*, not the host preflight
        # (issue #78, tested separately below). A real build now gates only on
        # the lightweight fetch-path preflight; default to a ready host so the
        # build path runs. The preflight-specific tests override this.
        with patch("beetroot.cli.builder.vm_fetch_preflight", return_value=[]):
            yield

    def test_vm_kernel_flag_builds_artifacts(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_vm_kernel") as mock_b:
            mock_b.return_value = VmArtifacts(
                kernel=Path("/c/bzImage"), rootfs=Path("/c/rootdisk.img")
            )
            result = runner.invoke(cli.app, ["build", "--vm-kernel"])
        assert result.exit_code == 0, result.stderr
        mock_b.assert_called_once_with(android_version=14, from_source=False, build_context=None)
        assert "/c/bzImage" in result.stdout
        assert "/c/rootdisk.img" in result.stdout

    def test_vm_kernel_android_version_flag(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_vm_kernel") as mock_b:
            mock_b.return_value = VmArtifacts(
                kernel=Path("/c/bzImage"), rootfs=Path("/c/rootdisk.img")
            )
            result = runner.invoke(cli.app, ["build", "--vm-kernel", "--android-version", "11"])
        assert result.exit_code == 0, result.stderr
        mock_b.assert_called_once_with(android_version=11, from_source=False, build_context=None)

    def test_vm_kernel_from_source_flag(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_vm_kernel") as mock_b:
            mock_b.return_value = VmArtifacts(
                kernel=Path("/c/bzImage"), rootfs=Path("/c/rootdisk.img")
            )
            result = runner.invoke(cli.app, ["build", "--vm-kernel", "--from-source"])
        assert result.exit_code == 0, result.stderr
        mock_b.assert_called_once_with(android_version=14, from_source=True, build_context=None)

    def test_vm_kernel_build_context_flag(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_vm_kernel") as mock_b:
            mock_b.return_value = VmArtifacts(
                kernel=Path("/c/bzImage"), rootfs=Path("/c/rootdisk.img")
            )
            result = runner.invoke(
                cli.app, ["build", "--vm-kernel", "--build-context", "/src/checkout"]
            )
        assert result.exit_code == 0, result.stderr
        mock_b.assert_called_once_with(
            android_version=14, from_source=False, build_context=Path("/src/checkout")
        )

    def test_vm_kernel_invalid_android_version_fails_fast(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_vm_kernel") as mock_b:
            result = runner.invoke(cli.app, ["build", "--vm-kernel", "--android-version", "99"])
        assert result.exit_code == 1
        mock_b.assert_not_called()
        assert "--android-version" in result.stderr
        assert "is not supported" in result.stderr
        # The canonical supported-version set is surfaced verbatim.
        for version in sorted(config._VALID_ANDROID_VERSIONS):
            assert str(version) in result.stderr

    def test_vm_kernel_failure_surfaces_error(self, cli_root: Path) -> None:
        from beetroot.builder import BootstrapError

        with patch("beetroot.cli.builder.build_vm_kernel", side_effect=BootstrapError("boom")):
            result = runner.invoke(cli.app, ["build", "--vm-kernel"])
        assert result.exit_code == 1
        assert "boom" in result.stderr


class TestVmKernelPreflight:
    def _problem(self) -> builder.PreflightProblem:
        return builder.PreflightProblem(
            requirement="socat",
            detail="static binary not found at /usr/bin/socat",
            fix="apt-get install socat",
        )

    def test_check_reports_ready_host(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.vm_build_preflight", return_value=[]):
            result = runner.invoke(cli.app, ["build", "--vm-kernel", "--check"])
        assert result.exit_code == 0, result.stderr
        assert "all host prerequisites satisfied" in result.stdout

    def test_check_lists_problems_and_exits_one(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.vm_build_preflight", return_value=[self._problem()]):
            result = runner.invoke(cli.app, ["build", "--vm-kernel", "--check"])
        assert result.exit_code == 1
        # The missing requirement and its fix are surfaced.
        assert "socat" in result.stderr
        assert "apt-get install socat" in result.stderr

    def test_check_without_vm_kernel_errors(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["build", "--check"])
        assert result.exit_code == 1
        assert "--check only applies to --vm-kernel" in result.stderr

    def test_fetch_preflight_blocks_build_when_problems(self, cli_root: Path) -> None:
        # A real build (no --check) must NOT call build_vm_kernel when the
        # lightweight fetch-path prerequisites (curl/tar) are missing; it errors
        # with the consolidated list.
        curl_missing = builder.PreflightProblem(
            requirement="curl", detail="not found on PATH", fix="apt-get install curl"
        )
        with (
            patch("beetroot.cli.builder.vm_fetch_preflight", return_value=[curl_missing]),
            patch("beetroot.cli.builder.build_vm_kernel") as mock_b,
        ):
            result = runner.invoke(cli.app, ["build", "--vm-kernel"])
        assert result.exit_code == 1
        mock_b.assert_not_called()
        assert "curl" in result.stderr
        assert "apt-get install curl" in result.stderr

    def test_real_build_does_not_gate_on_bake_toolchain(self, cli_root: Path) -> None:
        # The BLOCKER fix (review finding #1): a real build on a host with NO
        # docker/busybox/socat must NOT abort before build_vm_kernel — the
        # default path fetches both prebuilt artifacts and needs none of the
        # bake toolchain. Drive it with the fetch-path preflight clean (curl/tar
        # present) but assert the CLI never consults the bake superset and
        # dispatches straight to build_vm_kernel.
        with (
            patch("beetroot.cli.builder.vm_fetch_preflight", return_value=[]) as mock_fetch,
            patch("beetroot.cli.builder.vm_build_preflight") as mock_full,
            patch("beetroot.cli.builder.build_vm_kernel") as mock_b,
        ):
            mock_b.return_value = VmArtifacts(
                kernel=Path("/c/bzImage"), rootfs=Path("/c/rootdisk.img")
            )
            result = runner.invoke(cli.app, ["build", "--vm-kernel"])
        assert result.exit_code == 0, result.stderr
        mock_fetch.assert_called_once_with()
        mock_full.assert_not_called()  # bake superset is only for --check
        mock_b.assert_called_once()

    def test_default_build_is_unaffected(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bi:
            mock_bi.return_value = "redroid/redroid:14.0.0_litegapps_houdini_magisk"
            result = runner.invoke(cli.app, ["build"])
        assert result.exit_code == 0
        mock_bi.assert_called_once_with(gapps="minimal", gapps_vendor=None, build_context=None)

    def test_build_context_flag_passes_to_image_build(self, cli_root: Path) -> None:
        with patch("beetroot.cli.builder.build_image") as mock_bi:
            mock_bi.return_value = "redroid/redroid:14.0.0_litegapps_houdini_magisk"
            result = runner.invoke(cli.app, ["build", "--build-context", "/src/checkout"])
        assert result.exit_code == 0
        mock_bi.assert_called_once_with(
            gapps="minimal", gapps_vendor=None, build_context=Path("/src/checkout")
        )


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

    def test_apply_flip_to_vm_emits_inert_advisory_once(self, cli_root: Path) -> None:
        # issue #104 regression: in the canonical create-redroid → edit-to-vm →
        # apply flow the CLI apply verb dispatches on the *registry* kind, which
        # is still "redroid" at apply time, so the VM backend's own advisory
        # never runs. The redroid Instance.apply must surface the set-but-inert
        # binder: vm fields ONCE, naming them, as it flips the kind to vm.
        runner.invoke(cli.app, ["create", "alpha"])
        config.write_yaml(
            cli_root / "alpha" / "beetroot.yaml",
            config.InstanceConfig(
                binder="vm",
                android={"gapps": "full"},  # type: ignore[arg-type]
                frida=config.Frida(version="16.4.10"),
                magisk=config.Magisk(denylist=["com.example.app"]),
            ),
        )
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        # The registry kind is still redroid at apply time, so this exercises the
        # redroid Instance.apply path (not VmDeviceBackend.apply).
        meta = registry.get("alpha")
        assert meta is not None
        assert meta.backend.kind == "vm"
        # Exactly one advisory naming every set-but-inert field.
        err = result.stderr
        assert err.count("no effect under binder: vm") == 1
        assert "android.gapps: full" in err
        assert "frida" in err
        assert "magisk.denylist" in err

    def test_apply_flip_to_vm_with_no_inert_fields_is_silent(self, cli_root: Path) -> None:
        # Contrast: flipping to binder: vm with nothing set-but-inert (gapps none,
        # default denylist, no frida) emits no advisory — the early-return path.
        runner.invoke(cli.app, ["create", "alpha"])
        config.write_yaml(
            cli_root / "alpha" / "beetroot.yaml",
            config.InstanceConfig(binder="vm", android={"gapps": "none"}),  # type: ignore[arg-type]
        )
        result = runner.invoke(cli.app, ["apply", "alpha"])
        assert result.exit_code == 0, result.stderr
        assert "no effect under binder: vm" not in result.stderr

    def test_register_vm_with_inert_fields_emits_advisory_once(self, cli_root: Path) -> None:
        # issue #104 regression: a `register`-ed binder: vm dir is recorded as
        # kind=vm directly and its success hint is `next: beetroot up` (no
        # `apply` in between). Since `up` no longer warns per-boot, `register`
        # itself must surface the set-but-inert fields ONCE, naming them —
        # otherwise the register→up flow names them nowhere.
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(
            root / "beetroot.yaml",
            config.InstanceConfig(
                binder="vm",
                android={"gapps": "full"},  # type: ignore[arg-type]
                frida=config.Frida(version="16.4.10"),
                magisk=config.Magisk(denylist=["com.example.app"]),
            ),
        )
        result = runner.invoke(cli.app, ["register", str(root)])
        assert result.exit_code == 0, result.stderr
        err = result.stderr
        assert err.count("no effect under binder: vm") == 1
        assert "android.gapps: full" in err
        assert "frida" in err
        assert "magisk.denylist" in err

    def test_register_vm_with_no_inert_fields_is_silent(self, cli_root: Path) -> None:
        # Contrast: registering a binder: vm dir with nothing set-but-inert
        # (gapps none, default denylist, no frida) emits no advisory — the
        # early-return path. Note a *default* vm config has gapps "minimal",
        # which IS inert, so this must pin gapps none explicitly.
        root = cli_root / "vm2"
        root.mkdir()
        config.write_yaml(
            root / "beetroot.yaml",
            config.InstanceConfig(binder="vm", android={"gapps": "none"}),  # type: ignore[arg-type]
        )
        result = runner.invoke(cli.app, ["register", str(root)])
        assert result.exit_code == 0, result.stderr
        assert "no effect under binder: vm" not in result.stderr


@pytest.fixture
def _stub_adb_connect_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make ``up()``/``restart()`` skip the real ``adb connect`` poll.

    :meth:`VmDeviceBackend._wait_for_adb_connect` loops ``adb connect``
    against the freshly-launched guest until ``settings.vm_adb_connect_timeout``
    (default 60s) elapses, short-circuiting only when ``adb`` is absent from
    PATH. Any CLI test that drives a successful ``up``/``restart`` would
    otherwise poll a nonexistent guest and hit the 30s pytest-timeout whenever
    ``adb`` happens to be installed (e.g. in CI). Stub the wait to an instant
    success so these tests are deterministic and fast regardless of adb
    presence.
    """
    monkeypatch.setattr(
        vm_backend.VmDeviceBackend, "_wait_for_adb_connect", lambda _self, *_a: None
    )


@pytest.mark.usefixtures("_stub_adb_connect_wait")
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

    def test_up_vm_row_with_host_yaml_demands_apply(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # vm-registry row, but yaml hand-edited back to binder: host →
        # the up() guard fails fast with BackendCapabilityError (mapped to
        # exit 2 by cli.main; CliRunner surfaces it as result.exception)
        # rather than booting QEMU.
        _stage_vm_artifacts(monkeypatch, cli_root)
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="host"))

        def _no_launch(_self: object, _argv: list[str]) -> int:
            raise AssertionError("QEMU must not launch")

        monkeypatch.setattr(qemu.QemuProcess, "start", _no_launch)
        result = runner.invoke(cli.app, ["up", "vm1"])
        assert isinstance(result.exception, api.BackendCapabilityError)
        assert "beetroot apply vm1" in str(result.exception)

    def test_double_up_friendly_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An already-running VM → friendly error (not a traceback) via the
        # QemuLaunchError "already running" guard surfaced through the CLI.
        _stage_vm_artifacts(monkeypatch, cli_root)
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        monkeypatch.setattr(qemu.QemuProcess, "is_running", lambda _self: True)
        result = runner.invoke(cli.app, ["up", "vm1"])
        assert result.exit_code == 1
        assert "already running" in result.stderr

    def test_down_when_stopped_is_noop(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu.QemuProcess, "terminate", lambda _self: False)
        result = runner.invoke(cli.app, ["down", "vm1"])
        assert result.exit_code == 0, result.stderr
        assert "vm1 down" in result.stdout


@pytest.mark.usefixtures("_stub_adb_connect_wait")
class TestVmRestart:
    def test_restart_prints_banner_and_relaunches(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # restart re-launches QEMU, so it must print the same TCG banner up
        # prints — and launch the VM.
        _stage_vm_artifacts(monkeypatch, cli_root)
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        monkeypatch.setattr(qemu.QemuProcess, "terminate", lambda _self: True)
        launched: list[list[str]] = []

        def _start(_self: object, argv: list[str]) -> int:
            launched.append(argv)
            return 1

        monkeypatch.setattr(qemu.QemuProcess, "start", _start)
        result = runner.invoke(cli.app, ["restart", "vm1"])
        assert result.exit_code == 0, result.stderr
        assert "TCG (software)" in result.stderr
        assert "5-20x" in result.stderr
        assert launched
        assert "vm1 restarted" in result.stdout

    def test_restart_missing_artifact_raises_launch_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # restart on a vm instance with no kernel/rootfs raises
        # QemuLaunchError from up() (not the accel-banner path). cli.main()
        # maps it to a friendly error (covered separately); here we assert
        # the verb does not swallow it into a redroid-shaped path.
        monkeypatch.setattr(vm_backend, "settings", Settings(vm_kernel="", vm_rootfs=""))
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        monkeypatch.setattr(qemu.QemuProcess, "terminate", lambda _self: True)
        result = runner.invoke(cli.app, ["restart", "vm1"])
        assert isinstance(result.exception, qemu.QemuLaunchError)
        assert "no VM kernel configured" in str(result.exception)

    def test_restart_missing_artifact_friendly_via_main(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # B3: a non-`up` path to QEMU launch (restart) must surface as
        # `error: ...` + exit 1 via cli.main(), never a raw traceback.
        monkeypatch.setattr(vm_backend, "settings", Settings(vm_kernel="", vm_rootfs=""))
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "detect_accel", lambda _r: "tcg")
        monkeypatch.setattr(qemu.QemuProcess, "terminate", lambda _self: True)
        monkeypatch.setattr(sys, "argv", ["beetroot", "restart", "vm1"])
        buf = io.StringIO()
        console.set_consoles(stderr=Console(file=buf, force_terminal=False))
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        joined = buf.getvalue()
        assert "error: no VM kernel configured" in joined
        assert "Traceback" not in joined

    def test_restart_explicit_kvm_without_dev_kvm_is_friendly_error(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # accel: kvm without /dev/kvm → the banner's resolved_accel raises,
        # surfaced as a friendly error before any relaunch.
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(
            root / "beetroot.yaml",
            config.InstanceConfig(binder="vm", vm={"accel": "kvm"}),  # type: ignore[arg-type]
        )
        api.Instance.register(root, name="vm1")
        monkeypatch.setattr(qemu, "_dev_kvm_usable", lambda: False)

        def _no_launch(_self: object, _argv: list[str]) -> int:
            raise AssertionError("QEMU must not relaunch when kvm is demanded but absent")

        monkeypatch.setattr(qemu.QemuProcess, "start", _no_launch)
        monkeypatch.setattr(qemu.QemuProcess, "terminate", lambda _self: True)
        result = runner.invoke(cli.app, ["restart", "vm1"])
        assert result.exit_code == 1
        assert "/dev/kvm is absent" in result.stderr


class TestVmDestroyOrphan:
    def test_vm_orphan_can_be_destroyed(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # vm-kind row whose beetroot.yaml is gone (orphan) can still be
        # destroyed via the directory-backed orphan fallback.
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        # Remove the yaml → Manager.resolve now raises InstanceNotFoundError.
        (root / "beetroot.yaml").unlink()
        terminated: list[str] = []

        def _terminate(_self: object) -> bool:
            terminated.append("t")
            return True

        monkeypatch.setattr(qemu.QemuProcess, "terminate", _terminate)
        result = runner.invoke(cli.app, ["destroy", "vm1", "-y"])
        assert result.exit_code == 0, result.stderr
        assert "destroyed vm1" in result.stdout
        assert registry.get("vm1") is None
        assert not root.exists()
        # The orphan path terminated the QEMU process (no compose project).
        assert terminated == ["t"]

    def test_vm_orphan_dir_gone_cleans_registry(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # vm-kind row whose entire directory is gone → just clean the
        # registry row (no QEMU terminate, no rmtree).
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        import shutil as _shutil

        _shutil.rmtree(root)
        result = runner.invoke(cli.app, ["destroy", "vm1", "-y"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("vm1") is None


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


class TestVmLogs:
    """`beetroot logs <vm-instance>` reads the persisted QEMU console (issue #44 follow-up)."""

    def _register(self, cli_root: Path) -> Path:
        root = cli_root / "vm1"
        root.mkdir()
        config.write_yaml(root / "beetroot.yaml", config.InstanceConfig(binder="vm"))
        api.Instance.register(root, name="vm1")
        return root

    def test_logs_prints_qemu_console(self, cli_root: Path) -> None:
        root = self._register(cli_root)
        qemu.QemuProcess(root, 5555).console_log.write_text(
            "[ 0.000000] Linux\n[*] Zygisk newly enabled — restarting zygote\n"
        )
        result = runner.invoke(cli.app, ["logs", "vm1"])
        assert result.exit_code == 0, result.stderr
        assert "restarting zygote" in result.stdout

    def test_logs_warns_when_never_booted(self, cli_root: Path) -> None:
        self._register(cli_root)
        result = runner.invoke(cli.app, ["logs", "vm1"])
        assert result.exit_code == 0, result.stderr
        assert "no QEMU console log" in result.stderr
