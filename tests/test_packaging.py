"""Tests for the installed package's distribution metadata.

These guard the bits of `pyproject.toml` that aren't exercised by importing the
package — most importantly the `beetroot = "beetroot.cli:main"` console script,
which is what `uv tool install` wires up on the user's PATH. A regression here
would silently break the documented `beetroot <verb>` workflow.
"""
from __future__ import annotations

from importlib.metadata import entry_points, metadata


def test_beetroot_entry_point_registered() -> None:
    eps = entry_points(group="console_scripts")
    names = {ep.name for ep in eps}
    assert "beetroot" in names, (
        "console_scripts entry point 'beetroot' is missing — "
        "after `uv tool install`, users would not get a `beetroot` command on PATH"
    )
    beetroot_ep = next(ep for ep in eps if ep.name == "beetroot")
    assert beetroot_ep.value == "beetroot.cli:main"


def test_python_version_classifiers_match_requires_python() -> None:
    # CR #2 finding H1: ``classifiers`` listed 3.10/3.11/3.12/3.13 but
    # ``requires-python = ">=3.13"`` — the 3.10–3.12 classifiers were
    # misleading for PyPI search and let a Python 3.10 user think the
    # wheel would install. Keep the classifiers in lockstep with the
    # actual minimum.
    classifiers = metadata("beetroot").get_all("Classifier") or []
    py_classifiers = [
        c for c in classifiers
        if c.startswith("Programming Language :: Python :: ")
    ]
    # The generic ``:: Python :: 3`` marker stays; the only specific
    # minor version listed must be 3.13.
    assert "Programming Language :: Python :: 3" in py_classifiers
    assert "Programming Language :: Python :: 3.13" in py_classifiers
    for stale in ("3.10", "3.11", "3.12"):
        assert (
            f"Programming Language :: Python :: {stale}" not in py_classifiers
        ), f"stale Python {stale} classifier — pyproject.toml requires-python pins >=3.13"
