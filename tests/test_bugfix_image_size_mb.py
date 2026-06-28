"""Regression tests for defensive ``IMAGE_SIZE_MB`` parsing in builder.from_env.

A set-but-empty ``IMAGE_SIZE_MB`` (the common ``export IMAGE_SIZE_MB=`` case)
used to reach ``int("")`` and crash with a raw ``ValueError`` that escaped the
``beetroot build --vm-kernel`` handler as a traceback. The fix falls back to the
default for absent/empty values and raises a friendly ``BootstrapError`` (which
the CLI turns into ``error: ...`` + exit 1) for genuinely malformed values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import builder
from beetroot.builder import BootstrapError

_DEFAULT_IMAGE_SIZE_MB = 8192


def _from_env() -> builder._RootfsConfig:
    return builder._RootfsConfig.from_env(out_image=Path("/o.img"), vm_dir=Path("/vm"))


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
