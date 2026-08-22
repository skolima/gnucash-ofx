"""Tests for tools/evidence/cache_census.py."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from tools.evidence.cache_census import cache_census

from gnucash_ofx.cache import save_cached_balances, save_cached_month

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
JULY = date(2026, 7, 1)


def test_cache_census_counts_chunks_and_stranded_digests(tmp_path: Path) -> None:
    # "live" is still linked; "orphan" belongs to no current session.
    save_cached_month(tmp_path, "live", JULY, JULY, date(2026, 7, 31), [], now=NOW)
    save_cached_month(tmp_path, "orphan", JULY, JULY, date(2026, 7, 31), [], now=NOW)
    save_cached_balances(tmp_path, "live", {}, now=NOW)
    save_cached_balances(tmp_path, "orphan", {}, now=NOW)

    census = cache_census(tmp_path, live_uids=["live"])

    assert census.month_chunks_total == 2
    assert census.month_chunks_stranded == 1
    assert census.balance_chunks_total == 2
    assert census.balance_chunks_stranded == 1
    assert census.distinct_account_digests == 2
    assert census.stranded_account_digests == 1


def test_cache_census_on_an_empty_cache(tmp_path: Path) -> None:
    census = cache_census(tmp_path, live_uids=["live"])
    assert census.month_chunks_total == 0
    assert census.distinct_account_digests == 0


def test_cache_census_on_a_missing_directory(tmp_path: Path) -> None:
    census = cache_census(tmp_path / "does-not-exist", live_uids=[])
    assert census.month_chunks_total == 0
    assert census.balance_chunks_total == 0


def test_cache_census_ignores_non_chunk_files(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "not-a-chunk.json").write_text("{}", encoding="utf-8")
    census = cache_census(tmp_path, live_uids=[])
    assert census.month_chunks_total == 0
    assert census.balance_chunks_total == 0
    assert census.distinct_account_digests == 0
