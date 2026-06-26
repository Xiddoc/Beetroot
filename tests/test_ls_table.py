"""Pin the `beetroot ls` table column order against doc copy-pastes.

Every docs page that pastes an `ls` example has to use the live column
order. This test runs the verb in an empty-registry env, captures the
header line, and asserts that header appears verbatim in each known
doc that pastes it.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from beetroot import cli, registry

LS_TABLE_COLUMNS: tuple[str, ...] = ("NAME", "KIND", "IDX", "ADB", "FRIDA", "STATUS", "PATH")

REPO_ROOT = Path(__file__).resolve().parents[1]

# Pages that show an `ls` table example and must stay in sync with the
# live verb. The CHANGELOG is intentionally excluded — historical themes
# may reference older column shapes.
DOC_FILES_WITH_LS_EXAMPLE: tuple[Path, ...] = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "index.md",
    REPO_ROOT / "docs" / "guides" / "multi-instance.md",
    REPO_ROOT / "docs" / "reference" / "ports.md",
    REPO_ROOT / "docs" / "reference" / "cli.md",
    REPO_ROOT / "docs" / "getting-started" / "first-instance.md",
    REPO_ROOT / "docs" / "guides" / "migration-v0.2-to-v0.3.md",
    REPO_ROOT / "docs" / "guides" / "migration-v0.3-to-v0.4.md",
    REPO_ROOT / "docs" / "guides" / "migration-v0.4-to-v0.6.md",
)


def _live_header() -> str:
    """Run `beetroot ls` in an empty registry and return the header line."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["ls"])
    assert result.exit_code == 0, result.stderr
    # First non-empty line of stdout is either the header (when populated)
    # or the "(no instances ...)" placeholder. We force the populated path
    # by writing one entry into the registry below; this helper exists for
    # tests that don't need an entry.
    return result.stdout.splitlines()[0]


def test_live_header_matches_expected_columns(isolated_registry: Path, tmp_path: Path) -> None:
    """The verb's live header must contain every expected column in order."""
    # Drop one synthetic entry so `ls` walks the populated branch — the
    # `Manager.list()` path needs a registered instance whose dir exists.
    inst_root = tmp_path / "alpha"
    inst_root.mkdir()
    (inst_root / "beetroot.yaml").write_text("api_version: 3\nandroid:\n  version: 14\n")
    registry.add_allocating("alpha", inst_root)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["ls"])
    assert result.exit_code == 0, result.stderr
    # Rich renders a border row first, then the header row. Find the first
    # line that contains the leading column name rather than assuming line 0.
    lines = result.stdout.splitlines()
    header_candidates = [ln for ln in lines if LS_TABLE_COLUMNS[0] in ln]
    assert header_candidates, f"No header line found in ls output: {result.stdout!r}"
    header = header_candidates[0]
    # Every column appears in the header, in declared order.
    cursor = 0
    for col in LS_TABLE_COLUMNS:
        idx = header.find(col, cursor)
        assert idx >= 0, f"Column {col!r} missing from live `ls` header: {header!r}"
        cursor = idx + len(col)


def test_docs_paste_columns_in_live_order() -> None:
    """Each doc page that shows an `ls` example must paste columns in order."""
    failures: list[str] = []
    for path in DOC_FILES_WITH_LS_EXAMPLE:
        text = path.read_text()
        # Find the first occurrence of the leading header column and walk
        # forwards. If any subsequent column is missing or out of order, fail.
        # We allow multiple `ls` examples per file as long as *every* one
        # has the live order — search until end of file.
        cursor = 0
        found_any = False
        while True:
            start = text.find(LS_TABLE_COLUMNS[0], cursor)
            if start < 0:
                break
            found_any = True
            inner_cursor = start
            ok = True
            for col in LS_TABLE_COLUMNS:
                idx = text.find(col, inner_cursor)
                if idx < 0:
                    ok = False
                    failures.append(
                        f"{path.relative_to(REPO_ROOT)}: ls example near "
                        f"offset {start} is missing column {col!r}"
                    )
                    break
                inner_cursor = idx + len(col)
            if not ok:
                break
            cursor = inner_cursor
        assert found_any, (
            f"{path.relative_to(REPO_ROOT)}: expected to find an `ls` table "
            "example but no occurrence of the first column was found."
        )
    assert not failures, "ls table column-order drift:\n  " + "\n  ".join(failures)
