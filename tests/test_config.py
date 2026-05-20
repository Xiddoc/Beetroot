"""Tests for config.py — schema validation, YAML round-trip, env rendering."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from beetroot import paths
from beetroot.config import (
    SUPPORTED_API_VERSION,
    Android,
    Display,
    Frida,
    InstanceConfig,
    Magisk,
    Module,
    Ports,
    Resources,
    base_image_tag,
    load_yaml,
    render_env,
    write_yaml,
)


class TestApiVersion:
    def test_default_api_version_is_supported(self) -> None:
        cfg = InstanceConfig()
        assert cfg.api_version == SUPPORTED_API_VERSION
        assert cfg.api_version == 4

    def test_explicit_supported_version_succeeds(self) -> None:
        cfg = InstanceConfig.model_validate({"api_version": 4})
        assert cfg.api_version == 4

    def test_string_api_version_is_coerced(self) -> None:
        cfg = InstanceConfig.model_validate({"api_version": "4"})
        assert cfg.api_version == 4

    def test_zero_api_version_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 0})
        msg = str(exc_info.value)
        assert "not supported" in msg
        assert "CHANGELOG" in msg

    def test_v1_api_version_raises_via_direct_validate(self) -> None:
        # ``InstanceConfig.model_validate`` doesn't run the auto-bump
        # path — that lives in ``load_yaml``. A direct validate call
        # with a legacy api_version still raises so any code that
        # constructs the model without going through ``load_yaml``
        # surfaces the error explicitly.
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 1})
        msg = str(exc_info.value)
        assert "not supported" in msg
        assert "CHANGELOG" in msg

    def test_v2_api_version_raises_via_direct_validate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 2})
        msg = str(exc_info.value)
        assert "not supported" in msg
        assert "CHANGELOG" in msg

    def test_v3_api_version_raises_via_direct_validate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 3})
        msg = str(exc_info.value)
        assert "not supported" in msg
        assert "CHANGELOG" in msg

    def test_future_api_version_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 99})
        msg = str(exc_info.value)
        assert "not supported" in msg
        assert "CHANGELOG" in msg

    def test_yaml_roundtrip_preserves_api_version(self, tmp_path: Path) -> None:
        cfg = InstanceConfig()
        p = tmp_path / "beetroot.yaml"
        write_yaml(p, cfg)
        loaded = load_yaml(p)
        assert loaded.api_version == SUPPORTED_API_VERSION

    def test_api_version_is_first_field_in_yaml(self, tmp_path: Path) -> None:
        cfg = InstanceConfig()
        p = tmp_path / "beetroot.yaml"
        write_yaml(p, cfg)
        first_line = p.read_text().splitlines()[0]
        assert first_line.startswith("api_version:")


class TestFridaOptional:
    """v0.3 (T2): the frida: block in beetroot.yaml is opt-in."""

    def test_default_instance_config_has_no_frida(self) -> None:
        cfg = InstanceConfig()
        assert cfg.frida is None

    def test_empty_yaml_yields_none_frida(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_yaml(p)
        assert cfg.frida is None

    def test_yaml_without_frida_block_yields_none(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"android": {"version": 14}}))
        cfg = load_yaml(p)
        assert cfg.frida is None

    def test_explicit_null_yaml_yields_none(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text("frida: ~\n")
        cfg = load_yaml(p)
        assert cfg.frida is None

    def test_empty_frida_block_uses_model_default(self, tmp_path: Path) -> None:
        # The model's own default still applies when the block IS present
        # but empty — `frida: {}` instantiates Frida(version="16.4.10").
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"frida": {}}))
        cfg = load_yaml(p)
        assert cfg.frida is not None
        assert cfg.frida.version == Frida().version

    def test_explicit_frida_version_preserved(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"frida": {"version": "16.5.0"}}))
        cfg = load_yaml(p)
        assert cfg.frida is not None
        assert cfg.frida.version == "16.5.0"

    def test_render_env_omits_frida_specific_keys(self) -> None:
        # Behavior test: a user-supplied empty YAML flows through load_yaml
        # → render_env without producing any FRIDA-specific knob beyond the
        # unconditional bind-mount ports (FRIDA_PORT / FRIDA_PORT_CONTROL remain so
        # compose's `${FRIDA_PORT}` substitution still works).
        cfg = InstanceConfig()
        rendered_ports = {"adb": 5555, "frida": 27042, "frida_control": 27043}
        result = render_env("alpha", cfg, rendered_ports)
        # FRIDA_VERSION never appears — the version is consumed by frida_download,
        # not rendered into .env.
        assert "FRIDA_VERSION" not in result
        # The bind-mount port substitutions DO remain — disabled-frida
        # instances still need a non-empty value for compose to resolve
        # the port mapping line even though the binary is the zero-byte
        # placeholder.
        assert "FRIDA_PORT=" in result
        assert "FRIDA_PORT_CONTROL=" in result


class TestFridaVersionRegex:
    """T2 Agent 1: ``Frida.version`` is gated by a major.minor.patch regex."""

    def test_valid_version_accepted(self) -> None:
        assert Frida(version="16.4.10").version == "16.4.10"
        assert Frida(version="100.0.0").version == "100.0.0"
        assert Frida(version="1.0.0").version == "1.0.0"

    @pytest.mark.parametrize(
        "bad",
        [
            "16.4",           # missing patch
            "16.4.10-rc1",    # pre-release suffix
            "16.4.10.dev",    # extra component
            "v16.4.10",       # leading v
            "16.4.10 ",       # trailing whitespace
            "",               # empty
            "abc",            # non-numeric
        ],
    )
    def test_invalid_version_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match=r"major\.minor\.patch"):
            Frida(version=bad)


class TestFridaSha256:
    """T2 Agent 1: optional ``Frida.sha256`` is round-tripped via YAML."""

    def test_default_sha256_is_none(self) -> None:
        assert Frida().sha256 is None

    def test_explicit_sha256_preserved(self) -> None:
        digest = "a" * 64
        assert Frida(sha256=digest).sha256 == digest

    def test_yaml_roundtrip_with_sha256(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        digest = "b" * 64
        p.write_text(
            yaml.safe_dump({"frida": {"version": "16.4.10", "sha256": digest}})
        )
        cfg = load_yaml(p)
        assert cfg.frida is not None
        assert cfg.frida.sha256 == digest


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


class TestAndroidVersion:
    def test_invalid_version_raises(self) -> None:
        with pytest.raises(ValidationError, match="not supported"):
            Android(version=99)

    def test_legacy_base_image_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="base_image is no longer supported"):
            Android.model_validate({"base_image": "redroid/redroid:14"})


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

    def test_shared_mem_default(self) -> None:
        r = Resources()
        assert r.shared_mem == "256m"

    def test_shared_mem_can_be_set(self) -> None:
        r = Resources(shared_mem="512m")
        assert r.shared_mem == "512m"

    def test_shared_mem_has_no_shm_attr(self) -> None:
        r = Resources()
        assert not hasattr(r, "shm")

    def test_legacy_shm_field_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            Resources.model_validate({"shm": "512m"})
        msg = str(exc_info.value)
        assert "no longer supported" in msg
        assert "shared_mem" in msg
        assert "CHANGELOG" in msg

    # D4 — Docker-size-format validators
    def test_valid_mem_sizes_accepted(self) -> None:
        assert Resources(mem="3g").mem == "3g"
        assert Resources(mem="512m").mem == "512m"
        assert Resources(mem="1024").mem == "1024"
        assert Resources(mem="1.5G").mem == "1.5G"

    def test_invalid_mem_two_letter_suffix_rejected(self) -> None:
        # "3gb" uses a two-letter suffix — Docker silently ignores or
        # misinterprets it; catching it at load time keeps the error actionable.
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(mem="3gb")

    def test_invalid_mem_space_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(mem="512 m")

    def test_invalid_mem_empty_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(mem="")

    def test_valid_shared_mem_accepted(self) -> None:
        assert Resources(shared_mem="256m").shared_mem == "256m"

    def test_invalid_shared_mem_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(shared_mem="256mb")

    def test_valid_mem_reservation_accepted(self) -> None:
        r = Resources(mem_reservation="2g")
        assert r.mem_reservation == "2g"

    def test_invalid_mem_reservation_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(mem_reservation="2gb")

    def test_valid_memswap_limit_accepted(self) -> None:
        r = Resources(memswap_limit="4g")
        assert r.memswap_limit == "4g"

    def test_invalid_memswap_limit_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(memswap_limit="4GB")

    def test_none_mem_reservation_is_valid(self) -> None:
        r = Resources(mem_reservation=None)
        assert r.mem_reservation is None

    def test_none_memswap_limit_is_valid(self) -> None:
        r = Resources(memswap_limit=None)
        assert r.memswap_limit is None


class TestDisplayBounds:
    """D4 — Display fields must be > 0."""

    def test_width_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Display(width=0)

    def test_width_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Display(width=-1)

    def test_height_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Display(height=0)

    def test_height_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Display(height=-5)

    def test_fps_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Display(fps=0)

    def test_fps_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Display(fps=-10)

    def test_width_one_accepted(self) -> None:
        assert Display(width=1).width == 1

    def test_height_one_accepted(self) -> None:
        assert Display(height=1).height == 1

    def test_fps_one_accepted(self) -> None:
        assert Display(fps=1).fps == 1


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


class TestMagiskDenylist:
    """D1: per-package regex validator on ``magisk.denylist``.

    SQL-injection prophylaxis for the wire-up of the denylist through
    ``magisk-config.sh``'s SQLite REPLACE INTO. Refusing the malformed
    shape at config-load time keeps the helper script free of escaping
    logic.
    """

    def test_valid_packages_accepted(self) -> None:
        cfg = Magisk(
            denylist=["com.google.android.gms", "com.app_id", "com.x.y.z123"]
        )
        assert cfg.denylist[0] == "com.google.android.gms"

    def test_gms_denylist_default(self) -> None:
        # The GMS pair is the default so a bare ``beetroot create``
        # denylists root from GMS out of the box.
        assert Magisk().denylist == [
            "com.google.android.gms",
            "com.google.android.gms.unstable",
        ]

    def test_package_with_space_rejected(self) -> None:
        with pytest.raises(ValidationError, match="package id"):
            Magisk(denylist=["com.bad package"])

    def test_package_with_semicolon_rejected(self) -> None:
        # SQL-injection probe: a literal "; DROP TABLE settings;" must
        # be rejected by the validator before the helper ever sees it.
        with pytest.raises(ValidationError, match="package id"):
            Magisk(denylist=["com.app'; DROP TABLE settings;--"])

    def test_package_with_dash_rejected(self) -> None:
        # Dashes are not part of the Android package-id grammar; refuse
        # them so the validator can't drift to a looser shape later.
        with pytest.raises(ValidationError, match="package id"):
            Magisk(denylist=["com.bad-package"])

    def test_empty_package_rejected(self) -> None:
        with pytest.raises(ValidationError, match="package id"):
            Magisk(denylist=[""])

    def test_stealth_key_rejected_with_migration_hint(self, tmp_path: Path) -> None:
        # D1/D3: an old YAML using ``stealth:`` must fail with a clear,
        # actionable migration error — not silently drop the denylist.
        p = tmp_path / "old.yaml"
        p.write_text(
            "api_version: 4\n"
            "stealth:\n"
            "  denylist:\n"
            "    - com.google.android.gms\n"
        )
        with pytest.raises(ValidationError) as exc_info:
            load_yaml(p)
        msg = str(exc_info.value)
        assert "stealth" in msg
        assert "magisk" in msg.lower()
        assert "api_version" in msg


class TestPorts:
    def test_defaults_are_none(self) -> None:
        p = Ports()
        assert p.adb is None
        assert p.frida is None
        assert p.frida_control is None

    def test_instance_config_default_ports_is_empty(self) -> None:
        cfg = InstanceConfig()
        assert cfg.ports == Ports()

    def test_yaml_roundtrip_with_adb_override(self, tmp_path: Path) -> None:
        raw = {"ports": {"adb": 9000}}
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg = load_yaml(p)
        assert cfg.ports.adb == 9000
        assert cfg.ports.frida is None
        assert cfg.ports.frida_control is None

    def test_yaml_roundtrip_all_overrides(self, tmp_path: Path) -> None:
        raw = {"ports": {"adb": 1, "frida": 2, "frida_control": 3}}
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg = load_yaml(p)
        assert cfg.ports.adb == 1
        assert cfg.ports.frida == 2
        assert cfg.ports.frida_control == 3

    def test_write_then_load_preserves_override(self, tmp_path: Path) -> None:
        cfg = InstanceConfig(ports=Ports(adb=9000))
        p = tmp_path / "cfg.yaml"
        write_yaml(p, cfg)
        loaded = load_yaml(p)
        assert loaded.ports.adb == 9000
        assert loaded.ports.frida is None

    def test_missing_block_yields_empty_ports(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"display": {"width": 1080}}))
        cfg = load_yaml(p)
        assert cfg.ports == Ports()

    def test_port_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            Ports(adb=0)

    def test_port_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            Ports(frida=-1)

    def test_port_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            Ports(frida_control=65536)

    def test_port_far_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            Ports(adb=70000)

    def test_port_lower_boundary_valid(self) -> None:
        assert Ports(adb=1).adb == 1

    def test_port_upper_boundary_valid(self) -> None:
        assert Ports(frida=65535).frida == 65535

    def test_port_none_still_valid(self) -> None:
        p = Ports(adb=None, frida=None, frida_control=None)
        assert p.adb is None
        assert p.frida is None
        assert p.frida_control is None

    def test_adb_frida_collision_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be distinct"):
            Ports(adb=9000, frida=9000)

    def test_adb_frida_control_collision_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be distinct"):
            Ports(adb=9000, frida_control=9000)

    def test_frida_frida_control_collision_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be distinct"):
            Ports(frida=9000, frida_control=9000)

    def test_three_distinct_ports_valid(self) -> None:
        p = Ports(adb=9000, frida=9001, frida_control=9002)
        assert p.adb == 9000
        assert p.frida == 9001
        assert p.frida_control == 9002

    def test_none_pair_skipped_in_distinct_check(self) -> None:
        Ports(adb=9000, frida=None, frida_control=None)


class TestPermissiveDefaults:
    """Pydantic models use default (permissive) settings — coercion and extra keys work."""

    def test_android_version_string_coerced(self) -> None:
        a = Android.model_validate({"version": "14"})
        assert a.version == 14

    def test_display_width_string_coerced(self) -> None:
        d = Display.model_validate({"width": "720"})
        assert d.width == 720

    def test_resources_cpus_string_coerced(self) -> None:
        r = Resources.model_validate({"cpus": "2.0"})
        assert r.cpus == 2.0

    def test_android_unknown_field_ignored(self) -> None:
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


class TestRenderEnv:
    def _ports(self) -> dict[str, int]:
        return {"adb": 5555, "frida": 27042, "frida_control": 27043}

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
        assert "FRIDA_PORT_CONTROL=27043" in result

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
        assert f"SHM_SIZE={cfg.resources.shared_mem}" in result

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

    def test_emits_default_denylist_packages(self) -> None:
        # D1: the default Magisk model carries the GMS pair so a bare
        # ``beetroot create`` keeps the historical behaviour intact.
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert (
            "BEETROOT_DENYLIST_PACKAGES=com.google.android.gms,"
            "com.google.android.gms.unstable"
        ) in result

    def test_emits_custom_denylist_packages_as_csv(self) -> None:
        # The env var the bundled compose template consumes must be a
        # comma-separated list; toybox sh has no array support, so the
        # helper iterates via ``IFS=,``.
        cfg = InstanceConfig(magisk=Magisk(
            denylist=["com.app.one", "com.app.two", "com.x.y.z123"]
        ))
        result = render_env("alpha", cfg, self._ports())
        assert (
            "BEETROOT_DENYLIST_PACKAGES=com.app.one,com.app.two,com.x.y.z123"
            in result
        )

    def test_emits_empty_denylist_packages_when_explicitly_disabled(self) -> None:
        # An explicit empty list (``magisk.denylist: []``) must surface
        # as ``BEETROOT_DENYLIST_PACKAGES=`` (no value) so the helper's
        # ``if [ -n "$DENYLIST_PACKAGES" ]`` guard short-circuits and no
        # rows are SQL'd.
        cfg = InstanceConfig(magisk=Magisk(denylist=[]))
        result = render_env("alpha", cfg, self._ports())
        assert "BEETROOT_DENYLIST_PACKAGES=\n" in result

    def test_emits_known_safe_container_paths(self) -> None:
        # T2 (Agent 1 1.1 / Agent 3 1.1): render_env is the single
        # source of truth for the helper-side defaults — the compose
        # template still carries ``${VAR:-default}`` fallbacks for the
        # raw-compose escape hatch, but a Beetroot-rendered .env file
        # always sets them to known-safe values. v0.5's PR1 will
        # randomise these once stealth research validates a path.
        cfg = InstanceConfig()
        result = render_env("alpha", cfg, self._ports())
        assert "BEETROOT_MAGISK_DB=/data/adb/magisk.db" in result
        assert "BEETROOT_MODULES_DIR=/data/adb/modules_update" in result
        assert "BEETROOT_FRIDA_BIN=/data/local/tmp/frida-server" in result


class TestWriteLoadYamlRoundtrip:
    def test_default_config_roundtrip(self, tmp_path: Path) -> None:
        cfg = InstanceConfig()
        p = tmp_path / "beetroot.yaml"
        write_yaml(p, cfg)
        loaded = load_yaml(p)
        assert loaded.display.width == cfg.display.width
        assert loaded.display.fps == cfg.display.fps
        assert loaded.resources.mem == cfg.resources.mem
        # v0.3 (T2): frida is opt-in — both sides default to None.
        assert loaded.frida is None
        assert cfg.frida is None

    def test_custom_values_roundtrip(self, tmp_path: Path) -> None:
        raw = {
            "display": {"width": 1080, "height": 1920, "fps": 60, "gpu_mode": "guest"},
            "resources": {"mem": "6g", "cpus": 4.0, "shared_mem": "512m"},
        }
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg = load_yaml(p)
        assert cfg.display.width == 1080
        assert cfg.resources.mem == "6g"
        assert cfg.resources.shared_mem == "512m"

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
# Docker compose config integration test — uses the bundled compose template.
# ---------------------------------------------------------------------------

_DOCKER_AVAILABLE = shutil.which("docker") is not None


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker not available on PATH")
class TestDockerComposeConfig:
    """Regression tests that `docker compose config` validates successfully.

    These tests render the .env via render_env, drop it in a fresh instance
    directory, and shell out to `docker compose config` against the bundled
    template. No container is started — `config` only renders the YAML.
    """

    def _run_compose_config(self, instance_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  # ``instance_root`` is a test-controlled tmp_path
            [  # noqa: S607  # docker resolved via PATH; test helper invokes docker CLI on the host
                "docker",
                "compose",
                "-p",
                "test-beetroot-ci",
                "-f",
                str(paths.bundled_compose_file()),
                "--project-directory",
                str(instance_root),
                "--env-file",
                str(paths.instance_env(instance_root)),
                "config",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _populate(
        self, instance_root: Path, extra: dict[str, str] | None = None
    ) -> None:
        cfg = InstanceConfig()
        ports = {"adb": 5555, "frida": 27042, "frida_control": 27043}
        env_text = render_env("ci-test", cfg, ports)
        if extra:
            env_text += "".join(f"{k}={v}\n" for k, v in extra.items())
        (instance_root / ".env").write_text(env_text)
        # Compose checks the bind-mount source paths exist when resolving
        # config — create placeholders so the YAML validates.
        (instance_root / "data").mkdir()
        (instance_root / "modules").mkdir()
        (instance_root / "frida-server").write_bytes(b"")

    def test_default_config_is_valid(self, tmp_path: Path) -> None:
        instance = tmp_path / "alpha"
        instance.mkdir()
        self._populate(instance)
        result = self._run_compose_config(instance)
        assert result.returncode == 0, (
            f"docker compose config failed for default env.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_default_config_uses_mem_limit_as_reservation(self, tmp_path: Path) -> None:
        instance = tmp_path / "alpha"
        instance.mkdir()
        self._populate(instance)
        result = self._run_compose_config(instance)
        assert result.returncode == 0
        assert "mem_reservation" in result.stdout

    def test_explicit_mem_reservation_is_respected(self, tmp_path: Path) -> None:
        instance = tmp_path / "alpha"
        instance.mkdir()
        self._populate(instance, {"MEM_RESERVATION": "2g", "MEMSWAP_LIMIT": "4g"})
        result = self._run_compose_config(instance)
        assert result.returncode == 0, (
            f"docker compose config failed with explicit reservation.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
