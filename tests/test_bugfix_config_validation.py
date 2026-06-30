"""Bugfix regression tests for the config-denylist sweep.

Covers the config.py / load_yaml fixes shipped in this sweep:

* #194 — ``frida.sha256`` is validated as 64-char hex at config-load time.
* #195 — a well-known ``ports:`` mapping with a non-canonical guest port is
  rejected, and ``frida_control`` is required alongside ``frida``.
* #202 — the legacy ports-mapping migration note is deduped per resolved path
  so a fleet scan that re-loads a YAML only notes once.
* #215 — Docker's documented ``memswap_limit: -1`` (unlimited swap) is accepted
  while ``-1`` is still rejected for the other size fields.
* #220 — a pinned ``gapps_vendor`` that overrides the ``minimal``/``full``
  intent emits a one-line note.
* #242 — the dead ``_MIGRATION_REQUIRED_VERSIONS`` constant stays removed.
* #200 — the ``binder: vm`` inert-config advisory flags a non-empty
  ``modules:`` list the Magisk-less guest can never flash.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from beetroot import config
from beetroot.config import (
    Android,
    Frida,
    InstanceConfig,
    Module,
    PortMapping,
    Resources,
    inert_fields,
    load_yaml,
    render_env,
)


@pytest.fixture(autouse=True)
def _reset_ports_migration_dedup() -> Iterator[None]:
    """Clear the per-path ports-migration dedup set around each test.

    Mirrors conftest's ``_reset_api_version_warning_dedup`` for the companion
    ``_PORTS_MIGRATION_WARNED`` set so an order-shuffled run can't carry a
    populated path between tests (issue #202).
    """
    config._PORTS_MIGRATION_WARNED.clear()
    yield
    config._PORTS_MIGRATION_WARNED.clear()


class TestFridaSha256HexValidation:
    """#194: ``frida.sha256`` must be a 64-character hex digest."""

    @pytest.mark.parametrize(
        "bad",
        ["abc", "g" * 64, "not a hash", "", "a" * 63, "a" * 65],
    )
    def test_non_hex_or_wrong_length_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="64-character hex SHA-256"):
            Frida(version="16.4.10", sha256=bad)

    def test_lowercase_64_hex_accepted(self) -> None:
        digest = "0123456789abcdef" * 4
        assert Frida(version="16.4.10", sha256=digest).sha256 == digest

    def test_mixed_case_64_hex_accepted(self) -> None:
        digest = "0123456789ABCDEFabcdef0123456789ABCDEFab0123456789abcdef01234567"
        assert len(digest) == 64
        assert Frida(version="16.4.10", sha256=digest).sha256 == digest

    def test_none_passes_through(self) -> None:
        assert Frida(version="16.4.10").sha256 is None


class TestMemswapLimitUnlimited:
    """#215: ``memswap_limit: -1`` (unlimited swap) is accepted."""

    def test_minus_one_accepted_for_memswap(self) -> None:
        assert Resources(memswap_limit="-1").memswap_limit == "-1"

    def test_minus_one_rendered_into_env(self) -> None:
        cfg = InstanceConfig(resources=Resources(memswap_limit="-1"))
        assert "MEMSWAP_LIMIT=-1\n" in render_env("alpha", cfg)

    def test_minus_one_rejected_for_mem_reservation(self) -> None:
        # The -1 sentinel is memswap-only; mem_reservation has no such
        # documented sentinel and must still reject it as a malformed size.
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(mem_reservation="-1")

    def test_minus_one_rejected_for_mem(self) -> None:
        with pytest.raises(ValidationError, match="Docker size format"):
            Resources(mem="-1")


class TestGappsVendorOverrideNote:
    """#220: a pinned vendor overriding the intent emits a note."""

    @pytest.mark.parametrize("intent", ["minimal", "full"])
    def test_note_fires_when_vendor_overrides_intent(
        self, intent: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        Android(gapps=intent, gapps_vendor="opengapps")  # type: ignore[arg-type]
        err = capsys.readouterr().err
        assert "overrides the android.gapps" in err

    def test_no_note_when_vendor_unset(self, capsys: pytest.CaptureFixture[str]) -> None:
        Android(gapps="minimal")
        assert "overrides the android.gapps" not in capsys.readouterr().err

    def test_no_note_for_gapps_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        # gapps: none + a vendor is the contradiction rejected outright, so it
        # never reaches the override note; gapps: none with no vendor is clean.
        Android(gapps="none")
        assert "overrides the android.gapps" not in capsys.readouterr().err


class TestWellKnownGuestPortValidation:
    """#195: well-known mappings must use their canonical guest port."""

    def test_non_canonical_adb_guest_rejected(self) -> None:
        with pytest.raises(ValidationError, match="canonical guest port is 5555"):
            InstanceConfig(
                ports=[
                    PortMapping(service="adb", guest=9999),
                    PortMapping(service="frida", guest=27042),
                    PortMapping(service="frida_control", guest=27043),
                ]
            )

    def test_canonical_adb_guest_accepted(self) -> None:
        cfg = InstanceConfig(ports=[PortMapping(service="adb", guest=5555)])
        assert any(m.service == "adb" and m.guest == 5555 for m in cfg.ports)

    def test_arbitrary_service_guest_unconstrained(self) -> None:
        # A non-well-known mapping names its own guest port — no canonical
        # constraint applies.
        cfg = InstanceConfig(
            ports=[
                PortMapping(service="adb", guest=5555),
                PortMapping(service="telemetry", guest=9999),
            ]
        )
        assert any(m.service == "telemetry" and m.guest == 9999 for m in cfg.ports)

    def test_frida_control_required_with_frida_block(self) -> None:
        with pytest.raises(ValidationError, match="service: frida_control"):
            InstanceConfig(
                frida=Frida(version="16.4.10"),
                ports=[
                    PortMapping(service="adb", guest=5555),
                    PortMapping(service="frida", guest=27042),
                ],
            )

    def test_default_config_validates_clean(self) -> None:
        # The default seeds all three well-known services at canonical ports.
        InstanceConfig(frida=Frida(version="16.4.10"))


class TestLegacyPortsMigrationNoteDedup:
    """#202: the migration note fires once per resolved path."""

    def test_note_fires_once_across_two_loads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": 7, "ports": {"adb": 9000}}))
        load_yaml(p)
        load_yaml(p)
        note_lines = [
            line
            for line in capsys.readouterr().err.splitlines()
            if "migrated legacy ports mapping" in line
        ]
        assert len(note_lines) == 1, note_lines

    def test_unknown_key_does_not_emit_migration_note(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A non-well-known key raises the migration error instead of the note.
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.safe_dump({"api_version": 7, "ports": {"telemetry": 9000}}))
        with pytest.raises(ValidationError, match="non-well-known"):
            load_yaml(p)
        assert "migrated legacy ports mapping" not in capsys.readouterr().err


class TestDeadMigrationConstantRemoved:
    """#242: the never-read constant must not silently return."""

    def test_constant_is_gone(self) -> None:
        assert not hasattr(config, "_MIGRATION_REQUIRED_VERSIONS")


class TestVmInertModules:
    """#200: a non-empty modules list is inert under binder: vm."""

    def test_modules_flagged_inert_on_vm(self) -> None:
        cfg = InstanceConfig(
            binder="vm",
            modules=[Module(url="https://example.com/mod.zip")],
        )
        entries = inert_fields(cfg)
        assert any(e.startswith("modules") for e in entries), entries

    def test_no_modules_no_entry_on_vm(self) -> None:
        cfg = InstanceConfig(binder="vm")
        assert not any(e.startswith("modules") for e in inert_fields(cfg))

    def test_modules_honoured_on_redroid(self) -> None:
        # binder != vm honours modules → no inert entry at all.
        cfg = InstanceConfig(modules=[Module(url="https://example.com/mod.zip")])
        assert inert_fields(cfg) == []
