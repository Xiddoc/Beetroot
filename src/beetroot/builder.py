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

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from . import config, console, paths
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
    """Raised when a bootstrap step (git clone, patcher, build) fails."""


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
    """Compute the base-image tag for ``(version, gapps)``, mirroring config.base_image_tag."""
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


# The vendored micro-VM build artifacts (kernel config fragment, rootfs
# builder, guest init) live under ``docker/vm/`` in a source / editable
# install. ``beetroot build --vm-kernel`` resolves them relative to the
# build context, exactly like the redroid Dockerfile.
_VM_DIR = "vm"
_KERNEL_CONFIG = "kernel.config"
_ROOTFS_SCRIPT = "build-rootfs.sh"


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


def build_vm_kernel(
    *,
    out_dir: Path | None = None,
    build_context: Path | None = None,
    runner: SubprocessRunner | None = None,
) -> VmArtifacts:
    """
    Build the micro-VM guest kernel + rootfs for the ``binder: vm`` backend.

    Two steps, both delegated to the injected :class:`SubprocessRunner` so the
    heavyweight kernel compile + rootfs assembly stay shell steps this Python
    glue never runs directly:

    1. Build the guest kernel ``bzImage`` from a ``make defconfig`` base
       merged with the vendored ``docker/vm/kernel.config`` fragment (the
       §4.1 binder/cgroup/bpf/PSI deltas), via the kernel tree's own
       ``merge_config.sh`` + ``make``.
    2. Assemble the ext4 rootfs by running ``docker/vm/build-rootfs.sh``
       (busybox-static + Docker static bundle + ``guest-init.sh`` as
       ``/init``).

    Args:
        out_dir: Directory the ``bzImage`` and ``rootdisk.img`` are written
            to. Defaults to a ``vm`` subdir of the user cache.
        build_context: Directory containing the ``docker/vm/`` artifacts.
            Defaults to the repo root (source / editable install); pass it
            explicitly for a ``uv tool install`` setup where ``docker/`` is
            not bundled in the wheel.
        runner: Inject a :class:`SubprocessRunner` for testing. Defaults to
            :class:`DefaultRunner`.

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
    rootfs_script = vm_dir / _ROOTFS_SCRIPT
    kernel_out = out / "bzImage"
    rootfs_out = out / "rootdisk.img"

    out.mkdir(parents=True, exist_ok=True)

    # Step 1: build the guest kernel with the binder-enabled config fragment.
    with console.progress("Building micro-VM guest kernel (binder + cgroup + PSI)"):
        run.run(
            [
                "sh",
                "-c",
                # merge the defconfig base with the vendored fragment, then
                # build, then drop the bzImage where the launcher expects it.
                "make defconfig && "
                f"./scripts/kconfig/merge_config.sh -m .config {kernel_config} && "
                'make olddefconfig && make -j"$(nproc)" bzImage && '
                f"cp arch/x86/boot/bzImage {kernel_out}",
            ],
        )

    # Step 2: assemble the ext4 rootfs (busybox + Docker static + guest init).
    with console.progress("Assembling micro-VM guest rootfs"):
        run.run(["sh", str(rootfs_script), str(rootfs_out)])

    return VmArtifacts(kernel=kernel_out, rootfs=rootfs_out)
