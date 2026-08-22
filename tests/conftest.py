"""Shared test guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from gnucash_ofx.config import DEFAULT_CACHE_DIR, DEFAULT_OUTPUT_DIR, DEFAULT_STATE_DIR

# AppConfig's defaults are *relative* paths, bound when the dataclass is defined, so a test that
# builds one without naming its directories silently addresses the developer's real ./cache,
# ./state and ./output. That was survivable while the cache was only ever added to; it stopped
# being survivable when fetch_enablebanking gained a migration step that rewrites and deletes what
# it finds. Fail the test that does it rather than the person whose data it was.
_GUARDED = (DEFAULT_CACHE_DIR, DEFAULT_STATE_DIR, DEFAULT_OUTPUT_DIR)


def _snapshot(directory: Path) -> set[str] | None:
    return {p.name for p in directory.iterdir()} if directory.is_dir() else None


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_directories() -> object:
    before = {d: _snapshot(d) for d in _GUARDED}
    yield
    for directory, was in before.items():
        assert _snapshot(directory) == was, (
            f"a test changed {directory}/ - the real one. Pass an explicit path when building "
            f"AppConfig or calling into the cache; its defaults are relative and point here."
        )
