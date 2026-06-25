"""Tests for beetroot.vm.boot_cache (the binder:vm warm-start boot cache)."""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

from beetroot.vm import boot_cache
from beetroot.vm.qemu import QemuLaunchError

if TYPE_CHECKING:
    from collections.abc import Sequence


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


class TestPaths:
    def test_overlay_path(self, tmp_path: Path) -> None:
        assert boot_cache.overlay_path(tmp_path) == tmp_path / "vm-overlay.qcow2"

    def test_monitor_path(self, tmp_path: Path) -> None:
        assert boot_cache.monitor_path(tmp_path) == tmp_path / "qemu-monitor.sock"


# ---------------------------------------------------------------------------
# create_overlay
# ---------------------------------------------------------------------------


class TestCreateOverlay:
    def test_invokes_qemu_img_create(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        base = tmp_path / "rootdisk.img"
        overlay = tmp_path / "vm-overlay.qcow2"
        captured: dict[str, Sequence[str]] = {}

        def _run(argv: Sequence[str], **_kw: object) -> _FakeCompleted:
            captured["argv"] = argv
            return _FakeCompleted()

        monkeypatch.setattr("beetroot.vm.boot_cache.subprocess.run", _run)
        boot_cache.create_overlay(base, overlay)
        argv = list(captured["argv"])
        assert "create" in argv
        assert "qcow2" in argv
        assert str(base) in argv
        assert str(overlay) in argv

    def test_missing_binary_raises_launch_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> _FakeCompleted:
            raise FileNotFoundError("qemu-img")

        monkeypatch.setattr("beetroot.vm.boot_cache.subprocess.run", _boom)
        with pytest.raises(QemuLaunchError, match="not found"):
            boot_cache.create_overlay(tmp_path / "b.img", tmp_path / "o.qcow2")

    def test_nonzero_exit_raises_launch_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(1, "qemu-img", stderr="backing file missing")

        monkeypatch.setattr("beetroot.vm.boot_cache.subprocess.run", _boom)
        with pytest.raises(QemuLaunchError, match="qemu-img create failed"):
            boot_cache.create_overlay(tmp_path / "b.img", tmp_path / "o.qcow2")


# ---------------------------------------------------------------------------
# snapshot_present
# ---------------------------------------------------------------------------


class TestSnapshotPresent:
    def test_missing_overlay_is_false_without_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(*_a: object, **_k: object) -> _FakeCompleted:
            raise AssertionError("qemu-img must not run when the overlay is absent")

        monkeypatch.setattr("beetroot.vm.boot_cache.subprocess.run", _explode)
        assert boot_cache.snapshot_present(tmp_path / "absent.qcow2") is False

    def test_tag_present_is_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = tmp_path / "vm-overlay.qcow2"
        overlay.write_bytes(b"qcow2")
        listing = (
            "Snapshot list:\n"
            "ID        TAG           VM SIZE                DATE\n"
            "1         beetroot-boot     2.1 GiB 2026-06-20 15:00:00\n"
        )
        monkeypatch.setattr(
            "beetroot.vm.boot_cache.subprocess.run",
            lambda *_a, **_k: _FakeCompleted(stdout=listing),
        )
        assert boot_cache.snapshot_present(overlay) is True

    def test_tag_absent_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = tmp_path / "vm-overlay.qcow2"
        overlay.write_bytes(b"qcow2")
        monkeypatch.setattr(
            "beetroot.vm.boot_cache.subprocess.run",
            lambda *_a, **_k: _FakeCompleted(stdout="Snapshot list:\n(empty)\n"),
        )
        assert boot_cache.snapshot_present(overlay) is False

    def test_qemu_img_error_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = tmp_path / "vm-overlay.qcow2"
        overlay.write_bytes(b"qcow2")

        def _boom(*_a: object, **_k: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(1, "qemu-img")

        monkeypatch.setattr("beetroot.vm.boot_cache.subprocess.run", _boom)
        assert boot_cache.snapshot_present(overlay) is False

    def test_missing_binary_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = tmp_path / "vm-overlay.qcow2"
        overlay.write_bytes(b"qcow2")

        def _boom(*_a: object, **_k: object) -> _FakeCompleted:
            raise FileNotFoundError("qemu-img")

        monkeypatch.setattr("beetroot.vm.boot_cache.subprocess.run", _boom)
        assert boot_cache.snapshot_present(overlay) is False


# ---------------------------------------------------------------------------
# save_snapshot — driven against a real AF_UNIX monitor stand-in
# ---------------------------------------------------------------------------


def _serve_hmp(
    sock_path: Path, command_reply: bytes, *, banner: bytes = b"QEMU 8.2.2 monitor\n(qemu) "
) -> threading.Thread:
    """
    Stand in for QEMU HMP: send a banner+prompt on connect, then ``command_reply``.

    Models the real protocol the fixed ``save_snapshot`` relies on — the banner
    ends in a ``(qemu)`` prompt that the client must drain *before* its command
    is processed, and the command's reply ends in the next prompt. Records the
    bytes received so a test can assert the command that was sent.
    """
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def _run() -> None:
        conn, _ = server.accept()
        with conn:
            conn.sendall(banner)
            received.append(conn.recv(4096))
            conn.sendall(command_reply)
        server.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.received = received  # type: ignore[attr-defined]
    return thread


def _serve_hmp_split(sock_path: Path, command_reply: bytes) -> threading.Thread:
    """Like :func:`_serve_hmp` but sends the banner as two packets (text, prompt)."""
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def _run() -> None:
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"QEMU 8.2.2 monitor - type 'help'\n")
            time.sleep(0.05)
            conn.sendall(b"(qemu) ")
            received.append(conn.recv(4096))
            conn.sendall(command_reply)
        server.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.received = received  # type: ignore[attr-defined]
    return thread


class TestSaveSnapshot:
    def test_success_drains_banner_then_sends_savevm(self, tmp_path: Path) -> None:
        # The fix: drain the connect banner's prompt FIRST, then send savevm,
        # then read up to the next prompt. savevm is silent on success.
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_hmp(sock_path, b"(qemu) ")
        assert boot_cache.save_snapshot(sock_path, "beetroot-boot") is True
        thread.join(timeout=5)
        sent = b"".join(thread.received)  # type: ignore[attr-defined]
        assert b"savevm beetroot-boot" in sent

    def test_error_reply_returns_false(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_hmp(sock_path, b"Error: device has no medium\n(qemu) ")
        assert boot_cache.save_snapshot(sock_path) is False
        thread.join(timeout=5)

    def test_banner_split_across_packets_still_waits_for_command_prompt(
        self, tmp_path: Path
    ) -> None:
        # Regression guard for the real bug found in e2e: the banner arrives as
        # two packets ("...monitor\n" then "(qemu) "). The drain must consume
        # the WHOLE banner up to its prompt, then send savevm and wait for the
        # command's own prompt — not mistake a split banner prompt for the
        # savevm result. _serve_hmp_split sends the banner in two sends.
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_hmp_split(sock_path, b"(qemu) ")
        assert boot_cache.save_snapshot(sock_path, "beetroot-boot") is True
        thread.join(timeout=5)
        assert b"savevm beetroot-boot" in b"".join(thread.received)  # type: ignore[attr-defined]

    def test_connection_closed_before_command_prompt_is_handled(self, tmp_path: Path) -> None:
        # If QEMU closes after the command without re-printing the prompt, the
        # read loop must terminate on EOF (not spin on empty reads).
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_hmp(sock_path, b"")  # banner+prompt, then close, no reply
        # No error text was seen, so the best-effort result is True.
        assert boot_cache.save_snapshot(sock_path) is True
        thread.join(timeout=5)

    def test_unreachable_socket_returns_false(self, tmp_path: Path) -> None:
        # No server listening at this path → connect() raises OSError.
        assert boot_cache.save_snapshot(tmp_path / "nope.sock") is False

    def test_socket_error_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A reset/timeout during the exchange reports failure (the checkpoint is
        # uncertain), so the next `up` cold-boots rather than resume a bad state.
        class _FakeSock:
            def __enter__(self) -> _FakeSock:
                return self

            def __exit__(self, *_a: object) -> Literal[False]:
                return False

            def settimeout(self, _t: float) -> None:
                pass

            def connect(self, _addr: str) -> None:
                pass

            def sendall(self, _data: bytes) -> None:
                pass

            def recv(self, _n: int) -> bytes:
                raise OSError("connection reset")

        monkeypatch.setattr("beetroot.vm.boot_cache.socket.socket", lambda *_a, **_k: _FakeSock())
        assert boot_cache.save_snapshot(tmp_path / "qemu-monitor.sock") is False


# ---------------------------------------------------------------------------
# Overlay base-identity / auto-invalidation (issue #126)
# ---------------------------------------------------------------------------


class TestOverlayIdentity:
    def _files(self, tmp_path: Path, *, k: bytes = b"KERNEL", r: bytes = b"ROOTFS") -> tuple[Path, Path]:
        kernel = tmp_path / "bzImage"
        kernel.write_bytes(k)
        rootfs = tmp_path / "rootdisk.img"
        rootfs.write_bytes(r)
        return kernel, rootfs

    def test_overlay_key_path(self, tmp_path: Path) -> None:
        assert boot_cache.overlay_key_path(tmp_path) == tmp_path / "vm-overlay.cache-key"

    def test_base_identity_stable_and_order_independent(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        first = boot_cache.base_identity(kernel, rootfs)
        # Argument order must not matter (folded in basename order).
        assert first == boot_cache.base_identity(rootfs, kernel)
        assert len(first) == 16

    def test_base_identity_changes_with_content(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        before = boot_cache.base_identity(kernel, rootfs)
        kernel.write_bytes(b"REBUILT KERNEL")
        assert boot_cache.base_identity(kernel, rootfs) != before

    def test_record_and_read_roundtrip(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        boot_cache.record_identity(tmp_path, kernel, rootfs)
        assert boot_cache.read_identity(tmp_path) == boot_cache.base_identity(kernel, rootfs)

    def test_read_identity_none_when_absent(self, tmp_path: Path) -> None:
        assert boot_cache.read_identity(tmp_path) is None

    def test_read_identity_none_when_blank(self, tmp_path: Path) -> None:
        boot_cache.overlay_key_path(tmp_path).write_text("   \n")
        assert boot_cache.read_identity(tmp_path) is None

    def test_read_identity_none_on_oserror(self, tmp_path: Path) -> None:
        # A directory at the key path makes read_text raise OSError → None.
        boot_cache.overlay_key_path(tmp_path).mkdir()
        assert boot_cache.read_identity(tmp_path) is None

    def test_overlay_is_stale_false_when_matching(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        boot_cache.record_identity(tmp_path, kernel, rootfs)
        assert boot_cache.overlay_is_stale(tmp_path, kernel, rootfs) is False

    def test_overlay_is_stale_true_when_content_changed(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        boot_cache.record_identity(tmp_path, kernel, rootfs)
        rootfs.write_bytes(b"REBUILT ROOTFS")
        assert boot_cache.overlay_is_stale(tmp_path, kernel, rootfs) is True

    def test_overlay_is_stale_true_when_no_key(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        assert boot_cache.overlay_is_stale(tmp_path, kernel, rootfs) is True

    def test_discard_overlay_removes_overlay_and_key(self, tmp_path: Path) -> None:
        kernel, rootfs = self._files(tmp_path)
        boot_cache.overlay_path(tmp_path).write_bytes(b"q")
        boot_cache.record_identity(tmp_path, kernel, rootfs)
        boot_cache.discard_overlay(tmp_path)
        assert not boot_cache.overlay_path(tmp_path).exists()
        assert not boot_cache.overlay_key_path(tmp_path).exists()

    def test_discard_overlay_is_idempotent(self, tmp_path: Path) -> None:
        boot_cache.discard_overlay(tmp_path)  # nothing present → no error
        assert not boot_cache.overlay_path(tmp_path).exists()
