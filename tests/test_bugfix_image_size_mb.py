"""Regression tests for defensive ``IMAGE_SIZE_MB`` parsing in builder.from_env.

A set-but-empty ``IMAGE_SIZE_MB`` (the common ``export IMAGE_SIZE_MB=`` case)
used to reach ``int("")`` and crash with a raw ``ValueError`` that escaped the
``beetroot build --vm-kernel`` handler as a traceback. The fix falls back to the
default for absent/empty values and raises a friendly ``BootstrapError`` (which
the CLI turns into ``error: ...`` + exit 1) for genuinely malformed values.

Issue #267 extends this: a malformed ``IMAGE_SIZE_MB`` must not abort the whole
``beetroot build --vm-kernel --check`` preflight — it is parsed defensively and
appended as a :class:`~beetroot.builder.PreflightProblem` so it is reported
alongside every other missing prerequisite in one pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import builder, capabilities
from beetroot.builder import BootstrapError

_DEFAULT_IMAGE_SIZE_MB = 8192


def _from_env() -> builder._RootfsConfig:
    return builder._RootfsConfig.from_env(out_image=Path("/o.img"), vm_dir=Path("/vm"))


def _stub_bake_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the side-effecting/slow bake-preflight host probes to a ready host.

    Leaves ``IMAGE_SIZE_MB`` parsing (the code under test) alone; the static-bin
    checks may still add their own problems, which the IMAGE_SIZE_MB-focused
    assertions tolerate by filtering on ``requirement``.
    """
    monkeypatch.setattr("beetroot.builder.shutil.which", lambda _n: "/usr/bin/found")
    monkeypatch.setattr(capabilities, "docker_daemon_responsive", lambda: True)
    monkeypatch.setattr("beetroot.builder.os.geteuid", lambda: 0)
    monkeypatch.setattr("beetroot.builder.platform.machine", lambda: "x86_64")


class TestImageSizeMbParsing:
    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IMAGE_SIZE_MB", raising=False)
        assert _from_env().image_size_mb == _DEFAULT_IMAGE_SIZE_MB

    def test_empty_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The key regression: int("") would have raised. Set-but-empty must fall
        # back to the default, never crash.
        monkeypatch.setenv("IMAGE_SIZE_MB", "")
        assert _from_env().image_size_mb == _DEFAULT_IMAGE_SIZE_MB

    def test_non_numeric_raises_bootstrap_error_naming_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IMAGE_SIZE_MB", "not-a-number")
        with pytest.raises(BootstrapError, match="not-a-number") as exc:
            _from_env()
        # A friendly BootstrapError, not a bare ValueError leaking out.
        assert not isinstance(exc.value, ValueError)
        assert "IMAGE_SIZE_MB" in str(exc.value)

    def test_non_positive_raises_bootstrap_error_naming_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IMAGE_SIZE_MB", "0")
        with pytest.raises(BootstrapError, match="'0'"):
            _from_env()

    def test_valid_value_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IMAGE_SIZE_MB", "4096")
        assert _from_env().image_size_mb == 4096


class TestMalformedImageSizeMbInPreflight:
    """issue #267: a bad IMAGE_SIZE_MB is reported by --check, not raised."""

    @pytest.mark.parametrize("bad", ["not-a-number", "0", "-5"])
    def test_malformed_value_becomes_preflight_problem(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        _stub_bake_probes(monkeypatch)
        monkeypatch.setenv("IMAGE_SIZE_MB", bad)
        # No BootstrapError escapes: the bad value surfaces as a problem instead.
        problems = builder.vm_bake_preflight()
        size_problems = [p for p in problems if p.requirement == "IMAGE_SIZE_MB"]
        assert len(size_problems) == 1
        assert bad in size_problems[0].detail
        assert "positive integer" in size_problems[0].fix

    def test_malformed_value_does_not_starve_other_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A malformed size AND a genuinely missing bake tool must both be
        # reported in the single pass (the whole point of #267 vs. aborting).
        _stub_bake_probes(monkeypatch)
        monkeypatch.setenv("IMAGE_SIZE_MB", "huge")
        monkeypatch.setattr(
            "beetroot.builder.shutil.which", lambda n: None if n == "mke2fs" else "/usr/bin/found"
        )
        names = {p.requirement for p in builder.vm_bake_preflight()}
        assert {"IMAGE_SIZE_MB", "mke2fs"} <= names

    def test_env_var_restored_after_defensive_reparse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The defensive re-resolve pops IMAGE_SIZE_MB to avoid re-raising; it must
        # restore the original value so nothing downstream sees it vanish.
        _stub_bake_probes(monkeypatch)
        monkeypatch.setenv("IMAGE_SIZE_MB", "bogus")
        builder.vm_bake_preflight()
        import os

        assert os.environ["IMAGE_SIZE_MB"] == "bogus"

    def test_valid_value_yields_no_image_size_problem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_bake_probes(monkeypatch)
        monkeypatch.setenv("IMAGE_SIZE_MB", "4096")
        assert all(p.requirement != "IMAGE_SIZE_MB" for p in builder.vm_bake_preflight())
