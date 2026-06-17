"""
One-time base-image builder.

Rewrite of the legacy ``scripts/setup.sh`` as testable Python. Clones the
external ``ayasa520/redroid-script`` patcher, runs it to bake Magisk +
optional GApps + Houdini into a redroid base image, then layers Beetroot's
own ``entrypoint.sh`` / ``stealth.rc`` on top via ``docker compose build``.

Public surface:

* :class:`SubprocessRunner` — protocol describing how subprocess calls are
  dispatched. The default :class:`DefaultRunner` shells out via
  ``subprocess.run(check=True)``; tests inject a recording fake.
* :class:`BootstrapError` — raised when any step (clone, patch, build)
  fails.
* :data:`GAPPS_FLAGS` — mapping from gapps variant to the patcher CLI flags
  it needs.
* :func:`build_image` — entry point that orchestrates the three steps and
  returns the resulting image tag.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from . import config, console, kernel_download, paths
from .settings import settings

GappsVariant = Literal["none", "lite", "full", "mindthegapps"]

GAPPS_FLAGS: Final[dict[GappsVariant, list[str]]] = {
    "none": [],
    "lite": ["-lg"],
    "full": ["-g"],
    "mindthegapps": ["-mtg"],
}

_DEFAULT_REDROID_URL: Final[str] = "https://github.com/ayasa520/redroid-script.git"


def _default_work_dir() -> Path:
    """
    Return the default redroid-script clone directory under the user cache.

    Computed lazily so tests can monkeypatch ``platformdirs`` before the
    first call. v0.3 used ``/tmp/redroid``; v0.4 moves it under the
    per-user cache (T3) so it survives ``/tmp`` cleanups, and removes a
    static ``S108`` bandit finding.
    """
    return paths.user_cache_dir("redroid-script")


def _default_build_context() -> Path:
    """
    Return the default Docker build context for the Beetroot layer.

    For a source / editable install the bundled compose template lives at
    ``src/beetroot/templates/compose.yaml`` inside the repo, so walking four
    levels up yields the repo root (which contains ``docker/Dockerfile``).
    For a ``uv tool install`` wheel build the caller MUST pass an explicit
    ``build_context`` because ``docker/`` is not bundled in the wheel and the
    path derived here would be a cache directory with no ``docker/`` child.
    """
    return paths.bundled_compose_file().parent.parent.parent.parent


class BootstrapError(RuntimeError):
    """
    Raised when a bootstrap step (git clone, patcher, build) fails.
    """


class SubprocessRunner(Protocol):
    """
    Strategy object that executes external commands.

    Implementations should raise :class:`BootstrapError` (or let the caller
    translate a ``subprocess.CalledProcessError`` to one) when ``check`` is
    ``True`` and the command exits non-zero. Keeping this as a Protocol
    means tests can inject a fake that records calls without monkey-patching
    the global :mod:`subprocess` module.
    """

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Execute ``cmd``, optionally in ``cwd`` with extra environment.

        Args:
            cmd: The argv to execute, including the binary as ``cmd[0]``.
            cwd: Working directory; ``None`` inherits the parent's.
            check: If ``True``, raise on non-zero exit.
            env: Full environment to pass; ``None`` inherits the parent's.
        """
        ...


class DefaultRunner:
    """
    Production :class:`SubprocessRunner` that shells out via :mod:`subprocess`.

    Translates :class:`subprocess.CalledProcessError` into
    :class:`BootstrapError` so callers only need to catch one exception type.
    """

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Run ``cmd`` via :func:`subprocess.run`.

        Args:
            cmd: The argv to execute.
            cwd: Working directory; ``None`` inherits the parent's.
            check: If ``True``, raise :class:`BootstrapError` on non-zero exit.
            env: Extra environment to overlay on the parent's. ``None``
                inherits the parent's environment unmodified. A non-None
                dict is merged on top of ``os.environ`` rather than
                replacing it, so the child still sees ``PATH``,
                ``HOME``, ``DOCKER_CONFIG``, etc. — without this merge a
                bare ``{"BASE_IMAGE": tag}`` would launch ``docker``
                with no ``PATH`` and the build would fail with
                ``FileNotFoundError`` on a fresh shell.

        Raises:
            BootstrapError: If ``check`` is ``True`` and the command exits non-zero.
        """
        merged_env = {**os.environ, **env} if env is not None else None
        try:
            subprocess.run(list(cmd), cwd=cwd, check=check, env=merged_env)  # noqa: S603  # argv passed through from build_image; resolved via PATH
        except subprocess.CalledProcessError as exc:
            raise BootstrapError(
                f"command failed (exit {exc.returncode}): {' '.join(cmd)}"
            ) from exc


def _image_tag(android_version: int, gapps: GappsVariant) -> str:
    """
    Compute the base-image tag for ``(version, gapps)``, mirroring config.base_image_tag.
    """
    android = config.Android(version=android_version, gapps=gapps)
    return config.base_image_tag(android)


def _clone_url_matches(work: Path, url: str) -> bool:
    """
    Return True iff ``work`` is a git clone of exactly ``url``.

    Reads ``work/.git/config`` and looks for a ``[remote "origin"]`` stanza
    whose ``url`` key equals the requested URL (after stripping whitespace).
    A missing directory, missing ``.git/config``, or a URL mismatch all
    return False so the caller knows a fresh clone is required.
    """
    git_config = work / ".git" / "config"
    if not git_config.is_file():
        return False
    in_origin = False
    for line in git_config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == '[remote "origin"]':
            in_origin = True
        elif stripped.startswith("["):
            in_origin = False
        elif in_origin and stripped.startswith("url") and "=" in stripped:
            _, _, existing_url = stripped.partition("=")
            return existing_url.strip() == url
    return False


def build_image(  # noqa: PLR0913  # 6 keyword-only params; each is a distinct injectable concern
    *,
    gapps: GappsVariant = "lite",
    android_version: int = 14,
    redroid_script_url: str = _DEFAULT_REDROID_URL,
    work_dir: Path | None = None,
    build_context: Path | None = None,
    runner: SubprocessRunner | None = None,
) -> str:
    """
    Patch and build the redroid base image + Beetroot layer for ``gapps``.

    The three steps are:

    1. ``git clone --depth 1 <redroid_script_url> <work_dir>``.  If
       ``work_dir`` already contains a clone of the same URL the clone step
       is skipped so re-running ``beetroot build`` after a network interruption
       doesn't discard already-downloaded Magisk / GApps / Houdini artifacts.
       A pre-existing clone of a **different** URL is wiped and re-cloned so
       the caller never silently builds against the wrong source.
    2. ``uv run --with requests --with tqdm python -W ignore redroid.py
       -a <version>.0.0 [gapps-flag] -i -m`` from inside ``work_dir``.
    3. ``BASE_IMAGE=<tag> <docker_bin> compose build`` from ``build_context``
       (see below).

    Args:
        gapps: GMS variant to bake in.
        android_version: Android major version. Must be one of ``11``,
            ``12``, ``13``, ``14`` — validated against
            :class:`beetroot.config.Android`.
        redroid_script_url: Override the patcher source (testing / forks).
        work_dir: Override the clone directory (default is a subdir of
            the user cache; see :func:`_default_work_dir`).
        build_context: Directory passed to Docker as the build context and
            as ``--project-directory``.  Must contain a ``docker/``
            sub-directory with ``Dockerfile`` and the boot-script helpers.
            Defaults to ``paths.bundled_compose_file().parent.parent.parent.parent``
            — the repo root for a source / editable install.  For a
            ``uv tool install``-based setup you MUST pass this explicitly
            because ``docker/`` is not bundled in the wheel.
        runner: Inject a :class:`SubprocessRunner` for testing. Defaults to
            :class:`DefaultRunner`.

    Returns:
        The full base-image tag that was built, e.g.
        ``redroid/redroid:14.0.0_litegapps_houdini_magisk``.

    Raises:
        BootstrapError: If git clone, the patcher, or ``docker compose
            build`` fail.
        ValueError: If ``android_version`` is not one of the supported
            redroid versions (delegated to :class:`beetroot.config.Android`).
    """
    work = work_dir if work_dir is not None else _default_work_dir()
    ctx = build_context if build_context is not None else _default_build_context()
    run = runner if runner is not None else DefaultRunner()

    tag = _image_tag(android_version, gapps)

    # Step 1: clone the patcher — skip when an identical clone already exists
    # so a re-run doesn't discard already-downloaded Houdini / Magisk /
    # GApps artifacts.  Wipe and re-clone when the URL differs (different fork
    # or a corrupted work dir).
    with console.progress("Cloning redroid-script"):
        if _clone_url_matches(work, redroid_script_url):
            console.info(f"reusing existing clone at {work}")
        else:
            run.run(["rm", "-rf", str(work)])
            run.run(
                ["git", "clone", "--depth", "1", redroid_script_url, str(work)],
            )

    # Step 2: patch. ``-i`` installs Houdini, ``-m`` installs Magisk.
    patcher_cmd: list[str] = [
        "uv",
        "run",
        "--with",
        "requests",
        "--with",
        "tqdm",
        "python",
        "-W",
        "ignore",
        "redroid.py",
        "-a",
        f"{android_version}.0.0",
        *GAPPS_FLAGS[gapps],
        "-i",
        "-m",
    ]
    with console.progress("Patching base image (Magisk + Houdini + GApps)"):
        run.run(patcher_cmd, cwd=work)

    # Step 3: build the Beetroot layer on top via the bundled compose template.
    # ``build_context`` is the directory Docker uses as the build context — it
    # must contain ``docker/Dockerfile`` and the boot-script helpers.  Using
    # ``Path.cwd()`` here (the old behaviour) broke programmatic / uv-tool
    # invocations from outside the repo: the Dockerfile would not be found and
    # the build would fail with a misleading "context not found" error.
    with console.progress("Building Beetroot Docker layer"):
        run.run(
            [
                settings.docker_bin,
                "compose",
                "-f",
                str(paths.bundled_compose_file()),
                "--project-directory",
                str(ctx),
                "build",
            ],
            env={"BASE_IMAGE": tag, "BEETROOT_BUILD_CONTEXT": str(ctx)},
        )

    return tag


# ---------------------------------------------------------------------------
# Micro-VM guest rootfs assembly.
#
# Pure-Python port of the former ``docker/vm/build-rootfs.sh``. The §4.2 recipe
# from ``docs/design/binderless-hosts-qemu-tcg.md`` (corrected against the
# Stage B build log in ``docs/design/vm-rnd-log.md``): a busybox-static +
# Docker-static-bundle + static iptables-legacy + socat rootfs with the redroid
# image baked into ``/var/lib/docker`` (so the guest boots fully offline) and
# ``guest-init.sh`` installed as ``/init``. Filesystem layout work is done with
# the stdlib (typed, unit-testable against a tmp tree); only the external tools
# that have no clean stdlib equivalent (curl, tar, dockerd/docker, ldd, cc,
# mke2fs, cp -a) are dispatched through an injectable :class:`RootfsRunner`.
# ---------------------------------------------------------------------------

# Docker static bundle binaries staged into the guest /bin (dockerd's bridge
# driver also needs an iptables binary, staged separately below).
_DOCKER_STATIC_BINS: Final[tuple[str, ...]] = (
    "dockerd",
    "containerd",
    "containerd-shim-runc-v2",
    "runc",
    "docker",
    "ctr",
    "docker-proxy",
    "docker-init",
)

# Skeleton directories created in the guest rootfs before anything is staged.
_ROOTFS_DIRS: Final[tuple[str, ...]] = (
    "bin",
    "sbin",
    "proc",
    "sys",
    "dev",
    "run",
    "var/run",
    "var/log",
    "var/lib",
    "sys/fs/cgroup",
    "dev/binderfs",
    "etc",
    "tmp",
    "usr/sbin",
    "usr/bin",
    "lib/x86_64-linux-gnu",
    "lib64",
    "usr/lib/x86_64-linux-gnu",
)

# iptables-legacy multi-binary aliases dockerd's bridge driver looks up by name.
_IPTABLES_LINKS: Final[tuple[str, ...]] = (
    "iptables",
    "iptables-save",
    "iptables-restore",
    "ip6tables",
    "ip6tables-save",
    "ip6tables-restore",
)

# Where shared libraries resolved via ``ldd`` are staged in the guest.
_GUEST_LIB_DIR: Final[str] = "lib/x86_64-linux-gnu"

# An ``ldd`` ``lib => /path (0x..)`` dependency line has the absolute path in
# its 3rd whitespace field; vdso / loader lines have fewer fields.
_LDD_MIN_FIELDS: Final[int] = 3
_LDD_PATH_INDEX: Final[int] = 2

# Upper bound on the readiness poll for the throwaway staging dockerd used to
# bake the redroid image. One probe per second, mirroring the shell original.
_DOCKERD_READY_ATTEMPTS: Final[int] = 60

_DEFAULT_DOCKER_VERSION: Final[str] = "27.5.1"
_DEFAULT_REDROID_IMAGE: Final[str] = "redroid/redroid:11.0.0-latest"


def _sleep(seconds: float) -> None:
    """Sleep ``seconds`` (indirection so tests can monkeypatch the wait out)."""
    time.sleep(seconds)


class BackgroundProcess(Protocol):
    """A long-running child process (the throwaway staging dockerd) that can be stopped."""

    def stop(self) -> None:
        """Terminate the process and release any associated resources."""
        ...


class RootfsRunner(Protocol):
    """
    Executes the external tools the rootfs assembly cannot do in pure stdlib.

    Richer than :class:`SubprocessRunner`: the rootfs build needs to capture
    stdout (``ldd``, ``busybox --list``), probe a command's success without
    raising (the staging dockerd readiness loop), and spawn a background
    daemon. Tests inject a recording fake that materialises the files real
    tools would produce.
    """

    def run(
        self, cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        """Run ``cmd`` to completion, raising :class:`BootstrapError` on failure."""
        ...

    def try_run(
        self, cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> bool:
        """Run ``cmd`` and return whether it exited zero (never raises)."""
        ...

    def capture(self, cmd: Sequence[str], *, cwd: Path | None = None) -> str:
        """Run ``cmd`` and return its captured stdout, raising on failure."""
        ...

    def spawn(
        self,
        cmd: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> BackgroundProcess:
        """Start ``cmd`` in the background, streaming its output to ``log_path``."""
        ...


class _PopenProcess:
    """Adapts a :class:`subprocess.Popen` to the :class:`BackgroundProcess` protocol."""

    def __init__(self, proc: subprocess.Popen[bytes], log_handle: IO[bytes] | None) -> None:
        self._proc = proc
        self._log = log_handle

    def stop(self) -> None:
        """Terminate the child, escalating to SIGKILL if it ignores SIGTERM."""
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover  # defensive: SIGTERM-ignoring child
            self._proc.kill()
            self._proc.wait()
        if self._log is not None:
            self._log.close()


class DefaultRootfsRunner:
    """Production :class:`RootfsRunner` backed by :mod:`subprocess`."""

    def run(
        self, cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        """Run ``cmd`` via :func:`subprocess.run`, raising :class:`BootstrapError` on failure."""
        merged_env = {**os.environ, **env} if env is not None else None
        try:
            subprocess.run(list(cmd), cwd=cwd, env=merged_env, check=True)  # noqa: S603  # argv built from module constants + validated config
        except subprocess.CalledProcessError as exc:
            raise BootstrapError(
                f"command failed (exit {exc.returncode}): {' '.join(cmd)}"
            ) from exc

    def try_run(
        self, cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> bool:
        """Run ``cmd`` quietly and return ``True`` iff it exited zero."""
        merged_env = {**os.environ, **env} if env is not None else None
        return (
            subprocess.run(  # noqa: S603  # argv built from module constants + validated config
                list(cmd),
                cwd=cwd,
                env=merged_env,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )

    def capture(self, cmd: Sequence[str], *, cwd: Path | None = None) -> str:
        """Run ``cmd`` and return its stdout as text, raising :class:`BootstrapError` on failure."""
        try:
            result = subprocess.run(  # noqa: S603  # argv built from module constants + validated config
                list(cmd), cwd=cwd, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as exc:
            raise BootstrapError(
                f"command failed (exit {exc.returncode}): {' '.join(cmd)}"
            ) from exc
        return result.stdout

    def spawn(
        self,
        cmd: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> BackgroundProcess:
        """Start ``cmd`` detached, sending stdout+stderr to ``log_path`` (or discarding them)."""
        merged_env = {**os.environ, **env} if env is not None else None
        log_handle = log_path.open("wb") if log_path is not None else None
        proc = subprocess.Popen(  # noqa: S603  # argv built from module constants + validated config
            list(cmd),
            env=merged_env,
            stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        return _PopenProcess(proc, log_handle)


class _RootfsConfig(BaseModel):
    """Resolved knobs for one rootfs build (host source paths + guest tunables)."""

    model_config = ConfigDict(frozen=True)

    out_image: Path
    vm_dir: Path
    image_size_mb: int = 8192
    docker_version: str = _DEFAULT_DOCKER_VERSION
    docker_url: str
    redroid_image: str = _DEFAULT_REDROID_IMAGE
    redroid_tar: Path | None = None
    adbprobe_bin: Path | None = None
    busybox_bin: Path = Path("/usr/bin/busybox")
    xtables_multi: Path = Path("/usr/sbin/xtables-legacy-multi")
    socat_bin: Path = Path("/usr/bin/socat")
    ld_linux: Path = Path("/lib64/ld-linux-x86-64.so.2")

    @classmethod
    def from_env(cls, *, out_image: Path, vm_dir: Path) -> _RootfsConfig:
        """
        Build a config from defaults overlaid with the historical ``*`` env knobs.

        The former shell script honoured ``IMAGE_SIZE_MB``, ``DOCKER_VERSION``,
        ``DOCKER_URL``, ``REDROID_IMAGE``, ``REDROID_TAR``, ``ADBPROBE_BIN`` and
        ``BUSYBOX_BIN``; the port keeps the same names so existing build
        recipes keep working. The host docker binary comes from Beetroot's own
        :data:`settings.docker_bin` rather than a bespoke ``DOCKER_BIN``.

        Args:
            out_image: Path the packed ext4 image is written to.
            vm_dir: Directory holding ``guest-init.sh`` and ``adbprobe.c``.

        Returns:
            The resolved :class:`_RootfsConfig`.
        """
        version = os.environ.get("DOCKER_VERSION", _DEFAULT_DOCKER_VERSION)
        url = os.environ.get(
            "DOCKER_URL",
            f"https://download.docker.com/linux/static/stable/x86_64/docker-{version}.tgz",
        )
        redroid_tar = os.environ.get("REDROID_TAR") or None
        adbprobe_bin = os.environ.get("ADBPROBE_BIN") or None
        return cls(
            out_image=out_image,
            vm_dir=vm_dir,
            image_size_mb=int(os.environ.get("IMAGE_SIZE_MB", "8192")),
            docker_version=version,
            docker_url=url,
            redroid_image=os.environ.get("REDROID_IMAGE", _DEFAULT_REDROID_IMAGE),
            redroid_tar=Path(redroid_tar) if redroid_tar is not None else None,
            adbprobe_bin=Path(adbprobe_bin) if adbprobe_bin is not None else None,
            busybox_bin=Path(os.environ.get("BUSYBOX_BIN", "/usr/bin/busybox")),
        )


class _RootfsAssembly:
    """Drives one rootfs build inside a scratch ``work`` directory."""

    def __init__(
        self,
        cfg: _RootfsConfig,
        runner: RootfsRunner,
        work: Path,
        *,
        ready_attempts: int = _DOCKERD_READY_ATTEMPTS,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.work = work
        self.ready_attempts = ready_attempts
        self.root = work / "root"
        self.tgz = work / "docker.tgz"
        self.docker_extract = work / "docker"
        # The static bundle unpacks into a ``docker/`` top-level dir.
        self.dbin = self.docker_extract / "docker"

    def build(self) -> Path:
        """Fetch the static bundle, assemble the tree, and pack the ext4 image."""
        self._fetch_static_bundle()
        self._build_tree()
        self._pack_image()
        return self.cfg.out_image

    def _fetch_static_bundle(self) -> None:
        console.info(f"fetching Docker static bundle {self.cfg.docker_version}")
        self.runner.run(["curl", "-fsSL", self.cfg.docker_url, "-o", str(self.tgz)])
        self.docker_extract.mkdir(parents=True, exist_ok=True)
        self.runner.run(["tar", "-xzf", str(self.tgz), "-C", str(self.docker_extract)])

    def _build_tree(self) -> None:
        console.info(f"assembling rootfs tree in {self.root}")
        for rel in _ROOTFS_DIRS:
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").chmod(0o1777)

        self._stage_busybox()
        self._stage_docker_binaries()
        self._stage_iptables()
        self._stage_socat()
        self._stage_adbprobe()

        console.info("baking the redroid image into /var/lib/docker (offline boot)")
        stage = self._stage_docker_root()
        self.runner.run(["cp", "-a", str(stage), str(self.root / "var" / "lib" / "docker")])

        console.info("installing guest-init.sh as /init")
        init_dst = self.root / "init"
        shutil.copy(self.cfg.vm_dir / "guest-init.sh", init_dst)
        init_dst.chmod(0o755)

    def _stage_busybox(self) -> None:
        console.info(f"installing busybox ({self.cfg.busybox_bin}) + applet symlinks")
        busybox_dst = self.root / "bin" / "busybox"
        shutil.copy(self.cfg.busybox_bin, busybox_dst)
        busybox_dst.chmod(0o755)
        # Lay down every applet symlink now: /init is `#!/bin/sh`, so /bin/sh
        # must exist before the kernel can exec PID 1. Skip the ``busybox``
        # applet itself — some builds (e.g. Ubuntu's busybox-static 1.36) list
        # it, and symlinking ``bin/busybox -> busybox`` clobbers the real binary
        # with a self-referential loop, so the kernel's exec of /init (→ /bin/sh
        # → busybox) fails with -ELOOP. ``busybox --install -s`` skips it for the
        # same reason; the build-time port must too.
        for applet in self.runner.capture([str(self.cfg.busybox_bin), "--list"]).split():
            if applet == "busybox":
                continue
            self._symlink("busybox", self.root / "bin" / applet)

    def _stage_docker_binaries(self) -> None:
        console.info("installing Docker static bundle binaries")
        for name in _DOCKER_STATIC_BINS:
            dst = self.root / "bin" / name
            shutil.copy(self.dbin / name, dst)
            dst.chmod(0o755)

    def _stage_iptables(self) -> None:
        console.info("staging iptables-legacy (+ libs) — dockerd's bridge driver needs it")
        sbin = self.root / "usr" / "sbin"
        shutil.copy(self.cfg.xtables_multi, sbin / "xtables-legacy-multi")
        for name in _IPTABLES_LINKS:
            self._symlink("xtables-legacy-multi", sbin / name)
            self._symlink("xtables-legacy-multi", sbin / f"{name}-legacy")
        self._copy_libs(self.cfg.xtables_multi)
        self._copy_lib_file(self.cfg.ld_linux, self.root / "lib64")

    def _stage_socat(self) -> None:
        console.info("staging socat (+ libs) — the host-ADB relay")
        socat_dst = self.root / "bin" / "socat"
        shutil.copy(self.cfg.socat_bin, socat_dst)
        socat_dst.chmod(0o755)
        self._copy_libs(self.cfg.socat_bin)

    def _stage_adbprobe(self) -> None:
        dst = self.root / "usr" / "bin" / "adbprobe"
        prebuilt = self.cfg.adbprobe_bin
        source = self.cfg.vm_dir / "adbprobe.c"
        if prebuilt is not None and os.access(prebuilt, os.X_OK):
            shutil.copy(prebuilt, dst)
            dst.chmod(0o755)
        elif source.is_file() and shutil.which("cc") is not None:
            try:
                self.runner.run(["cc", "-static", "-O2", "-o", str(dst), str(source)])
            except BootstrapError:
                console.warn("adbprobe build failed; guest will skip the relay self-test")
            else:
                dst.chmod(0o755)
        else:
            console.info("no adbprobe (no ADBPROBE_BIN, no cc); guest skips relay self-test")

    def _stage_docker_root(self) -> Path:
        """Bake the redroid image into a throwaway data-root via a staging dockerd."""
        stage = self.work / "dockerroot"
        stage.mkdir(parents=True, exist_ok=True)

        tar = self.cfg.redroid_tar
        if tar is None:
            console.info(f"pulling {self.cfg.redroid_image} and saving to a tarball")
            self.runner.run([settings.docker_bin, "pull", self.cfg.redroid_image])
            tar = self.work / "redroid.tar"
            self.runner.run([settings.docker_bin, "save", self.cfg.redroid_image, "-o", str(tar)])

        sock = self.work / "stage.sock"
        host = f"unix://{sock}"
        console.info(f"loading {self.cfg.redroid_image} into a staging dockerd")
        proc = self.runner.spawn(
            [
                str(self.dbin / "dockerd"),
                f"--data-root={stage}",
                f"--host={host}",
                f"--exec-root={self.work / 'stage-exec'}",
                f"--pidfile={self.work / 'stage.pid'}",
                "--iptables=false",
                "--bridge=none",
            ],
            log_path=self.work / "stage-dockerd.log",
        )
        try:
            self._wait_docker_ready(host)
            self.runner.run([str(self.dbin / "docker"), f"--host={host}", "load", "-i", str(tar)])
        finally:
            proc.stop()
        _sleep(3)
        return stage

    def _wait_docker_ready(self, host: str) -> None:
        for _ in range(self.ready_attempts):
            if self.runner.try_run([str(self.dbin / "docker"), f"--host={host}", "info"]):
                return
            _sleep(1)
        raise BootstrapError("staging dockerd did not become ready")

    def _pack_image(self) -> None:
        console.info(f"packing {self.cfg.out_image} ({self.cfg.image_size_mb} MiB ext4)")
        self.runner.run(
            [
                "mke2fs",
                "-q",
                "-t",
                "ext4",
                "-d",
                str(self.root),
                str(self.cfg.out_image),
                f"{self.cfg.image_size_mb}M",
            ]
        )

    def _copy_libs(self, binary: Path) -> None:
        """Stage every absolute shared-library dependency of ``binary`` (best-effort)."""
        for line in self.runner.capture(["ldd", str(binary)]).splitlines():
            fields = line.split()
            # Mirror the original ``awk '{print $3}' | grep '^/'``: the 3rd
            # whitespace field of a ``lib => /path (0x..)`` line is the path;
            # vdso / loader lines have fewer fields or a non-absolute 3rd field.
            if len(fields) >= _LDD_MIN_FIELDS and fields[_LDD_PATH_INDEX].startswith("/"):
                self._copy_lib_file(Path(fields[_LDD_PATH_INDEX]), self.root / _GUEST_LIB_DIR)

    @staticmethod
    def _copy_lib_file(src: Path, dest_dir: Path) -> None:
        """Copy ``src`` (dereferencing symlinks) into ``dest_dir``, ignoring missing files."""
        with contextlib.suppress(OSError):
            shutil.copy(src, dest_dir)

    @staticmethod
    def _symlink(target: str, link: Path) -> None:
        link.unlink(missing_ok=True)
        link.symlink_to(target)


def build_rootfs(
    *,
    out_image: Path,
    vm_dir: Path,
    runner: RootfsRunner | None = None,
) -> Path:
    """
    Assemble the micro-VM guest ext4 rootfs (pure-Python port of build-rootfs.sh).

    Stages busybox + the Docker static bundle + static iptables-legacy + socat,
    bakes the redroid image into ``/var/lib/docker`` (so the guest boots fully
    offline), installs ``guest-init.sh`` as ``/init``, and packs the tree into a
    raw ext4 image with ``mke2fs -d`` (no loop mount, no root needed).

    Args:
        out_image: Path the packed ext4 image is written to.
        vm_dir: Directory holding ``guest-init.sh`` and (optionally)
            ``adbprobe.c``.
        runner: Inject a :class:`RootfsRunner` for testing. Defaults to
            :class:`DefaultRootfsRunner`.

    Returns:
        ``out_image`` (the path of the assembled rootfs image).

    Raises:
        BootstrapError: If any external tool (curl, tar, dockerd, docker,
            mke2fs, …) fails.
    """
    cfg = _RootfsConfig.from_env(out_image=out_image, vm_dir=vm_dir)
    run = runner if runner is not None else DefaultRootfsRunner()
    with tempfile.TemporaryDirectory(prefix="beetroot-rootfs-") as work:
        return _RootfsAssembly(cfg, run, Path(work)).build()


# The vendored micro-VM build artifacts (kernel config fragment, rootfs
# builder, guest init) live under ``docker/vm/`` in a source / editable
# install. ``beetroot build --vm-kernel`` resolves them relative to the
# build context, exactly like the redroid Dockerfile.
_VM_DIR = "vm"
_KERNEL_CONFIG = "kernel.config"

# The guest kernel version pinned by docs/design/vm-rnd-log.md. Kept in sync
# with KERNEL_VERSION in .github/workflows/e2e.yml and vm-kernel-release.yml
# (the publishing workflow) — a bump here means re-running that workflow so a
# matching prebuilt bzImage exists for the new version.
KERNEL_VERSION: Final = "6.12.9"


class VmArtifacts(BaseModel):
    """
    Host paths to the guest kernel + rootfs produced by ``beetroot build --vm-kernel``.

    Attributes:
        kernel: Path to the built guest ``bzImage``.
        rootfs: Path to the built guest ext4 root image.
    """

    model_config = ConfigDict(frozen=True)
    kernel: Path
    rootfs: Path


class _RootfsBuildFn(Protocol):
    """The :func:`build_rootfs` call shape, injectable so ``build_vm_kernel`` is unit-testable."""

    def __call__(
        self, *, out_image: Path, vm_dir: Path, runner: RootfsRunner | None = None
    ) -> Path:
        """Assemble the guest rootfs at ``out_image`` from artifacts in ``vm_dir``."""
        ...


class _KernelFetchFn(Protocol):
    """The :func:`kernel_download.fetch_prebuilt` call shape, injectable for testing."""

    def __call__(self, *, version: str, fingerprint: str, out_path: Path) -> Path:
        """Download the prebuilt ``bzImage`` for (version, fingerprint) to ``out_path``."""
        ...


def build_vm_kernel(  # noqa: PLR0913  # 6 keyword-only params; each is a distinct injectable concern
    *,
    out_dir: Path | None = None,
    build_context: Path | None = None,
    runner: SubprocessRunner | None = None,
    rootfs_build: _RootfsBuildFn = build_rootfs,
    from_source: bool = False,
    kernel_fetch: _KernelFetchFn = kernel_download.fetch_prebuilt,
) -> VmArtifacts:
    """
    Build the micro-VM guest kernel + rootfs for the ``binder: vm`` backend.

    Two steps:

    1. Obtain the guest kernel ``bzImage``. By default this **fetches a
       prebuilt kernel** matching the pinned version + the
       ``docker/vm/kernel.config`` fingerprint from the repo's GitHub release
       (~12 MiB, seconds) — so a fresh host skips the ~7-min compile. If no
       matching prebuilt exists (config edited, version bumped, release not yet
       published, or network blocked) it falls back to compiling from a
       ``make defconfig`` base merged with the vendored fragment via the kernel
       tree's ``merge_config.sh`` + ``make`` (the heavyweight compile stays a
       shell step the injected :class:`SubprocessRunner` dispatches). Pass
       ``from_source=True`` to skip the fetch and always compile.
    2. Assemble the ext4 rootfs via :func:`build_rootfs` (busybox-static +
       Docker static bundle + ``guest-init.sh`` as ``/init``) — pure-Python,
       no longer a shell script.

    Args:
        out_dir: Directory the ``bzImage`` and ``rootdisk.img`` are written
            to. Defaults to a ``vm`` subdir of the user cache.
        build_context: Directory containing the ``docker/vm/`` artifacts.
            Defaults to the repo root (source / editable install); pass it
            explicitly for a ``uv tool install`` setup where ``docker/`` is
            not bundled in the wheel.
        runner: Inject a :class:`SubprocessRunner` (the kernel step) for
            testing. Defaults to :class:`DefaultRunner`.
        rootfs_build: Inject the rootfs assembler for testing. Defaults to
            :func:`build_rootfs`.
        from_source: Skip the prebuilt fetch and always compile the kernel.
        kernel_fetch: Inject the prebuilt fetcher for testing. Defaults to
            :func:`kernel_download.fetch_prebuilt`.

    Returns:
        The :class:`VmArtifacts` naming the built kernel + rootfs paths.

    Raises:
        BootstrapError: If the kernel build or rootfs assembly fails.
    """
    ctx = build_context if build_context is not None else _default_build_context()
    out = out_dir if out_dir is not None else paths.user_cache_dir(_VM_DIR)
    run = runner if runner is not None else DefaultRunner()

    vm_dir = ctx / "docker" / _VM_DIR
    kernel_config = vm_dir / _KERNEL_CONFIG
    kernel_out = out / "bzImage"
    rootfs_out = out / "rootdisk.img"

    out.mkdir(parents=True, exist_ok=True)

    # Step 1: obtain the kernel — prebuilt fetch (fast) with a source-compile
    # fallback, unless the caller forces from_source.
    fetched = False
    if not from_source:
        fingerprint = kernel_download.config_fingerprint(kernel_config)
        try:
            kernel_fetch(version=KERNEL_VERSION, fingerprint=fingerprint, out_path=kernel_out)
            console.success(f"fetched prebuilt guest kernel {KERNEL_VERSION} ({fingerprint})")
            fetched = True
        except kernel_download.KernelFetchError as e:
            console.info(f"no matching prebuilt kernel ({e}); compiling from source")

    if not fetched:
        # Use ccache transparently when it's on PATH — a no-op on a cold build,
        # but it turns a re-compile of unchanged source (CI build lanes, local
        # iteration) into a near-instant cache hit. ccache keys on preprocessed
        # source content, so it hits even across fresh checkouts when the cache
        # dir is persisted. The benchmark lane, which intentionally times a cold
        # compile, sets CCACHE_DISABLE=1 (ccache then just execs the real gcc).
        cc_prefix = 'CC="ccache gcc" ' if shutil.which("ccache") is not None else ""
        with console.progress("Building micro-VM guest kernel (binder + cgroup + PSI)"):
            run.run(
                [
                    "sh",
                    "-c",
                    # merge the defconfig base with the vendored fragment, then
                    # build, then drop the bzImage where the launcher expects it.
                    "make defconfig && "
                    f"./scripts/kconfig/merge_config.sh -m .config {kernel_config} && "
                    f'make olddefconfig && make {cc_prefix}-j"$(nproc)" bzImage && '
                    f"cp arch/x86/boot/bzImage {kernel_out}",
                ],
            )

    # Step 2: assemble the ext4 rootfs (busybox + Docker static + guest init).
    with console.progress("Assembling micro-VM guest rootfs"):
        rootfs_build(out_image=rootfs_out, vm_dir=vm_dir)

    return VmArtifacts(kernel=kernel_out, rootfs=rootfs_out)
