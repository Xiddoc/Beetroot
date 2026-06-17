"""Tests for builder.py — base-image bootstrap (clone + patch + build)."""

from __future__ import annotations

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

    def __call__(
        self, *, out_image: Path, vm_dir: Path, runner: builder.RootfsRunner | None = None
    ) -> Path:
        self.calls.append((out_image, vm_dir))
        return out_image


@dataclass
class _KernelFetchRecorder:
    """A fake kernel_download.download: writes a stub bzImage and records calls."""

    src: Path
    calls: int = 0
    fail: Exception | None = None

    def __call__(self) -> Path:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.src.write_bytes(b"fake bzImage")
        return self.src


class TestBuildVmKernel:
    def test_fetches_kernel_then_assembles_rootfs(self, tmp_path: Path) -> None:
        cached = tmp_path / "cache" / "bzImage-6.12.9-x86_64"
        fetch = _KernelFetchRecorder(src=cached)
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        (ctx / "docker" / "vm").mkdir(parents=True)
        out = tmp_path / "out"
        artifacts = builder.build_vm_kernel(
            out_dir=out, build_context=ctx, kernel_fetch=fetch, rootfs_build=rec
        )
        # The kernel is now a downloaded prebuilt (no compile); the rootfs is
        # assembled in pure Python via the injected build_rootfs.
        assert fetch.calls == 1
        # The fetched, cached bzImage is copied to the canonical out/bzImage
        # that vm.yaml's vm.kernel references.
        assert artifacts.kernel == out / "bzImage"
        assert artifacts.kernel.read_bytes() == b"fake bzImage"
        assert rec.calls == [(out / "rootdisk.img", ctx / "docker" / "vm")]
        assert artifacts.rootfs == out / "rootdisk.img"
        assert out.is_dir()

    def test_kernel_fetch_failure_propagates_as_bootstrap_error(self, tmp_path: Path) -> None:
        fetch = _KernelFetchRecorder(
            src=tmp_path / "k", fail=kernel_download.KernelFetchError("HTTP 404")
        )
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        (ctx / "docker" / "vm").mkdir(parents=True)
        with pytest.raises(BootstrapError, match="HTTP 404"):
            builder.build_vm_kernel(
                out_dir=tmp_path / "out", build_context=ctx, kernel_fetch=fetch, rootfs_build=rec
            )
        assert rec.calls == []  # rootfs step never reached

    def test_kernel_sha_mismatch_propagates_as_bootstrap_error(self, tmp_path: Path) -> None:
        # A sha256 mismatch surfaces from kernel_download.download as a
        # ValueError; build_vm_kernel must translate it to BootstrapError so
        # the CLI's single except-clause catches it.
        fetch = _KernelFetchRecorder(src=tmp_path / "k", fail=ValueError("sha256 mismatch"))
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        (ctx / "docker" / "vm").mkdir(parents=True)
        with pytest.raises(BootstrapError, match="sha256 mismatch"):
            builder.build_vm_kernel(
                out_dir=tmp_path / "out", build_context=ctx, kernel_fetch=fetch, rootfs_build=rec
            )
        assert rec.calls == []

    def test_default_out_dir_under_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = tmp_path / "cache" / "vm"
        fetch = _KernelFetchRecorder(src=cache / "bzImage-6.12.9-x86_64")
        ctx = tmp_path / "repo"
        (ctx / "docker" / "vm").mkdir(parents=True)
        monkeypatch.setattr("beetroot.builder.paths.user_cache_dir", lambda _sub: cache)
        artifacts = builder.build_vm_kernel(
            build_context=ctx, kernel_fetch=fetch, rootfs_build=_RootfsBuildRecorder()
        )
        assert artifacts.kernel == cache / "bzImage"

    def test_default_build_context(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fetch = _KernelFetchRecorder(src=tmp_path / "cache" / "bzImage-6.12.9-x86_64")
        rec = _RootfsBuildRecorder()
        ctx = tmp_path / "repo"
        (ctx / "docker" / "vm").mkdir(parents=True)
        monkeypatch.setattr(builder, "_default_build_context", lambda: ctx)
        builder.build_vm_kernel(out_dir=tmp_path / "out", kernel_fetch=fetch, rootfs_build=rec)
        assert rec.calls[0][1] == ctx / "docker" / "vm"


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
