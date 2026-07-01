"""Tests for config.py — schema validation, YAML round-trip, env rendering."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from beetroot import config, paths
from beetroot.config import (
    DEFAULT_ANDROID_VERSION,
    SUPPORTED_API_VERSION,
    Android,
    Display,
    Frida,
    InstanceConfig,
    Magisk,
    Module,
    PortMapping,
    Resources,
    base_image_tag,
    is_pinned_frida_version,
    load_yaml,
    render_compose_ports_override,
    render_env,
    resolve_gapps_vendor,
    resolve_rendering,
    vm_redroid_image,
    write_yaml,
)
from beetroot.ports import EXTRA_POOL_BASE, resolve_ports


class TestApiVersion:
    def test_default_api_version_is_supported(self) -> None:
        cfg = InstanceConfig()
        assert cfg.api_version == SUPPORTED_API_VERSION
        assert cfg.api_version == 8

    def test_explicit_supported_version_succeeds(self) -> None:
        cfg = InstanceConfig.model_validate({"api_version": 8})
        assert cfg.api_version == 8

    def test_string_api_version_is_coerced(self) -> None:
        cfg = InstanceConfig.model_validate({"api_version": "8"})
        assert cfg.api_version == 8

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

    def test_v5_api_version_raises_via_direct_validate(self) -> None:
        # #124 bumped SUPPORTED to 6; a direct validate of the now-legacy 5
        # still raises (auto-bump only happens in load_yaml).
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 5})
        msg = str(exc_info.value)
        assert "not supported" in msg
        assert "CHANGELOG" in msg

    def test_v6_api_version_raises_via_direct_validate(self) -> None:
        # #107 bumped SUPPORTED to 7; a direct validate of the now-legacy 6
        # still raises (auto-bump only happens in load_yaml).
        with pytest.raises(ValidationError) as exc_info:
            InstanceConfig.model_validate({"api_version": 6})
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
        # but empty — `frida: {}` instantiates Frida(version="auto").
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"frida": {}}))
        cfg = load_yaml(p)
        assert cfg.frida is not None
        assert cfg.frida.version == Frida().version == "auto"

    def test_explicit_frida_version_preserved(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"frida": {"version": "16.5.0"}}))
        cfg = load_yaml(p)
        assert cfg.frida is not None
        assert cfg.frida.version == "16.5.0"

    def test_render_env_omits_frida_specific_keys(self) -> None:
        # Behavior test: a user-supplied empty YAML flows through load_yaml
        # → render_env without producing any FRIDA-specific knob. Since v8
        # (issue #108) ports live in the compose override, not .env, so no
        # FRIDA_PORT lines appear here at all.
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        # FRIDA_VERSION never appears — the version is consumed by frida_download,
        # not rendered into .env.
        assert "FRIDA_VERSION" not in result
        # Ports moved to compose.override.yaml in v8; .env no longer carries them.
        assert "FRIDA_PORT" not in result
        assert "ADB_PORT" not in result


class TestFridaVersionRegex:
    """T2 Agent 1: ``Frida.version`` is gated by a major.minor.patch regex."""

    def test_valid_version_accepted(self) -> None:
        assert Frida(version="16.4.10").version == "16.4.10"
        assert Frida(version="100.0.0").version == "100.0.0"
        assert Frida(version="1.0.0").version == "1.0.0"

    @pytest.mark.parametrize(
        "bad",
        [
            "16.4",  # missing patch
            "16.4.10-rc1",  # pre-release suffix
            "16.4.10.dev",  # extra component
            "v16.4.10",  # leading v
            "16.4.10 ",  # trailing whitespace
            "",  # empty
            "abc",  # non-numeric
        ],
    )
    def test_invalid_version_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match=r"major\.minor\.patch"):
            Frida(version=bad)

    def test_default_version_is_auto(self) -> None:
        assert Frida().version == "auto"

    @pytest.mark.parametrize("symbolic", ["auto", "latest"])
    def test_symbolic_versions_accepted(self, symbolic: str) -> None:
        assert Frida(version=symbolic).version == symbolic

    def test_sha256_rejected_with_symbolic_version(self) -> None:
        # A digest pins one specific build, so it can't accompany a moving
        # target (auto/latest) — issue #105.
        for symbolic in ("auto", "latest"):
            with pytest.raises(ValidationError, match="requires a pinned"):
                Frida(version=symbolic, sha256="a" * 64)

    def test_sha256_allowed_with_pinned_version(self) -> None:
        assert Frida(version="16.4.10", sha256="a" * 64).version == "16.4.10"

    def test_is_pinned_frida_version(self) -> None:
        assert is_pinned_frida_version("16.4.10")
        assert is_pinned_frida_version("100.0.0")
        assert not is_pinned_frida_version("latest")
        assert not is_pinned_frida_version("auto")
        assert not is_pinned_frida_version("16.4")


class TestFridaSha256:
    """T2 Agent 1: optional ``Frida.sha256`` is round-tripped via YAML."""

    def test_default_sha256_is_none(self) -> None:
        assert Frida().sha256 is None

    def test_explicit_sha256_preserved(self) -> None:
        digest = "a" * 64
        assert Frida(version="16.4.10", sha256=digest).sha256 == digest

    def test_yaml_roundtrip_with_sha256(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        digest = "b" * 64
        p.write_text(yaml.safe_dump({"frida": {"version": "16.4.10", "sha256": digest}}))
        cfg = load_yaml(p)
        assert cfg.frida is not None
        assert cfg.frida.sha256 == digest


class TestAndroidGapps:
    def test_default_gapps_is_minimal(self) -> None:
        a = Android()
        assert a.gapps == "minimal"
        assert a.gapps_vendor is None

    def test_valid_gapps_none(self) -> None:
        a = Android(gapps="none")
        assert a.gapps == "none"

    def test_valid_gapps_full(self) -> None:
        a = Android(gapps="full")
        assert a.gapps == "full"

    def test_valid_gapps_vendor_override(self) -> None:
        a = Android(gapps="full", gapps_vendor="mindthegapps")
        assert a.gapps == "full"
        assert a.gapps_vendor == "mindthegapps"

    def test_invalid_gapps_raises(self) -> None:
        with pytest.raises(ValidationError):
            Android(gapps="blah")  # type: ignore[arg-type]

    def test_invalid_gapps_vendor_raises(self) -> None:
        with pytest.raises(ValidationError):
            Android(gapps_vendor="blah")  # type: ignore[arg-type]

    def test_legacy_lite_value_rejected_with_migration_hint(self) -> None:
        with pytest.raises(ValidationError, match="gapps_vendor: litegapps"):
            Android.model_validate({"gapps": "lite"})

    def test_legacy_mindthegapps_value_rejected_with_migration_hint(self) -> None:
        with pytest.raises(ValidationError, match="gapps_vendor: mindthegapps"):
            Android.model_validate({"gapps": "mindthegapps"})

    def test_vendor_with_none_intent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="asks for no GApps"):
            Android(gapps="none", gapps_vendor="litegapps")


class TestResolveGappsVendor:
    def test_none_intent_resolves_to_no_vendor(self) -> None:
        assert resolve_gapps_vendor(Android(gapps="none")) is None

    def test_minimal_defaults_to_litegapps(self) -> None:
        assert resolve_gapps_vendor(Android(gapps="minimal")) == "litegapps"

    def test_full_defaults_to_opengapps(self) -> None:
        assert resolve_gapps_vendor(Android(gapps="full")) == "opengapps"

    def test_explicit_vendor_overrides_intent_default(self) -> None:
        assert resolve_gapps_vendor(Android(gapps="full", gapps_vendor="mindthegapps")) == (
            "mindthegapps"
        )


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
        assert (
            base_image_tag(Android(gapps="full")) == "redroid/redroid:14.0.0_gapps_houdini_magisk"
        )

    def test_mindthegapps_via_vendor(self) -> None:
        assert (
            base_image_tag(Android(gapps="full", gapps_vendor="mindthegapps"))
            == "redroid/redroid:14.0.0_mindthegapps_houdini_magisk"
        )

    def test_version_reflected_in_tag(self) -> None:
        assert (
            base_image_tag(Android(version=13)) == "redroid/redroid:13.0.0_litegapps_houdini_magisk"
        )


class TestDefaultAndroidVersion:
    def test_constant_is_the_schema_default(self) -> None:
        # Single source of truth (issue #82): the schema default IS the constant.
        assert Android().version == DEFAULT_ANDROID_VERSION

    def test_constant_drives_default_base_image(self) -> None:
        assert f":{DEFAULT_ANDROID_VERSION}.0.0" in base_image_tag(Android())


class TestVmRedroidImage:
    def test_derives_plain_latest_tag(self) -> None:
        # The vm guest bakes an UNMODIFIED upstream redroid image (-latest
        # suffix), distinct from base_image_tag's Magisk-layered tag.
        assert vm_redroid_image(14) == "redroid/redroid:14.0.0-latest"
        assert vm_redroid_image(11) == "redroid/redroid:11.0.0-latest"

    def test_default_version_matches_create_default(self) -> None:
        # A default `create` and a default `build --vm-kernel` agree (issue #82).
        assert vm_redroid_image(DEFAULT_ANDROID_VERSION) == "redroid/redroid:14.0.0-latest"


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

    # cpus / pids_limit bounds — Docker treats 0 as *unlimited* (the opposite
    # of a cap) and a negative cpus aborts container start with a cgroup error
    # detached from the offending YAML line, so both must fail at load time
    # like every sibling numeric knob (Display.width/height/fps, Vm.*).
    def test_valid_cpus_accepted(self) -> None:
        assert Resources(cpus=0.5).cpus == 0.5
        assert Resources(cpus=8).cpus == 8

    def test_zero_cpus_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than 0"):
            Resources(cpus=0)

    def test_negative_cpus_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than 0"):
            Resources(cpus=-1)

    def test_valid_pids_limit_accepted(self) -> None:
        assert Resources(pids_limit=1).pids_limit == 1

    def test_zero_pids_limit_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            Resources(pids_limit=0)

    def test_negative_pids_limit_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            Resources(pids_limit=-5)


class TestResourceMemoryOrdering:
    """#267 — reject an inverted soft-floor / swap-cap against ``mem`` at load."""

    def test_mem_reservation_above_mem_rejected(self) -> None:
        # A soft floor above the hard cap fails opaquely at compose-up; catch it.
        with pytest.raises(ValidationError, match="cannot exceed the hard resources"):
            Resources(mem="1g", mem_reservation="2g")

    def test_memswap_limit_below_mem_rejected(self) -> None:
        # The total memory+swap cap must be >= the hard mem cap.
        with pytest.raises(ValidationError, match="cannot be below the hard resources"):
            Resources(mem="4g", memswap_limit="2g")

    def test_mem_reservation_equal_to_mem_accepted(self) -> None:
        # Equality is the boundary — a floor exactly at the cap is fine.
        assert Resources(mem="2g", mem_reservation="2g").mem_reservation == "2g"

    def test_memswap_limit_equal_to_mem_accepted(self) -> None:
        assert Resources(mem="2g", memswap_limit="2g").memswap_limit == "2g"

    def test_valid_ordering_across_suffixes_accepted(self) -> None:
        # A floor below and a swap cap above the mem cap, expressed with mixed
        # suffixes + bare bytes, all parse to bytes and pass.
        r = Resources(mem="1g", mem_reservation="512m", memswap_limit="2147483648")
        assert r.mem_reservation == "512m"
        assert r.memswap_limit == "2147483648"

    def test_memswap_unlimited_sentinel_accepted(self) -> None:
        # ``-1`` means unlimited swap — never "below" mem, so it is skipped.
        assert Resources(mem="8g", memswap_limit="-1").memswap_limit == "-1"

    def test_unset_optionals_unaffected(self) -> None:
        # The default (both None) never trips the ordering check.
        r = Resources(mem="1g")
        assert r.mem_reservation is None
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


class TestRendering:
    """`display.rendering` is a validated intent enum (issue #106)."""

    def test_default_is_auto(self) -> None:
        assert Display().rendering == "auto"

    @pytest.mark.parametrize("value", ["gpu", "software", "auto"])
    def test_valid_values_accepted(self, value: str) -> None:
        assert Display.model_validate({"rendering": value}).rendering == value

    def test_typo_rejected_at_load(self) -> None:
        with pytest.raises(ValidationError):
            Display.model_validate({"rendering": "hostt"})

    def test_legacy_gpu_mode_rejected_with_migration_hint(self) -> None:
        with pytest.raises(ValidationError, match=r"display\.rendering"):
            Display.model_validate({"gpu_mode": "host"})

    @pytest.mark.parametrize(
        ("rendering", "expected"),
        [("gpu", "host"), ("software", "guest")],
    )
    def test_resolve_rendering_maps_to_redroid_vocab(self, rendering: str, expected: str) -> None:
        assert resolve_rendering(rendering) == expected

    def test_resolve_auto_uses_gpu_when_render_node_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("beetroot.config._host_has_render_node", lambda: True)
        assert resolve_rendering("auto") == "host"

    def test_resolve_auto_uses_software_without_render_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("beetroot.config._host_has_render_node", lambda: False)
        assert resolve_rendering("auto") == "guest"

    def test_render_env_emits_resolved_gpu_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # auto + no render node → software → redroid "guest"
        monkeypatch.setattr("beetroot.config._host_has_render_node", lambda: False)
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "DISPLAY_GPU=guest" in result

    def test_host_has_render_node_reflects_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "_RENDER_NODE_DIR", tmp_path)
        assert config._host_has_render_node() is False
        (tmp_path / "renderD128").touch()
        assert config._host_has_render_node() is True


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
        digest = "abcdef01" * 8
        m = Module(url="https://example.com/mod.zip", sha256=digest)
        assert m.sha256 == digest

    def test_short_sha256_rejected(self) -> None:
        # #194: a non-64-hex module.sha256 is rejected at load time (mirrors
        # frida.sha256), not late after a full download + extract.
        with pytest.raises(ValidationError, match="64-character hex SHA-256"):
            Module(url="https://example.com/mod.zip", sha256="abc123")

    def test_trailing_newline_sha256_rejected(self) -> None:
        # #194: ``$`` matches before a trailing newline, so a digest pasted with
        # a trailing ``\n`` must still be rejected (``fullmatch``, not ``match``).
        with pytest.raises(ValidationError, match="64-character hex SHA-256"):
            Module(url="https://example.com/mod.zip", sha256="a" * 64 + "\n")

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
        cfg = Magisk(denylist=["com.google.android.gms", "com.app_id", "com.x.y.z123"])
        assert cfg.denylist[0] == "com.google.android.gms"

    def test_package_with_process_accepted(self) -> None:
        # issue #170: the ``package/process`` shape enrols a process of a
        # package (e.g. the DroidGuard process of GMS) under its REAL
        # package. Both halves are validated against the package grammar.
        cfg = Magisk(
            denylist=["com.google.android.gms/com.google.android.gms.unstable"]
        )
        assert cfg.denylist[0] == "com.google.android.gms/com.google.android.gms.unstable"

    def test_package_with_malformed_process_rejected(self) -> None:
        # The process half is validated against the same grammar; a dash in
        # it (not part of the Android package-id grammar) is refused.
        with pytest.raises(ValidationError, match=r"package\[/process\] id"):
            Magisk(denylist=["com.google.android.gms/bad-proc"])

    def test_package_with_two_slashes_rejected(self) -> None:
        # Only a single optional '/' is allowed; a second slash is neither a
        # valid package nor process half.
        with pytest.raises(ValidationError, match=r"package\[/process\] id"):
            Magisk(denylist=["com.a/com.b/com.c"])

    def test_gms_denylist_default(self) -> None:
        # issue #170: the default hides root in the GMS main process AND its
        # ``.unstable`` DroidGuard process, both under the REAL package
        # ``com.google.android.gms`` — the ``.unstable`` string is a PROCESS,
        # not a package, so it must ride on the package/process form.
        assert Magisk().denylist == [
            "com.google.android.gms",
            "com.google.android.gms/com.google.android.gms.unstable",
        ]

    def test_package_with_space_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"package\[/process\] id"):
            Magisk(denylist=["com.bad package"])

    def test_package_with_semicolon_rejected(self) -> None:
        # SQL-injection probe: a literal "; DROP TABLE settings;" must
        # be rejected by the validator before the helper ever sees it.
        with pytest.raises(ValidationError, match=r"package\[/process\] id"):
            Magisk(denylist=["com.app'; DROP TABLE settings;--"])

    def test_package_with_dash_rejected(self) -> None:
        # Dashes are not part of the Android package-id grammar; refuse
        # them so the validator can't drift to a looser shape later.
        with pytest.raises(ValidationError, match=r"package\[/process\] id"):
            Magisk(denylist=["com.bad-package"])

    def test_empty_package_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"package\[/process\] id"):
            Magisk(denylist=[""])

    def test_stealth_key_rejected_with_migration_hint(self, tmp_path: Path) -> None:
        # D1/D3: an old YAML using ``stealth:`` must fail with a clear,
        # actionable migration error — not silently drop the denylist.
        p = tmp_path / "old.yaml"
        p.write_text("api_version: 4\nstealth:\n  denylist:\n    - com.google.android.gms\n")
        with pytest.raises(ValidationError) as exc_info:
            load_yaml(p)
        msg = str(exc_info.value)
        assert "stealth" in msg
        assert "magisk" in msg.lower()
        assert "api_version" in msg


def _services(cfg: InstanceConfig) -> dict[str, int | None]:
    """Map service name → host for a config's well-known seeded mappings."""
    return {m.service: m.host for m in cfg.ports if m.service is not None}


class TestPortMapping:
    def test_default_seeds_three_well_known_services(self) -> None:
        cfg = InstanceConfig()
        assert [m.service for m in cfg.ports] == ["adb", "frida", "frida_control"]
        assert all(m.host is None for m in cfg.ports)
        assert [m.guest for m in cfg.ports] == [5555, 27042, 27043]

    def test_guest_required(self) -> None:
        with pytest.raises(ValidationError):
            PortMapping(service="adb")  # type: ignore[call-arg]

    def test_guest_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            PortMapping(guest=0)

    def test_guest_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            PortMapping(guest=70000)

    def test_host_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            PortMapping(guest=8080, host=-1)

    def test_host_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            PortMapping(guest=8080, host=65536)

    def test_host_boundaries_valid(self) -> None:
        assert PortMapping(guest=1, host=1).host == 1
        assert PortMapping(guest=2, host=65535).host == 65535

    def test_arbitrary_unlabelled_mapping_valid(self) -> None:
        m = PortMapping(guest=8080, host=9000)
        assert m.service is None
        assert (m.guest, m.host) == (8080, 9000)


class TestPortsListValidation:
    def test_arbitrary_entries_appended(self) -> None:
        cfg = InstanceConfig(
            ports=[
                *InstanceConfig().ports,
                PortMapping(guest=8080, host=9000),
                PortMapping(guest=8081),
            ]
        )
        assert len(cfg.ports) == 5

    def test_duplicate_service_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate service"):
            InstanceConfig(
                ports=[
                    PortMapping(service="adb", guest=5555),
                    PortMapping(service="adb", guest=5556),
                ]
            )

    def test_duplicate_guest_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate guest"):
            InstanceConfig(
                ports=[
                    PortMapping(service="adb", guest=5555),
                    PortMapping(guest=5555, host=9000),
                ]
            )

    def test_duplicate_explicit_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate explicit host"):
            InstanceConfig(
                ports=[
                    PortMapping(guest=8080, host=9000),
                    PortMapping(guest=8081, host=9000),
                ]
            )

    def test_unset_hosts_not_treated_as_duplicates(self) -> None:
        # Two host=None entries are fine — they auto-allocate distinctly.
        InstanceConfig(
            ports=[
                PortMapping(service="adb", guest=5555),
                PortMapping(guest=8081),
                PortMapping(guest=8082),
            ]
        )

    def test_ports_omitting_adb_rejected(self) -> None:
        # adb_address (and the doctor adb.connect row) is derived from the
        # service: adb mapping; a list without it would KeyError downstream, so
        # it is rejected at load time with a clear message.
        with pytest.raises(ValidationError, match="service: adb"):
            InstanceConfig(ports=[PortMapping(service="frida", guest=27042)])

    def test_frida_block_without_frida_service_rejected(self) -> None:
        # A frida: block needs a service: frida mapping to derive frida_address.
        with pytest.raises(ValidationError, match="service: frida"):
            InstanceConfig(
                frida=Frida(version="16.4.10"),
                ports=[PortMapping(service="adb", guest=5555)],
            )

    def test_frida_service_not_required_without_frida_block(self) -> None:
        # No frida: block → no service: frida required.
        InstanceConfig(ports=[PortMapping(service="adb", guest=5555)])

    def test_explicit_empty_list_rejected_missing_adb(self, tmp_path: Path) -> None:
        # An explicit empty list is the NEW-form "forward nothing" — distinct
        # from an absent key (which seeds defaults via the default_factory). But
        # every backend derives its adb_address from a service: adb mapping, so
        # an empty list (no adb) is rejected by the required-addressing-services
        # validator rather than silently producing an unaddressable instance.
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": SUPPORTED_API_VERSION, "ports": []}))
        with pytest.raises(ValidationError, match="service: adb"):
            load_yaml(p)


class TestPortsListYamlRoundtrip:
    def test_list_form_roundtrips(self, tmp_path: Path) -> None:
        cfg = InstanceConfig(
            ports=[
                PortMapping(service="adb", guest=5555, host=9000),
                PortMapping(guest=8080),
            ]
        )
        p = tmp_path / "cfg.yaml"
        write_yaml(p, cfg)
        loaded = load_yaml(p)
        assert _services(loaded)["adb"] == 9000
        assert any(m.service is None and m.guest == 8080 for m in loaded.ports)

    def test_default_config_roundtrips_as_list(self, tmp_path: Path) -> None:
        cfg = InstanceConfig()
        p = tmp_path / "cfg.yaml"
        write_yaml(p, cfg)
        loaded = load_yaml(p)
        assert [m.service for m in loaded.ports] == ["adb", "frida", "frida_control"]


class TestLegacyPortsMappingMigration:
    def test_adb_host_override_translated(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": 7, "ports": {"adb": 9000}}))
        cfg = load_yaml(p)
        assert _services(cfg) == {"adb": 9000, "frida": None, "frida_control": None}
        assert cfg.api_version == SUPPORTED_API_VERSION

    def test_all_well_known_overrides_translated(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(
            yaml.safe_dump({"api_version": 7, "ports": {"adb": 1, "frida": 2, "frida_control": 3}})
        )
        cfg = load_yaml(p)
        assert _services(cfg) == {"adb": 1, "frida": 2, "frida_control": 3}

    def test_empty_mapping_seeds_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": 7, "ports": {}}))
        cfg = load_yaml(p)
        # An empty OLD-form mapping means "use defaults": the key is dropped so
        # the default_factory seeds the three well-known services (NOT an empty
        # list — that would drop adb/frida and KeyError the address accessors).
        assert [m.service for m in cfg.ports] == ["adb", "frida", "frida_control"]
        assert _services(cfg) == {"adb": None, "frida": None, "frida_control": None}

    def test_unknown_key_raises_migration_error(self) -> None:
        with pytest.raises(ValidationError, match="non-well-known"):
            InstanceConfig.model_validate({"ports": {"telemetry": 9000}})

    def test_old_form_unknown_key_raises_and_suppresses_autobump_note(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A populated old-form ports dict carrying a non-well-known key triggers
        # the migration error; the "auto-upgraded" note must be suppressed
        # (printing it before the contradicting error is wrong), mirroring the
        # gpu_mode / gapps precedent.
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": 7, "ports": {"telemetry": 9000}}))
        with pytest.raises(ValidationError, match="non-well-known"):
            load_yaml(p)
        assert "auto-upgraded" not in capsys.readouterr().err

    def test_migrated_override_reflected_in_resolve(self, tmp_path: Path) -> None:
        # Behavior test on the final resolved artifact for the legacy form.
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": 7, "ports": {"adb": 9000}}))
        cfg = load_yaml(p)
        resolved = resolve_ports(0, cfg.ports)
        hosts = {rp.service: rp.host for rp in resolved}
        assert hosts == {"adb": 9000, "frida": 27042, "frida_control": 27043}


class TestRenderComposePortsOverride:
    def test_default_override_yaml(self) -> None:
        cfg = InstanceConfig()
        resolved = resolve_ports(0, cfg.ports)
        doc = yaml.safe_load(render_compose_ports_override(resolved))
        assert doc["services"]["phone"]["ports"] == [
            "5555:5555",
            "27042:27042",
            "27043:27043",
        ]

    def test_override_yaml_with_arbitrary_and_explicit(self) -> None:
        # The crux artifact test: explicit host wins, arbitrary auto-allocates.
        cfg = InstanceConfig(
            ports=[
                *InstanceConfig().ports,
                PortMapping(guest=8080, host=9000),
                PortMapping(guest=8081),
            ]
        )
        resolved = resolve_ports(0, cfg.ports)
        doc = yaml.safe_load(render_compose_ports_override(resolved))
        entries = set(doc["services"]["phone"]["ports"])
        assert entries == {
            "5555:5555",
            "27042:27042",
            "27043:27043",
            "9000:8080",
            f"{EXTRA_POOL_BASE}:8081",
        }

    def test_override_ends_with_newline(self) -> None:
        resolved = resolve_ports(0, InstanceConfig().ports)
        assert render_compose_ports_override(resolved).endswith("\n")


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
    def test_contains_base_image(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "BASE_IMAGE=redroid/redroid:14.0.0_litegapps_houdini_magisk" in result

    def test_base_image_reflects_gapps_none(self) -> None:
        cfg = InstanceConfig(android=Android(gapps="none"))
        result = render_env("alpha", cfg)
        assert "BASE_IMAGE=redroid/redroid:14.0.0_houdini_magisk" in result

    def test_contains_instance_name(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "INSTANCE_NAME=alpha" in result

    def test_omits_port_lines(self) -> None:
        # Since v8 (issue #108) ports live in compose.override.yaml, not .env.
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "ADB_PORT" not in result
        assert "FRIDA_PORT" not in result
        assert "FRIDA_PORT_CONTROL" not in result

    def test_contains_mem_limit(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"MEM_LIMIT={cfg.resources.mem}" in result

    def test_contains_cpus(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"CPUS={cfg.resources.cpus}" in result

    def test_contains_shm_size(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"SHM_SIZE={cfg.resources.shared_mem}" in result

    def test_contains_display_width(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"DISPLAY_WIDTH={cfg.display.width}" in result

    def test_contains_display_height(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"DISPLAY_HEIGHT={cfg.display.height}" in result

    def test_contains_display_fps(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"DISPLAY_FPS={cfg.display.fps}" in result

    def test_contains_display_gpu(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert f"DISPLAY_GPU={resolve_rendering(cfg.display.rendering)}" in result

    def test_ends_with_newline(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert result.endswith("\n")

    def test_all_lines_are_key_value(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        for line in result.strip().splitlines():
            assert "=" in line, f"line not KEY=VALUE: {line!r}"

    def test_contains_pids_limit_default(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "PIDS_LIMIT=4096" in result

    def test_pids_limit_custom(self) -> None:
        cfg = InstanceConfig(resources=Resources(pids_limit=1500))
        result = render_env("alpha", cfg)
        assert "PIDS_LIMIT=1500" in result

    def test_mem_reservation_omitted_when_unset(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "MEM_RESERVATION" not in result

    def test_memswap_limit_omitted_when_unset(self) -> None:
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert "MEMSWAP_LIMIT" not in result

    def test_mem_reservation_emitted_when_set(self) -> None:
        cfg = InstanceConfig(resources=Resources(mem_reservation="2g"))
        result = render_env("alpha", cfg)
        assert "MEM_RESERVATION=2g" in result

    def test_memswap_limit_emitted_when_set(self) -> None:
        cfg = InstanceConfig(resources=Resources(memswap_limit="4g"))
        result = render_env("alpha", cfg)
        assert "MEMSWAP_LIMIT=4g" in result

    def test_emits_default_denylist_packages(self) -> None:
        # issue #170: the default carries the GMS main package plus the
        # ``.unstable`` DroidGuard process under its real package, joined with
        # a comma; the ``/`` inside the second entry survives the CSV join.
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
        assert (
            "BEETROOT_DENYLIST_PACKAGES=com.google.android.gms,"
            "com.google.android.gms/com.google.android.gms.unstable"
        ) in result

    def test_emits_custom_denylist_packages_as_csv(self) -> None:
        # The env var the bundled compose template consumes must be a
        # comma-separated list; toybox sh has no array support, so the
        # helper iterates via ``IFS=,``.
        cfg = InstanceConfig(magisk=Magisk(denylist=["com.app.one", "com.app.two", "com.x.y.z123"]))
        result = render_env("alpha", cfg)
        assert "BEETROOT_DENYLIST_PACKAGES=com.app.one,com.app.two,com.x.y.z123" in result

    def test_emits_empty_denylist_packages_when_explicitly_disabled(self) -> None:
        # An explicit empty list (``magisk.denylist: []``) must surface
        # as ``BEETROOT_DENYLIST_PACKAGES=`` (no value) so the helper's
        # ``if [ -n "$DENYLIST_PACKAGES" ]`` guard short-circuits and no
        # rows are SQL'd.
        cfg = InstanceConfig(magisk=Magisk(denylist=[]))
        result = render_env("alpha", cfg)
        assert "BEETROOT_DENYLIST_PACKAGES=\n" in result

    def test_emits_known_safe_container_paths(self) -> None:
        # T2 (Agent 1 1.1 / Agent 3 1.1): render_env is the single
        # source of truth for the helper-side defaults — the compose
        # template still carries ``${VAR:-default}`` fallbacks for the
        # raw-compose escape hatch, but a Beetroot-rendered .env file
        # always sets them to known-safe values. v0.5's PR1 will
        # randomise these once stealth research validates a path.
        cfg = InstanceConfig()
        result = render_env("alpha", cfg)
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
            "display": {"width": 1080, "height": 1920, "fps": 60, "rendering": "software"},
            "resources": {"mem": "6g", "cpus": 4.0, "shared_mem": "512m"},
        }
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg = load_yaml(p)
        assert cfg.display.width == 1080
        assert cfg.display.rendering == "software"
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
                "-f",
                str(paths.instance_compose_override(instance_root)),
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

    def _populate(self, instance_root: Path, extra: dict[str, str] | None = None) -> None:
        cfg = InstanceConfig()
        env_text = render_env("ci-test", cfg)
        if extra:
            env_text += "".join(f"{k}={v}\n" for k, v in extra.items())
        (instance_root / ".env").write_text(env_text)
        (instance_root / "compose.override.yaml").write_text(
            render_compose_ports_override(resolve_ports(0, cfg.ports))
        )
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

    def test_default_config_does_not_disable_swap(self, tmp_path: Path) -> None:
        # An all-defaults instance must NOT resolve memswap_limit == mem_limit
        # (Docker reads that as "zero swap" — #169). The template defaults
        # MEMSWAP_LIMIT to 0, which compose treats as unset and drops from the
        # resolved config, so Docker applies its normal swap allowance.
        instance = tmp_path / "alpha"
        instance.mkdir()
        self._populate(instance)
        result = self._run_compose_config(instance)
        assert result.returncode == 0
        assert "memswap_limit" not in result.stdout

    def test_explicit_memswap_limit_is_respected(self, tmp_path: Path) -> None:
        instance = tmp_path / "alpha"
        instance.mkdir()
        self._populate(instance, {"MEMSWAP_LIMIT": "4g"})
        result = self._run_compose_config(instance)
        assert result.returncode == 0, (
            f"docker compose config failed with explicit memswap_limit.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # 4g == 4294967296 bytes — compose normalises the size to bytes.
        assert "4294967296" in result.stdout

    def test_explicit_mem_reservation_is_respected(self, tmp_path: Path) -> None:
        instance = tmp_path / "alpha"
        instance.mkdir()
        self._populate(instance, {"MEM_RESERVATION": "2g", "MEMSWAP_LIMIT": "4g"})
        result = self._run_compose_config(instance)
        assert result.returncode == 0, (
            f"docker compose config failed with explicit reservation.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestBinderMode:
    """The `binder` runtime-selection knob (auto | host | vm)."""

    def test_default_is_auto(self) -> None:
        assert InstanceConfig().binder == "auto"

    def test_accepts_host_and_vm(self) -> None:
        assert InstanceConfig(binder="host").binder == "host"
        assert InstanceConfig(binder="vm").binder == "vm"

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            InstanceConfig(binder="qemu")  # type: ignore[arg-type]  # deliberately invalid literal

    def test_round_trips_through_yaml(self, tmp_path: Path) -> None:
        from beetroot.config import write_yaml

        yaml_path = tmp_path / "beetroot.yaml"
        write_yaml(yaml_path, InstanceConfig(binder="vm"))
        assert load_yaml(yaml_path).binder == "vm"


class TestVmConfig:
    """The optional `vm:` micro-VM tunables block (consulted when binder: vm)."""

    def test_defaults(self) -> None:
        vm = InstanceConfig().vm
        assert vm.kernel is None
        assert vm.rootfs is None
        assert vm.accel == "auto"
        assert vm.smp == "auto"
        assert vm.memory_mib == 8192

    def test_smp_auto_is_accepted(self) -> None:
        assert InstanceConfig(vm={"smp": "auto"}).vm.smp == "auto"  # type: ignore[arg-type]

    def test_rejects_negative_smp(self) -> None:
        with pytest.raises(ValidationError):
            InstanceConfig(vm={"smp": -1})  # type: ignore[arg-type]

    def test_rejects_non_auto_smp_string(self) -> None:
        with pytest.raises(ValidationError):
            InstanceConfig(vm={"smp": "all"})  # type: ignore[arg-type]

    def test_explicit_values(self) -> None:
        cfg = InstanceConfig(
            vm={  # type: ignore[arg-type]
                "kernel": "/k/bz",
                "rootfs": "/r/disk.img",
                "accel": "tcg",
                "smp": 2,
                "memory_mib": 2048,
            }
        )
        assert cfg.vm.kernel == "/k/bz"
        assert cfg.vm.rootfs == "/r/disk.img"
        assert cfg.vm.accel == "tcg"
        assert cfg.vm.smp == 2

    def test_rejects_bad_accel(self) -> None:
        with pytest.raises(ValidationError):
            InstanceConfig(vm={"accel": "hax"})  # type: ignore[arg-type]

    def test_rejects_zero_smp(self) -> None:
        with pytest.raises(ValidationError):
            InstanceConfig(vm={"smp": 0})  # type: ignore[arg-type]

    def test_rejects_tiny_memory(self) -> None:
        with pytest.raises(ValidationError):
            InstanceConfig(vm={"memory_mib": 64})  # type: ignore[arg-type]

    def test_round_trips_through_yaml(self, tmp_path: Path) -> None:
        from beetroot.config import write_yaml

        yaml_path = tmp_path / "beetroot.yaml"
        write_yaml(
            yaml_path,
            InstanceConfig(binder="vm", vm={"kernel": "/k", "accel": "kvm", "smp": 8}),  # type: ignore[arg-type]
        )
        loaded = load_yaml(yaml_path)
        assert loaded.vm.kernel == "/k"
        assert loaded.vm.accel == "kvm"
        assert loaded.vm.smp == 8

    def test_empty_yaml_vm_block_uses_defaults(self) -> None:
        # binder: vm with no vm: section is valid (env defaults apply at runtime).
        cfg = InstanceConfig.model_validate({"api_version": 8, "binder": "vm"})
        assert cfg.vm.accel == "auto"
