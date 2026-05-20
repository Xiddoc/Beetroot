"""Tests for the ``beetroot status <name>`` verb."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from beetroot import cli, paths, registry

runner = CliRunner()


def _ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestStatusRedroid:
    def test_emits_required_fields(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        with patch("subprocess.run", return_value=_ok_proc()):
            result = runner.invoke(cli.app, ["status", "alpha"])
        assert result.exit_code == 0, result.stderr
        row = json.loads(result.stdout)
        for required_field in (
            "name", "kind", "index", "created_at", "ports",
            "status", "adb_address", "frida_address", "stealth_paths",
        ):
            assert required_field in row, f"missing {required_field!r}"
        assert row["name"] == "alpha"
        assert row["kind"] == "redroid"
        assert row["adb_address"] == "localhost:5555"
        assert row["frida_address"] == "localhost:27042"
        assert row["stealth_paths"] == {}
        assert row["ports"]["adb"] == 5555
        assert row["ports"]["frida"] == 27042
        # v0.3 back-compat keys live alongside the v0.4 fields so
        # existing scripts piping through jq keep working.
        assert row["path"] == str(registry.instance_path("alpha"))

    def test_unknown_name_exits_1(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["status", "nonexistent"])
        assert result.exit_code == 1
        assert "nonexistent" in result.stderr


class TestStatusAdb:
    def _seed_adb_instance(
        self, cli_root: Path, name: str = "phone", index: int = 0,
        serial: str = "emulator-5554",
    ) -> None:
        del cli_root  # fixture present only for XDG isolation
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = registry.InstanceMeta(
            backend=registry.AdbBackendConfig(serial=serial),
            index=index,
            created_at=datetime.now(UTC),
        )
        doc = registry.RegistryFile(instances={name: meta})
        path.write_text(doc.model_dump_json(indent=2))

    def test_emits_serial_omits_absolute_path(self, cli_root: Path) -> None:
        self._seed_adb_instance(cli_root)
        result = runner.invoke(cli.app, ["status", "phone"])
        assert result.exit_code == 0, result.stderr
        row = json.loads(result.stdout)
        assert row["name"] == "phone"
        assert row["kind"] == "adb"
        assert row["serial"] == "emulator-5554"
        # Adb backend has no on-disk instance dir; absolute_path /
        # path must not surface for adb-kind rows.
        assert "absolute_path" not in row
        assert "path" not in row
        # Spec: ports.frida2 is omitted for adb-kind rows because
        # the frida-control port is a redroid-only concept (the second
        # forwarded port). Adb-kind rows don't surface a ports key
        # at all in this impl.
        assert "ports" not in row or "frida2" not in row.get("ports", {})
        assert "stealth_paths" in row

    def test_index1_adb_reports_correct_frida_port(self, cli_root: Path) -> None:
        # B1 regression guard: an adb device at index 1 must report the
        # stride-of-10 frida port for index 1 (27052), NOT the hardcoded
        # index-0 default (27042). This distinguishes a real fix from code
        # that only happened to pass because the test seeded index 0.
        self._seed_adb_instance(cli_root, name="phone1", index=1, serial="emulator-5564")
        result = runner.invoke(cli.app, ["status", "phone1"])
        assert result.exit_code == 0, result.stderr
        row = json.loads(result.stdout)
        # adb_address is the raw serial, not a host:port pair.
        assert row["adb_address"] == "emulator-5564"
        # frida_address must reflect index 1 (frida_port = 27042 + 1*10 = 27052).
        assert row["frida_address"] == "localhost:27052"
