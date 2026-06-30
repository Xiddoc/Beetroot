"""Tests for the repo-root ``.dockerignore`` build-context allowlist (#207)."""

from __future__ import annotations

from beetroot import builder


def test_dockerignore_exists_at_build_context_root() -> None:
    dockerignore = builder._default_build_context() / ".dockerignore"
    assert dockerignore.is_file()


def test_dockerignore_excludes_all_then_reincludes_docker() -> None:
    dockerignore = builder._default_build_context() / ".dockerignore"
    lines = {
        line.strip()
        for line in dockerignore.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "*" in lines
    assert "!docker/" in lines
    assert "!docker/**" in lines
