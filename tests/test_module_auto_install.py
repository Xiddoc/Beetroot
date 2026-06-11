"""End-to-end CLI tests for ``beetroot module --auto-install`` (issue #7).

These drive the full user-input → artifact path: a Typer invocation of
``module <name> <zip>... --auto-install`` against an adopted adb device,
asserting on the recorded adb argv sequence, the per-module ok/failed
report lines, and the process exit code. All subprocess calls are
stubbed — no real adb is ever invoked.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import cli

runner = CliRunner()


@pytest.fixture
def stub_adb(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture every subprocess.run call inside backends.adb; always succeed."""
    captured: list[list[str]] = []

    def _fake_run(
        cmd: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        captured.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\n",
            stderr="",
        )

    monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
    return captured


def _adopt_phone() -> None:
    result = runner.invoke(cli.app, ["adopt", "emulator-5554", "--name", "phone"])
    assert result.exit_code == 0, result.stderr


def _write_zip(directory: Path, name: str, payload: bytes = b"PK\x03\x04fake") -> Path:
    zip_path = directory / name
    zip_path.write_bytes(payload)
    return zip_path


class TestAutoInstallHappyPath:
    def test_single_zip_full_argv_sequence_report_and_exit_code(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        # The behavior-test mandate: user input (CLI invocation) → final
        # artifact (adb argv sequence + report lines + exit code).
        _adopt_phone()
        zip_path = _write_zip(tmp_path, "MyModule.zip")
        result = runner.invoke(
            cli.app, ["module", "phone", str(zip_path), "--auto-install"]
        )
        assert result.exit_code == 0, result.stderr
        assert stub_adb == [
            [
                "adb", "-s", "emulator-5554", "push",
                str(zip_path), "/data/local/tmp/beetroot-module-0.zip",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c",
                "'magisk --install-module /data/local/tmp/beetroot-module-0.zip'",
            ],
            [
                "adb", "-s", "emulator-5554", "shell",
                "su", "-c", "'rm -f /data/local/tmp/beetroot-module-0.zip'",
            ],
        ]
        assert f"[beetroot] ok: {zip_path}" in result.output
        assert "magisk --install-module" in result.output

    def test_matching_sha256_end_to_end(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        _adopt_phone()
        payload = b"PK\x03\x04pinned"
        zip_path = _write_zip(tmp_path, "Pinned.zip", payload)
        sha = hashlib.sha256(payload).hexdigest()
        result = runner.invoke(
            cli.app,
            ["module", "phone", str(zip_path), "--auto-install", "--sha256", sha],
        )
        assert result.exit_code == 0, result.stderr
        assert len(stub_adb) == 3

    def test_multiple_zips_each_get_an_ok_line(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        _adopt_phone()
        first = _write_zip(tmp_path, "First.zip")
        second = _write_zip(tmp_path, "Second.zip")
        result = runner.invoke(
            cli.app,
            ["module", "phone", str(first), str(second), "--auto-install"],
        )
        assert result.exit_code == 0, result.stderr
        assert f"[beetroot] ok: {first}" in result.output
        assert f"[beetroot] ok: {second}" in result.output
        pushes = [cmd for cmd in stub_adb if cmd[3] == "push"]
        assert len(pushes) == 2


class TestAutoInstallFailureReporting:
    def test_one_failure_reports_all_and_exits_nonzero(
        self,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Bad.zip's magisk install fails; Good.zip must still be
        # installed and reported ok, and the verb must exit 1.
        captured: list[list[str]] = []

        def _fake_run(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            captured.append(list(cmd))
            failing = (
                "magisk --install-module" in cmd[-1]
                and "beetroot-module-0.zip" in cmd[-1]
            )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1 if failing else 0,
                stdout="",
                stderr="! Unable to install" if failing else "",
            )

        monkeypatch.setattr("beetroot.backends.adb.subprocess.run", _fake_run)
        _adopt_phone()
        bad = _write_zip(tmp_path, "Bad.zip")
        good = _write_zip(tmp_path, "Good.zip")
        result = runner.invoke(
            cli.app, ["module", "phone", str(bad), str(good), "--auto-install"]
        )
        assert result.exit_code == 1
        assert f"[beetroot] failed: {bad}" in result.stderr
        assert "Unable to install" in result.stderr
        assert f"[beetroot] ok: {good}" in result.output
        assert "'magisk --install-module /data/local/tmp/beetroot-module-1.zip'" in [
            cmd[-1] for cmd in captured
        ]

    def test_sha256_mismatch_refuses_push_and_exits_nonzero(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        _adopt_phone()
        zip_path = _write_zip(tmp_path, "Tampered.zip")
        result = runner.invoke(
            cli.app,
            [
                "module", "phone", str(zip_path),
                "--auto-install", "--sha256", "0" * 64,
            ],
        )
        assert result.exit_code == 1
        assert stub_adb == []
        assert "sha256 mismatch" in result.stderr
        assert f"[beetroot] failed: {zip_path}" in result.stderr

    def test_adb_absent_is_a_friendly_error(
        self,
        isolated_registry: Path,
        stub_adb: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # cli_root stubs `which` to find adb; here we adopt first, then
        # drop adb off PATH so the auto-install guard trips.
        import shutil

        _adopt_phone()
        zip_path = _write_zip(tmp_path, "M.zip")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = runner.invoke(
            cli.app, ["module", "phone", str(zip_path), "--auto-install"]
        )
        assert result.exit_code == 1
        assert "error: adb not found on PATH" in result.stderr
        assert stub_adb == []


class TestArgumentValidation:
    def test_sha256_count_mismatch_errors(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        _adopt_phone()
        first = _write_zip(tmp_path, "First.zip")
        second = _write_zip(tmp_path, "Second.zip")
        result = runner.invoke(
            cli.app,
            [
                "module", "phone", str(first), str(second),
                "--auto-install", "--sha256", "0" * 64,
            ],
        )
        assert result.exit_code == 1
        assert "once per source" in result.stderr
        assert stub_adb == []

    def test_multiple_sources_without_auto_install_errors(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        _adopt_phone()
        first = _write_zip(tmp_path, "First.zip")
        second = _write_zip(tmp_path, "Second.zip")
        result = runner.invoke(
            cli.app, ["module", "phone", str(first), str(second)]
        )
        assert result.exit_code == 1
        assert "exactly one source" in result.stderr
        assert stub_adb == []

    def test_multiple_sha256_without_auto_install_errors(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        _adopt_phone()
        zip_path = _write_zip(tmp_path, "M.zip")
        result = runner.invoke(
            cli.app,
            [
                "module", "phone", str(zip_path),
                "--sha256", "0" * 64, "--sha256", "1" * 64,
            ],
        )
        assert result.exit_code == 1
        assert "at most one --sha256" in result.stderr
        assert stub_adb == []


class TestCapabilityGating:
    def test_redroid_backend_exits_2(
        self,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Instance does not implement AutoModuleInstaller, so the flag
        # must capability-gate via cli.main()'s BackendCapabilityError
        # handler → exit code 2 (not a traceback, not exit 1).
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        zip_path = _write_zip(tmp_path, "M.zip")
        monkeypatch.setattr(
            sys, "argv",
            ["beetroot", "module", "alpha", str(zip_path), "--auto-install"],
        )
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2

    def test_safe_default_on_adb_device_is_unchanged(
        self, cli_root: Path, stub_adb: list[list[str]], tmp_path: Path
    ) -> None:
        # Regression guard: without the flag, the adb backend keeps the
        # push-to-Downloads behaviour byte-for-byte.
        _adopt_phone()
        zip_path = _write_zip(tmp_path, "MyModule.zip")
        result = runner.invoke(cli.app, ["module", "phone", str(zip_path)])
        assert result.exit_code == 0, result.stderr
        assert [
            "adb", "-s", "emulator-5554", "push",
            str(zip_path), "/sdcard/Download/MyModule.zip",
        ] in stub_adb
        assert "[beetroot] module pushed to phone" in result.output
