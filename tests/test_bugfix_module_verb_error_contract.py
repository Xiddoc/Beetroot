"""The ``module`` verb maps a bare ``ValueError`` to the friendly error contract.

Regression guard for the bug where ``beetroot module <name> <source>
--sha256 <wrong-digest>`` dumped a raw traceback instead of the uniform
``error: ...`` + exit 1 line.

``Instance.add_module`` stages the module zip *before* mutating
``beetroot.yaml`` (so a bad add never half-commits). For a redroid
instance that staging walks ``modules_download.stage_for_instance`` →
``_resolve`` → ``verify_sha256``, which on a digest mismatch raises a
*bare* ``ValueError`` — NOT a ``pydantic.ValidationError``. ``cli.main``'s
global except chain catches ``ValidationError`` (a ``ValueError``
subclass) but not a plain ``ValueError``, so without a verb-scoped catch
the mismatch escaped as a Rich-rendered Python traceback.

The verb now wraps ``add_module`` in a ``try/except ValueError`` that
routes through ``_error``. This test drives the full user-input →
final-artifact path: a real on-disk module zip whose true digest differs
from the ``--sha256`` value the user passes, invoked through
``cli.main()`` (the only entry point that exercises the except chain —
``CliRunner.invoke`` short-circuits it).
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from beetroot import cli, console, paths, registry

_WRONG_DIGEST = "0" * 64


def _run_main_with_argv(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """Drive cli.main() under a faked argv. Returns (exit_code, stderr)."""
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    console.set_consoles(stderr=Console(file=buf, force_terminal=False))
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0), buf.getvalue()
    return 0, buf.getvalue()


def test_module_sha256_mismatch_is_friendly_not_traceback(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A redroid instance + a real module zip on disk whose true sha256
    # cannot match the all-zeros digest we pass on the command line.
    assert CliRunner().invoke(cli.app, ["create", "alpha"]).exit_code == 0
    root = registry.instance_path("alpha")
    module_zip = root / "mod.zip"
    with zipfile.ZipFile(module_zip, "w") as zf:
        zf.writestr("module.prop", "id=demo\n")

    code, err = _run_main_with_argv(
        ["beetroot", "module", "alpha", str(module_zip), "--sha256", _WRONG_DIGEST],
        monkeypatch,
    )

    assert code == 1
    assert err.startswith("error:")
    assert "sha256 mismatch" in err
    assert "Traceback" not in err
    # The bad add must not have mutated the source-of-truth YAML.
    assert "mod.zip" not in paths.instance_yaml(root).read_text(encoding="utf-8")
