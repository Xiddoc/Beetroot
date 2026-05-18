"""Every ``${VAR}`` in the bundled compose template is covered by render_env.

CLAUDE.md says: "If you add a new substitution, update both" — meaning
every ``${VAR}`` token in ``src/beetroot/templates/compose.yaml`` must
have a matching line in :func:`config.render_env`. The bundled compose
file already references ``BEETROOT_MAGISK_DB``, ``BEETROOT_MODULES_DIR``
and ``BEETROOT_FRIDA_BIN`` for v0.4 stealth-posture work; they should
still appear in ``render_env``'s output with safe empty defaults.

This test parses the template for ``${VAR}`` tokens and asserts each
appears in ``render_env``'s output for a representative
``InstanceConfig``. The build-only ``BEETROOT_BUILD_CONTEXT`` token
is allowlisted because it's set by ``builder.py``, not ``render_env``.
"""
from __future__ import annotations

import re

from beetroot import config, paths, ports

# ``BEETROOT_BUILD_CONTEXT`` is consumed by ``builder.py`` via the
# DefaultRunner env arg; the compose template references it via the
# ``${BEETROOT_BUILD_CONTEXT:-.}`` form but render_env doesn't write
# it.
_BUILD_ONLY_VARS = frozenset({"BEETROOT_BUILD_CONTEXT"})

# Tokens that appear as both ``${X}`` and ``${X:-default}`` — the
# regex picks them up once.
_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}")


def _template_vars() -> set[str]:
    text = paths.bundled_compose_file().read_text()
    return set(_VAR_PATTERN.findall(text))


def _rendered_env_keys() -> set[str]:
    cfg = config.InstanceConfig(
        resources=config.Resources(
            mem_reservation="2g", memswap_limit="4g",
        ),
        frida=config.Frida(version="16.4.10"),
    )
    rendered = config.render_env(
        "alpha", cfg, ports.resolve_ports(0, cfg.ports)
    )
    return {
        line.partition("=")[0].strip()
        for line in rendered.splitlines()
        if "=" in line
    }


def test_every_template_var_has_a_render_env_line() -> None:
    template = _template_vars()
    rendered = _rendered_env_keys()
    missing = template - rendered - _BUILD_ONLY_VARS
    assert not missing, (
        f"compose.yaml references ${{VAR}} tokens that render_env() does "
        f"not emit: {sorted(missing)}. Either add them to render_env or "
        f"allowlist them in _BUILD_ONLY_VARS."
    )


def test_render_env_emits_stealth_overrides() -> None:
    # The three v0.4 stealth-posture overrides MUST be present (even
    # if empty) so a future change to make them required by the
    # template doesn't break compose with a missing-var error.
    rendered = _rendered_env_keys()
    for required in ("BEETROOT_MAGISK_DB", "BEETROOT_MODULES_DIR",
                     "BEETROOT_FRIDA_BIN"):
        assert required in rendered, (
            f"render_env() does not emit {required}; bundled compose "
            f"template references it via ${{{required}:-}}."
        )


def test_no_unused_render_env_vars_drift_silently() -> None:
    # The reverse direction: any key render_env emits should also
    # appear somewhere in the compose template. A drift here points
    # at dead lines in render_env that should be culled. The
    # allowlist captures vars consumed elsewhere (none today, but
    # future-proof the test).
    template = _template_vars()
    rendered = _rendered_env_keys()
    unused = rendered - template
    assert not unused, (
        f"render_env() emits {sorted(unused)} but compose.yaml doesn't "
        f"reference them. Either remove from render_env or document a "
        f"non-compose consumer."
    )
