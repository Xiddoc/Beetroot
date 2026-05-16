"""Tests for the installed package's distribution metadata.

These guard the bits of `pyproject.toml` that aren't exercised by importing the
package — most importantly the `beetroot = "beetroot.cli:main"` console script,
which is what `uv tool install` wires up on the user's PATH. A regression here
would silently break the documented `beetroot <verb>` workflow.
"""
from __future__ import annotations

from importlib.metadata import entry_points


def test_beetroot_entry_point_registered() -> None:
    eps = entry_points(group="console_scripts")
    names = {ep.name for ep in eps}
    assert "beetroot" in names, (
        "console_scripts entry point 'beetroot' is missing — "
        "after `uv tool install`, users would not get a `beetroot` command on PATH"
    )
    beetroot_ep = next(ep for ep in eps if ep.name == "beetroot")
    assert beetroot_ep.value == "beetroot.cli:main"
