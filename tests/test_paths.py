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

    def test_multi_level_walk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # Ports moved to the per-instance compose.override.yaml in v8 (issue
        # #108), so the bundled template no longer references ADB_PORT /
        # FRIDA_PORT; the remaining well-known substitutions still must be here.
        text = paths.bundled_compose_file().read_text()
        for var in ("INSTANCE_NAME", "BASE_IMAGE", "MEM_LIMIT", "DISPLAY_WIDTH"):
            assert f"${{{var}}}" in text or f"${{{var}:-" in text

    def test_bundled_compose_file_is_stable_across_calls(self) -> None:
        # T2 Agent 2 B-8: ``importlib.resources.as_file`` returns an
        # extracted-tempdir path inside a context manager that's gone
        # the moment the context exits. The helper caches a stable
        # path under the user cache so subsequent ``docker compose -f``
        # invocations resolve identically.
        first = paths.bundled_compose_file()
        second = paths.bundled_compose_file()
        assert first == second
        # The path must still resolve to a readable file after the
        # ``as_file`` context manager has exited.
        assert first.is_file()
        assert first.read_text()

    def test_bundled_compose_file_handles_zip_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Simulate a wheel-install path by faking
        # ``importlib.resources.files(...)`` to return a Traversable
        # whose ``is_file()`` is False but ``read_bytes()`` works (the
        # zipimporter / multi-zip shape). The helper must extract the
        # bytes into the user cache and return the cache path.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        # Clear the module-level cache so the helper actually runs.
        monkeypatch.setattr(paths, "_BUNDLED_COMPOSE_CACHE", None)

        class _ZipResource:
            def is_file(self) -> bool:
                return False

            def read_bytes(self) -> bytes:
                return b"zipped: compose content\n"

        class _Files:
            def joinpath(self, name: str) -> _ZipResource:
                return _ZipResource()

        # ``as_file`` must accept this traversable too — patch it to
        # yield the resource unchanged (the real implementation extracts
        # into a tempdir, but the contract is "yields a thing
        # ``read_bytes`` works on").
        import contextlib
        from collections.abc import Iterator

        @contextlib.contextmanager
        def _fake_as_file(resource: _ZipResource) -> Iterator[_ZipResource]:
            yield resource

        # Patch the importlib.resources symbols the helper imports at
        # module load. Direct attribute access on ``paths.importlib``
        # works at runtime but doesn't satisfy mypy strict-mode export
        # checks; using the canonical module reference does.
        monkeypatch.setattr(importlib.resources, "files", lambda _pkg: _Files())
        monkeypatch.setattr(importlib.resources, "as_file", _fake_as_file)

        result = paths.bundled_compose_file()
        assert result.is_file()
        assert result.read_bytes() == b"zipped: compose content\n"
        # The cache lives under user_cache_dir("templates").
        assert result.parent == paths.user_cache_dir("templates"), (
            f"unexpected cache location: {result}"
        )

        # Hit the "cache already exists with identical bytes" branch
        # (no write). Clear the in-memory cache so the helper re-runs
        # the cache-target check, then verify the file's mtime hasn't
        # moved — proves no redundant write happened.
        monkeypatch.setattr(paths, "_BUNDLED_COMPOSE_CACHE", None)
        first_mtime = result.stat().st_mtime_ns
        second = paths.bundled_compose_file()
        assert second == result
        assert second.stat().st_mtime_ns == first_mtime, (
            "helper re-wrote a cached file whose bytes already matched"
        )


class TestBundledVmDir:
    def test_resolves_editable_install(self) -> None:
        # In the source / editable checkout the three vm assets live on a real
        # filesystem path; bundled_vm_dir returns the containing directory.
        d = paths.bundled_vm_dir()
        assert d.is_dir()
        for name in ("kernel.config", "guest-init.sh", "adbprobe.c"):
            assert (d / name).is_file(), name

    def test_assets_resolve_via_importlib_resources(self) -> None:
        pkg = importlib.resources.files("beetroot.templates.vm")
        for name in ("kernel.config", "guest-init.sh", "adbprobe.c"):
            assert pkg.joinpath(name).is_file(), name

    def test_stable_across_calls(self) -> None:
        assert paths.bundled_vm_dir() == paths.bundled_vm_dir()

    def test_handles_zip_install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Simulate a wheel install: the package data has no real filesystem
        # path (``is_file()`` is False), so the helper must extract every asset
        # into the user cache and return that directory.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setattr(paths, "_BUNDLED_VM_DIR_CACHE", None)

        payloads = {
            "kernel.config": b"CONFIG_FOO=y\n",
            "guest-init.sh": b"#!/bin/sh\n",
            "adbprobe.c": b"int main(){}\n",
        }

        class _ZipResource:
            def __init__(self, name: str) -> None:
                self._name = name

            def is_file(self) -> bool:
                return False

            def read_bytes(self) -> bytes:
                return payloads[self._name]

        class _Files:
            def joinpath(self, name: str) -> _ZipResource:
                return _ZipResource(name)

        import contextlib
        from collections.abc import Iterator

        @contextlib.contextmanager
        def _fake_as_file(resource: _ZipResource) -> Iterator[_ZipResource]:
            yield resource

        monkeypatch.setattr(importlib.resources, "files", lambda _pkg: _Files())
        monkeypatch.setattr(importlib.resources, "as_file", _fake_as_file)

        result = paths.bundled_vm_dir()
        assert result == paths.user_cache_dir("vm-assets")
        for name, content in payloads.items():
            assert (result / name).read_bytes() == content

        # "cache already present with identical bytes" branch: clear the
        # in-memory cache, re-run, assert no rewrite happened.
        monkeypatch.setattr(paths, "_BUNDLED_VM_DIR_CACHE", None)
        mtimes = {n: (result / n).stat().st_mtime_ns for n in payloads}
        second = paths.bundled_vm_dir()
        assert second == result
        for name in payloads:
            assert (result / name).stat().st_mtime_ns == mtimes[name], name


class TestUserRegistryFile:
    def test_default_under_home_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert paths.user_registry_file() == (tmp_path / ".config" / "beetroot" / "instances.json")

    def test_respects_xdg_config_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "myxdg"))
        assert paths.user_registry_file() == (tmp_path / "myxdg" / "beetroot" / "instances.json")


class TestUserCacheDir:
    def test_default_under_home_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert paths.user_cache_dir("frida") == (tmp_path / ".cache" / "beetroot" / "frida")

    def test_respects_xdg_cache_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "mycache"))
        assert paths.user_cache_dir("modules") == (tmp_path / "mycache" / "beetroot" / "modules")

    def test_subdir_is_a_path_segment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        a = paths.user_cache_dir("frida")
        b = paths.user_cache_dir("modules")
        assert a.parent == b.parent
        assert a.name == "frida"
        assert b.name == "modules"
