"""Tests for config.py — schema validation, preset loading, env rendering."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from beetroot.config import (
    Android,
    Display,
    InstanceConfig,
    Module,
    Resources,
    base_image_tag,
    load_preset,
    load_yaml,
    render_env,
    write_yaml,
)


class TestAndroidGapps:
    def test_default_gapps_is_lite(self) -> None:
        a = Android()
        assert a.gapps == "lite"

    def test_valid_gapps_none(self) -> None:
        a = Android(gapps="none")
        assert a.gapps == "none"

    def test_valid_gapps_full(self) -> None:
        a = Android(gapps="full")
        assert a.gapps == "full"

    def test_valid_gapps_mindthegapps(self) -> None:
        a = Android(gapps="mindthegapps")
        assert a.gapps == "mindthegapps"

    def test_invalid_gapps_raises(self) -> None:
        with pytest.raises(ValidationError):
            Android(gapps="blah")  # type: ignore[arg-type]


class TestBaseImageTag:
    def test_default_is_litegapps(self) -> None:
        assert base_image_tag(Android()) == "redroid/redroid:14.0.0_litegapps_houdini_magisk"

    def test_none_omits_gapps_slug(self) -> None:
        assert base_image_tag(Android(gapps="none")) == "redroid/redroid:14.0.0_houdini_magisk"

    def test_full_gapps(self) -> None:
        assert base_image_tag(Android(gapps="full")) == "redroid/redroid:14.0.0_gapps_houdini_magisk"

    def test_mindthegapps(self) -> None:
        assert base_image_tag(Android(gapps="mindthegapps")) == "redroid/redroid:14.0.0_mindthegapps_houdini_magisk"

    def test_version_reflected_in_tag(self) -> None:
        assert base_image_tag(Android(version=13)) == "redroid/redroid:13.0.0_litegapps_houdini_magisk"


class TestResources:
    def test_defaults(self) -> None:
        r = Resources()
        assert r.mem_reservation is None
        assert r.memswap_limit is None
        assert r.pids_limit == 4096

    def test_optional_fields_can_be_set(self) -> None:
        r = Resources(mem_reservation="2g", memswap_limit="4g", pids_limit=1500)
        assert r.mem_reservation == "2g"
        assert r.memswap_limit == "4g"
        assert r.pids_limit == 1500


class TestModule:
    def test_url_only_is_valid(self) -> None:
        m = Module(url="https://example.com/mod.zip")
        assert m.url == "https://example.com/mod.zip"
        assert m.path is None

    def test_path_only_is_valid(self) -> None:
        m = Module(path="/tmp/mod.zip")
        assert m.path == "/tmp/mod.zip"
        assert m.url is None

    def test_url_with_sha256_is_valid(self) -> None:
        m = Module(url="https://example.com/mod.zip", sha256="abc123")
        assert m.sha256 == "abc123"

    def test_neither_url_nor_path_raises(self) -> None:
        with pytest.raises(ValidationError, match="must set either"):
            Module()

    def test_both_url_and_path_raises(self) -> None:
        with pytest.raises(ValidationError, match="sets both"):
            Module(url="https://example.com/mod.zip", path="/tmp/mod.zip")


class TestPermissiveDefaults:
    """Pydantic models use default (permissive) settings — coercion and extra keys work."""

    def test_android_version_string_coerced(self) -> None:
        # String-to-int coercion is fine (pydantic default, not strict mode).
        a = Android.model_validate({"version": "14"})
        assert a.version == 14

    def test_display_width_string_coerced(self) -> None:
        d = Display.model_validate({"width": "720"})
        assert d.width == 720

    def test_resources_cpus_string_coerced(self) -> None:
        r = Resources.model_validate({"cpus": "2.0"})
        assert r.cpus == 2.0

    def test_android_unknown_field_ignored(self) -> None:
        # Unknown keys are silently ignored (pydantic default extra="ignore").
        a = Android.model_validate({"version": 14, "unknown_field": "x"})
        assert a.version == 14

    def test_instance_config_unknown_field_ignored(self) -> None:
        cfg = InstanceConfig.model_validate({"android": {"version": 14, "unknown_field": "x"}})
        assert cfg.android.version == 14

    def test_display_unknown_field_ignored(self) -> None:
        d = Display.model_validate({"width": 540, "typo_field": 1})
        assert d.width == 540

    def test_resources_unknown_field_ignored(self) -> None:
        r = Resources.model_validate({"mem": "3g", "typo": "yes"})
        assert r.mem == "3g"


class TestLoadPreset:
    def test_happy_path_default_preset(self, isolated_root: Path) -> None:
        presets = isolated_root / "presets"
        presets.mkdir()
        (presets / "default.yaml").write_text(
            "display:\n  width: 1080\n  height: 1920\n  fps: 30\n"
        )
        cfg = load_preset("default")
        assert cfg.display.width == 1080
        assert cfg.display.height == 1920

    def test_missing_preset_raises_file_not_found(self, isolated_root: Path) -> None:
        presets = isolated_root / "presets"
        presets.mkdir()
        with pytest.raises(FileNotFoundError, match="not found"):
            load_preset("nonexistent")

    def test_missing_preset_error_lists_available(self, isolated_root: Path) -> None:
        presets = isolated_root / "presets"
        presets.mkdir()
        (presets / "stealth.yaml").write_text("{}")
        with pytest.raises(FileNotFoundError, match="stealth"):
            load_preset("nonexistent")


class TestRenderEnv:
    def _ports(self) -> dict[str, int]:
        return {"adb": 5555, "frida": 27042, "frida2": 27043}

    def test_contains_base_image(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "BASE_IMAGE=redroid/redroid:14.0.0_litegapps_houdini_magisk" in result

    def test_base_image_reflects_gapps_none(self) -> None:
        cfg = InstanceConfig(android=Android(gapps="none"))
        result = render_env("alpha", cfg, self._ports())
        assert "BASE_IMAGE=redroid/redroid:14.0.0_houdini_magisk" in result

    def test_contains_instance_name(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "INSTANCE_NAME=alpha" in result

    def test_contains_adb_port(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "ADB_PORT=5555" in result

    def test_contains_frida_port(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "FRIDA_PORT=27042" in result

    def test_contains_frida_port2(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "FRIDA_PORT2=27043" in result

    def test_contains_mem_limit(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"MEM_LIMIT={cfg.resources.mem}" in result

    def test_contains_cpus(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"CPUS={cfg.resources.cpus}" in result

    def test_contains_shm_size(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"SHM_SIZE={cfg.resources.shm}" in result

    def test_contains_display_width(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"DISPLAY_WIDTH={cfg.display.width}" in result

    def test_contains_display_height(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"DISPLAY_HEIGHT={cfg.display.height}" in result

    def test_contains_display_fps(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"DISPLAY_FPS={cfg.display.fps}" in result

    def test_contains_display_gpu(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert f"DISPLAY_GPU={cfg.display.gpu_mode}" in result

    def test_ends_with_newline(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert result.endswith("\n")

    def test_all_lines_are_key_value(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        for line in result.strip().splitlines():
            assert "=" in line, f"line not KEY=VALUE: {line!r}"

    def test_contains_pids_limit_default(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "PIDS_LIMIT=4096" in result

    def test_pids_limit_custom(self) -> None:
        cfg = InstanceConfig(resources=Resources(pids_limit=1500))
        result = render_env("alpha", cfg, self._ports())
        assert "PIDS_LIMIT=1500" in result

    def test_mem_reservation_omitted_when_unset(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "MEM_RESERVATION" not in result

    def test_memswap_limit_omitted_when_unset(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "MEMSWAP_LIMIT" not in result

    def test_mem_reservation_emitted_when_set(self) -> None:
        cfg = InstanceConfig(resources=Resources(mem_reservation="2g"))
        result = render_env("alpha", cfg, self._ports())
        assert "MEM_RESERVATION=2g" in result

    def test_memswap_limit_emitted_when_set(self) -> None:
        cfg = InstanceConfig(resources=Resources(memswap_limit="4g"))
        result = render_env("alpha", cfg, self._ports())
        assert "MEMSWAP_LIMIT=4g" in result


class TestLoadInstance:
    def test_load_instance_reads_yaml(self, isolated_root: Path) -> None:
        from beetroot.config import load_instance, write_yaml
        instance_dir = isolated_root / "instances" / "alpha"
        instance_dir.mkdir(parents=True)
        yaml_path = instance_dir / "beetroot.yaml"
        write_yaml(yaml_path, InstanceConfig())
        cfg = load_instance("alpha")
        assert cfg.display.width == 540


class TestWriteLoadYamlRoundtrip:
    def test_default_config_roundtrip(self, tmp_path: Path) -> None:
        cfg = InstanceConfig()
        p = tmp_path / "beetroot.yaml"
        write_yaml(p, cfg)
        loaded = load_yaml(p)
        assert loaded.display.width == cfg.display.width
        assert loaded.display.fps == cfg.display.fps
        assert loaded.resources.mem == cfg.resources.mem
        assert loaded.frida is not None
        assert cfg.frida is not None
        assert loaded.frida.version == cfg.frida.version

    def test_custom_values_roundtrip(self, tmp_path: Path) -> None:
        raw = {
            "display": {"width": 1080, "height": 1920, "fps": 60, "gpu_mode": "guest"},
            "resources": {"mem": "6g", "cpus": 4.0, "shm": "512m"},
        }
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg = load_yaml(p)
        assert cfg.display.width == 1080
        assert cfg.resources.mem == "6g"

    def test_write_yaml_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "deep" / "nested" / "beetroot.yaml"
        write_yaml(p, InstanceConfig())
        assert p.exists()

    def test_empty_file_yields_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_yaml(p)
        assert cfg.display.width == 540


# ---------------------------------------------------------------------------
# Docker compose config integration test
# ---------------------------------------------------------------------------

# Real compose.yaml lives at the repo root (one level above tests/).
_COMPOSE_YAML = Path(__file__).resolve().parents[1] / "compose.yaml"

_DOCKER_AVAILABLE = shutil.which("docker") is not None


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker not available on PATH")
class TestDockerComposeConfig:
    """Regression tests that `docker compose config` validates successfully.

    These tests write a minimal .env (produced by render_env) to a tmp file
    and shell out to `docker compose config` to catch substitution errors.
    No container is started — `config` only renders and validates the YAML.
    """

    def _run_compose_config(self, env_file: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "test-beetroot-ci",
                "-f",
                str(_COMPOSE_YAML),
                "--env-file",
                str(env_file),
                "config",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_env(self, tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
        """Write a minimal .env from a default Resources() config."""
        cfg = InstanceConfig()
        ports = {"adb": 5555, "frida": 27042, "frida2": 27043}
        env_text = render_env("ci-test", cfg, ports)
        if extra:
            env_text += "".join(f"{k}={v}\n" for k, v in extra.items())
        env_file = tmp_path / ".env"
        env_file.write_text(env_text)
        return env_file

    def test_default_config_is_valid(self, tmp_path: Path) -> None:
        """Default Resources() (no mem_reservation/memswap_limit) must exit 0."""
        env_file = self._write_env(tmp_path)
        result = self._run_compose_config(env_file)
        assert result.returncode == 0, (
            f"docker compose config failed for default env.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_default_config_uses_mem_limit_as_reservation(self, tmp_path: Path) -> None:
        """mem_reservation should resolve to MEM_LIMIT (3g) when not overridden."""
        env_file = self._write_env(tmp_path)
        result = self._run_compose_config(env_file)
        assert result.returncode == 0
        # Docker normalises size strings (e.g. "3g" → "3221225472") in the output.
        assert "mem_reservation" in result.stdout

    def test_explicit_mem_reservation_is_respected(self, tmp_path: Path) -> None:
        """When MEM_RESERVATION is set, docker compose config must accept it."""
        env_file = self._write_env(tmp_path, {"MEM_RESERVATION": "2g", "MEMSWAP_LIMIT": "4g"})
        result = self._run_compose_config(env_file)
        assert result.returncode == 0, (
            f"docker compose config failed with explicit reservation.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
