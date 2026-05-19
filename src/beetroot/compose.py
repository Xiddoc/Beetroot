"""
Subprocess wrappers around ``docker compose``.

The compose template ships inside the wheel (see
:func:`paths.bundled_compose_file`); the per-instance state lives under
the instance directory (``--project-directory <instance_dir>``). We always
invoke compose with ``-p <project>`` set to the instance name and
``--env-file`` pointing at the instance's generated .env. Compose is
authoritative for container state — the registry only knows about
allocation, not runtime status.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import paths
from .settings import settings


class ComposeError(RuntimeError):
    """Raised when a ``docker compose`` subcommand exits with a non-zero status."""


def _ensure_docker() -> None:
    if shutil.which(settings.docker_bin) is None:
        raise ComposeError("docker not found on PATH")


def _base_cmd(name: str, instance_root: Path) -> list[str]:
    _ensure_docker()
    return [
        settings.docker_bin,
        "compose",
        "-p",
        name,
        "-f",
        str(paths.bundled_compose_file()),
        "--project-directory",
        str(instance_root),
        "--env-file",
        str(paths.instance_env(instance_root)),
    ]


def run(
    name: str,
    instance_root: Path,
    args: Sequence[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """
    Run ``docker compose -p <name> ...``, inheriting stdio by default.

    Args:
        name: Instance name used as the compose project name.
        instance_root: The instance directory (cwd for the subprocess and
            the value of ``--project-directory``).
        args: Subcommand and flags to append after the base compose args.
        **kwargs: Forwarded verbatim to ``subprocess.run``.

    Returns:
        The completed process result.
    """
    cmd = _base_cmd(name, instance_root) + list(args)
    return subprocess.run(cmd, cwd=instance_root, check=False, **kwargs)


def up(name: str, instance_root: Path) -> None:
    """
    Start an instance with ``compose up -d``.

    Args:
        name: Instance name.
        instance_root: The instance directory.

    Raises:
        ComposeError: If compose exits with a non-zero status.
    """
    res = run(name, instance_root, ["up", "-d"])
    if res.returncode != 0:
        raise ComposeError(f"`compose up` failed for {name} (exit {res.returncode})")


def down(name: str, instance_root: Path, *, volumes: bool = False) -> None:
    """
    Stop an instance with ``compose down``.

    Args:
        name: Instance name.
        instance_root: The instance directory.
        volumes: If ``True``, also remove named volumes.

    Raises:
        ComposeError: If compose exits with a non-zero status.
    """
    args = ["down"]
    if volumes:
        args.append("--volumes")
    res = run(name, instance_root, args)
    if res.returncode != 0:
        raise ComposeError(f"`compose down` failed for {name} (exit {res.returncode})")


def logs(name: str, instance_root: Path, follow: bool = False) -> None:
    """
    Tail container logs for an instance.

    Args:
        name: Instance name.
        instance_root: The instance directory.
        follow: If ``True``, stream logs continuously (``-f``).
    """
    args = ["logs"]
    if follow:
        args.append("-f")
    run(name, instance_root, args)


def ps_status(name: str, instance_root: Path) -> str:
    """
    Return a one-word container status for an instance.

    Queries ``docker compose ps --format json`` live; never reads from cache.

    Args:
        name: Instance name.
        instance_root: The instance directory.

    Returns:
        One of ``running``, ``exited``, or ``not-created``.
    """
    res = run(
        name,
        instance_root,
        ["ps", "--format", "json"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return "not-created"
    for line in res.stdout.strip().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = str(entry.get("State", "")).lower()
        if state:
            return state
    return "not-created"


def build(name: str, instance_root: Path) -> None:
    """
    Build the Docker image for an instance.

    Args:
        name: Instance name.
        instance_root: The instance directory.

    Raises:
        ComposeError: If compose exits with a non-zero status.
    """
    res = run(name, instance_root, ["build"])
    if res.returncode != 0:
        raise ComposeError(f"`compose build` failed for {name} (exit {res.returncode})")
