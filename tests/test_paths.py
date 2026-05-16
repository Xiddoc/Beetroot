"""Tests for paths.py — filesystem layout accessors."""
from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import paths


class TestRepoRoot:
    def test_returns_path(self) -> None:
        assert isinstance(paths.repo_root(), Path)

    def test_is_absolute(self) -> None:
        assert paths.repo_root().is_absolute()


class TestRepoRootDiscovery:
    """Cwd-walking discovery of the compose.yaml marker."""

    def test_from_root_dir_returns_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "compose.yaml").touch()
        monkeypatch.chdir(tmp_path)
        assert paths.repo_root() == tmp_path.resolve()

    def test_from_subdir_walks_up_to_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "compose.yaml").touch()
        sub = tmp_path / "src" / "beetroot"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert paths.repo_root() == tmp_path.resolve()

    def test_multi_level_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "foo" / "bar" / "baz"
        project.mkdir(parents=True)
        (project / "compose.yaml").touch()
        deep = project / "deep" / "nested" / "dir"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        assert paths.repo_root() == project.resolve()

    def test_no_marker_raises_project_root_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tmp_path has no compose.yaml in itself or any ancestor below /.
        monkeypatch.chdir(tmp_path)
        with pytest.raises(paths.ProjectRootNotFoundError):
            paths.repo_root()

    def test_error_is_filenotfound_subclass(self) -> None:
        assert issubclass(paths.ProjectRootNotFoundError, FileNotFoundError)

    def test_error_message_names_marker_and_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(paths.ProjectRootNotFoundError) as exc_info:
            paths.repo_root()
        msg = str(exc_info.value)
        assert "compose.yaml" in msg
        assert str(tmp_path.resolve()) in msg
        assert "ancestor" in msg

    def test_explicit_start_overrides_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # cwd has no marker; the explicit start dir does.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        project = tmp_path / "project"
        project.mkdir()
        (project / "compose.yaml").touch()

        assert paths.repo_root(start=project) == project.resolve()

    def test_explicit_start_walks_up(self, tmp_path: Path) -> None:
        (tmp_path / "compose.yaml").touch()
        sub = tmp_path / "a" / "b" / "c"
        sub.mkdir(parents=True)
        assert paths.repo_root(start=sub) == tmp_path.resolve()

    def test_explicit_start_can_raise(self, tmp_path: Path) -> None:
        # tmp_path has no marker — explicit start still raises.
        with pytest.raises(paths.ProjectRootNotFoundError):
            paths.repo_root(start=tmp_path)


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


class TestLayoutWithRealRepoRoot:
    """Exercise the real (un-monkeypatched) repo_root() via monkeypatch.chdir.

    These tests verify the layout accessors compose correctly with the
    discovery-based repo_root, not just the monkeypatched stub used by
    isolated_root.
    """

    def test_accessors_with_chdir_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "compose.yaml").touch()
        monkeypatch.chdir(tmp_path)
        resolved = tmp_path.resolve()
        assert paths.instances_dir() == resolved / "instances"
        assert paths.instance_dir("alpha") == resolved / "instances" / "alpha"
        assert paths.registry_file() == resolved / "instances.json"
        assert paths.compose_file() == resolved / "compose.yaml"
        assert paths.presets_dir() == resolved / "presets"
        assert paths.frida_cache_dir() == resolved / ".cache" / "frida"
