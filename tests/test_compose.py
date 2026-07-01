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


def _fail_result(returncode: int = 1, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


@pytest.fixture(autouse=True)
def _mock_docker_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make shutil.which('docker') return a truthy path so _ensure_docker passes."""
    import shutil

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )


class TestBaseCommand:
    def test_up_command_structure(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["docker", "compose"]
        assert "-p" in cmd
        assert "alpha" in cmd
        assert "-f" in cmd
        assert "--project-directory" in cmd
        assert "--env-file" in cmd

    def test_project_name_is_instance_name(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("bravo", tmp_path)
        cmd = mock_run.call_args[0][0]
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "bravo"

    def test_compose_file_path_is_bundled(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        f_idx = cmd.index("-f")
        assert cmd[f_idx + 1] == str(paths.bundled_compose_file())

    def test_project_directory_is_instance_root(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        pd_idx = cmd.index("--project-directory")
        assert cmd[pd_idx + 1] == str(tmp_path)

    def test_env_file_path_is_instance_env(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        env_idx = cmd.index("--env-file")
        assert cmd[env_idx + 1] == str(paths.instance_env(tmp_path))

    def test_cwd_is_instance_root(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        assert mock_run.call_args.kwargs["cwd"] == tmp_path


class TestUp:
    def test_up_includes_up_d(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "up" in cmd
        assert "-d" in cmd

    def test_up_does_not_add_build_flag(self, tmp_path: Path) -> None:
        """compose.up never emits `--build` after T5 (call `compose.build` separately)."""
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--build" not in cmd

    def test_up_nonzero_exit_raises_compose_error(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose up"):
                compose.up("alpha", tmp_path)

    def test_up_captures_stderr_only_and_streams_stdout(self, tmp_path: Path) -> None:
        # #276: capture stderr (so its tail can be folded into the error) but
        # leave stdout inherited so compose progress still streams to the
        # terminal — NOT capture_output=True (which would hide all output).
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.up("alpha", tmp_path)
        assert mock_run.call_args.kwargs["stderr"] is subprocess.PIPE
        assert mock_run.call_args.kwargs["text"] is True
        assert "capture_output" not in mock_run.call_args.kwargs

    def test_up_folds_stderr_into_compose_error(self, tmp_path: Path) -> None:
        # #276: the raised message must carry the daemon's reason.
        stderr = "Error response from daemon: pull access denied for redroid/redroid"
        with patch("subprocess.run", return_value=_fail_result(1, stderr=stderr)):
            with pytest.raises(ComposeError, match="pull access denied"):
                compose.up("alpha", tmp_path)


class TestDown:
    def test_down_includes_down(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.down("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd

    def test_down_with_volumes_adds_volumes_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.down("alpha", tmp_path, volumes=True)
        cmd = mock_run.call_args[0][0]
        assert "--volumes" in cmd

    def test_down_without_volumes_no_volumes_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.down("alpha", tmp_path, volumes=False)
        cmd = mock_run.call_args[0][0]
        assert "--volumes" not in cmd

    def test_down_nonzero_exit_raises_compose_error(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose down"):
                compose.down("alpha", tmp_path)

    def test_down_folds_stderr_into_compose_error(self, tmp_path: Path) -> None:
        # #276: the raised message must carry the daemon's reason.
        stderr = "Error response from daemon: network alpha_default has active endpoints"
        with patch("subprocess.run", return_value=_fail_result(1, stderr=stderr)):
            with pytest.raises(ComposeError, match="active endpoints"):
                compose.down("alpha", tmp_path)


class TestPsStatus:
    def test_running_state(self, tmp_path: Path) -> None:
        stdout = '{"State": "running", "Name": "alpha"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "running"

    def test_exited_state(self, tmp_path: Path) -> None:
        stdout = '{"State": "exited", "Name": "alpha"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "exited"

    def test_empty_stdout_returns_not_created(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result("")):
            assert compose.ps_status("alpha", tmp_path) == "not-created"

    def test_nonzero_exit_returns_not_created(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            assert compose.ps_status("alpha", tmp_path) == "not-created"

    def test_multiple_json_lines_returns_first_state(self, tmp_path: Path) -> None:
        stdout = '{"State": "running"}\n{"State": "exited"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "running"

    def test_invalid_json_falls_through_to_not_created(self, tmp_path: Path) -> None:
        stdout = "not json at all\n"
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "not-created"

    def test_json_missing_state_key_returns_not_created(self, tmp_path: Path) -> None:
        stdout = '{"Name": "alpha"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "not-created"

    def test_non_dict_json_line_is_skipped_without_crashing(self, tmp_path: Path) -> None:
        # #277: a well-formed JSON line that decodes to a non-object (a bare
        # list, string, or number) has no ``.get`` — it must be skipped, not
        # raise an AttributeError that escapes the JSONDecodeError-only guard.
        stdout = '["not", "an", "object"]\n42\n"bare string"\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "not-created"

    def test_non_dict_json_line_skipped_then_valid_dict_wins(self, tmp_path: Path) -> None:
        # #277: a non-dict line preceding a valid object must be skipped, and
        # the following object's State still resolved.
        stdout = '["junk"]\n{"State": "running"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "running"

    def test_state_is_lowercased(self, tmp_path: Path) -> None:
        stdout = '{"State": "Running"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "running"

    def test_starting_state(self, tmp_path: Path) -> None:
        stdout = '{"State": "starting"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "starting"

    def test_restarting_maps_to_starting(self, tmp_path: Path) -> None:
        # Docker emits both ``starting`` and ``restarting``; both
        # map to the same closed-enum value so callers don't need
        # to handle both.
        stdout = '{"State": "restarting"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "starting"

    def test_created_state(self, tmp_path: Path) -> None:
        stdout = '{"State": "created"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "created"

    def test_paused_state(self, tmp_path: Path) -> None:
        stdout = '{"State": "paused"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "paused"

    def test_dead_maps_to_exited(self, tmp_path: Path) -> None:
        # Dead / removing both terminal — squash into the
        # exited bucket so the doctor verb's logic stays simple.
        stdout = '{"State": "dead"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "exited"

    def test_unknown_state_falls_through_to_unknown(self, tmp_path: Path) -> None:
        # Future Docker releases may invent new state strings. The
        # closed-enum maps them to ``"unknown"`` rather than crashing
        # or silently passing them through as ``"running"``.
        stdout = '{"State": "warp-drive-engaged"}\n'
        with patch("subprocess.run", return_value=_ok_result(stdout)):
            assert compose.ps_status("alpha", tmp_path) == "unknown"

    def test_daemon_unreachable_distinguished_from_not_created(self, tmp_path: Path) -> None:
        # Agent 2 B-7: surface a precise error when the daemon itself
        # is unreachable, vs. the project simply not existing yet.
        stderr = (
            "Cannot connect to the Docker daemon at "
            "unix:///var/run/docker.sock. Is the docker daemon running?"
        )
        res = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=stderr,
        )
        with patch("subprocess.run", return_value=res):
            assert compose.ps_status("alpha", tmp_path) == "docker-unreachable"

    def test_docker_binary_missing_returns_docker_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If ``docker`` isn't on PATH, ``_ensure_docker`` raises and
        # we catch + surface that as ``docker-unreachable``.
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert compose.ps_status("alpha", tmp_path) == "docker-unreachable"

    def test_failed_to_connect_api_phrasing_is_docker_unreachable(self, tmp_path: Path) -> None:
        # #178: a custom/rootless socket via DOCKER_HOST fails with the
        # "failed to connect to the docker API at ..." phrasing, which lacks
        # the "cannot connect to the docker daemon" substring. It must still
        # map to ``docker-unreachable`` (not the misleading ``not-created``).
        stderr = (
            "failed to connect to the docker API at unix:///tmp/missing.sock; "
            "check if the path is correct and if the daemon is running"
        )
        res = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)
        with patch("subprocess.run", return_value=res):
            assert compose.ps_status("alpha", tmp_path) == "docker-unreachable"

    def test_timeout_returns_docker_unreachable(self, tmp_path: Path) -> None:
        # #178: a reachable-but-wedged daemon (or an unresponsive TCP
        # DOCKER_HOST) must degrade to ``docker-unreachable`` rather than
        # hang the verb forever.
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker compose ps", timeout=1),
        ):
            assert compose.ps_status("alpha", tmp_path) == "docker-unreachable"

    def test_ps_status_passes_bounded_timeout(self, tmp_path: Path) -> None:
        # The read-only probe must be bounded so ls/status/doctor can't hang.
        with patch("subprocess.run", return_value=_ok_result("")) as mock_run:
            compose.ps_status("alpha", tmp_path)
        assert mock_run.call_args.kwargs["timeout"] == compose._PS_STATUS_TIMEOUT


class TestComposeError:
    def test_is_runtime_error(self) -> None:
        assert issubclass(ComposeError, RuntimeError)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(ComposeError):
            raise ComposeError("test error")


class TestDockerNotFound:
    def test_raises_compose_error_when_docker_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ComposeError, match="docker not found"):
            compose.up("alpha", tmp_path)


class TestLogs:
    def test_logs_includes_logs_subcommand(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "logs" in cmd

    def test_logs_with_follow_adds_f_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", tmp_path, follow=True)
        cmd = mock_run.call_args[0][0]
        assert "-f" in cmd

    def test_logs_without_follow_no_f_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", tmp_path, follow=False)
        cmd = mock_run.call_args[0][0]
        logs_idx = cmd.index("logs")
        assert "-f" not in cmd[logs_idx:]

    def test_logs_raises_on_nonzero_non_follow(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose logs"):
                compose.logs("alpha", tmp_path, follow=False)

    def test_logs_folds_stderr_into_compose_error(self, tmp_path: Path) -> None:
        # #276: a non-follow logs failure must carry the daemon's reason.
        stderr = "no such service: alpha"
        with patch("subprocess.run", return_value=_fail_result(1, stderr=stderr)):
            with pytest.raises(ComposeError, match="no such service"):
                compose.logs("alpha", tmp_path, follow=False)

    def test_logs_non_follow_captures_stderr_only(self, tmp_path: Path) -> None:
        # #276: non-follow mode captures stderr (for the error) but leaves
        # stdout inherited — the logs themselves are on stdout and must stream
        # to the terminal, so it must NOT use capture_output=True.
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", tmp_path, follow=False)
        assert mock_run.call_args.kwargs["stderr"] is subprocess.PIPE
        assert mock_run.call_args.kwargs["text"] is True
        assert "capture_output" not in mock_run.call_args.kwargs

    def test_logs_follow_does_not_capture_stderr(self, tmp_path: Path) -> None:
        # #276: follow mode must keep streaming to the terminal (inherit stdio),
        # so it must NOT capture output.
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.logs("alpha", tmp_path, follow=True)
        assert "capture_output" not in mock_run.call_args.kwargs

    def test_logs_tolerates_nonzero_in_follow(self, tmp_path: Path) -> None:
        # Ctrl-C out of a ``logs -f`` stream surfaces as a non-zero (SIGINT)
        # exit; that is the expected way to stop it, so it must not raise.
        with patch("subprocess.run", return_value=_fail_result(-2)):
            compose.logs("alpha", tmp_path, follow=True)


class TestBuild:
    def test_build_includes_build_subcommand(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            compose.build("alpha", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "build" in cmd

    def test_build_nonzero_exit_raises_compose_error(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_fail_result(1)):
            with pytest.raises(ComposeError, match="compose build"):
                compose.build("alpha", tmp_path)

    def test_build_streams_output_and_error_is_exit_code_only(self, tmp_path: Path) -> None:
        # #276: `docker compose build` is verbose and its output is the
        # deliverable, so build inherits stdio (no capture) and its output
        # streams to the terminal. The error therefore stays exit-code-only —
        # the build log was already visible on the terminal.
        stderr = "failed to solve: dockerfile parse error on line 3"
        with patch("subprocess.run", return_value=_fail_result(1, stderr=stderr)) as mock_run:
            with pytest.raises(ComposeError) as excinfo:
                compose.build("alpha", tmp_path)
        assert "capture_output" not in mock_run.call_args.kwargs
        assert "stderr" not in mock_run.call_args.kwargs
        # stderr is NOT folded in (it was never captured — it streamed live).
        assert "dockerfile parse error" not in str(excinfo.value)
        assert str(excinfo.value) == "`compose build` failed for alpha (exit 1)"


class TestLifecycleErrorFormatting:
    def test_stderr_tail_is_trimmed_to_bound(self, tmp_path: Path) -> None:
        # #276: a multi-kilobyte failure log is trimmed to its tail so the
        # error string can't balloon; the exit code prefix stays present.
        stderr = "x" * 5000 + "TAIL_MARKER"
        with patch("subprocess.run", return_value=_fail_result(1, stderr=stderr)):
            with pytest.raises(ComposeError) as excinfo:
                compose.up("alpha", tmp_path)
        message = str(excinfo.value)
        assert "TAIL_MARKER" in message
        assert "exit 1" in message
        assert len(message) < 5000

    def test_empty_stderr_omits_the_colon_suffix(self, tmp_path: Path) -> None:
        # #276: when the daemon gives no stderr, the message is just the exit
        # code — no dangling ``: `` suffix.
        with patch("subprocess.run", return_value=_fail_result(1, stderr="")):
            with pytest.raises(ComposeError) as excinfo:
                compose.up("alpha", tmp_path)
        assert str(excinfo.value) == "`compose up` failed for alpha (exit 1)"
