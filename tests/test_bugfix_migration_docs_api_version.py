"""Pin the migration index's stated api_version against the live constant.

Issue #213: ``docs/guides/migration.md`` hard-codes the value of
``SUPPORTED_API_VERSION`` in prose ("currently **N**"). The constant drifted
to 8 while the doc still said 6, leaving the migration index without
walkthroughs for the 6->7 (gapps split) and 7->8 (ports list) hops. This test
asserts the documented number tracks ``config.SUPPORTED_API_VERSION`` so the
two surfaces can't silently disagree again, and that the index actually names
both of the hops landed since v0.6.
"""

from __future__ import annotations

import re
from pathlib import Path

from beetroot import config

_MIGRATION_INDEX = (
    Path(__file__).resolve().parents[1] / "docs" / "guides" / "migration.md"
)


def test_migration_index_states_live_supported_api_version() -> None:
    text = _MIGRATION_INDEX.read_text()
    match = re.search(r"`SUPPORTED_API_VERSION`.*?currently \*\*(\d+)\*\*", text)
    assert match is not None, (
        "migration.md no longer states SUPPORTED_API_VERSION in the "
        "'currently **N**' form this test pins"
    )
    assert int(match.group(1)) == config.SUPPORTED_API_VERSION


def test_migration_index_covers_the_hops_since_v06() -> None:
    text = _MIGRATION_INDEX.read_text()
    assert "`api_version: 6` → `7`" in text, "missing the 6->7 gapps-split walkthrough"
    assert "`api_version: 7` → `8`" in text, "missing the 7->8 ports-list walkthrough"
