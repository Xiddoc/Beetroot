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
from typing import Literal

from . import paths
from .settings import settings

# Closed enum of the strings ``ps_status`` may return. The compose
# subcommand reports a free-form ``State`` field; this enum gates every
# string we ever surface to callers so verbs (``doctor``, ``status``)
# and snapshot can pattern-match without falling back to ``str``.
ComposeStatus = Literal[
    "running",
    "exited",
    "starting",
    "created",
    "paused",
    "not-created",
    "docker-unreachable",
    "unknown",
]


# ``docker compose ps --format json`` reports State strings from a
# stable vocabulary (see https://docs.docker.com/engine/reference/commandline/compose_ps/).
# We map them to our closed ComposeStatus literal so downstream pattern
# matches don't see free-form strings. Unknown states fall through to
# ``"unknown"`` so future Docker releases adding states don't crash
# the CLI; the audit-flagged ``docker-unreachable`` is distinguished
# from ``not-created`` so ``beetroot doctor`` can give a precise
# error.
_STATE_TO_STATUS: dict[str, ComposeStatus] = {
    "running": "running",
    "exited": "exited",
    "starting": "starting",
    "created": "created",
    "paused": "paused",
    "restarting": "starting",
    "dead": "exited",
    "removing": "exited",
}


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
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """
    Run ``docker compose -p <name> ...``, inheriting stdio by default.

    Args:
        name: Instance name used as the compose project name.
        instance_root: The instance directory (cwd for the subprocess and
            the value of ``--project-directory``).
        args: Subcommand and flags to append after the base compose args.
        **kwargs: Forwarded verbatim to ``subprocess.run``. Typed as
            ``object`` so mypy under ``disallow_any_explicit`` accepts
            them; callers pass shapes that ``subprocess.run`` itself
            validates (``capture_output``, ``text``, …).

    Returns:
        The completed process result. ``[str]`` is correct under the
        ``text=True`` default we add; callers that opt into binary
        stdout would need to re-cast, but no caller does today.
    """
    cmd = _base_cmd(name, instance_root) + list(args)
    # ``**kwargs: object`` makes the **kwargs spread incompatible with
    # subprocess.run's overload set (which discriminates on
    # ``capture_output``/``text``/etc.). We narrow back to
    # ``CompletedProcess[str]`` at the call boundary; the two suppressions
    # are the cost of expressing "callers pass whatever subprocess.run
    # accepts" under ``disallow_any_explicit``.
    result: subprocess.CompletedProcess[str] = subprocess.run(  # type: ignore[call-overload]  # **kwargs is object-typed for strictness
        cmd, cwd=instance_root, check=False, **kwargs,
    )
    return result


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


def ps_status(name: str, instance_root: Path) -> ComposeStatus:
    """
    Return a closed-enum container status for an instance.

    Queries ``docker compose ps --format json`` live; never reads from
    cache. Distinguishes "docker daemon unreachable" from "not-created"
    so callers (``beetroot doctor``, ``ls --json``) can give a precise
    diagnostic.

    Args:
        name: Instance name.
        instance_root: The instance directory.

    Returns:
        One of the :data:`ComposeStatus` literals. ``"unknown"`` is
        used when compose reports a state string we don't recognise
        (e.g. a future Docker release introducing a new state); it
        never silently maps to ``"running"``.
    """
    try:
        res = run(
            name,
            instance_root,
            ["ps", "--format", "json"],
            capture_output=True,
            text=True,
        )
    except ComposeError:
        # ``_ensure_docker`` raised — docker binary not on PATH.
        return "docker-unreachable"
    if res.returncode != 0:
        # Non-zero is most commonly "no such project" → not created.
        # We could distinguish "daemon not running" via stderr scraping,
        # but that's fragile; map to docker-unreachable only when
        # compose stderr explicitly reports it.
        stderr = (res.stderr or "").lower()
        if "cannot connect to the docker daemon" in stderr:
            return "docker-unreachable"
        return "not-created"
    if not res.stdout.strip():
        return "not-created"
    for line in res.stdout.strip().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = str(entry.get("State", "")).lower()
        if state:
            return _STATE_TO_STATUS.get(state, "unknown")
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
