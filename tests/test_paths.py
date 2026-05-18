"""Tests for paths.py — filesystem layout accessors."""
from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest

from beetroot import paths


class TestInstanceRootDiscovery:
    """Cwd-walking discovery of the beetroot.yaml marker."""

    def test_from_root_dir_returns_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "beetroot.yaml").touch()
        monkeypatch.chdir(tmp_path)
        assert paths.instance_root() == tmp_path.resolve()

    def test_from_subdir_walks_up_to_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "beetroot.yaml").touch()
        sub = tmp_path / "deep" / "nested"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert paths.instance_root() == tmp_path.resolve()

    def test_multi_level_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "foo" / "bar" / "baz"
        project.mkdir(parents=True)
        (project / "beetroot.yaml").touch()
        deep = project / "deep" / "nested" / "dir"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        assert paths.instance_root() == project.resolve()

    def test_no_marker_raises_instance_root_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(paths.InstanceRootNotFoundError):
            paths.instance_root()

    def test_error_is_filenotfound_subclass(self) -> None:
        assert issubclass(paths.InstanceRootNotFoundError, FileNotFoundError)

    def test_error_message_names_marker_and_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(paths.InstanceRootNotFoundError) as exc_info:
            paths.instance_root()
        msg = str(exc_info.value)
        assert "beetroot.yaml" in msg
        assert str(tmp_path.resolve()) in msg
        assert "ancestor" in msg

    def test_explicit_start_overrides_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        project = tmp_path / "project"
        project.mkdir()
        (project / "beetroot.yaml").touch()

        assert paths.instance_root(start=project) == project.resolve()

    def test_explicit_start_walks_up(self, tmp_path: Path) -> None:
        (tmp_path / "beetroot.yaml").touch()
        sub = tmp_path / "a" / "b" / "c"
        sub.mkdir(parents=True)
        assert paths.instance_root(start=sub) == tmp_path.resolve()

    def test_explicit_start_can_raise(self, tmp_path: Path) -> None:
        with pytest.raises(paths.InstanceRootNotFoundError):
            paths.instance_root(start=tmp_path)


class TestInstanceLayoutAccessors:
    """All instance accessors take an explicit ``root`` argument — no globals."""

    def test_instance_yaml(self, tmp_path: Path) -> None:
        assert paths.instance_yaml(tmp_path) == tmp_path / "beetroot.yaml"

    def test_instance_env(self, tmp_path: Path) -> None:
        assert paths.instance_env(tmp_path) == tmp_path / ".env"

    def test_instance_data(self, tmp_path: Path) -> None:
        assert paths.instance_data(tmp_path) == tmp_path / "data"

    def test_instance_modules(self, tmp_path: Path) -> None:
        assert paths.instance_modules(tmp_path) == tmp_path / "modules"

    def test_instance_frida(self, tmp_path: Path) -> None:
        assert paths.instance_frida(tmp_path) == tmp_path / "frida-server"

    def test_two_unrelated_roots_share_no_paths(self, tmp_path: Path) -> None:
        alpha = tmp_path / "a"
        bravo = tmp_path / "b"
        assert paths.instance_data(alpha) != paths.instance_data(bravo)
        assert paths.instance_modules(alpha) != paths.instance_modules(bravo)


class TestBundledComposeFile:
    def test_bundled_compose_file_resolves(self) -> None:
        p = paths.bundled_compose_file()
        assert p.is_file()
        assert p.name == "compose.yaml"

    def test_bundled_compose_file_via_importlib_resources(self) -> None:
        ref = importlib.resources.files("beetroot.templates").joinpath("compose.yaml")
        assert ref.is_file(), (
            "bundled compose.yaml should resolve via importlib.resources "
            "in both editable and wheel installs. We don't simulate a wheel "
            "install here because hatchling's editable install uses real "
            "files; the assertion that .is_file() holds for the standard "
            "importlib.resources lookup covers the API contract."
        )

    def test_bundled_compose_file_contains_substitutions(self) -> None:
        text = paths.bundled_compose_file().read_text()
        for var in ("INSTANCE_NAME", "ADB_PORT", "FRIDA_PORT", "FRIDA_PORT2"):
            assert f"${{{var}}}" in text or f"${{{var}:-" in text


class TestUserRegistryFile:
    def test_default_under_home_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert paths.user_registry_file() == (
            tmp_path / ".config" / "beetroot" / "instances.json"
        )

    def test_respects_xdg_config_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "myxdg"))
        assert paths.user_registry_file() == (
            tmp_path / "myxdg" / "beetroot" / "instances.json"
        )


class TestUserCacheDir:
    def test_default_under_home_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert paths.user_cache_dir("frida") == (
            tmp_path / ".cache" / "beetroot" / "frida"
        )

    def test_respects_xdg_cache_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "mycache"))
        assert paths.user_cache_dir("modules") == (
            tmp_path / "mycache" / "beetroot" / "modules"
        )

    def test_subdir_is_a_path_segment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        a = paths.user_cache_dir("frida")
        b = paths.user_cache_dir("modules")
        assert a.parent == b.parent
        assert a.name == "frida"
        assert b.name == "modules"
