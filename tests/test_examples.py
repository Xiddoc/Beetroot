"""Validate the shipped ``examples/`` configs against the live config schema.

The examples are documentation, but a stale or malformed one is worse than no
example — researchers copy them verbatim over their ``beetroot.yaml``. These
tests parse every instance-config example through :class:`config.InstanceConfig`
so a schema change (or a typo) can't silently rot a shipped example, and pin the
LSPosed/Vector recipe's integrity guarantees.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from beetroot import config

EXAMPLES = Path(__file__).parent.parent / "examples"

# ``adb-device.yaml`` documents `beetroot adopt` output — a registry-meta shape
# (name / backend / index / created_at), not an InstanceConfig — so it's parsed
# differently and excluded from the InstanceConfig sweep below.
_INSTANCE_CONFIG_EXAMPLES = sorted(
    p for p in EXAMPLES.glob("*.yaml") if p.name != "adb-device.yaml"
)


@pytest.mark.parametrize("path", _INSTANCE_CONFIG_EXAMPLES, ids=lambda p: p.name)
def test_example_parses_as_instance_config(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    config.InstanceConfig.model_validate(data)


def test_lsposed_example_pins_vector_with_sha256() -> None:
    data = yaml.safe_load((EXAMPLES / "lsposed.yaml").read_text())
    cfg = config.InstanceConfig.model_validate(data)
    assert cfg.modules, "lsposed.yaml must declare the framework module"
    # The framework module is the Vector (LSPosed) Zygisk zip…
    assert any(m.url and "Vector" in m.url for m in cfg.modules), [m.url for m in cfg.modules]
    # …and every URL-sourced module pins a sha256 — a Zygisk framework runs with
    # full root, so an unverified download is not acceptable in a shipped recipe.
    assert all(m.sha256 for m in cfg.modules if m.url)
