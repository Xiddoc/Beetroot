"""Guards on the fixed `binder: vm` ADB-port contract (issue #237).

The in-guest relay (``guest-init.sh``) and the host-side QEMU ``hostfwd``
target must agree on a single guest ADB port, ``5555``. ``guest-init.sh`` pins
``ADB_TCP_PORT=5555`` (non-overridable); this test asserts the Python side
emits the same number, so a future divergence between the documented guest
contract and the ``hostfwd`` target fails CI.
"""

from __future__ import annotations

from pathlib import Path

from beetroot.vm import qemu


def _argv(**over: object) -> list[str]:
    kwargs: dict[str, object] = {
        "qemu_bin": "qemu-system-x86_64",
        "accel": "tcg",
        "kernel": Path("/img/bzImage"),
        "rootfs": Path("/img/rootdisk.img"),
        "smp": 4,
        "memory_mib": 8192,
        "host_adb_port": 5575,
    }
    kwargs.update(over)
    return qemu.build_qemu_argv(**kwargs)  # type: ignore[arg-type]


def test_hostfwd_guest_slot_matches_guest_adb_port_contract() -> None:
    argv = _argv(host_adb_port=5575)
    netdev = argv[argv.index("-netdev") + 1]
    # netdev looks like: user,id=net0,hostfwd=tcp:127.0.0.1:5575-:5555 — the
    # guest slot (after the final ':') must equal _GUEST_ADB_PORT, the same
    # fixed 5555 the guest-init relay binds (issue #237).
    hostfwd = next(part for part in netdev.split(",") if part.startswith("hostfwd="))
    guest_port = int(hostfwd.rsplit(":", 1)[1])
    assert guest_port == qemu._GUEST_ADB_PORT
    assert qemu._GUEST_ADB_PORT == 5555
