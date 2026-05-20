"""Tests for the ``beetroot env <name> --all`` flag."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from beetroot import cli, paths, registry

runner = CliRunner()


class TestEnvAllRedroid:
    def test_no_flag_emits_only_port_exports(self, cli_root: Path) -> None:
        # Back-compat: bare ``beetroot env <name>`` emits EXACTLY two
        # lines (ANDROID_DEVICE + FRIDA_DEVICE) — every researcher's
        # ``eval $(beetroot env ...)`` workflow depends on this.
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["env", "alpha"])
        assert result.exit_code == 0, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert lines == [
            "export ANDROID_DEVICE=localhost:5555",
            "export FRIDA_DEVICE=localhost:27042",
        ]
        # No BEETROOT_* keys must leak into the v0.3 output shape.
        assert "BEETROOT_" not in result.stdout

    def test_all_flag_emits_every_beetroot_key(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        result = runner.invoke(cli.app, ["env", "alpha", "--all"])
        assert result.exit_code == 0, result.stderr
        # Every BEETROOT_* key from render_env must appear.
        for required in (
            "INSTANCE_NAME=alpha",
            "ADB_PORT=5555",
            "FRIDA_PORT=27042",
            "FRIDA_PORT_CONTROL=27043",
            "BEETROOT_MAGISK_DB=",
            "BEETROOT_MODULES_DIR=",
            "BEETROOT_FRIDA_BIN=",
            "BEETROOT_DENYLIST_PACKAGES=",
            "ANDROID_DEVICE=localhost:5555",
            "FRIDA_DEVICE=localhost:27042",
        ):
            assert required in result.stdout, f"missing {required!r} in --all output"
        # Every emitted line must be eval-able (prefixed with ``export``).
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            assert stripped.startswith("export "), f"non-eval-able line: {line!r}"


class TestEnvAllAdb:
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

    def test_adb_all_emits_serial_and_frida_host(self, cli_root: Path) -> None:
        self._seed_adb_instance(cli_root)
        result = runner.invoke(cli.app, ["env", "phone", "--all"])
        assert result.exit_code == 0, result.stderr
        assert "export ADB_SERIAL=emulator-5554" in result.stdout
        assert "FRIDA_HOST=localhost:" in result.stdout
        # render_env's BEETROOT_* keys must NOT leak into the adb path —
        # render_env assumes a redroid backend and would crash trying
        # to read ``.config`` from a non-Instance.
        assert "BEETROOT_MAGISK_DB" not in result.stdout
