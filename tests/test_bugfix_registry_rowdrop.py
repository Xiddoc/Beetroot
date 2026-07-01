"""Bugfix: a row-invalid registry entry must not be silently dropped (#252).

``_read``'s per-row loop used to wrap ``InstanceMeta.model_validate`` in
``except Exception: continue``, so a row that passed the envelope check
(valid JSON, ``version == 3``, a dict ``backend``) but failed *row-level*
validation was silently dropped. That freed its port index for reuse — the
next ``create`` handed the same index to a new instance — and deleted the
row on the next write.

The fix distinguishes two kinds of row-level failure:

* A **known-kind backend whose payload is rejected** (e.g. a ``redroid`` row
  carrying an ``adb``-only field) is preserved *opaquely* — the row loads,
  its index stays reserved, and it round-trips byte-for-byte, exactly like an
  unknown-kind row. The good sibling row loads alongside it.
* A row too broken to salvage (no usable integer ``index`` / an unparseable
  ``created_at``) is treated as envelope-level corruption and surfaced
  **loudly** via the ``.bak``-and-empty path — never silently continued.

Both branches are exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beetroot import paths, registry
from beetroot.registry import RedroidBackendConfig, UnresolvedBackendConfig


def _write_raw_registry(instances: dict[str, object]) -> Path:
    """Hand-write a v3-envelope registry JSON with the given ``instances`` map."""
    path = paths.user_registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 3, "instances": instances}))
    return path


class TestKnownKindInvalidBackendIsPreservedOpaquely:
    """A known-kind row with a rejected payload survives instead of vanishing."""

    def _mixed_doc(self) -> dict[str, object]:
        # ``alpha`` is a valid adb row (index 0). ``bravo`` is a ``redroid``
        # row (index 1) carrying an adb-only ``serial`` field, which
        # ``RedroidBackendConfig``'s ``extra="forbid"`` rejects — a
        # row-level validation failure on an otherwise-sound row.
        return {
            "alpha": {
                "backend": {"kind": "adb", "serial": "emulator-5554"},
                "index": 0,
                "created_at": "2026-05-19T12:00:00+00:00",
            },
            "bravo": {
                "backend": {
                    "kind": "redroid",
                    "absolute_path": "/var/lib/beetroot/bravo",
                    "serial": "should-not-be-here",
                },
                "index": 1,
                "created_at": "2026-05-19T13:00:00+00:00",
            },
        }

    def test_good_row_loads_and_bad_row_is_preserved(self, isolated_registry: Path) -> None:
        _write_raw_registry(self._mixed_doc())

        instances = registry.list_instances()

        # The good row loads normally ...
        assert set(instances) == {"alpha", "bravo"}
        assert isinstance(instances["alpha"].backend, registry.AdbBackendConfig)
        # ... and the row-invalid row is preserved opaquely, not dropped.
        assert isinstance(instances["bravo"].backend, UnresolvedBackendConfig)
        assert instances["bravo"].backend.kind == "redroid"
        assert instances["bravo"].index == 1

    def test_bad_rows_index_stays_reserved(self, isolated_registry: Path) -> None:
        _write_raw_registry(self._mixed_doc())

        # The dropped-row bug freed index 1 for reuse; the fix keeps it
        # reserved so neither used_indices nor lowest_free_index hand it out.
        assert registry.used_indices() == {0, 1}
        from beetroot import ports

        assert ports.lowest_free_index(registry.used_indices()) == 2

    def test_bad_row_survives_read_write_read_round_trip(self, isolated_registry: Path) -> None:
        path = _write_raw_registry(self._mixed_doc())

        # Read, then write the (unchanged) doc back, then read again: the
        # opaque row must round-trip byte-for-byte, not disappear on write.
        first = registry._read(path)
        registry._write(path, first)
        second = registry._read(path)

        assert set(second.instances) == {"alpha", "bravo"}
        bravo = second.instances["bravo"].backend
        assert isinstance(bravo, UnresolvedBackendConfig)
        # The rejected known-kind payload is preserved verbatim, extra
        # field and all.
        raw = json.loads(path.read_text())
        assert raw["instances"]["bravo"]["backend"] == {
            "kind": "redroid",
            "absolute_path": "/var/lib/beetroot/bravo",
            "serial": "should-not-be-here",
        }

    def test_new_create_does_not_reuse_the_reserved_index(
        self, isolated_registry: Path, tmp_path: Path
    ) -> None:
        # End-to-end: the port index of a row-invalid entry must not be
        # co-allocated to a freshly created instance.
        _write_raw_registry(self._mixed_doc())

        idx = registry.add_allocating(
            "charlie",
            backend=RedroidBackendConfig(absolute_path=str(tmp_path / "charlie")),
        )
        assert idx == 2


class TestUnsalvageableRowSurfacesLoudly:
    """A row with no usable index / bad created_at triggers backup-and-empty."""

    def test_bad_created_at_backs_up_the_file(self, isolated_registry: Path) -> None:
        # ``created_at`` is not a valid timestamp and the backend is a
        # known kind — the opaque re-wrap can't rescue an unparseable
        # meta field, so the whole file is surfaced loudly (backed up).
        path = _write_raw_registry(
            {
                "alpha": {
                    "backend": {"kind": "adb", "serial": "emulator-5554"},
                    "index": 0,
                    "created_at": "not-a-timestamp",
                },
            }
        )

        result = registry.list_instances()

        assert result == {}
        bak = path.with_suffix(path.suffix + ".bak")
        assert bak.exists()
        # The clear error is surfaced on stderr, not silently swallowed.
        assert not path.exists()

    def test_missing_index_backs_up_the_file(
        self, isolated_registry: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No ``index`` at all: there is no integer to keep reserved, so
        # the row is unsalvageable and the file is backed up loudly.
        path = _write_raw_registry(
            {
                "alpha": {
                    "backend": {"kind": "adb", "serial": "emulator-5554"},
                    "created_at": "2026-05-19T12:00:00+00:00",
                },
            }
        )

        result = registry.list_instances()

        assert result == {}
        assert path.with_suffix(path.suffix + ".bak").exists()
        err = capsys.readouterr().err
        assert "unsalvageable row" in err
        assert ".bak" in err

    def test_unknown_kind_with_bad_meta_also_surfaces_loudly(
        self, isolated_registry: Path
    ) -> None:
        # An *unknown*-kind backend already round-trips opaquely, but if
        # its meta is unusable (bad index type) the row is still
        # unsalvageable — the loud path fires here too.
        path = _write_raw_registry(
            {
                "alpha": {
                    "backend": {"kind": "spaceship", "warp": 9},
                    "index": "not-an-int",
                    "created_at": "2026-05-19T12:00:00+00:00",
                },
            }
        )

        assert registry.list_instances() == {}
        assert path.with_suffix(path.suffix + ".bak").exists()


class TestOpaqueBackendKindNormalization:
    """A non-string ``kind`` normalizes to an empty discriminator."""

    def test_non_string_kind_is_wrapped_with_empty_kind(self) -> None:
        # Drives ``_opaque_backend``'s non-str branch via the public
        # ``_parse_backend_config`` dispatch: a numeric ``kind`` is never
        # in the registry, so it falls through to the opaque wrapper.
        cfg = registry._parse_backend_config({"kind": 123, "blob": "x"})
        assert isinstance(cfg, UnresolvedBackendConfig)
        assert cfg.kind == ""
        # The raw dict is preserved verbatim for round-tripping.
        assert registry._dump_backend_config(cfg) == {"kind": 123, "blob": "x"}
