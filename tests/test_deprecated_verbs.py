"""Migration-hint tests for v0.2 verbs/options removed in v0.3.

`beetroot setup` and `beetroot create --preset` both shipped in v0.2.
v0.3 removed them — the worry is that a v0.2 user upgrading without
reading CHANGELOG gets a bare Typer ``Error: No such command``
``No such option`` and is left guessing.

These tests pin the friendly migration hint: exit 1 + an `error:`
line with explicit migration instructions in stderr.
"""
from __future__ import annotations

from typer.testing import CliRunner

from beetroot import cli

runner = CliRunner()


class TestSetupVerb:
    def test_setup_no_args_prints_migration_hint(self) -> None:
        result = runner.invoke(cli.app, ["setup"])
        assert result.exit_code == 1
        assert "error:" in result.stderr
        # The hint must name the new verb so users know where to go.
        assert "build" in result.stderr

    def test_setup_with_v02_variant_arg_prints_migration_hint(self) -> None:
        # v0.2 invocation was `beetroot setup lite`. The trailing arg
        # must not break the migration hint.
        result = runner.invoke(cli.app, ["setup", "lite"])
        assert result.exit_code == 1
        assert "error:" in result.stderr
        assert "build" in result.stderr

    def test_setup_is_hidden_from_help(self) -> None:
        # The deprecated verb is hidden so `--help` keeps showing only
        # the supported surface.
        result = runner.invoke(cli.app, ["--help"])
        assert result.exit_code == 0
        # `setup` MUST NOT appear in the top-level help.
        # Look in stdout (Typer prints help to stdout).
        assert " setup " not in result.stdout
        assert "\nsetup\n" not in result.stdout


class TestPresetOption:
    def test_create_preset_prints_migration_hint(self, cli_root: object) -> None:
        result = runner.invoke(
            cli.app, ["create", "alpha", "--preset", "with-frida"]
        )
        assert result.exit_code == 1
        assert "error:" in result.stderr
        # The hint must mention the examples/ directory + apply verb.
        assert "examples/" in result.stderr
        assert "with-frida" in result.stderr
        assert "apply" in result.stderr

    def test_create_help_does_not_advertise_preset(self) -> None:
        # The option is hidden, so `--help` keeps showing only the
        # supported surface for new users.
        result = runner.invoke(cli.app, ["create", "--help"])
        assert result.exit_code == 0
        assert "--preset" not in result.stdout
