"""Tests for paths.py — filesystem layout accessors."""
from __future__ import annotations

from pathlib import Path

from beetroot import paths


class TestRepoRoot:
    def test_returns_path(self) -> None:
        assert isinstance(paths.repo_root(), Path)

    def test_is_absolute(self) -> None:
        assert paths.repo_root().is_absolute()


class TestLayoutConsistency:
    """Every accessor must return a path under repo_root(), and parent-child
    relationships must match the documented layout."""

    def test_instances_dir_under_root(self, isolated_root: Path) -> None:
        assert paths.instances_dir() == isolated_root / "instances"

    def test_instance_dir_under_instances_dir(self, isolated_root: Path) -> None:
        assert paths.instance_dir("alpha") == paths.instances_dir() / "alpha"

    def test_instance_data_under_instance_dir(self, isolated_root: Path) -> None:
        assert paths.instance_data("alpha") == paths.instance_dir("alpha") / "data"

    def test_instance_modules_under_instance_dir(self, isolated_root: Path) -> None:
        assert paths.instance_modules("alpha") == paths.instance_dir("alpha") / "modules"

    def test_instance_frida_under_instance_dir(self, isolated_root: Path) -> None:
        assert paths.instance_frida("alpha") == paths.instance_dir("alpha") / "frida-server"

    def test_instance_yaml_under_instance_dir(self, isolated_root: Path) -> None:
        assert paths.instance_yaml("alpha") == paths.instance_dir("alpha") / "beetroot.yaml"

    def test_instance_env_under_instance_dir(self, isolated_root: Path) -> None:
        assert paths.instance_env("alpha") == paths.instance_dir("alpha") / ".env"

    def test_registry_file_under_root(self, isolated_root: Path) -> None:
        assert paths.registry_file() == isolated_root / "instances.json"

    def test_presets_dir_under_root(self, isolated_root: Path) -> None:
        assert paths.presets_dir() == isolated_root / "presets"

    def test_compose_file_under_root(self, isolated_root: Path) -> None:
        assert paths.compose_file() == isolated_root / "compose.yaml"

    def test_frida_cache_dir_under_root(self, isolated_root: Path) -> None:
        assert paths.frida_cache_dir() == isolated_root / ".cache" / "frida"

    def test_instance_data_under_root(self, isolated_root: Path) -> None:
        assert paths.instance_data("alpha").is_relative_to(isolated_root)

    def test_different_instances_are_distinct(self, isolated_root: Path) -> None:
        assert paths.instance_dir("alpha") != paths.instance_dir("bravo")
        assert paths.instance_data("alpha") != paths.instance_data("bravo")
