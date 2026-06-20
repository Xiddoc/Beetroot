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


def _serve_once(sock_path: Path, reply: bytes) -> threading.Thread:
    """Start a one-shot AF_UNIX server that records the command and replies."""
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def _run() -> None:
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(4096))
            conn.sendall(reply)
        server.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.received = received  # type: ignore[attr-defined]
    return thread


def _serve_chunks(sock_path: Path, chunks: list[bytes]) -> threading.Thread:
    """
    Serve ``chunks`` as separate sends (with gaps) and keep the socket open.

    The gaps force the client into multiple ``recv`` calls so the drain loop
    must terminate on the ``(qemu)`` prompt re-appearing (the real-QEMU case
    where the monitor connection stays open), not on EOF.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def _run() -> None:
        conn, _ = server.accept()
        conn.recv(4096)
        for chunk in chunks:
            conn.sendall(chunk)
            time.sleep(0.05)
        # Hold the connection open until the test tears the server down — this
        # is what makes the client rely on the prompt-break, not EOF.
        time.sleep(2)
        conn.close()
        server.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


class TestSaveSnapshot:
    def test_success_sends_savevm_and_returns_true(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_once(sock_path, b"(qemu) ")
        assert boot_cache.save_snapshot(sock_path, "beetroot-boot") is True
        thread.join(timeout=5)
        sent = b"".join(thread.received)  # type: ignore[attr-defined]
        assert b"savevm beetroot-boot" in sent

    def test_error_reply_returns_false(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_once(sock_path, b"Error: device has no medium\n(qemu) ")
        assert boot_cache.save_snapshot(sock_path) is False
        thread.join(timeout=5)

    def test_drains_until_prompt_when_connection_stays_open(self, tmp_path: Path) -> None:
        # Mirrors real QEMU HMP: the monitor connection stays open, so the
        # drain loop must stop when the "(qemu)" prompt re-appears across
        # multiple recvs, not on EOF.
        sock_path = tmp_path / "qemu-monitor.sock"
        thread = _serve_chunks(sock_path, [b"banner line, no prompt yet\n", b"(qemu) "])
        assert boot_cache.save_snapshot(sock_path) is True
        thread.join(timeout=5)

    def test_unreachable_socket_returns_false(self, tmp_path: Path) -> None:
        # No server listening at this path → connect() raises OSError.
        assert boot_cache.save_snapshot(tmp_path / "nope.sock") is False

    def test_recv_error_mid_drain_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A reset/timeout during the drain (after the command was sent) must
        # not propagate — the checkpoint was already issued, so the read loop
        # returns what it has rather than crashing the boot.
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
        # No error text was read, so the best-effort result is a clean True.
        assert boot_cache.save_snapshot(tmp_path / "qemu-monitor.sock") is True
