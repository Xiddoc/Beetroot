"""Tests for the deprecated ``beetroot env`` verb (removed in v0.6).

The verb is hidden and now proxies to a JSON status row so existing
scripts that eval'd the output still get machine-readable data, with a
deprecation hint on stderr pointing at ``status --json``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from beetroot import cli, paths, registry

runner = CliRunner()


class TestEnvDeprecatedRedroid:
    def test_deprecated_hint_goes_to_stderr(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["env", "alpha"])
        assert result.exit_code == 0, result.stderr
        # Deprecation hint must go to stderr only.
        assert "removed in v0.6" in result.stderr
        assert "status --json" in result.stderr

    def test_json_row_goes_to_stdout(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["env", "alpha"])
        assert result.exit_code == 0, result.stderr
        # Machine-readable JSON row goes to stdout.
        assert "adb_address" in result.stdout
        assert "frida_address" in result.stdout

    def test_all_flag_accepted_without_error(self, cli_root: Path) -> None:
        # --all was removed with env; the hidden alias accepts but ignores it.
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["env", "alpha", "--all"])
        assert result.exit_code == 0, result.stderr


class TestEnvDeprecatedAdb:
    def _seed_adb_instance(self, cli_root: Path, name: str = "phone") -> None:
        del cli_root  # fixture present only for XDG isolation
        path = paths.user_registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = registry.InstanceMeta(
            backend=registry.AdbBackendConfig(serial="emulator-5554"),
            index=0,
            created_at=datetime.now(UTC),
        )
        doc = registry.RegistryFile(instances={name: meta})
        path.write_text(doc.model_dump_json(indent=2))

    def test_adb_env_emits_deprecated_hint(self, cli_root: Path) -> None:
        self._seed_adb_instance(cli_root)
        result = runner.invoke(cli.app, ["env", "phone"])
        assert result.exit_code == 0, result.stderr
        assert "removed in v0.6" in result.stderr

    def test_adb_env_emits_json_row(self, cli_root: Path) -> None:
        self._seed_adb_instance(cli_root)
        result = runner.invoke(cli.app, ["env", "phone"])
        assert result.exit_code == 0, result.stderr
        # JSON row includes addresses.
        assert "adb_address" in result.stdout
        assert "frida_address" in result.stdout
