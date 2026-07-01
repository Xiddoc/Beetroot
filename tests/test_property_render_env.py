"""
Property-based invariants for ``config.render_env``.

For arbitrary ``InstanceConfig`` shapes, asserts that every line of
the rendered ``.env`` is:

1. Of the form ``KEY=VALUE`` where ``KEY`` matches the shell-safe
   regex ``^[A-Z_][A-Z0-9_]*$``.
2. Free of un-quoted shell-injection vectors (single-quote, double-
   quote, backtick, ``$``-substitution).

The compose template's ``--env-file`` reader does the same parse;
this test guarantees we never silently emit a line that compose
would refuse or — worse — silently mis-parse.

Hypothesis is derandomized so CI failures reproduce.
"""

from __future__ import annotations

import re
from typing import Literal

import hypothesis.strategies as st
from hypothesis import given, settings

from beetroot import config

_GappsLit = Literal["none", "minimal", "full"]
_RenderingLit = Literal["gpu", "software", "auto"]

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Characters that would force shell-evaluation if interpreted as a
# raw shell line. compose's --env-file documentation says "don't put
# special characters in values"; we conservatively forbid the worst
# of them outright.
_INJECTION_CHARS = ("'", '"', "`", "$")


@st.composite
def instance_configs(draw: st.DrawFn) -> config.InstanceConfig:
    """Generate a valid InstanceConfig spanning the most expressive fields."""
    android_version = draw(st.sampled_from([11, 12, 13, 14]))
    gapps: _GappsLit = draw(
        st.sampled_from(["none", "minimal", "full"]),
    )
    mem = draw(st.sampled_from(["1g", "2g", "3g", "4g", "8g"]))
    cpus = draw(st.floats(min_value=0.5, max_value=8.0, allow_nan=False, allow_infinity=False))
    width = draw(st.integers(min_value=240, max_value=4096))
    height = draw(st.integers(min_value=240, max_value=4096))
    fps = draw(st.integers(min_value=1, max_value=120))
    rendering: _RenderingLit = draw(
        st.sampled_from(["gpu", "software", "auto"]),
    )
    pids_limit = draw(st.integers(min_value=64, max_value=65536))
    # #267: Resources now rejects an inverted soft-floor / swap-cap, so only
    # draw a mem_reservation at-or-below and a memswap_limit at-or-above ``mem``.
    mem_bytes = config._docker_size_to_bytes(mem)
    mem_reservation = draw(
        st.one_of(
            st.none(),
            st.sampled_from(["512m", "1g", "2g"]).filter(
                lambda v: config._docker_size_to_bytes(v) <= mem_bytes
            ),
        )
    )
    memswap_limit = draw(
        st.one_of(
            st.none(),
            st.sampled_from(["2g", "4g", "8g", "-1"]).filter(
                lambda v: v == "-1" or config._docker_size_to_bytes(v) >= mem_bytes
            ),
        )
    )
    return config.InstanceConfig(
        api_version=config.SUPPORTED_API_VERSION,
        android=config.Android(version=android_version, gapps=gapps),
        display=config.Display(width=width, height=height, fps=fps, rendering=rendering),
        resources=config.Resources(
            mem=mem,
            cpus=cpus,
            shared_mem=draw(st.sampled_from(["128m", "256m", "512m"])),
            mem_reservation=mem_reservation,
            memswap_limit=memswap_limit,
            pids_limit=pids_limit,
        ),
    )


@given(cfg=instance_configs())
@settings(deadline=None, derandomize=True, max_examples=200)
def test_render_env_lines_are_shell_safe_key_value_pairs(
    cfg: config.InstanceConfig,
) -> None:
    """Every rendered .env line parses as a shell-safe KEY=VALUE pair."""
    out = config.render_env("alpha", cfg)
    # The trailing newline is intentional; split on "\n" and drop the
    # empty tail.
    raw_lines = out.splitlines()
    assert raw_lines, "render_env returned no lines"
    for line in raw_lines:
        # Empty lines are not emitted by render_env; assert that
        # invariant first so a future regression surfaces here.
        assert line, "unexpected blank line in render_env output"
        key, _, value = line.partition("=")
        assert _KEY_RE.match(key), f"invalid KEY shape: {line!r}"
        # An empty value is allowed (e.g. the BEETROOT_MAGISK_DB
        # default-emit-empty line) but must still be free of
        # shell-injection chars.
        for bad in _INJECTION_CHARS:
            assert bad not in value, f"shell-injection character {bad!r} in value of {line!r}"


@given(cfg=instance_configs())
@settings(deadline=None, derandomize=True, max_examples=50)
def test_render_env_emits_required_keys(cfg: config.InstanceConfig) -> None:
    """The compose template's required substitutions are always present."""
    out = config.render_env("alpha", cfg)
    keys = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    # Ports moved to compose.override.yaml in v8 (issue #108), so ADB_PORT /
    # FRIDA_PORT are no longer emitted here.
    required = {
        "INSTANCE_NAME",
        "BASE_IMAGE",
        "MEM_LIMIT",
        "CPUS",
        "SHM_SIZE",
        "PIDS_LIMIT",
        "DISPLAY_WIDTH",
        "DISPLAY_HEIGHT",
        "DISPLAY_FPS",
        "DISPLAY_GPU",
    }
    missing = required - keys
    assert not missing, f"render_env missing required keys: {missing}"


def test_render_env_denylist_slash_entry_is_shell_safe() -> None:
    """The ``package/process`` denylist entry survives as a shell-safe value.

    issue #170: the default denylist now carries a ``package/process`` entry
    whose ``/`` rides through the CSV join into ``BEETROOT_DENYLIST_PACKAGES``.
    ``/`` is not a shell-injection vector, so the invariant above holds; this
    focused case pins that the slash-bearing entry is actually emitted (not
    dropped) and stays free of the forbidden characters.
    """
    cfg = config.InstanceConfig()
    out = config.render_env("alpha", cfg)
    denylist_line = next(
        line for line in out.splitlines() if line.startswith("BEETROOT_DENYLIST_PACKAGES=")
    )
    value = denylist_line.partition("=")[2]
    assert "com.google.android.gms/com.google.android.gms.unstable" in value
    for bad in _INJECTION_CHARS:
        assert bad not in value
