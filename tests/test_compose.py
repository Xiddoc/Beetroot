"""Tests for compose.py — docker compose subprocess wrappers."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import compose, paths
from beetroot.compose import ComposeError


def _ok_result(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail_result(returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _mock_docker_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make shutil.which('docker') return a truthy path so _ensure_docker passes."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)


class TestBaseCommand:
    def test_up_command_structure(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha")
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["docker", "compose"]
        assert "-p" in cmd
        assert "alpha" in cmd
        assert "-f" in cmd
        assert "--env-file" in cmd

    def test_project_name_is_instance_name(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("bravo")
        cmd = mock_run.call_args[0][0]
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "bravo"

    def test_compose_file_path_is_correct(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha")
        cmd = mock_run.call_args[0][0]
        f_idx = cmd.index("-f")
        assert cmd[f_idx + 1] == str(paths.compose_file())

    def test_env_file_path_is_instance_env(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha")
        cmd = mock_run.call_args[0][0]
        env_idx = cmd.index("--env-file")
        assert cmd[env_idx + 1] == str(paths.instance_env("alpha"))


class TestUp:
    def test_up_includes_up_d(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha")
        cmd = mock_run.call_args[0][0]
        assert "up" in cmd
        assert "-d" in cmd

    def test_up_with_build_adds_build_flag(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", build=True)
        cmd = mock_run.call_args[0][0]
        assert "--build" in cmd

    def test_up_without_build_no_build_flag(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", build=False)
        cmd = mock_run.call_args[0][0]
        assert "--build" not in cmd

    def test_up_nonzero_exit_raises_compose_error(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose up"):
                compose.up("alpha")


class TestDown:
    def test_down_includes_down(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.down("alpha")
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd

    def test_down_with_volumes_adds_volumes_flag(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.down("alpha", volumes=True)
        cmd = mock_run.call_args[0][0]
        assert "--volumes" in cmd

    def test_down_without_volumes_no_volumes_flag(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.down("alpha", volumes=False)
        cmd = mock_run.call_args[0][0]
        assert "--volumes" not in cmd

    def test_down_nonzero_exit_raises_compose_error(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose down"):
                compose.down("alpha")


class TestPsStatus:
    def test_running_state(self, isolated_root: Path) -> None:
        stdout = '{"State": "running", "Name": "alpha"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha") == "running"

    def test_exited_state(self, isolated_root: Path) -> None:
        stdout = '{"State": "exited", "Name": "alpha"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha") == "exited"

    def test_empty_stdout_returns_not_created(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result("")):
            assert compose.ps_status("alpha") == "not-created"

    def test_nonzero_exit_returns_not_created(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            assert compose.ps_status("alpha") == "not-created"

    def test_multiple_json_lines_returns_first_state(self, isolated_root: Path) -> None:
        stdout = '{"State": "running"}\n{"State": "exited"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha") == "running"

    def test_invalid_json_falls_through_to_not_created(self, isolated_root: Path) -> None:
        stdout = "not json at all\n"
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha") == "not-created"

    def test_json_missing_state_key_returns_not_created(self, isolated_root: Path) -> None:
        stdout = '{"Name": "alpha"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha") == "not-created"

    def test_state_is_lowercased(self, isolated_root: Path) -> None:
        stdout = '{"State": "Running"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha") == "running"


class TestComposeError:
    def test_is_runtime_error(self) -> None:
        assert issubclass(ComposeError, RuntimeError)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(ComposeError):
            raise ComposeError("test error")


class TestDockerNotFound:
    def test_raises_compose_error_when_docker_missing(self, isolated_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ComposeError, match="docker not found"):
            compose.up("alpha")


class TestLogs:
    def test_logs_includes_logs_subcommand(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha")
        cmd = mock_run.call_args[0][0]
        assert "logs" in cmd

    def test_logs_with_follow_adds_f_flag(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", follow=True)
        cmd = mock_run.call_args[0][0]
        assert "-f" in cmd

    def test_logs_without_follow_no_f_flag(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", follow=False)
        cmd = mock_run.call_args[0][0]
        # Base command uses '-f' for --file; check the subcommand args don't have it
        logs_idx = cmd.index("logs")
        assert "-f" not in cmd[logs_idx:]


class TestBuild:
    def test_build_includes_build_subcommand(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.build("alpha")
        cmd = mock_run.call_args[0][0]
        assert "build" in cmd

    def test_build_nonzero_exit_raises_compose_error(self, isolated_root: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose build"):
                compose.build("alpha")
