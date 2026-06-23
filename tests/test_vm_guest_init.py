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

import re
from pathlib import Path

GUEST_INIT = Path(__file__).parent.parent / "src" / "beetroot" / "templates" / "vm" / "guest-init.sh"


def _run_redroid_body() -> str:
    text = GUEST_INIT.read_text()
    # Extract the run_redroid() function body so the assertions are scoped to
    # the launch, not an unrelated comment elsewhere in the file.
    match = re.search(r"run_redroid\(\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert match, "run_redroid() not found in guest-init.sh"
    return match.group(1)


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
