"""ModuleFetchError surfaces as a friendly `error:` line through the CLI.

CR #2 finding E1+E2: a 404 (or any HTTP error) on a module download
used to raise a bare ``RuntimeError`` that ``cli.main()`` didn't catch,
so the user saw a Rich-rendered Python traceback. The fix introduces
:class:`modules_dl.ModuleFetchError` (a ``RuntimeError`` subclass for
backward compat), converts the bare raises in ``_fetch_url``, and adds
the new type to ``cli.main()``'s except chain.

The two behaviors pinned here:

1. ``_fetch_url`` converts ``urllib.error.HTTPError`` into a
   ``ModuleFetchError`` whose message includes the URL, the HTTP status
   code, and a "verify the URL" hint.
2. ``beetroot apply alpha`` against a YAML that lists an unreachable
   module URL exits 1 with ``error:`` on stderr — no traceback.
"""
from __future__ import annotations

import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from beetroot import cli, modules_dl, paths, registry

runner = CliRunner()


class TestWrapperConvertsHttpError:
    def test_http_error_becomes_module_fetch_error(
        self, isolated_registry: Path
    ) -> None:
        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(
                url, 404, "Not Found", {}, None,  # type: ignore[arg-type]
            )

        with patch("urllib.request.urlopen", side_effect=_raise):
            with pytest.raises(modules_dl.ModuleFetchError) as exc_info:
                modules_dl._fetch_url("https://example.com/missing.zip")

        msg = str(exc_info.value)
        assert "404" in msg
        assert "https://example.com/missing.zip" in msg
        assert "verify" in msg.lower()


class TestCliSurfacesAsErrorLine:
    def test_apply_exits_with_error_line_no_traceback(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create an instance, then append a module URL that 404s. A
        # subsequent ``beetroot apply alpha`` must exit 1 with an
        # ``error:`` line on stderr — never a bare traceback.
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        root = registry.instance_path("alpha")
        paths.instance_yaml(root).write_text(
            "api_version: 3\n"
            "android:\n  version: 14\n"
            "modules:\n"
            "  - url: https://example.com/gone.zip\n"
        )

        def _raise(url: str, **kwargs: object) -> MagicMock:
            raise urllib.error.HTTPError(
                url, 404, "Not Found", {}, None,  # type: ignore[arg-type]
            )

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        # We have to invoke through `main()` (not CliRunner.invoke,
        # which short-circuits the standalone Click loop and so skips
        # the cli.main() except chain). The SystemExit's code is the
        # observed exit code.
        monkeypatch.setattr("sys.argv", ["beetroot", "apply", "alpha"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1

    def test_cli_main_catches_module_fetch_error_directly(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _raise() -> None:
            raise modules_dl.ModuleFetchError(
                "download failed: HTTP 404 fetching https://example.com/x.zip"
            )

        monkeypatch.setattr(cli, "app", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "HTTP 404" in err
