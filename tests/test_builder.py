"""Tests for builder.py — base-image bootstrap (clone + patch + build)."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from beetroot import builder, config, kernel_download
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

    def test_work_dir_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    def test_docker_compose_build_sets_placeholder_instance_name(self) -> None:
        # Regression for #114: recent Docker Compose validates the template's
        # ``container_name: ${INSTANCE_NAME}`` even on ``build``, so the
        # build-only env must carry a non-empty, pattern-valid placeholder or
        # the build aborts before producing ``beetroot:latest``.
        runner = FakeRunner()
        build_image(runner=runner)
        env = runner.calls[3].env
        assert env is not None
        name = env["INSTANCE_NAME"]
        assert name
        assert re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$", name)

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
    def test_tag_matches_config_base_image_tag(self, gapps: GappsVariant, version: int) -> None:
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
            '[remote "origin"]\n    url = https://example.com/DIFFERENT.git\n'
        )
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is False

    def test_returns_false_when_no_remote_origin(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches

        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n    repositoryformatversion = 0\n")
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is False

    def test_returns_true_strips_whitespace(self, tmp_path: Path) -> None:
        from beetroot.builder import _clone_url_matches

        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            '[remote "origin"]\n    url =   https://example.com/repo.git   \n'
        )
        assert _clone_url_matches(tmp_path, "https://example.com/repo.git") is True


class TestBuilderIdempotency:
    """Builder idempotency: skip rm+clone when the clone URL already matches."""

    def test_skips_rm_and_clone_when_url_matches(self, tmp_path: Path) -> None:
        # Pre-stage a fake git clone with the default URL.
        from beetroot.builder import _DEFAULT_REDROID_URL

        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            f'[remote "origin"]\n    url = {_DEFAULT_REDROID_URL}\n'
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
            '[remote "origin"]\n    url = https://example.com/DIFFERENT.git\n'
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
            f'[remote "origin"]\n    url = {_DEFAULT_REDROID_URL}\n'
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

    def test_default_build_context_used_when_not_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_env_build_context_used_for_image_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # BEETROOT_BUILD_CONTEXT overrides the default for the redroid layer
        # build too (not just --vm-kernel).
        from beetroot.settings import Settings

        monkeypatch.setattr(builder, "settings", Settings(build_context=str(tmp_path)))
        runner = FakeRunner()
        build_image(runner=runner)
        build_call = runner.calls[-1]
        pd_idx = build_call.cmd.index("--project-directory")
        assert build_call.cmd[pd_idx + 1] == str(tmp_path)
        assert build_call.env is not None
        assert build_call.env["BEETROOT_BUILD_CONTEXT"] == str(tmp_path)


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
        assert any(label in output for label in ["redroid-script", "Patching", "Building"]), (
            f"no progress output captured; got: {output!r}"
        )


@dataclass
class _RootfsBuildRecorder:
    """Records build_rootfs calls so build_vm_kernel can be tested in isolation."""

    calls: list[tuple[Path, Path]] = field(default_factory=list)
    android_versions: list[int] = field(default_factory=list)

    def __call__(
        self,
        *,
        out_image: Path,
        vm_dir: Path,
        android_version: int = config.DEFAULT_ANDROID_VERSION,
        runner: builder.RootfsRunner | None = None,
    ) -> Path:
        self.calls.append((out_image, vm_dir))
        self.android_versions.append(android_version)
        return out_image


def _make_vm_context(ctx: Path) -> Path:
    """Create a ``<ctx>/docker/vm`` with the assets ``_resolve_vm_dir`` requires."""
    vm = ctx / "docker" / "vm"
    vm.mkdir(parents=True)
    (vm / "kernel.config").write_text("CONFIG_FOO=y\n", encoding="utf-8")
    (vm / "guest-init.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return vm


class TestBuildVmKernel:
    def test_runs_kernel_then_rootfs_steps(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        out = tmp_path / "out"
        artifacts = builder.build_vm_kernel(
            out_dir=out, build_context=ctx, runner=runner, rootfs_build=rec, from_source=True
        )
        # The compile fallback now fetches + extracts the kernel source first
        # (curl, tar) and then runs the shell compile; the rootfs is assembled
        # in pure Python via the injected build_rootfs.
        assert [c.cmd[0] for c in runner.calls] == ["curl", "tar", "sh"]
        kernel_cmd = runner.calls[-1].cmd
        assert kernel_cmd[0] == "sh"
        assert kernel_cmd[1] == "-c"
        assert "kernel.config" in kernel_cmd[2]
        assert "bzImage" in kernel_cmd[2]
        # The compile runs from inside the freshly-extracted source tree.
        assert runner.calls[-1].cwd is not None
        assert runner.calls[-1].cwd.name == f"linux-{builder.KERNEL_VERSION}"
        assert rec.calls == [(out / "rootdisk.img", ctx / "docker" / "vm")]
        assert artifacts.kernel == out / "bzImage"
        assert artifacts.rootfs == out / "rootdisk.img"
        assert out.is_dir()

    def test_default_android_version_threaded_to_rootfs(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        builder.build_vm_kernel(
            out_dir=tmp_path / "out",
            build_context=ctx,
            runner=runner,
            rootfs_build=rec,
            from_source=True,
        )
        # An unflagged build bakes the shared default version (issue #82).
        assert rec.android_versions == [config.DEFAULT_ANDROID_VERSION]

    def test_explicit_android_version_threaded_to_rootfs(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        builder.build_vm_kernel(
            out_dir=tmp_path / "out",
            build_context=ctx,
            runner=runner,
            rootfs_build=rec,
            from_source=True,
            android_version=11,
        )
        assert rec.android_versions == [11]

    def test_uses_ccache_when_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeRunner()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ccache")
        builder.build_vm_kernel(
            out_dir=tmp_path / "out",
            build_context=ctx,
            runner=runner,
            rootfs_build=_RootfsBuildRecorder(),
            from_source=True,
        )
        assert 'make CC="ccache gcc" -j' in runner.calls[-1].cmd[2]

    def test_no_ccache_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = FakeRunner()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        builder.build_vm_kernel(
            out_dir=tmp_path / "out",
            build_context=ctx,
            runner=runner,
            rootfs_build=_RootfsBuildRecorder(),
            from_source=True,
        )
        cmd = runner.calls[-1].cmd[2]
        assert 'CC="ccache gcc"' not in cmd
        assert 'make -j"$(nproc)" bzImage' in cmd

    def test_kernel_step_failure_propagates(self, tmp_path: Path) -> None:
        runner = FakeRunner(fail_on="sh")
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        with pytest.raises(BootstrapError):
            builder.build_vm_kernel(
                out_dir=tmp_path / "out",
                build_context=ctx,
                runner=runner,
                rootfs_build=rec,
                from_source=True,
            )
        assert rec.calls == []  # rootfs step never reached

    def test_default_out_dir_under_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeRunner()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        cache = tmp_path / "cache" / "vm"
        monkeypatch.setattr("beetroot.builder.paths.user_cache_dir", lambda _sub: cache)
        artifacts = builder.build_vm_kernel(
            build_context=ctx, runner=runner, rootfs_build=_RootfsBuildRecorder(), from_source=True
        )
        assert artifacts.kernel == cache / "bzImage"

    def test_default_uses_bundled_vm_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no build_context and no BEETROOT_BUILD_CONTEXT, the assets come
        # from package data via paths.bundled_vm_dir — so a wheel install with
        # no docker/ tree still builds.
        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        bundled = _make_vm_context(tmp_path / "wheel").parent  # .../docker
        bundled = bundled / "vm"
        monkeypatch.setattr(builder, "_build_context_from_env", lambda: None)
        monkeypatch.setattr("beetroot.builder.paths.bundled_vm_dir", lambda: bundled)
        builder.build_vm_kernel(
            out_dir=tmp_path / "out", runner=runner, rootfs_build=rec, from_source=True
        )
        assert rec.calls[0][1] == bundled

    def test_env_build_context_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BEETROOT_BUILD_CONTEXT supplies the build context when no explicit
        # --build-context is passed; assets resolve from <ctx>/docker/vm.
        from beetroot.settings import Settings

        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "checkout"
        _make_vm_context(ctx)
        monkeypatch.setattr(builder, "settings", Settings(build_context=str(ctx)))
        # bundled_vm_dir must NOT be consulted when the env var is set.
        monkeypatch.setattr(
            "beetroot.builder.paths.bundled_vm_dir",
            lambda: pytest.fail("bundled_vm_dir should not be used when override set"),
        )
        builder.build_vm_kernel(
            out_dir=tmp_path / "out", runner=runner, rootfs_build=rec, from_source=True
        )
        assert rec.calls[0][1] == ctx / "docker" / "vm"

    def test_missing_assets_raise_actionable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty bundled dir (assets genuinely missing) raises an error that
        # names both fixes: a source checkout and --build-context / the env var.
        runner = FakeRunner()
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(builder, "_build_context_from_env", lambda: None)
        monkeypatch.setattr("beetroot.builder.paths.bundled_vm_dir", lambda: empty)
        with pytest.raises(BootstrapError) as exc:
            builder.build_vm_kernel(
                out_dir=tmp_path / "out",
                runner=runner,
                rootfs_build=_RootfsBuildRecorder(),
                from_source=True,
            )
        msg = str(exc.value)
        assert "kernel.config" in msg
        assert "guest-init.sh" in msg
        assert "--build-context" in msg
        assert "BEETROOT_BUILD_CONTEXT" in msg
        assert "source checkout" in msg
        assert runner.calls == []  # never reached the kernel compile

    def test_fetches_prebuilt_and_skips_compile(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        (ctx / "docker" / "vm" / "kernel.config").write_text("CONFIG_FOO=y\n")
        out = tmp_path / "out"
        fetch_calls: list[tuple[str, str, Path]] = []

        def fake_fetch(*, version: str, fingerprint: str, out_path: Path) -> Path:
            fetch_calls.append((version, fingerprint, out_path))
            out_path.write_bytes(b"prebuilt")
            return out_path

        artifacts = builder.build_vm_kernel(
            out_dir=out, build_context=ctx, runner=runner, rootfs_build=rec, kernel_fetch=fake_fetch
        )
        # Prebuilt fetched -> no source compile (no shell step), rootfs still built.
        assert runner.calls == []
        assert fetch_calls == [(builder.KERNEL_VERSION, fetch_calls[0][1], out / "bzImage")]
        assert rec.calls == [(out / "rootdisk.img", ctx / "docker" / "vm")]
        assert artifacts.kernel == out / "bzImage"

    def test_falls_back_to_compile_when_no_prebuilt(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        (ctx / "docker" / "vm" / "kernel.config").write_text("CONFIG_FOO=y\n")

        def failing_fetch(*, version: str, fingerprint: str, out_path: Path) -> Path:
            raise kernel_download.KernelFetchError("HTTP 404")

        builder.build_vm_kernel(
            out_dir=tmp_path / "out",
            build_context=ctx,
            runner=runner,
            rootfs_build=rec,
            kernel_fetch=failing_fetch,
        )
        # Fetch failed -> compiled from source: the source tree is fetched +
        # extracted (curl, tar) and the shell compile then runs.
        assert [c.cmd[0] for c in runner.calls] == ["curl", "tar", "sh"]
        assert "bzImage" in runner.calls[-1].cmd[2]

    def test_compile_fallback_fetches_and_extracts_kernel_source(self, tmp_path: Path) -> None:
        # On a fresh host (no prebuilt) the fallback must be self-contained: it
        # downloads linux-<version>.tar.xz from cdn.kernel.org, extracts it, and
        # compiles from the extracted tree rather than assuming the cwd is one
        # (issue #74).
        runner = FakeRunner()
        ctx = tmp_path / "repo"
        _make_vm_context(ctx)
        builder.build_vm_kernel(
            out_dir=tmp_path / "out",
            build_context=ctx,
            runner=runner,
            rootfs_build=_RootfsBuildRecorder(),
            from_source=True,
        )
        curl_cmd = runner.calls[0].cmd
        tar_cmd = runner.calls[1].cmd
        assert curl_cmd[0] == "curl"
        # cdn.kernel.org URL with the major-version dir derived from the version.
        url = next(a for a in curl_cmd if a.startswith("https://"))
        assert url == builder._kernel_source_url(builder.KERNEL_VERSION)
        assert "cdn.kernel.org" in url
        assert "/v6.x/" in url
        # downloaded to a tarball that tar then extracts.
        assert curl_cmd[-1].endswith(f"linux-{builder.KERNEL_VERSION}.tar.xz")
        assert tar_cmd[0] == "tar"
        assert "-xf" in tar_cmd
        # tar extracts into the scratch dir the sh compile then builds inside:
        # the compile cwd is <scratch>/linux-<version>, and tar's -C target is
        # that same <scratch> parent.
        compile_cwd = runner.calls[-1].cwd
        assert compile_cwd is not None
        extract_dir = tar_cmd[tar_cmd.index("-C") + 1]
        assert str(compile_cwd.parent) == extract_dir

    def test_relative_context_and_out_dir_resolve_to_absolute_in_compile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for the #74 cwd-boundary trap: the compile runs with
        # cwd=<scratch source tree>, so a *relative* build_context / out_dir must
        # be resolved to absolute paths first — else merge_config.sh can't find
        # the vendored kernel.config and `cp ... bzImage` lands in the throwaway
        # tree. Drive it with relative paths from a chdir'd cwd.
        monkeypatch.chdir(tmp_path)
        _make_vm_context(tmp_path / "repo")
        runner = FakeRunner()
        builder.build_vm_kernel(
            out_dir=Path("out"),
            build_context=Path("repo"),
            runner=runner,
            rootfs_build=_RootfsBuildRecorder(),
            from_source=True,
        )
        compile_cmd = runner.calls[-1].cmd[2]
        # The kernel.config arg to merge_config.sh is absolute.
        assert f"{tmp_path}/repo/docker/vm/kernel.config" in compile_cmd
        # The cp target (bzImage) is absolute, not relative to the scratch cwd.
        assert f"cp arch/x86/boot/bzImage {tmp_path}/out/bzImage" in compile_cmd

    def test_kernel_source_url_derives_major_version_dir(self) -> None:
        assert builder._kernel_source_url("6.12.9") == (
            "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.9.tar.xz"
        )
        assert builder._kernel_source_url("5.15.0") == (
            "https://cdn.kernel.org/pub/linux/kernel/v5.x/linux-5.15.0.tar.xz"
        )


# ---------------------------------------------------------------------------
# Micro-VM rootfs assembly (pure-Python port of build-rootfs.sh).
# ---------------------------------------------------------------------------


class _FakeBackground:
    """Records stop() so tests can assert the staging dockerd was torn down."""

    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


class FakeRootfsRunner:
    """RootfsRunner that records calls and materialises the files real tools produce."""

    def __init__(
        self,
        *,
        applets: Sequence[str] = ("sh", "mount", "poweroff"),
        ldd_output: str = "",
        info_ready: bool = True,
        fail_on: str | None = None,
    ) -> None:
        self.runs: list[list[str]] = []
        self.try_runs: list[list[str]] = []
        self.captures: list[list[str]] = []
        self.spawns: list[list[str]] = []
        self.applets = list(applets)
        self.ldd_output = ldd_output
        self.info_ready = info_ready
        self.fail_on = fail_on
        self.background = _FakeBackground()

    def run(
        self, cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        argv = list(cmd)
        self.runs.append(argv)
        if self.fail_on is not None and argv[0] == self.fail_on:
            raise BootstrapError(f"fake failure on {self.fail_on}")
        self._side_effects(argv)

    def try_run(
        self, cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> bool:
        self.try_runs.append(list(cmd))
        return self.info_ready

    def capture(self, cmd: Sequence[str], *, cwd: Path | None = None) -> str:
        argv = list(cmd)
        self.captures.append(argv)
        if argv[0] == "ldd":
            return self.ldd_output
        return "\n".join(self.applets) + "\n"

    def spawn(
        self,
        cmd: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> _FakeBackground:
        self.spawns.append(list(cmd))
        return self.background

    @staticmethod
    def _side_effects(argv: list[str]) -> None:
        head = argv[0]
        if head == "curl":
            out = Path(argv[argv.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"tgz")
        elif head == "tar":
            dbin = Path(argv[argv.index("-C") + 1]) / "docker"
            dbin.mkdir(parents=True, exist_ok=True)
            for name in builder._DOCKER_STATIC_BINS:
                (dbin / name).write_bytes(b"bin")
        elif head == "cc":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"adbprobe")
        elif "save" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"tar")
        elif head == "cp" and "-a" in argv:
            shutil.copytree(Path(argv[-2]), Path(argv[-1]))
        elif head == "mke2fs":
            Path(argv[-2]).write_bytes(b"ext4")


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder, "_sleep", lambda _seconds: None)


def _make_rootfs_config(tmp_path: Path, **overrides: object) -> builder._RootfsConfig:
    """Build a _RootfsConfig whose host source paths point at real tmp files."""
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir(exist_ok=True)
    (vm_dir / "guest-init.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    host = tmp_path / "host"
    host.mkdir(exist_ok=True)
    for name in ("busybox", "xtables-legacy-multi", "socat", "ld-linux.so"):
        (host / name).write_bytes(b"x")
    defaults: dict[str, object] = {
        "out_image": tmp_path / "rootdisk.img",
        "vm_dir": vm_dir,
        "docker_url": "http://example.invalid/docker.tgz",
        "busybox_bin": host / "busybox",
        "xtables_multi": host / "xtables-legacy-multi",
        "socat_bin": host / "socat",
        "ld_linux": host / "ld-linux.so",
    }
    defaults.update(overrides)
    return builder._RootfsConfig(**defaults)  # type: ignore[arg-type]


def _run_assembly(
    tmp_path: Path, runner: FakeRootfsRunner, cfg: builder._RootfsConfig, **kwargs: object
) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return builder._RootfsAssembly(cfg, runner, work, **kwargs).build()  # type: ignore[arg-type]


@pytest.mark.usefixtures("_no_sleep")
class TestRootfsAssembly:
    def test_full_build_stages_everything(self, tmp_path: Path) -> None:
        real_lib = tmp_path / "host" / "libreal.so"
        ldd = (
            "\tlinux-vdso.so.1 (0x00007fff)\n"  # 2 fields -> skipped
            f"\tlibreal.so => {real_lib} (0x00007f00)\n"  # copied
            "\tlibmissing.so => /nonexistent/lib.so (0x00007f01)\n"  # OSError -> skipped
            "\t/lib64/ld.so (0x00007f02)\n"  # loader line, 2 fields -> skipped
        )
        cfg = _make_rootfs_config(tmp_path)
        real_lib.write_bytes(b"lib")
        runner = FakeRootfsRunner(applets=("sh", "mount"), ldd_output=ldd)
        out = _run_assembly(tmp_path, runner, cfg)

        root = tmp_path / "work" / "root"
        assert out == cfg.out_image
        assert out.read_bytes() == b"ext4"  # mke2fs ran
        assert (root / "bin" / "busybox").is_file()
        assert (root / "bin" / "sh").is_symlink()
        assert (root / "bin" / "dockerd").is_file()
        assert (root / "usr" / "sbin" / "iptables").is_symlink()
        assert (root / "usr" / "sbin" / "iptables-legacy").is_symlink()
        assert (root / "bin" / "socat").is_file()
        assert (root / "init").is_file()
        assert (root / "var" / "lib" / "docker").is_dir()
        assert (root / "lib" / "x86_64-linux-gnu" / "libreal.so").is_file()
        assert not (root / "lib" / "x86_64-linux-gnu" / "lib.so").exists()
        assert (root / "lib64" / "ld-linux.so").is_file()
        assert (root / "tmp").stat().st_mode & 0o1777 == 0o1777
        # The redroid image was baked via pull + save + a staging dockerd.
        assert ["docker", "pull", cfg.redroid_image] in runner.runs
        assert runner.spawns
        assert runner.background.stopped == 1
        # issue #82: the baked Android version is recorded beside the image,
        # and the baked redroid tag is recorded inside the rootfs for guest-init.
        marker = builder.rootfs_version_marker(out)
        assert marker.read_text(encoding="utf-8").strip() == str(cfg.android_version)
        assert (root / "etc" / "beetroot" / "redroid-image").read_text(
            encoding="utf-8"
        ).strip() == cfg.redroid_image

    def test_baked_version_marker_tracks_configured_version(self, tmp_path: Path) -> None:
        cfg = _make_rootfs_config(
            tmp_path, android_version=11, redroid_image="redroid/redroid:11.0.0-latest"
        )
        runner = FakeRootfsRunner(applets=("sh",))
        out = _run_assembly(tmp_path, runner, cfg)
        assert builder.read_rootfs_version(out) == 11
        root = tmp_path / "work" / "root"
        assert (root / "etc" / "beetroot" / "redroid-image").read_text(
            encoding="utf-8"
        ).strip() == "redroid/redroid:11.0.0-latest"

    def _assembly(self, tmp_path: Path) -> builder._RootfsAssembly:
        cfg = _make_rootfs_config(tmp_path)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        return builder._RootfsAssembly(cfg, FakeRootfsRunner(), work)

    def test_verify_guest_image_marker_raises_when_missing(self, tmp_path: Path) -> None:
        # issue #97: a missing baked-image marker would make guest-init silently
        # fall back to a legacy image; the build must fail loudly instead.
        assembly = self._assembly(tmp_path)
        assembly.root.mkdir(parents=True, exist_ok=True)
        with pytest.raises(builder.BootstrapError, match="issue #97"):
            assembly._verify_guest_image_marker()

    def test_verify_guest_image_marker_raises_when_empty(self, tmp_path: Path) -> None:
        # A partially-written marker (whitespace only) is treated as missing.
        assembly = self._assembly(tmp_path)
        marker = assembly._guest_image_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("   \n", encoding="utf-8")
        with pytest.raises(builder.BootstrapError, match="missing or empty"):
            assembly._verify_guest_image_marker()

    def test_verify_guest_image_marker_passes_when_present(self, tmp_path: Path) -> None:
        assembly = self._assembly(tmp_path)
        marker = assembly._guest_image_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("redroid/redroid:14.0.0-latest\n", encoding="utf-8")
        assembly._verify_guest_image_marker()  # no raise

    def test_busybox_applet_does_not_clobber_real_binary(self, tmp_path: Path) -> None:
        # `busybox --list` includes `busybox` itself; symlinking /bin/busybox ->
        # busybox would replace the real binary with a self-referential link
        # (ELOOP), breaking /bin/sh and panicking the guest on /init. The real
        # binary must survive as a regular file while other applets are symlinks.
        cfg = _make_rootfs_config(tmp_path)
        runner = FakeRootfsRunner(applets=("busybox", "sh", "mount"))
        _run_assembly(tmp_path, runner, cfg)

        bin_dir = tmp_path / "work" / "root" / "bin"
        assert (bin_dir / "busybox").is_file()
        assert not (bin_dir / "busybox").is_symlink()
        assert (bin_dir / "sh").is_symlink()
        assert (bin_dir / "sh").readlink() == Path("busybox")

    def test_symlink_overwrites_existing(self, tmp_path: Path) -> None:
        link = tmp_path / "link"
        link.symlink_to("old-target")
        builder._RootfsAssembly._symlink("busybox", link)
        assert link.is_symlink()
        assert link.readlink() == Path("busybox")

    def test_prebuilt_adbprobe_is_copied(self, tmp_path: Path) -> None:
        probe = tmp_path / "adbprobe"
        probe.write_bytes(b"\x7fELF")
        probe.chmod(0o755)
        cfg = _make_rootfs_config(tmp_path, adbprobe_bin=probe)
        runner = FakeRootfsRunner()
        _run_assembly(tmp_path, runner, cfg)
        assert (tmp_path / "work" / "root" / "usr" / "bin" / "adbprobe").is_file()
        assert not any(c[0] == "cc" for c in runner.runs)

    def test_adbprobe_compiled_from_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _make_rootfs_config(tmp_path)
        (cfg.vm_dir / "adbprobe.c").write_text("int main(){}\n", encoding="utf-8")
        monkeypatch.setattr("beetroot.builder.shutil.which", lambda _name: "/usr/bin/cc")
        runner = FakeRootfsRunner()
        _run_assembly(tmp_path, runner, cfg)
        assert (tmp_path / "work" / "root" / "usr" / "bin" / "adbprobe").is_file()
        assert any(c[0] == "cc" for c in runner.runs)

    def test_adbprobe_compile_failure_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _make_rootfs_config(tmp_path)
        (cfg.vm_dir / "adbprobe.c").write_text("int main(){}\n", encoding="utf-8")
        monkeypatch.setattr("beetroot.builder.shutil.which", lambda _name: "/usr/bin/cc")
        runner = FakeRootfsRunner(fail_on="cc")
        out = _run_assembly(tmp_path, runner, cfg)
        # Build still completes; adbprobe is simply absent.
        assert out.read_bytes() == b"ext4"
        assert not (tmp_path / "work" / "root" / "usr" / "bin" / "adbprobe").exists()

    def test_prebuilt_tarball_skips_pull(self, tmp_path: Path) -> None:
        tar = tmp_path / "redroid.tar"
        tar.write_bytes(b"tar")
        cfg = _make_rootfs_config(tmp_path, redroid_tar=tar)
        runner = FakeRootfsRunner()
        _run_assembly(tmp_path, runner, cfg)
        assert not any("pull" in c for c in runner.runs)
        assert not any("save" in c for c in runner.runs)

    def test_staging_dockerd_never_ready_raises(self, tmp_path: Path) -> None:
        cfg = _make_rootfs_config(tmp_path)
        runner = FakeRootfsRunner(info_ready=False)
        with pytest.raises(BootstrapError, match="staging dockerd"):
            _run_assembly(tmp_path, runner, cfg, ready_attempts=2)
        # The spawned daemon is still torn down on the failure path.
        assert runner.background.stopped == 1

    def test_fetch_failure_propagates(self, tmp_path: Path) -> None:
        cfg = _make_rootfs_config(tmp_path)
        runner = FakeRootfsRunner(fail_on="curl")
        with pytest.raises(BootstrapError, match="curl"):
            _run_assembly(tmp_path, runner, cfg)


class TestRootfsConfigFromEnv:
    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "DOCKER_VERSION",
            "DOCKER_URL",
            "REDROID_TAR",
            "ADBPROBE_BIN",
            "IMAGE_SIZE_MB",
            "REDROID_IMAGE",
            "BUSYBOX_BIN",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = builder._RootfsConfig.from_env(out_image=Path("/o.img"), vm_dir=Path("/vm"))
        assert cfg.docker_version == "27.5.1"
        assert cfg.docker_url.endswith("docker-27.5.1.tgz")
        assert cfg.redroid_tar is None
        assert cfg.adbprobe_bin is None
        assert cfg.image_size_mb == 8192
        assert cfg.busybox_bin == Path("/usr/bin/busybox")
        # issue #82: with no REDROID_IMAGE env, the image is derived from the
        # default Android version (NOT the old hardcoded 11).
        assert cfg.android_version == config.DEFAULT_ANDROID_VERSION
        assert cfg.redroid_image == config.vm_redroid_image(config.DEFAULT_ANDROID_VERSION)

    def test_android_version_derives_image_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REDROID_IMAGE", raising=False)
        cfg = builder._RootfsConfig.from_env(
            out_image=Path("/o.img"), vm_dir=Path("/vm"), android_version=11
        )
        assert cfg.android_version == 11
        assert cfg.redroid_image == "redroid/redroid:11.0.0-latest"

    def test_redroid_image_env_wins_over_android_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDROID_IMAGE", "redroid/redroid:13.0.0-latest")
        cfg = builder._RootfsConfig.from_env(
            out_image=Path("/o.img"), vm_dir=Path("/vm"), android_version=11
        )
        # The version field still tracks the requested value (the marker), but
        # the explicit env override selects the actual image to bake.
        assert cfg.android_version == 11
        assert cfg.redroid_image == "redroid/redroid:13.0.0-latest"

    def test_env_overrides_are_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_VERSION", "26.0.0")
        monkeypatch.delenv("DOCKER_URL", raising=False)
        monkeypatch.setenv("REDROID_TAR", "/tmp/r.tar")
        monkeypatch.setenv("ADBPROBE_BIN", "/usr/local/bin/adbprobe")
        monkeypatch.setenv("IMAGE_SIZE_MB", "4096")
        monkeypatch.setenv("REDROID_IMAGE", "redroid/redroid:12.0.0-latest")
        monkeypatch.setenv("BUSYBOX_BIN", "/bin/busybox")
        cfg = builder._RootfsConfig.from_env(out_image=Path("/o.img"), vm_dir=Path("/vm"))
        assert cfg.docker_version == "26.0.0"
        assert cfg.docker_url.endswith("docker-26.0.0.tgz")  # derived from version
        assert cfg.redroid_tar == Path("/tmp/r.tar")
        assert cfg.adbprobe_bin == Path("/usr/local/bin/adbprobe")
        assert cfg.image_size_mb == 4096
        assert cfg.redroid_image == "redroid/redroid:12.0.0-latest"
        assert cfg.busybox_bin == Path("/bin/busybox")

    def test_explicit_docker_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_URL", "http://mirror.invalid/d.tgz")
        monkeypatch.setenv("REDROID_TAR", "")  # empty -> None via `or None`
        cfg = builder._RootfsConfig.from_env(out_image=Path("/o.img"), vm_dir=Path("/vm"))
        assert cfg.docker_url == "http://mirror.invalid/d.tgz"
        assert cfg.redroid_tar is None


class TestRootfsVersionMarker:
    def test_marker_path_sits_beside_image(self, tmp_path: Path) -> None:
        img = tmp_path / "rootdisk.img"
        assert builder.rootfs_version_marker(img) == tmp_path / "rootdisk.img.android-version"

    def test_read_returns_none_when_no_marker(self, tmp_path: Path) -> None:
        assert builder.read_rootfs_version(tmp_path / "rootdisk.img") is None

    def test_read_parses_recorded_version(self, tmp_path: Path) -> None:
        img = tmp_path / "rootdisk.img"
        builder.rootfs_version_marker(img).write_text("13\n", encoding="utf-8")
        assert builder.read_rootfs_version(img) == 13

    def test_read_returns_none_on_garbage_marker(self, tmp_path: Path) -> None:
        img = tmp_path / "rootdisk.img"
        builder.rootfs_version_marker(img).write_text("not-an-int\n", encoding="utf-8")
        assert builder.read_rootfs_version(img) is None


class TestBuildRootfs:
    def test_delegates_to_assembly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        class _FakeAssembly:
            def __init__(
                self,
                cfg: builder._RootfsConfig,
                runner: object,
                work: Path,
                **_kwargs: object,
            ) -> None:
                seen["cfg"] = cfg
                seen["runner"] = runner
                seen["work_exists"] = work.is_dir()

            def build(self) -> Path:
                return tmp_path / "rootdisk.img"

        monkeypatch.setattr(builder, "_RootfsAssembly", _FakeAssembly)
        runner = FakeRootfsRunner()
        out = builder.build_rootfs(
            out_image=tmp_path / "rootdisk.img", vm_dir=tmp_path / "vm", runner=runner
        )
        assert out == tmp_path / "rootdisk.img"
        assert seen["runner"] is runner
        assert seen["work_exists"] is True
        assert isinstance(seen["cfg"], builder._RootfsConfig)

    def test_android_version_flows_into_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        class _FakeAssembly:
            def __init__(
                self, cfg: builder._RootfsConfig, runner: object, work: Path, **_kw: object
            ) -> None:
                seen["cfg"] = cfg

            def build(self) -> Path:
                return tmp_path / "rootdisk.img"

        monkeypatch.delenv("REDROID_IMAGE", raising=False)
        monkeypatch.setattr(builder, "_RootfsAssembly", _FakeAssembly)
        builder.build_rootfs(
            out_image=tmp_path / "rootdisk.img",
            vm_dir=tmp_path / "vm",
            android_version=11,
            runner=FakeRootfsRunner(),
        )
        cfg = seen["cfg"]
        assert isinstance(cfg, builder._RootfsConfig)
        assert cfg.android_version == 11
        assert cfg.redroid_image == "redroid/redroid:11.0.0-latest"

    def test_default_runner_is_constructed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class _FakeAssembly:
            def __init__(self, cfg: object, runner: object, work: Path, **_kwargs: object) -> None:
                captured["runner"] = runner

            def build(self) -> Path:
                return tmp_path / "rootdisk.img"

        monkeypatch.setattr(builder, "_RootfsAssembly", _FakeAssembly)
        builder.build_rootfs(out_image=tmp_path / "rootdisk.img", vm_dir=tmp_path / "vm")
        assert isinstance(captured["runner"], builder.DefaultRootfsRunner)


class TestDefaultRootfsRunner:
    def test_run_success_and_failure(self) -> None:
        runner = builder.DefaultRootfsRunner()
        runner.run(["true"], env={"X": "1"})  # env-merge branch
        with pytest.raises(BootstrapError):
            runner.run(["false"])

    def test_try_run_reports_exit_status(self) -> None:
        runner = builder.DefaultRootfsRunner()
        assert runner.try_run(["true"]) is True
        assert runner.try_run(["false"], env={"X": "1"}) is False

    def test_capture_returns_stdout(self) -> None:
        runner = builder.DefaultRootfsRunner()
        assert runner.capture(["printf", "hello"]) == "hello"
        with pytest.raises(BootstrapError):
            runner.capture(["false"])

    def test_spawn_writes_log_then_stops(self, tmp_path: Path) -> None:
        runner = builder.DefaultRootfsRunner()
        log = tmp_path / "daemon.log"
        proc = runner.spawn(["sh", "-c", "echo hi; sleep 30"], env={"X": "1"}, log_path=log)
        proc.stop()
        assert log.is_file()

    def test_spawn_without_log(self) -> None:
        runner = builder.DefaultRootfsRunner()
        proc = runner.spawn(["sleep", "30"])
        proc.stop()


class TestSleepHelper:
    def test_sleep_invokes_time_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr("beetroot.builder.time.sleep", slept.append)
        builder._sleep(0.0)
        assert slept == [0.0]


class TestDockerDaemonResponsive:
    def test_true_when_info_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "beetroot.builder.subprocess.run",
            lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=0),
        )
        assert builder._docker_daemon_responsive() is True

    def test_false_when_info_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "beetroot.builder.subprocess.run",
            lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=1),
        )
        assert builder._docker_daemon_responsive() is False

    def test_false_when_docker_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object, **_k: object) -> object:
            raise FileNotFoundError

        monkeypatch.setattr("beetroot.builder.subprocess.run", _boom)
        assert builder._docker_daemon_responsive() is False


class TestVmBuildPreflight:
    def _cfg(self, tmp_path: Path, *, present: tuple[str, ...]) -> builder._RootfsConfig:
        # Build a _RootfsConfig whose static-binary paths point under tmp_path;
        # create only the files named in ``present`` so the rest read as missing.
        paths = {name: tmp_path / name for name in ("busybox", "socat", "xtables-legacy-multi")}
        for name in present:
            paths[name].write_bytes(b"\x7fELF")
        return builder._RootfsConfig(
            out_image=tmp_path / "rootdisk.img",
            vm_dir=tmp_path / "vm",
            docker_url="https://example/docker.tgz",
            busybox_bin=paths["busybox"],
            socat_bin=paths["socat"],
            xtables_multi=paths["xtables-legacy-multi"],
        )

    def _ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        present: tuple[str, ...] = ("busybox", "socat", "xtables-legacy-multi"),
        which: object = None,
        daemon: bool = True,
    ) -> None:
        cfg = self._cfg(tmp_path, present=present)
        monkeypatch.setattr(builder._RootfsConfig, "from_env", lambda **_k: cfg)
        monkeypatch.setattr("beetroot.builder.shutil.which", which or (lambda _n: "/usr/bin/found"))
        monkeypatch.setattr(builder, "_docker_daemon_responsive", lambda: daemon)

    def test_ready_host_has_no_problems(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ready(monkeypatch, tmp_path)
        assert builder.vm_build_preflight() == []

    def test_missing_static_bin_reported_with_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ready(monkeypatch, tmp_path, present=("busybox", "xtables-legacy-multi"))
        problems = builder.vm_build_preflight()
        assert [p.requirement for p in problems] == ["socat"]
        assert problems[0].fix == "apt-get install socat"

    def test_missing_path_tool_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ready(
            monkeypatch, tmp_path, which=lambda n: None if n == "curl" else "/usr/bin/found"
        )
        problems = builder.vm_build_preflight()
        assert [p.requirement for p in problems] == ["curl"]
        assert "curl" in problems[0].fix

    def test_docker_cli_missing_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ready(
            monkeypatch, tmp_path, which=lambda n: None if n == "docker" else "/usr/bin/found"
        )
        problems = builder.vm_build_preflight()
        assert [p.requirement for p in problems] == ["docker"]

    def test_daemon_down_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._ready(monkeypatch, tmp_path, daemon=False)
        problems = builder.vm_build_preflight()
        assert [p.requirement for p in problems] == ["Docker daemon"]
        assert "REDROID_TAR" in problems[0].fix  # rate-limit guidance

    def test_redroid_tar_skips_daemon_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ready(monkeypatch, tmp_path, daemon=False)
        # With a pre-saved tarball the bake never pulls, so a down daemon is fine.
        assert builder.vm_build_preflight(redroid_tar=tmp_path / "redroid.tar") == []

    def test_reports_everything_missing_in_one_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of #78: a bare host surfaces ALL gaps at once.
        self._ready(monkeypatch, tmp_path, present=(), which=lambda _n: None, daemon=False)
        names = {p.requirement for p in builder.vm_build_preflight()}
        assert {"busybox", "socat", "xtables-legacy-multi", "curl", "tar"} <= names
