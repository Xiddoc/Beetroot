"""Regression test: the module that fails on a mid-batch device drop is reported.

When :meth:`beetroot.backends.adb.AdbDevice.auto_install_modules` aborts
because a module install raises and a serial re-probe shows the device
genuinely offline, the *failing* module's row used to vanish: its result
row was never appended (it raised before that line), and the ``skipped``
count only covers the strictly-later, un-attempted modules. So the report
silently lost one module — ``N`` remaining were declared skipped while
``N+1`` actually never installed. This test pins the corrected behavior:
the failing module now appears as a ``ok=False`` row, and every source is
accounted for exactly once across ``results`` + ``skipped``.
"""

from __future__ import annotations

import shutil

import pytest

from beetroot import api, registry
from beetroot.backends import adb as adb_backend


def _make_device() -> adb_backend.AdbDevice:
    return adb_backend.AdbDevice(
        name="phone",
        config=registry.AdbBackendConfig(serial="emulator-5554"),
        host_forward_port=27042,
    )


def test_failing_module_is_recorded_on_mid_batch_offline_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    # Pre-flight passes; the device drops mid-batch.
    monkeypatch.setattr(adb_backend.AdbDevice, "_preflight_root_and_magisk", lambda self: None)

    sources = ["a.zip", "b.zip", "c.zip"]

    def _fake_install_one(self: adb_backend.AdbDevice, source: str, sha256: object, index: int) -> str:
        del self, sha256
        if index == 1:
            raise RuntimeError(f"adb: error: device offline ({source})")
        return f"installed {source}"

    monkeypatch.setattr(adb_backend.AdbDevice, "_auto_install_one", _fake_install_one)
    # The authoritative re-probe says the device is gone.
    monkeypatch.setattr(adb_backend, "serial_is_available", lambda serial: False)

    with pytest.raises(api.DevicePreflightError) as exc_info:
        _make_device().auto_install_modules(sources)

    results = exc_info.value.results
    rows = [(r.source, r.ok) for r in results]
    # Index 0 succeeded; index 1 is the failing module and must be present
    # as a failed row (before the fix it was absent from both results and
    # the skipped count).
    assert rows == [("a.zip", True), ("b.zip", False)]

    failing_row = results[1]
    assert failing_row.source == "b.zip"
    assert failing_row.ok is False

    # Only index 2 was never attempted → "1 remaining module skipped".
    assert "(1 remaining module skipped)" in str(exc_info.value)

    # Every source is accounted for exactly once: results covers 0..1,
    # skipped covers 2 → len(results) + skipped == len(sources).
    skipped = 1
    assert len(results) + skipped == len(sources)


def test_offline_abort_detail_without_adb_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #223: when the underlying adb error text is empty, the offline-abort
    # row's detail is the bare stage-neutral message with no "last adb error"
    # suffix (covers the falsy-``adb_error`` branch).
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(adb_backend.AdbDevice, "_preflight_root_and_magisk", lambda self: None)

    def _raise_empty(self: adb_backend.AdbDevice, source: str, sha256: object, index: int) -> str:
        del self, source, sha256, index
        raise RuntimeError("")

    monkeypatch.setattr(adb_backend.AdbDevice, "_auto_install_one", _raise_empty)
    monkeypatch.setattr(adb_backend, "serial_is_available", lambda serial: False)

    with pytest.raises(api.DevicePreflightError) as exc_info:
        _make_device().auto_install_modules(["a.zip"])

    row = exc_info.value.results[0]
    assert row.ok is False
    assert row.detail == "device went offline during this module"
    assert "last adb error" not in row.detail
