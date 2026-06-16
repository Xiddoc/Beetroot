"""Docker daemon-liveness probe shared by the daemon-dependent tests.

``shutil.which("docker")`` only proves the docker **CLI** is installed; it
says nothing about whether the **daemon** is running. A handful of tests in
this suite shell out to operations that genuinely need a live daemon —
``docker run`` (``test_container_boot.py``) and ``docker compose down`` (the
``destroy``-driven restore tests). On a host with the CLI but no daemon (e.g.
the Claude Code on the web sandbox, where ``dockerd`` is opt-in) those tests
would otherwise **fail** with a connection error instead of **skipping**
(issue #59).

``daemon_available()`` runs ``docker info`` — which, unlike ``docker compose
config``, requires the daemon — with a short timeout and caches the verdict
for the process, so the probe shells out at most once per session. Tests gate
on it via ``@pytest.mark.skipif(not daemon_available(), ...)``.

Note: ``docker compose config`` (``test_config.py``) only renders YAML and
does **not** touch the daemon, so it stays guarded on CLI presence alone —
gating it on daemon liveness would needlessly skip a test that works fine
daemonless.
"""

from __future__ import annotations

import functools
import shutil
import subprocess


@functools.cache
def daemon_available() -> bool:
    """Return ``True`` when a Docker daemon is reachable.

    Probes ``docker info`` (which connects to the daemon) with a short
    timeout. The result is cached for the process, so the subprocess runs
    at most once per test session.
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],  # noqa: S607  # docker resolved via PATH, guarded above
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
