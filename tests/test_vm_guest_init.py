"""Contract guard for ``src/beetroot/templates/vm/guest-init.sh``.

The micro-VM guest recreates the redroid container on every boot
(``docker rm -f`` + ``docker run``). For a persistent instance — and for any
flash → reboot → activate flow (a Zygisk module such as LSPosed only goes live
on the *second* boot, when magiskd reads the persisted ``zygisk=1`` at
``post-fs-data``) — redroid's ``/data`` must survive the reboot. guest-init.sh
bind-mounts a directory on the persistent guest rootfs as the container's
``/data``; these tests pin that so a refactor can't silently reintroduce the
ephemeral-``/data`` behaviour.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GUEST_INIT = (
    Path(__file__).parent.parent / "src" / "beetroot" / "templates" / "vm" / "guest-init.sh"
)


def _function_source(name: str) -> str:
    text = GUEST_INIT.read_text()
    match = re.search(rf"\n{name}\(\)\s*\{{(.*?)\n\}}", text, re.DOTALL)
    assert match, f"{name}() not found in guest-init.sh"
    return f"{name}() {{{match.group(1)}\n}}"


def _run_redroid_body() -> str:
    # Extract the run_redroid() function body so the assertions are scoped to
    # the launch, not an unrelated comment elsewhere in the file.
    text = GUEST_INIT.read_text()
    match = re.search(r"run_redroid\(\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert match, "run_redroid() not found in guest-init.sh"
    return match.group(1)


def _legacy_fallback_image() -> str:
    text = GUEST_INIT.read_text()
    match = re.search(r'_LEGACY_FALLBACK_IMAGE="([^"]+)"', text)
    assert match, "_LEGACY_FALLBACK_IMAGE assignment not found in guest-init.sh"
    return match.group(1)


def _run_resolve(
    tmp_path: Path, *, env_image: str | None = None, marker: str | None = None
) -> tuple[str, str]:
    """
    Run guest-init.sh's ``resolve_redroid_image()`` in isolation under ``sh``.

    The real script runs ``main`` (which mounts filesystems and shells out to
    docker), so we lift just the resolver function plus its two constants into a
    tiny harness with a stub ``log`` and a marker pointed at ``tmp_path``.

    Returns ``(resolved_image, stderr)``.
    """
    marker_path = tmp_path / "redroid-image"
    if marker is not None:
        marker_path.write_text(marker)
    harness = f"""
set -u
log() {{ echo "[guest-init] $*" >&2; }}
_BAKED_IMAGE_FILE="{marker_path}"
_LEGACY_FALLBACK_IMAGE="{_legacy_fallback_image()}"
{_function_source("resolve_redroid_image")}
resolve_redroid_image
printf 'RESOLVED=%s\\n' "${{REDROID_IMAGE:-}}"
"""
    env = {"PATH": os.environ.get("PATH", "")}
    if env_image is not None:
        env["REDROID_IMAGE"] = env_image
    res = subprocess.run(  # noqa: S603  # fixed argv; runs the shipped resolver under a controlled harness
        ["sh", "-c", harness],  # noqa: S607  # `sh` is universal POSIX, matching how the guest execs /init
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    resolved = ""
    for line in res.stdout.splitlines():
        if line.startswith("RESOLVED="):
            resolved = line[len("RESOLVED=") :]
    return resolved, res.stderr


def test_redroid_data_is_bind_mounted_for_persistence() -> None:
    body = _run_redroid_body()
    # The container's /data is bind-mounted from a host (guest-rootfs) dir.
    assert re.search(r'-v\s+"\$\{?_data_dir\}?:/data"', body), body
    assert "docker run" in body


def test_data_dir_is_overridable_and_created() -> None:
    body = _run_redroid_body()
    # Overridable via env, with a default, and the dir is created before use.
    assert "BEETROOT_GUEST_DATA_DIR" in body
    assert "/var/lib/redroid-data" in body
    assert re.search(r'mkdir -p "\$_data_dir"', body), body


# --- issue #97: the missing-marker fallback must be loud, never silent ---


def test_main_resolves_image_before_anything_else() -> None:
    # resolve_redroid_image must run first in main(); if a later refactor drops
    # the call the guest would boot with an unset REDROID_IMAGE.
    text = GUEST_INIT.read_text()
    match = re.search(r"main\(\)\s*\{\n(.*?)\n\}", text, re.DOTALL)
    assert match, "main() not found"
    first_call = next(
        line.strip() for line in match.group(1).splitlines() if line.strip() and "#" not in line
    )
    assert first_call == "resolve_redroid_image", match.group(1)


def test_env_override_wins_without_warning(tmp_path: Path) -> None:
    resolved, stderr = _run_resolve(
        tmp_path,
        env_image="redroid/redroid:13.0.0-latest",
        marker="redroid/redroid:12.0.0-latest\n",
    )
    assert resolved == "redroid/redroid:13.0.0-latest"
    assert "WARN" not in stderr


def test_marker_is_used_when_present_without_warning(tmp_path: Path) -> None:
    resolved, stderr = _run_resolve(tmp_path, marker="redroid/redroid:14.0.0-latest\n")
    assert resolved == "redroid/redroid:14.0.0-latest"
    assert "WARN" not in stderr


def test_missing_marker_falls_back_loudly(tmp_path: Path) -> None:
    resolved, stderr = _run_resolve(tmp_path, marker=None)
    assert resolved == _legacy_fallback_image()
    # The fallback must be PROMINENT and name both the fallback image and #97 —
    # a silent fallback is exactly the "boots Android 11" anti-pattern #97 kills.
    assert "WARN" in stderr
    assert _legacy_fallback_image() in stderr
    assert "#97" in stderr


def test_empty_marker_falls_back_loudly(tmp_path: Path) -> None:
    # A partial/interrupted bake can leave a zero-/whitespace-only marker; that
    # must take the loud fallback, not boot an empty image reference.
    resolved, stderr = _run_resolve(tmp_path, marker="\n")
    assert resolved == _legacy_fallback_image()
    assert "WARN" in stderr


def test_legacy_fallback_is_the_historical_value() -> None:
    # The fallback is deliberately the historical Android-11 image (a pre-#82
    # rootfs baked that into /var/lib/docker), NOT DEFAULT_ANDROID_VERSION.
    assert _legacy_fallback_image() == "redroid/redroid:11.0.0-latest"
