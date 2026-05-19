"""F2 guardrail — boot helpers do not terminate their sourcing shell.

``docker/entrypoint.sh`` sources each helper with ``. /flash-modules.sh``
(and ``. /launch-frida.sh``). A bare ``exit 0`` inside a sourced script
terminates the *parent* shell, so the helper after it never runs and
the trailing ``wait`` is unreachable.

These tests source each helper from a sh -c wrapper that prints
``POST`` afterwards. If the helper exits the parent shell, ``POST``
never reaches stdout — the assertion catches that regression.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

HELPERS_DIR = Path(__file__).parent.parent / "docker"


def _source_and_post(helper_name: str, env: dict[str, str]) -> str:
    """Source ``HELPERS_DIR/helper_name`` from sh and echo POST after."""
    helper = HELPERS_DIR / helper_name
    assert helper.is_file(), f"{helper} missing"
    res = subprocess.run(  # noqa: S603  # ``helper`` is a known repo-local fixture path
        ["sh", "-c", f". {helper}; echo POST"],  # noqa: S607  # sh resolved via PATH; test helper runs against a fixture
        check=False,
        capture_output=True,
        text=True,
        env={**env, "PATH": "/usr/bin:/bin"},
    )
    return res.stdout


def test_flash_modules_sourced_does_not_kill_parent_shell(tmp_path: Path) -> None:
    # Point BEETROOT_MODULES_DIR at a path that doesn't exist; the
    # original helper bailed with `exit 0` on this branch and killed
    # the parent shell, swallowing POST. The fix falls through.
    out = _source_and_post(
        "flash-modules.sh",
        env={"BEETROOT_MODULES_DIR": str(tmp_path / "does-not-exist")},
    )
    assert "POST" in out, f"parent shell died — full stdout: {out!r}"


def test_flash_modules_sourced_with_existing_dir(tmp_path: Path) -> None:
    # Existing-dir branch must also fall through. No zips, so the for
    # loop body never runs — but POST must still reach stdout.
    modules = tmp_path / "modules"
    modules.mkdir()
    out = _source_and_post(
        "flash-modules.sh",
        env={"BEETROOT_MODULES_DIR": str(modules)},
    )
    assert "POST" in out, f"parent shell died — full stdout: {out!r}"


def test_launch_frida_sourced_missing_binary(tmp_path: Path) -> None:
    # Mirror the flash-modules test for launch-frida. The helper's
    # binary-missing branch has historically been safe (no `exit`),
    # but pin the contract so a future refactor can't sneak one in.
    out = _source_and_post(
        "launch-frida.sh",
        env={"BEETROOT_FRIDA_BIN": str(tmp_path / "does-not-exist")},
    )
    assert "POST" in out, f"parent shell died — full stdout: {out!r}"


def test_launch_frida_sourced_with_executable(tmp_path: Path) -> None:
    # Executable-present branch must launch the binary in the
    # background and return, not block. Use /bin/true as the stand-in
    # for frida-server: it forks, exits 0 quickly, parent moves on.
    stub = tmp_path / "frida-server"
    stub.write_text("#!/bin/sh\n# stand-in for frida-server\nsleep 0\n")
    stub.chmod(0o755)
    out = _source_and_post(
        "launch-frida.sh",
        env={"BEETROOT_FRIDA_BIN": str(stub)},
    )
    assert "POST" in out, f"parent shell died — full stdout: {out!r}"
