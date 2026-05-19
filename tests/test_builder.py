"""Tests for builder.py — base-image bootstrap (clone + patch + build)."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import builder, config
from beetroot.builder import (
    GAPPS_FLAGS,
    BootstrapError,
    DefaultRunner,
    GappsVariant,
    build_image,
)
from beetroot.settings import settings


@dataclass
class _Call:
    cmd: list[str]
    cwd: Path | None
    check: bool
    env: dict[str, str] | None


@dataclass
class FakeRunner:
    """Records every run() invocation; optionally fails on a matching command head."""

    calls: list[_Call] = field(default_factory=list)
    fail_on: str | None = None
    fail_exit: int = 1

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(_Call(list(cmd), cwd, check, env))
        if self.fail_on is not None and cmd[0] == self.fail_on:
            raise BootstrapError(f"fake failure on {self.fail_on} (exit {self.fail_exit})")


class TestGappsFlags:
    def test_none_has_no_flags(self) -> None:
        assert GAPPS_FLAGS["none"] == []

    def test_lite_uses_lg(self) -> None:
        assert GAPPS_FLAGS["lite"] == ["-lg"]

    def test_full_uses_g(self) -> None:
        assert GAPPS_FLAGS["full"] == ["-g"]

    def test_mindthegapps_uses_mtg(self) -> None:
        assert GAPPS_FLAGS["mindthegapps"] == ["-mtg"]

    def test_covers_all_variants(self) -> None:
        assert set(GAPPS_FLAGS.keys()) == {"none", "lite", "full", "mindthegapps"}


class TestBootstrapErrorType:
    def test_is_runtime_error(self) -> None:
        assert issubclass(BootstrapError, RuntimeError)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(BootstrapError, match="boom"):
            raise BootstrapError("boom")


class TestCommandSequence:
    def test_three_steps_in_order(self) -> None:
        runner = FakeRunner()
        build_image(gapps="lite", runner=runner)
        # rm -rf, git clone, uv run patcher, docker compose build → 4 calls
        assert len(runner.calls) == 4
        assert runner.calls[0].cmd[0] == "rm"
        assert runner.calls[1].cmd[0] == "git"
        assert runner.calls[1].cmd[1] == "clone"
        assert runner.calls[2].cmd[0] == "uv"
        assert "redroid.py" in runner.calls[2].cmd
        assert runner.calls[3].cmd[1] == "compose"
        assert "build" in runner.calls[3].cmd

    def test_clone_uses_depth_one(self) -> None:
        runner = FakeRunner()
        build_image(runner=runner)
        clone = runner.calls[1].cmd
        depth_idx = clone.index("--depth")
        assert clone[depth_idx + 1] == "1"

    def test_clone_url_default(self) -> None:
        runner = FakeRunner()
        build_image(runner=runner)
        assert "https://github.com/ayasa520/redroid-script.git" in runner.calls[1].cmd

    def test_clone_url_override(self) -> None:
        runner = FakeRunner()
        url = "https://example.com/fork.git"
        build_image(redroid_script_url=url, runner=runner)
        assert url in runner.calls[1].cmd

    def test_work_dir_default(self) -> None:
        runner = FakeRunner()
        build_image(runner=runner)
        # rm and git clone both target the work dir
        assert "/tmp/redroid" in runner.calls[0].cmd
        assert "/tmp/redroid" in runner.calls[1].cmd
        # patcher runs from inside it
        assert runner.calls[2].cwd == Path("/tmp/redroid")

    def test_work_dir_override(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        build_image(work_dir=tmp_path, runner=runner)
        assert str(tmp_path) in runner.calls[0].cmd
        assert str(tmp_path) in runner.calls[1].cmd
        assert runner.calls[2].cwd == tmp_path

    def test_patcher_includes_android_version(self) -> None:
        runner = FakeRunner()
        build_image(android_version=13, runner=runner)
        patcher = runner.calls[2].cmd
        a_idx = patcher.index("-a")
        assert patcher[a_idx + 1] == "13.0.0"

    def test_patcher_always_includes_houdini_and_magisk(self) -> None:
        runner = FakeRunner()
        build_image(runner=runner)
        patcher = runner.calls[2].cmd
        assert "-i" in patcher
        assert "-m" in patcher

    def test_patcher_uv_run_uses_requests_and_tqdm(self) -> None:
        runner = FakeRunner()
        build_image(runner=runner)
        patcher = runner.calls[2].cmd
        assert patcher[:3] == ["uv", "run", "--with"]
        assert "requests" in patcher
        assert "tqdm" in patcher

    def test_docker_compose_build_uses_settings_docker_bin(self) -> None:
        runner = FakeRunner()
        with patch.object(settings, "docker_bin", "/opt/docker"):
            build_image(runner=runner)
        build = runner.calls[3].cmd
        assert build[0] == "/opt/docker"
        assert build[1] == "compose"
        assert "build" in build

    def test_docker_compose_build_passes_base_image_env(self) -> None:
        runner = FakeRunner()
        tag = build_image(gapps="lite", android_version=14, runner=runner)
        assert runner.calls[3].env is not None
        assert runner.calls[3].env["BASE_IMAGE"] == tag

    def test_docker_compose_build_points_at_bundled_template(self) -> None:
        from beetroot import paths
        runner = FakeRunner()
        build_image(runner=runner)
        build = runner.calls[3].cmd
        f_idx = build.index("-f")
        assert build[f_idx + 1] == str(paths.bundled_compose_file())


class TestGappsFlagInjection:
    @pytest.mark.parametrize(
        ("gapps", "expected_flag"),
        [
            ("lite", "-lg"),
            ("full", "-g"),
            ("mindthegapps", "-mtg"),
        ],
    )
    def test_variant_injects_correct_flag(self, gapps: GappsVariant, expected_flag: str) -> None:
        runner = FakeRunner()
        build_image(gapps=gapps, runner=runner)
        patcher = runner.calls[2].cmd
        assert expected_flag in patcher

    def test_none_injects_no_gapps_flag(self) -> None:
        runner = FakeRunner()
        build_image(gapps="none", runner=runner)
        patcher = runner.calls[2].cmd
        for flag in ("-lg", "-g", "-mtg"):
            assert flag not in patcher


class TestReturnedTag:
    @pytest.mark.parametrize(
        ("gapps", "version"),
        [
            ("none", 14),
            ("lite", 14),
            ("full", 13),
            ("mindthegapps", 12),
        ],
    )
    def test_tag_matches_config_base_image_tag(
        self, gapps: GappsVariant, version: int
    ) -> None:
        runner = FakeRunner()
        tag = build_image(gapps=gapps, android_version=version, runner=runner)
        expected = config.base_image_tag(config.Android(version=version, gapps=gapps))
        assert tag == expected

    def test_lite_default(self) -> None:
        runner = FakeRunner()
        tag = build_image(runner=runner)
        assert tag == "redroid/redroid:14.0.0_litegapps_houdini_magisk"


class TestFailures:
    def test_clone_failure_raises_bootstrap_error(self) -> None:
        runner = FakeRunner(fail_on="git")
        with pytest.raises(BootstrapError, match="git"):
            build_image(runner=runner)
        # docker compose build must not have run
        assert all(call.cmd[0] != "docker" for call in runner.calls)

    def test_patcher_failure_raises_bootstrap_error(self) -> None:
        runner = FakeRunner(fail_on="uv")
        with pytest.raises(BootstrapError, match="uv"):
            build_image(runner=runner)

    def test_build_failure_raises_bootstrap_error(self) -> None:
        runner = FakeRunner(fail_on="docker")
        with pytest.raises(BootstrapError, match="docker"):
            build_image(runner=runner)


class TestDefaultRunner:
    def test_run_invokes_subprocess_run_with_check(self) -> None:
        runner = DefaultRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            runner.run(["echo", "hi"], cwd=Path("/tmp"), env={"X": "1"})
        # env is merged on top of os.environ so the child still sees PATH etc.
        kwargs = mock_run.call_args.kwargs
        assert kwargs["cwd"] == Path("/tmp")
        assert kwargs["check"] is True
        assert kwargs["env"]["X"] == "1"
        assert "PATH" in kwargs["env"]

    def test_run_translates_calledprocesserror_to_bootstrap_error(self) -> None:
        runner = DefaultRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(2, ["false"])
            with pytest.raises(BootstrapError, match="exit 2"):
                runner.run(["false"])

    def test_run_defaults_cwd_and_env_to_none(self) -> None:
        runner = DefaultRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            runner.run(["true"])
        kwargs = mock_run.call_args.kwargs
        assert kwargs["cwd"] is None
        assert kwargs["env"] is None
        assert kwargs["check"] is True

    def test_run_passes_check_false_through(self) -> None:
        runner = DefaultRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            runner.run(["true"], check=False)
        assert mock_run.call_args.kwargs["check"] is False

    def test_run_env_none_does_not_inject_environ(self) -> None:
        # When env is None we must pass through None — never an empty dict
        # and never a copy of os.environ — so subprocess inherits the
        # parent's env unmodified.
        runner = DefaultRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            runner.run(["true"])
        assert mock_run.call_args.kwargs["env"] is None


class TestDefaultRunnerInjection:
    def test_no_runner_uses_default_runner(self) -> None:
        with patch.object(builder, "DefaultRunner") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.run.return_value = None
            build_image(gapps="lite")
        mock_cls.assert_called_once()
        # Four subprocess invocations: rm, clone, patch, build
        assert mock_inst.run.call_count == 4


class TestSettingsHonoured:
    def test_beetroot_docker_bin_env_overrides_build_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Settings is read at import time, so patch the attribute directly —
        # this mirrors how the rest of the suite (test_compose.py via shutil.which)
        # patches the resolved settings instance rather than re-reading env.
        monkeypatch.setattr(settings, "docker_bin", "/opt/docker")
        runner = FakeRunner()
        build_image(runner=runner)
        assert runner.calls[3].cmd[0] == "/opt/docker"
