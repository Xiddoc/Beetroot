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

    def test_work_dir_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # v0.4 (T3) moved the default clone location off ``/tmp/redroid``
        # to the per-user cache via ``platformdirs`` (closes Agent 4's
        # ``S108`` bandit finding). Pin the cache root via the XDG env
        # var so the assertion has a stable expected path; platformdirs
        # honours ``XDG_CACHE_HOME`` on Linux.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        expected = str(tmp_path / "beetroot" / "redroid-script")
        runner = FakeRunner()
        build_image(runner=runner)
        assert expected in runner.calls[0].cmd
        assert expected in runner.calls[1].cmd
        assert runner.calls[2].cwd == Path(expected)

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

    def test_docker_compose_build_uses_settings_docker_bin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``settings`` is now a frozen pydantic model (T3) so direct
        # attribute assignment raises. Swap the module-level singleton
        # via the same monkeypatch pattern the env-tests use.
        from beetroot.settings import Settings
        monkeypatch.setattr(builder, "settings", Settings(docker_bin="/opt/docker"))
        runner = FakeRunner()
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
        # T3 froze ``Settings`` so the in-place ``setattr`` pattern no
        # longer works. Swap the module-level instance instead — this
        # is the contract documented in settings.py's module docstring.
        from beetroot.settings import Settings
        monkeypatch.setattr(builder, "settings", Settings(docker_bin="/opt/docker"))
        runner = FakeRunner()
        build_image(runner=runner)
        assert runner.calls[3].cmd[0] == "/opt/docker"


class TestCloneUrlMatches:
    """Unit tests for _clone_url_matches (idempotency helper)."""

    def test_returns_false_when_dir_does_not_exist(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches
        assert _clone_url_matches(tmp_path / "nonexistent", "https://example.com/repo.git") is False

    def test_returns_false_when_git_config_missing(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches
        (tmp_path / ".git").mkdir()
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is False

    def test_returns_true_when_url_matches(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            "[core]\n"
            "    repositoryformatversion = 0\n"
            '[remote "origin"]\n'
            "    url = https://example.com/repo.git\n"
            "    fetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is True

    def test_returns_false_when_url_differs(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            '[remote "origin"]\n'
            "    url = https://example.com/DIFFERENT.git\n"
        )
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is False

    def test_returns_false_when_no_remote_origin(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            "[core]\n"
            "    repositoryformatversion = 0\n"
        )
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is False

    def test_returns_true_strips_whitespace(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            '[remote "origin"]\n'
            "    url =   https://example.com/repo.git   \n"
        )
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is True


class TestBuilderIdempotency:
    """Builder idempotency: skip rm+clone when the clone URL already matches."""

    def test_skips_rm_and_clone_when_url_matches(self, tmp_path: Path) -> None:
        # Pre-stage a fake git clone with the default URL.
        from beetroot.builder import _DEFAULT_REDROID_URL
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            '[remote "origin"]\n'
            f"    url = {_DEFAULT_REDROID_URL}\n"
        )
        runner = FakeRunner()
        build_image(work_dir=tmp_path, runner=runner)

        # Only 2 calls: patch + compose build (rm and git clone skipped).
        assert len(runner.calls) == 2
        assert runner.calls[0].cmd[0] == "uv"
        assert runner.calls[1].cmd[1] == "compose"

    def test_re_clones_when_url_differs(self, tmp_path: Path) -> None:
        # Pre-stage a clone with a DIFFERENT URL.
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            '[remote "origin"]\n'
            "    url = https://example.com/DIFFERENT.git\n"
        )
        runner = FakeRunner()
        build_image(work_dir=tmp_path, runner=runner)

        # 4 calls: rm + clone (because URL mismatch) + patch + build.
        assert len(runner.calls) == 4
        assert runner.calls[0].cmd[0] == "rm"
        assert runner.calls[1].cmd[0] == "git"

    def test_re_clones_when_no_existing_clone(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        build_image(work_dir=tmp_path / "fresh", runner=runner)
        assert len(runner.calls) == 4
        assert runner.calls[0].cmd[0] == "rm"
        assert runner.calls[1].cmd[1] == "clone"

    def test_idempotent_run_does_not_change_return_value(self, tmp_path: Path) -> None:
        # The image tag returned must be the same regardless of whether clone
        # was skipped (the tag is derived from gapps+version, not the clone step).
        from beetroot.builder import _DEFAULT_REDROID_URL
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            '[remote "origin"]\n'
            f"    url = {_DEFAULT_REDROID_URL}\n"
        )
        runner = FakeRunner()
        tag = build_image(work_dir=tmp_path, runner=runner)
        assert tag == "redroid/redroid:14.0.0_litegapps_houdini_magisk"


class TestBuildContext:
    """build_context param: build context is derived from package, not cwd."""

    def test_explicit_build_context_used_in_compose_build(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        build_image(build_context=tmp_path, runner=runner)
        build_call = runner.calls[-1]
        # --project-directory must be the explicit build_context
        pd_idx = build_call.cmd.index("--project-directory")
        assert build_call.cmd[pd_idx + 1] == str(tmp_path)
        # BEETROOT_BUILD_CONTEXT env must also match
        assert build_call.env is not None
        assert build_call.env["BEETROOT_BUILD_CONTEXT"] == str(tmp_path)

    def test_default_build_context_derives_from_package(self) -> None:
        # The default is derived from paths.bundled_compose_file(), not Path.cwd().
        from beetroot import paths
        from beetroot.builder import _default_build_context
        expected = paths.bundled_compose_file().parent.parent.parent.parent
        assert _default_build_context() == expected

    def test_default_build_context_used_when_not_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from beetroot.builder import _default_build_context
        expected_ctx = _default_build_context()
        runner = FakeRunner()
        build_image(runner=runner)
        build_call = runner.calls[-1]
        pd_idx = build_call.cmd.index("--project-directory")
        assert build_call.cmd[pd_idx + 1] == str(expected_ctx)
        assert build_call.env is not None
        assert build_call.env["BEETROOT_BUILD_CONTEXT"] == str(expected_ctx)

    def test_default_build_context_is_not_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # cwd is changed to a random tmp dir; the default build context
        # must still resolve from the package, not cwd.
        from beetroot.builder import _default_build_context
        monkeypatch.chdir(tmp_path)
        ctx = _default_build_context()
        assert ctx != tmp_path


class TestBuilderProgress:
    """Console progress bars are invoked for each long phase."""

    def test_progress_called_for_clone_phase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Verify console.progress is called at least once during build_image.
        import io

        from rich.console import Console

        from beetroot import console
        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=False)
        console.set_consoles(stderr=test_console)
        runner = FakeRunner()
        build_image(runner=runner)
        # The progress context manager writes its description to stderr.
        output = buf.getvalue()
        # At minimum, one of the three progress labels must appear.
        assert any(
            label in output
            for label in ["redroid-script", "Patching", "Building"]
        ), f"no progress output captured; got: {output!r}"
