"""T4 round-trip + plumbing tests for ``RedroidBackendConfig.stealth_paths``.

Cases pinned (per the T4 spec):

1. ``snapshot()`` of an instance whose registry row carries a populated
   ``stealth_paths`` writes those entries verbatim into the manifest's
   ``path_layout`` field.
2. ``restore()`` of a manifest whose ``path_layout`` is populated writes
   the same blob into the new instance's
   ``RedroidBackendConfig.stealth_paths``.
3. v0.3/v0.4-shaped manifest (empty ``path_layout``) restores cleanly —
   the new instance's ``stealth_paths`` stays ``{}`` and the rendered
   ``.env`` falls back to the v0.4 ``modules_update`` defaults.
4. ``render_env`` is byte-pinned both ways: empty ``stealth_paths`` emits
   the default ``BEETROOT_*`` lines; populated entries override them
   while absent keys retain their defaults.
5. ``render_env`` ignores unknown keys (forward-compat for v0.5 schema).
6. ``registry.set_stealth_paths`` raises ``RegistryError`` for unknown
   names and for adb-backed rows — the slot is redroid-only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from beetroot import config, ports, registry, snapshot

_MIN_YAML = "api_version: 3\nandroid:\n  version: 14\n"
_SAMPLE_LAYOUT = {
    "frida_bin": "/data/adb/modules/x7q4z/bin/svc",
    "modules_dir": "/.x7q4z_flash",
    "magisk_db": "/data/adb/.x7q4z.db",
}


def _make_instance(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "beetroot.yaml").write_text(_MIN_YAML)
    (root / "data").mkdir()
    (root / "modules").mkdir()
    return root


# --------------------------------------------------------------------- #
# 1. Snapshot writes the source's stealth_paths into manifest.path_layout
# --------------------------------------------------------------------- #


class TestSnapshotWritesStealthPaths:
    def test_populated_stealth_paths_appear_in_manifest(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        registry.set_stealth_paths("alpha", _SAMPLE_LAYOUT)

        archive = snapshot.snapshot(src, tmp_path / "out")

        manifest = snapshot.read_manifest(archive)
        assert manifest.path_layout == _SAMPLE_LAYOUT

    def test_empty_stealth_paths_yields_empty_path_layout(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Belt-and-braces against a regression that would coerce a
        # missing-key blob to something other than ``{}``.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        # No set_stealth_paths call — slot stays at the model default.
        archive = snapshot.snapshot(src, tmp_path / "out")
        assert snapshot.read_manifest(archive).path_layout == {}

    def test_manifest_layout_is_independent_of_later_mutation(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # The manifest model is frozen (extra="forbid"), but the dict
        # *inside* it must also be insulated from later registry
        # mutation. snapshot.py takes ``dict(backend.stealth_paths)``
        # to guarantee that.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        registry.set_stealth_paths("alpha", _SAMPLE_LAYOUT)
        archive = snapshot.snapshot(src, tmp_path / "out")
        # Mutate the source row to a different layout.
        registry.set_stealth_paths("alpha", {"frida_bin": "/other/path"})
        # The archive's manifest must still carry the original
        # snapshot-time layout.
        assert snapshot.read_manifest(archive).path_layout == _SAMPLE_LAYOUT


# --------------------------------------------------------------------- #
# 2. Restore replays manifest.path_layout into the new row's stealth_paths
# --------------------------------------------------------------------- #


class TestRestoreReplaysIntoRegistry:
    def test_populated_layout_lands_in_new_row(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        registry.set_stealth_paths("alpha", _SAMPLE_LAYOUT)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        snapshot.restore(
            archive, dest_name="beta", dest_path=tmp_path / "beta",
        )

        beta = registry.get("beta")
        assert beta is not None
        assert isinstance(beta.backend, registry.RedroidBackendConfig)
        assert beta.backend.stealth_paths == _SAMPLE_LAYOUT

    def test_empty_layout_leaves_dest_slot_empty(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # v0.3 / v0.4-default-shape manifests have path_layout = {}.
        # The restored row must NOT inherit any stealth_paths from
        # nowhere — the slot stays at the model default ({}).
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        snapshot.restore(
            archive, dest_name="beta", dest_path=tmp_path / "beta",
        )

        beta = registry.get("beta")
        assert beta is not None
        assert isinstance(beta.backend, registry.RedroidBackendConfig)
        assert beta.backend.stealth_paths == {}


# --------------------------------------------------------------------- #
# 3. v0.3 → v0.4 forward-compat: empty manifest, modules_update default
# --------------------------------------------------------------------- #


class TestForwardCompatEmptyManifest:
    def test_restored_env_carries_modules_update_default(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # The restored instance's .env must carry the new v0.4
        # ``BEETROOT_MODULES_DIR=/data/adb/modules_update`` default
        # — not the v0.3 ``/flash_dir`` invention. ``up`` against
        # the new instance would otherwise still bind-mount to the
        # old container path.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        # snapshot.restore stages .env via Instance._stage_local —
        # so an empty stealth_paths slot must still render the
        # modules_update default.
        snapshot.restore(
            archive, dest_name="beta", dest_path=tmp_path / "beta",
        )

        env = (tmp_path / "beta" / ".env").read_text()
        assert "BEETROOT_MODULES_DIR=/data/adb/modules_update" in env
        assert "BEETROOT_FRIDA_BIN=/data/local/tmp/frida-server" in env
        assert "BEETROOT_MAGISK_DB=/data/adb/magisk.db" in env
        # And NOT the legacy invented path.
        assert "/flash_dir" not in env

    def test_v04_populated_layout_renders_into_env_on_restore(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # End-to-end behaviour test: a snapshot carrying a populated
        # path_layout must render those overrides into the restored
        # instance's .env — proving snapshot → restore → render_env
        # is wired all the way through.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        registry.set_stealth_paths("alpha", _SAMPLE_LAYOUT)
        archive = snapshot.snapshot(src, tmp_path / "out")
        registry.remove("alpha")

        snapshot.restore(
            archive, dest_name="beta", dest_path=tmp_path / "beta",
        )

        env = (tmp_path / "beta" / ".env").read_text()
        assert (
            f"BEETROOT_FRIDA_BIN={_SAMPLE_LAYOUT['frida_bin']}" in env
        )
        assert (
            f"BEETROOT_MODULES_DIR={_SAMPLE_LAYOUT['modules_dir']}" in env
        )
        assert (
            f"BEETROOT_MAGISK_DB={_SAMPLE_LAYOUT['magisk_db']}" in env
        )


# --------------------------------------------------------------------- #
# 4. render_env byte-pinned override semantics
# --------------------------------------------------------------------- #


def _env_dict(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from a rendered .env blob."""
    return {
        line.partition("=")[0]: line.partition("=")[2]
        for line in text.splitlines()
        if "=" in line
    }


class TestRenderEnvOverrides:
    def test_no_stealth_paths_emits_v04_defaults(self) -> None:
        cfg = config.InstanceConfig()
        rendered = config.render_env(
            "alpha", cfg, ports.resolve_ports(0, cfg.ports),
        )
        env = _env_dict(rendered)
        assert env["BEETROOT_MAGISK_DB"] == "/data/adb/magisk.db"
        assert env["BEETROOT_MODULES_DIR"] == "/data/adb/modules_update"
        assert env["BEETROOT_FRIDA_BIN"] == "/data/local/tmp/frida-server"

    def test_empty_dict_treated_as_no_stealth_paths(self) -> None:
        cfg = config.InstanceConfig()
        rendered = config.render_env(
            "alpha", cfg, ports.resolve_ports(0, cfg.ports),
            stealth_paths={},
        )
        env = _env_dict(rendered)
        assert env["BEETROOT_MODULES_DIR"] == "/data/adb/modules_update"

    def test_modules_dir_override_only(self) -> None:
        # Pin the spec's case-4 fixture: a single-key override changes
        # only that line; the other two ``BEETROOT_*`` paths keep
        # their defaults.
        cfg = config.InstanceConfig()
        rendered = config.render_env(
            "alpha", cfg, ports.resolve_ports(0, cfg.ports),
            stealth_paths={"modules_dir": "/custom"},
        )
        env = _env_dict(rendered)
        assert env["BEETROOT_MODULES_DIR"] == "/custom"
        # Other defaults stand.
        assert env["BEETROOT_MAGISK_DB"] == "/data/adb/magisk.db"
        assert env["BEETROOT_FRIDA_BIN"] == "/data/local/tmp/frida-server"

    def test_all_three_overrides(self) -> None:
        cfg = config.InstanceConfig()
        rendered = config.render_env(
            "alpha", cfg, ports.resolve_ports(0, cfg.ports),
            stealth_paths=_SAMPLE_LAYOUT,
        )
        env = _env_dict(rendered)
        assert env["BEETROOT_FRIDA_BIN"] == _SAMPLE_LAYOUT["frida_bin"]
        assert env["BEETROOT_MODULES_DIR"] == _SAMPLE_LAYOUT["modules_dir"]
        assert env["BEETROOT_MAGISK_DB"] == _SAMPLE_LAYOUT["magisk_db"]

    def test_unknown_keys_are_ignored_silently(self) -> None:
        # Forward-compat: a v0.5/v0.6 ``stealth_module_id`` key would
        # land here when a future snapshot is restored against an
        # older host. The render must not fault on the unknown key —
        # absent recognised keys still fall through to defaults.
        cfg = config.InstanceConfig()
        rendered = config.render_env(
            "alpha", cfg, ports.resolve_ports(0, cfg.ports),
            stealth_paths={"stealth_module_id": "x7q4z"},
        )
        env = _env_dict(rendered)
        assert env["BEETROOT_MODULES_DIR"] == "/data/adb/modules_update"
        # And the unknown key did NOT land as a BEETROOT_* line.
        assert "BEETROOT_STEALTH_MODULE_ID" not in env

    def test_render_env_lines_are_shell_safe(self) -> None:
        # Defensive: with a populated stealth_paths blob, every emitted
        # line must still be a single ``KEY=VALUE`` pair on one line.
        # The test pins this for the .env-parsed-by-compose contract.
        cfg = config.InstanceConfig()
        rendered = config.render_env(
            "alpha", cfg, ports.resolve_ports(0, cfg.ports),
            stealth_paths=_SAMPLE_LAYOUT,
        )
        for line in rendered.splitlines():
            assert "\n" not in line
            assert "=" in line


# --------------------------------------------------------------------- #
# 5. registry.set_stealth_paths error paths
# --------------------------------------------------------------------- #


class TestSetStealthPathsErrors:
    def test_unknown_name_raises(self, isolated_registry: Path) -> None:
        with pytest.raises(registry.RegistryError, match="unknown instance"):
            registry.set_stealth_paths("ghost", {"frida_bin": "/x"})

    def test_adb_backend_rejected(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # Hand-write an adb-shaped row and confirm set_stealth_paths
        # refuses — the slot lives on RedroidBackendConfig only.
        path = isolated_registry / "config" / "beetroot" / "instances.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 3,
            "instances": {
                "phone": {
                    "backend": {"kind": "adb", "serial": "emulator-5554"},
                    "index": 0,
                    "created_at": "2026-05-19T00:00:00+00:00",
                },
            },
        }))
        with pytest.raises(registry.RegistryError, match="redroid-only"):
            registry.set_stealth_paths("phone", {"frida_bin": "/x"})

    def test_set_then_get_round_trip(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        registry.set_stealth_paths("alpha", _SAMPLE_LAYOUT)
        meta = registry.get("alpha")
        assert meta is not None
        assert isinstance(meta.backend, registry.RedroidBackendConfig)
        assert meta.backend.stealth_paths == _SAMPLE_LAYOUT

    def test_caller_mutation_does_not_leak_into_registry(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # set_stealth_paths must dict() its input so a later mutation
        # of the caller's dict doesn't retroactively edit the row.
        src = _make_instance(tmp_path / "alpha")
        registry.add_allocating("alpha", src)
        layout = dict(_SAMPLE_LAYOUT)
        registry.set_stealth_paths("alpha", layout)
        layout["frida_bin"] = "/different/path"
        meta = registry.get("alpha")
        assert meta is not None
        assert isinstance(meta.backend, registry.RedroidBackendConfig)
        assert meta.backend.stealth_paths["frida_bin"] == (
            _SAMPLE_LAYOUT["frida_bin"]
        )
