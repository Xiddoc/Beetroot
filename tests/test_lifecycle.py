"""Tests for the first-class ``lifecycle: ephemeral|durable`` intent field (#124).

Covers the schema field, the ``Instance.create`` / ``beetroot create --lifecycle``
write path, ``destroy``'s escalated confirmation copy, and the snapshot manifest
stamp. The runtime guardrail (the #123 boot_cache advisory suppressed for an
ephemeral instance) lives in ``tests/test_vm_backend.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from beetroot import api, cli, config, paths, registry, snapshot

runner = CliRunner()


class TestLifecycleSchema:
    def test_default_is_durable(self) -> None:
        assert config.InstanceConfig().lifecycle == "durable"

    @pytest.mark.parametrize("value", ["ephemeral", "durable"])
    def test_accepts_valid_values(self, value: str) -> None:
        cfg = config.InstanceConfig.model_validate({"lifecycle": value})
        assert cfg.lifecycle == value

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            config.InstanceConfig.model_validate({"lifecycle": "throwaway"})

    def test_apply_dump_roundtrips_lifecycle(self, tmp_path: Path) -> None:
        # A full model dump (what `beetroot apply` rewrites) carries the field,
        # making the intent greppable in the committed YAML.
        p = tmp_path / "beetroot.yaml"
        config.write_yaml(p, config.InstanceConfig(lifecycle="ephemeral"))
        assert "lifecycle: ephemeral" in p.read_text()
        assert config.load_yaml(p).lifecycle == "ephemeral"


class TestCreateLifecycle:
    def test_create_default_omits_lifecycle_key(self, cli_root: Path) -> None:
        # The minimal YAML stays minimal; durable is implied by the schema.
        inst = api.Instance.create("alpha")
        assert "lifecycle" not in paths.instance_yaml(inst.root).read_text()

    def test_create_ephemeral_writes_key(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha", lifecycle="ephemeral")
        text = paths.instance_yaml(inst.root).read_text()
        assert "lifecycle: ephemeral" in text
        assert config.load_yaml(inst.root / "beetroot.yaml").lifecycle == "ephemeral"

    def test_create_durable_writes_key(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha", lifecycle="durable")
        assert "lifecycle: durable" in paths.instance_yaml(inst.root).read_text()

    def test_create_rejects_lifecycle_with_explicit_cfg(self, cli_root: Path) -> None:
        with pytest.raises(ValueError, match="lifecycle only with the default config"):
            api.Instance.create(
                "alpha", cfg=config.InstanceConfig(), lifecycle="ephemeral"
            )

    def test_cli_create_lifecycle_writes_key(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["create", "alpha", "--lifecycle", "ephemeral"])
        assert result.exit_code == 0, result.stderr
        root = registry.instance_path("alpha")
        assert "lifecycle: ephemeral" in (root / "beetroot.yaml").read_text()

    def test_cli_create_rejects_bad_lifecycle(self, cli_root: Path) -> None:
        result = runner.invoke(cli.app, ["create", "alpha", "--lifecycle", "bogus"])
        assert result.exit_code == 1
        assert "ephemeral" in result.stderr
        assert "durable" in result.stderr
        # The bad flag aborts before any registry side effect.
        assert registry.get("alpha") is None


class TestInstanceLifecycleHelper:
    def test_unknown_instance_defaults_durable(self, cli_root: Path) -> None:
        assert cli._instance_lifecycle("ghost") == "durable"

    def test_reads_ephemeral_from_config(self, cli_root: Path) -> None:
        api.Instance.create("alpha", lifecycle="ephemeral")
        assert cli._instance_lifecycle("alpha") == "ephemeral"

    def test_missing_yaml_defaults_durable(self, cli_root: Path) -> None:
        api.Instance.create("alpha", lifecycle="ephemeral")
        paths.instance_yaml(registry.instance_path("alpha")).unlink()
        assert cli._instance_lifecycle("alpha") == "durable"

    def test_malformed_yaml_defaults_durable(self, cli_root: Path) -> None:
        api.Instance.create("alpha", lifecycle="ephemeral")
        paths.instance_yaml(registry.instance_path("alpha")).write_text("{:not yaml")
        assert cli._instance_lifecycle("alpha") == "durable"


class TestDestroyPromptCopy:
    def test_durable_prompt_is_escalated(self, cli_root: Path) -> None:
        api.Instance.create("alpha", lifecycle="durable")
        prompt = cli._destroy_prompt("alpha")
        assert "DURABLE" in prompt
        assert "PERMANENTLY" in prompt

    def test_ephemeral_prompt_is_plain(self, cli_root: Path) -> None:
        api.Instance.create("alpha", lifecycle="ephemeral")
        prompt = cli._destroy_prompt("alpha")
        assert "DURABLE" not in prompt
        assert "cannot be undone" in prompt


class TestSnapshotManifestLifecycle:
    def test_manifest_defaults_durable(self) -> None:
        # A manifest from an archive predating the field (no lifecycle key)
        # restores as durable.
        m = snapshot.Manifest(
            name="x", source_index=0, created_at="t", beetroot_version="0"
        )
        assert m.lifecycle == "durable"

    def test_read_lifecycle_reads_value(self, cli_root: Path) -> None:
        inst = api.Instance.create("alpha", lifecycle="ephemeral")
        assert snapshot._read_lifecycle(paths.instance_yaml(inst.root)) == "ephemeral"

    def test_build_manifest_stamps_lifecycle(self) -> None:
        m = snapshot._build_manifest(
            name="x", source_index=1, path_layout={}, lifecycle="ephemeral"
        )
        assert m.lifecycle == "ephemeral"
