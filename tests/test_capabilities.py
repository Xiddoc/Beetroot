"""
Tests for the host capability survey (`beetroot modes`).

The classifier is pure, so every mode/host-state combination is asserted
directly; `survey()` is tested with the probes mocked, and the CLI verb is
driven through the Typer runner. These are behaviour tests: a wrong verdict
here is exactly the class of mistake the feature exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest
from typer.testing import CliRunner

from beetroot import capabilities, cli, hostcheck

runner = CliRunner()


def _binder(state: hostcheck.BinderState) -> hostcheck.BinderStatus:
    return hostcheck.BinderStatus(state=state, reason=f"reason-{state}", remedy=f"remedy-{state}")


def _by_mode(results: list[capabilities.ModeSupport]) -> dict[str, capabilities.ModeSupport]:
    return {r.mode: r for r in results}


# ---- _redroid_host --------------------------------------------------------


def test_redroid_host_ready_with_docker_daemon_is_supported() -> None:
    r = capabilities._redroid_host(
        _binder("ready"), docker=True, docker_daemon=True, host_is_x86_64=True
    )
    assert r.status == "supported"


def test_redroid_host_ready_docker_cli_but_daemon_down_needs_setup() -> None:
    # issue #179: CLI present but ``dockerd`` not answering is NOT "supported" —
    # redroid can't boot a container without a live daemon.
    r = capabilities._redroid_host(
        _binder("ready"), docker=True, docker_daemon=False, host_is_x86_64=True
    )
    assert r.status == "needs-setup"
    assert "daemon is not responding" in r.reason
    assert r.remedy == "start the Docker daemon"


def test_redroid_host_ready_without_docker_needs_setup() -> None:
    r = capabilities._redroid_host(
        _binder("ready"), docker=False, docker_daemon=False, host_is_x86_64=True
    )
    assert r.status == "needs-setup"
    assert "Docker" in r.remedy


def test_redroid_host_loadable_needs_setup() -> None:
    r = capabilities._redroid_host(
        _binder("loadable"), docker=True, docker_daemon=True, host_is_x86_64=True
    )
    assert r.status == "needs-setup"
    assert r.remedy == "remedy-loadable"


def test_redroid_host_unsupported() -> None:
    r = capabilities._redroid_host(
        _binder("unsupported"), docker=True, docker_daemon=True, host_is_x86_64=True
    )
    assert r.status == "unsupported"


def test_redroid_host_unknown() -> None:
    r = capabilities._redroid_host(
        _binder("unknown"), docker=True, docker_daemon=True, host_is_x86_64=True
    )
    assert r.status == "unknown"


def test_redroid_host_non_x86_64_is_unsupported_before_binder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # issue #190: the x86_64 redroid image can't run natively on a foreign arch,
    # no matter how ready the binder driver is — arch overrides the binder state.
    monkeypatch.setattr("beetroot.capabilities.platform.machine", lambda: "aarch64")
    r = capabilities._redroid_host(
        _binder("ready"), docker=True, docker_daemon=True, host_is_x86_64=False
    )
    assert r.status == "unsupported"
    assert "aarch64" in r.reason
    assert "vm" in r.remedy


# ---- _vm_kvm --------------------------------------------------------------


def test_vm_kvm_no_kvm_is_unsupported() -> None:
    r = capabilities._vm_kvm(kvm=False, qemu_present=True, host_is_x86_64=True)
    assert r.status == "unsupported"
    assert "TCG" in r.remedy


def test_vm_kvm_kvm_without_qemu_needs_setup() -> None:
    r = capabilities._vm_kvm(kvm=True, qemu_present=False, host_is_x86_64=True)
    assert r.status == "needs-setup"
    assert "QEMU" in r.remedy


def test_vm_kvm_kvm_and_qemu_supported() -> None:
    r = capabilities._vm_kvm(kvm=True, qemu_present=True, host_is_x86_64=True)
    assert r.status == "supported"


def test_vm_kvm_non_x86_64_is_unsupported_even_with_kvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # issue #190: KVM cross-arch is physically impossible — a foreign-arch host
    # with its own /dev/kvm still can't accelerate the x86_64 guest.
    monkeypatch.setattr("beetroot.capabilities.platform.machine", lambda: "aarch64")
    r = capabilities._vm_kvm(kvm=True, qemu_present=True, host_is_x86_64=False)
    assert r.status == "unsupported"
    assert "aarch64" in r.reason
    assert "TCG" in r.remedy


# ---- _vm_tcg --------------------------------------------------------------


def test_vm_tcg_without_qemu_needs_setup() -> None:
    r = capabilities._vm_tcg(qemu_present=False, host_is_x86_64=True)
    assert r.status == "needs-setup"


def test_vm_tcg_with_qemu_supported() -> None:
    r = capabilities._vm_tcg(qemu_present=True, host_is_x86_64=True)
    assert r.status == "supported"
    assert "5-20x" in r.reason


def test_vm_tcg_non_x86_64_stays_reachable_with_cross_arch_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # issue #190: TCG can boot the x86_64 guest cross-arch, so it must NOT be
    # unsupported on a foreign arch — but the reason flags the extra cost so the
    # host isn't told the path is plainly fast.
    monkeypatch.setattr("beetroot.capabilities.platform.machine", lambda: "aarch64")
    r = capabilities._vm_tcg(qemu_present=True, host_is_x86_64=False)
    assert r.status == "supported"
    assert "cross-arch" in r.reason
    assert "5-20x" not in r.reason


def test_vm_tcg_non_x86_64_without_qemu_still_needs_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("beetroot.capabilities.platform.machine", lambda: "aarch64")
    r = capabilities._vm_tcg(qemu_present=False, host_is_x86_64=False)
    assert r.status == "needs-setup"
    assert "cross-arch" in r.reason


# ---- _adb_adopt -----------------------------------------------------------


def test_adb_adopt_without_adb_needs_setup() -> None:
    r = capabilities._adb_adopt(adb_present=False)
    assert r.status == "needs-setup"
    # Even before adb is installed, the DETAIL must flag the external-device need.
    assert "external rooted device" in r.remedy


def test_adb_adopt_with_adb_supported() -> None:
    r = capabilities._adb_adopt(adb_present=True)
    assert r.status == "supported"
    assert "external rooted device" in r.remedy


# ---- classify_modes -------------------------------------------------------


def test_classify_modes_returns_all_four_in_order() -> None:
    results = capabilities.classify_modes(
        binder=_binder("ready"),
        kvm=True,
        qemu_present=True,
        docker=True,
        docker_daemon=True,
        adb_present=True,
        host_is_x86_64=True,
    )
    assert [r.mode for r in results] == [
        capabilities.MODE_REDROID_HOST,
        capabilities.MODE_VM_KVM,
        capabilities.MODE_VM_TCG,
        capabilities.MODE_ADB,
    ]
    assert all(r.status == "supported" for r in results)


def test_classify_modes_binder_ready_daemon_up_redroid_supported() -> None:
    # issue #179: the redroid host verdict must gate on daemon liveness, so a
    # ready binder + live daemon is the only path to "supported".
    results = _by_mode(
        capabilities.classify_modes(
            binder=_binder("ready"),
            kvm=False,
            qemu_present=False,
            docker=True,
            docker_daemon=True,
            adb_present=False,
            host_is_x86_64=True,
        )
    )
    assert results[capabilities.MODE_REDROID_HOST].status == "supported"


def test_classify_modes_binder_ready_daemon_down_redroid_needs_setup() -> None:
    # issue #179: the misleading case — CLI present, binder ready, but the daemon
    # is stopped. Must be needs-setup with the daemon-start remedy, not supported.
    results = _by_mode(
        capabilities.classify_modes(
            binder=_binder("ready"),
            kvm=False,
            qemu_present=False,
            docker=True,
            docker_daemon=False,
            adb_present=False,
            host_is_x86_64=True,
        )
    )
    redroid = results[capabilities.MODE_REDROID_HOST]
    assert redroid.status == "needs-setup"
    assert redroid.remedy == "start the Docker daemon"


def test_classify_modes_binderless_kvmless_host_only_tcg_and_adb() -> None:
    # This is *this* environment's profile (CONFIG_ANDROID_BINDER_IPC unset,
    # no /dev/kvm, qemu+adb installed) — the exact case that misled us before.
    results = _by_mode(
        capabilities.classify_modes(
            binder=_binder("unsupported"),
            kvm=False,
            qemu_present=True,
            docker=True,
            docker_daemon=True,
            adb_present=True,
            host_is_x86_64=True,
        )
    )
    assert results[capabilities.MODE_REDROID_HOST].status == "unsupported"
    assert results[capabilities.MODE_VM_KVM].status == "unsupported"
    assert results[capabilities.MODE_VM_TCG].status == "supported"
    assert results[capabilities.MODE_ADB].status == "supported"


def test_classify_modes_non_x86_64_host_kvm_and_redroid_unsupported_tcg_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # issue #190: on a non-x86_64 host the x86_64 KVM path and the native redroid
    # image are both unsupported, but the TCG cross-arch path stays reachable.
    monkeypatch.setattr("beetroot.capabilities.platform.machine", lambda: "aarch64")
    results = _by_mode(
        capabilities.classify_modes(
            binder=_binder("ready"),  # even a ready binder can't rescue a foreign arch
            kvm=True,  # even a native /dev/kvm can't accelerate the x86_64 guest
            qemu_present=True,
            docker=True,
            docker_daemon=True,
            adb_present=True,
            host_is_x86_64=False,
        )
    )
    assert results[capabilities.MODE_REDROID_HOST].status == "unsupported"
    assert results[capabilities.MODE_VM_KVM].status == "unsupported"
    assert "aarch64" in results[capabilities.MODE_VM_KVM].reason
    assert results[capabilities.MODE_VM_TCG].status == "supported"
    assert "cross-arch" in results[capabilities.MODE_VM_TCG].reason
    # adb is arch-agnostic (drives a remote device) — unaffected by the guard.
    assert results[capabilities.MODE_ADB].status == "supported"


def test_classify_modes_x86_64_host_keeps_current_behavior() -> None:
    # The x86_64 path is unchanged: a ready host with kvm+qemu keeps all four
    # supported, proving the guard only fires on a foreign arch (issue #190).
    results = _by_mode(
        capabilities.classify_modes(
            binder=_binder("ready"),
            kvm=True,
            qemu_present=True,
            docker=True,
            docker_daemon=True,
            adb_present=True,
            host_is_x86_64=True,
        )
    )
    assert results[capabilities.MODE_REDROID_HOST].status == "supported"
    assert results[capabilities.MODE_VM_KVM].status == "supported"
    assert results[capabilities.MODE_VM_TCG].status == "supported"


# ---- survey ---------------------------------------------------------------


def test_survey_wires_probes_into_classifier() -> None:
    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("ready")),
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="kvm"),
        mock.patch("shutil.which", return_value="/usr/bin/x"),
        mock.patch.object(capabilities, "docker_daemon_responsive", return_value=True),
    ):
        results = capabilities.survey()
    assert len(results) == 4
    assert _by_mode(results)[capabilities.MODE_VM_KVM].status == "supported"


def test_survey_threads_live_docker_daemon_into_redroid_verdict() -> None:
    # issue #179: with the CLI present + binder ready, a *live* daemon must land
    # the redroid host verdict on "supported".
    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("ready")),
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="tcg"),
        mock.patch("shutil.which", return_value="/usr/bin/x"),
        mock.patch.object(capabilities, "docker_daemon_responsive", return_value=True) as probe,
    ):
        results = _by_mode(capabilities.survey())
    probe.assert_called_once()
    assert results[capabilities.MODE_REDROID_HOST].status == "supported"


def test_survey_threads_dead_docker_daemon_into_redroid_verdict() -> None:
    # issue #179: CLI present + binder ready but the daemon probe fails → the
    # threaded ``False`` must demote the redroid verdict to needs-setup.
    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("ready")),
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="tcg"),
        mock.patch("shutil.which", return_value="/usr/bin/x"),
        mock.patch.object(capabilities, "docker_daemon_responsive", return_value=False) as probe,
    ):
        results = _by_mode(capabilities.survey())
    probe.assert_called_once()
    redroid = results[capabilities.MODE_REDROID_HOST]
    assert redroid.status == "needs-setup"
    assert redroid.remedy == "start the Docker daemon"


def test_survey_skips_daemon_probe_when_docker_cli_absent() -> None:
    # Without the CLI there's no daemon to probe — the classifier ignores the
    # flag on that path anyway, and short-circuit avoids a pointless subprocess.
    def fake_which(name: str) -> str | None:
        return "/usr/bin/qemu" if name.startswith("qemu") else None

    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("ready")),
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="tcg"),
        mock.patch("shutil.which", side_effect=fake_which),
        mock.patch.object(capabilities, "docker_daemon_responsive", return_value=True) as probe,
    ):
        results = _by_mode(capabilities.survey())
    probe.assert_not_called()
    assert results[capabilities.MODE_REDROID_HOST].status == "needs-setup"


def test_survey_detects_tcg_only_host() -> None:
    def fake_which(name: str) -> str | None:
        return "/usr/bin/qemu" if name.startswith("qemu") else None

    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("unsupported")),
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="tcg"),
        mock.patch("shutil.which", side_effect=fake_which),
    ):
        results = _by_mode(capabilities.survey())
    assert results[capabilities.MODE_VM_TCG].status == "supported"
    assert results[capabilities.MODE_VM_KVM].status == "unsupported"
    assert results[capabilities.MODE_ADB].status == "needs-setup"  # adb not on PATH


def test_survey_accepts_injected_settings() -> None:
    from beetroot.settings import Settings

    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("ready")),
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="tcg"),
        mock.patch("shutil.which", return_value=None),
    ):
        results = capabilities.survey(Settings())
    assert len(results) == 4


def test_survey_threads_non_x86_64_host_into_verdicts() -> None:
    # issue #190: survey() must probe platform.machine() and thread a foreign
    # arch through the classifier — the real host arch (via qemu.host_is_guest_arch)
    # is what selects the KVM/redroid unsupported + TCG cross-arch verdicts.
    with (
        mock.patch("beetroot.hostcheck.binder_status", return_value=_binder("ready")),
        # A ready binder + all binaries present would normally give host/KVM
        # "supported" on x86_64 — the foreign arch is the sole reason they flip.
        mock.patch("beetroot.vm.qemu.detect_accel", return_value="tcg"),
        mock.patch("shutil.which", return_value="/usr/bin/x"),
        mock.patch.object(capabilities, "docker_daemon_responsive", return_value=True),
        # Patch the shared platform module so BOTH the survey probe
        # (qemu.host_is_guest_arch) and the classifier reason strings
        # (capabilities.platform.machine) agree on the foreign arch.
        mock.patch("platform.machine", return_value="aarch64"),
    ):
        results = _by_mode(capabilities.survey())
    assert results[capabilities.MODE_REDROID_HOST].status == "unsupported"
    assert results[capabilities.MODE_VM_KVM].status == "unsupported"
    assert "aarch64" in results[capabilities.MODE_VM_KVM].reason
    assert results[capabilities.MODE_VM_TCG].status == "supported"
    assert "cross-arch" in results[capabilities.MODE_VM_TCG].reason


# ---- docker_daemon_responsive (relocated from builder, issue #179) --------


class TestDockerDaemonResponsive:
    def test_true_when_info_succeeds(self) -> None:
        with mock.patch(
            "beetroot.capabilities.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ):
            assert capabilities.docker_daemon_responsive() is True

    def test_false_when_info_nonzero(self) -> None:
        with mock.patch(
            "beetroot.capabilities.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ):
            assert capabilities.docker_daemon_responsive() is False

    def test_false_when_docker_missing(self) -> None:
        with mock.patch("beetroot.capabilities.subprocess.run", side_effect=FileNotFoundError):
            assert capabilities.docker_daemon_responsive() is False


# ---- CLI verb -------------------------------------------------------------


def test_modes_verb_renders_table() -> None:
    fake = [
        capabilities.ModeSupport(mode="m1", status="supported", reason="r1", remedy=""),
        capabilities.ModeSupport(mode="m2", status="unsupported", reason="r2", remedy="do-x"),
    ]
    with mock.patch.object(capabilities, "survey", return_value=fake):
        result = runner.invoke(cli.app, ["modes"])
    assert result.exit_code == 0
    assert "MODE" in result.stdout
    assert "m1" in result.stdout
    assert "supported" in result.stdout


def test_modes_verb_json_output() -> None:
    fake = [capabilities.ModeSupport(mode="m1", status="supported", reason="r1", remedy="")]
    with mock.patch.object(capabilities, "survey", return_value=fake):
        result = runner.invoke(cli.app, ["modes", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == [{"mode": "m1", "status": "supported", "reason": "r1", "remedy": ""}]
