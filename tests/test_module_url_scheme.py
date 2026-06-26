"""Module URL schemes are restricted to http(s).

CR #2 finding G1: ``modules_download._fetch_url`` used to call
``urllib.request.urlopen`` against any URL, so a beetroot.yaml with
``url: file:///etc/passwd`` would silently exfiltrate that host file
into the module cache. The fix is twofold:

1. The ``Module`` pydantic validator rejects non-http(s) ``url`` values
   at parse time, so a malformed YAML fails loud before any I/O.
2. ``_fetch_url`` re-checks the prefix at the call site as
   belt-and-suspenders for third-party scripts that hand-build a URL.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beetroot import modules_download
from beetroot.config import Module


class TestPydanticValidator:
    def test_file_scheme_url_rejected_at_model_construction(self) -> None:
        with pytest.raises(ValidationError, match="unsupported scheme"):
            Module(url="file:///etc/passwd")

    def test_ftp_scheme_url_rejected_at_model_construction(self) -> None:
        with pytest.raises(ValidationError, match="unsupported scheme"):
            Module(url="ftp://example.com/mod.zip")

    def test_bare_scheme_url_rejected_at_model_construction(self) -> None:
        with pytest.raises(ValidationError, match="unsupported scheme"):
            Module(url="bad-scheme://x")

    def test_http_scheme_accepted(self) -> None:
        # http:// is allowed (an upstream may serve over plain HTTP);
        # the sha256 field is how integrity is gated, not the scheme.
        m = Module(url="http://example.com/mod.zip")
        assert m.url == "http://example.com/mod.zip"

    def test_https_scheme_accepted(self) -> None:
        m = Module(url="https://example.com/mod.zip")
        assert m.url == "https://example.com/mod.zip"


class TestFetchUrlBeltAndSuspenders:
    def test_fetch_url_rejects_file_scheme_directly(self) -> None:
        # Even if a caller bypasses the pydantic validator (e.g. a
        # third-party script that calls _fetch_url with a hand-built
        # URL), the function-level guard still refuses.
        with pytest.raises(modules_download.ModuleFetchError, match="unsupported scheme"):
            modules_download._fetch_url("file:///etc/passwd")

    def test_fetch_url_rejects_arbitrary_scheme_directly(self) -> None:
        with pytest.raises(modules_download.ModuleFetchError, match="unsupported scheme"):
            modules_download._fetch_url("gopher://example.com/x")
