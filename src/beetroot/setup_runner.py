"""
One-time base-image bootstrap.

Rewrite of the legacy ``scripts/setup.sh`` as testable Python. Clones the
external ``ayasa520/redroid-script`` patcher, runs it to bake Magisk +
optional GApps + Houdini into a redroid base image, then layers Beetroot's
own ``entrypoint.sh`` / ``stealth.rc`` on top via ``docker compose build``.

The module name is ``setup_runner`` rather than ``setup`` because the
filename ``setup.py`` is historically reserved for the Python build system
and tooling (setuptools, distutils, etc.) may special-case it even in
non-package locations.

Public surface:

* :class:`SubprocessRunner` — protocol describing how subprocess calls are
  dispatched. The default :class:`DefaultRunner` shells out via
  ``subprocess.run(check=True)``; tests inject a recording fake.
* :class:`BootstrapError` — raised when any step (clone, patch, build)
  fails.
* :data:`GAPPS_FLAGS` — mapping from gapps variant to the patcher CLI flags
  it needs.
* :func:`bootstrap_base_image` — entry point that orchestrates the three
  steps and returns the resulting image tag.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, Protocol

from . import config, paths
from .settings import settings

GappsVariant = Literal["none", "lite", "full", "mindthegapps"]

GAPPS_FLAGS: Final[dict[GappsVariant, list[str]]] = {
    "none": [],
    "lite": ["-lg"],
    "full": ["-g"],
    "mindthegapps": ["-mtg"],
}

_DEFAULT_WORK_DIR: Final[Path] = Path("/tmp/redroid")
_DEFAULT_REDROID_URL: Final[str] = "https://github.com/ayasa520/redroid-script.git"


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
            env: Full environment to pass; ``None`` inherits the parent's.

        Raises:
            BootstrapError: If ``check`` is ``True`` and the command exits non-zero.
        """
        try:
            subprocess.run(list(cmd), cwd=cwd, check=check, env=env)
        except subprocess.CalledProcessError as exc:
            raise BootstrapError(
                f"command failed (exit {exc.returncode}): {' '.join(cmd)}"
            ) from exc


def _image_tag(android_version: int, gapps: GappsVariant) -> str:
    """Compute the base-image tag for ``(version, gapps)``, mirroring config.base_image_tag."""
    android = config.Android(version=android_version, gapps=gapps)
    return config.base_image_tag(android)


def bootstrap_base_image(
    *,
    gapps: GappsVariant = "lite",
    android_version: int = 14,
    redroid_script_url: str = _DEFAULT_REDROID_URL,
    work_dir: Path | None = None,
    runner: SubprocessRunner | None = None,
) -> str:
    """
    Patch and build the redroid base image + Beetroot layer for ``gapps``.

    The three steps are:

    1. ``git clone --depth 1 <redroid_script_url> <work_dir>`` (after wiping
       any existing clone).
    2. ``uv run --with requests --with tqdm python -W ignore redroid.py
       -a <version>.0.0 [gapps-flag] -i -m`` from inside ``work_dir``.
    3. ``BASE_IMAGE=<tag> <docker_bin> compose build`` from the repo root.

    Args:
        gapps: GMS variant to bake in.
        android_version: Android major version. Must be one of ``11``,
            ``12``, ``13``, ``14`` — validated against
            :class:`beetroot.config.Android`.
        redroid_script_url: Override the patcher source (testing / forks).
        work_dir: Override the clone directory (default ``/tmp/redroid``).
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
    work = work_dir if work_dir is not None else _DEFAULT_WORK_DIR
    run = runner if runner is not None else DefaultRunner()

    tag = _image_tag(android_version, gapps)

    # Step 1: clone the patcher. ``rm -rf`` first so re-running is idempotent.
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
    run.run(patcher_cmd, cwd=work)

    # Step 3: build the Beetroot layer on top via the bundled compose template,
    # passing the freshly produced base tag via the BASE_IMAGE env var (consumed
    # by docker/Dockerfile) and the cwd via BEETROOT_BUILD_CONTEXT (so the
    # template's ${BEETROOT_BUILD_CONTEXT} substitution finds the local
    # docker/ dir).
    cwd = Path.cwd()
    run.run(
        [
            settings.docker_bin,
            "compose",
            "-f",
            str(paths.bundled_compose_file()),
            "--project-directory",
            str(cwd),
            "build",
        ],
        env={"BASE_IMAGE": tag, "BEETROOT_BUILD_CONTEXT": str(cwd)},
    )

    return tag
