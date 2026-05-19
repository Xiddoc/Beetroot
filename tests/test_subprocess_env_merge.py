"""F3 guardrail — ``builder.DefaultRunner`` merges env on top of ``os.environ``.

The original v0.3 code passed a 2-key env dict directly to
``subprocess.run(env=...)``, which REPLACES the parent's environment.
The child shelled out to ``docker compose build`` with no ``PATH`` and
errored out with ``FileNotFoundError: 'docker'`` on the first
``beetroot build`` invocation of a fresh install.

This test runs a real ``/usr/bin/env`` (a tiny utility that prints its
environment to stdout — no docker involved) through ``DefaultRunner``
and asserts both the explicit override key and the inherited ``PATH``
are present in the captured output.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from beetroot.builder import DefaultRunner


def test_default_runner_env_merges_with_parent_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    runner = DefaultRunner()

    out_file = tmp_path / "env.txt"
    # /usr/bin/env > out_file is the cleanest way to surface the
    # subprocess's stdout into the test: DefaultRunner.run inherits
    # stdio from the parent (no capture flag), but we can redirect via
    # a shell. Use a tiny intermediate shell rather than monkey-patching
    # subprocess — that way we exercise the real DefaultRunner.run code
    # path end-to-end without spying on subprocess.run.
    runner.run(
        ["sh", "-c", f"/usr/bin/env > {out_file}"],
        env={"FOO": "bar"},
    )
    text = out_file.read_text()
    assert "FOO=bar" in text
    assert "PATH=" in text


def test_default_runner_env_none_inherits_parent_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEETROOT_TEST_PARENT_KEY", "from-parent")
    runner = DefaultRunner()
    out_file = tmp_path / "env.txt"
    runner.run(["sh", "-c", f"/usr/bin/env > {out_file}"])
    text = out_file.read_text()
    assert "BEETROOT_TEST_PARENT_KEY=from-parent" in text


def test_default_runner_env_merges_via_subprocess_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Direct unit-test mirror of the F3 fix: a dict env arg is overlaid
    # on os.environ at the subprocess.run boundary. Spy on subprocess.run
    # so we can inspect the exact kwargs passed.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    runner = DefaultRunner()
    captured: dict[str, object] = {}
    real_run = subprocess.run

    def _spy(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        # kwargs is loosely typed for the spy; the real_run call
        # returns CompletedProcess[Any] which mypy widens to Any.
        result: subprocess.CompletedProcess[str] = real_run(  # type: ignore[call-overload]
            cmd, **kwargs
        )
        return result

    monkeypatch.setattr(subprocess, "run", _spy)
    runner.run(["true"], env={"BASE_IMAGE": "demo"})

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["BASE_IMAGE"] == "demo"
    assert env.get("PATH") == os.environ["PATH"]
    assert env.get("HOME") == os.environ["HOME"]
