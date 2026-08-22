"""Tests for the local fetch cache."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from gnucash_ofx.cache import (
    CHUNK_VERSION,
    LATE_BOOKING_MARGIN,
    balance_cache_path,
    cached_window,
    chunk_ttl,
    load_cached_balances,
    load_cached_month,
    migrate_legacy_entries,
    month_chunk_path,
    month_end,
    months_in,
    save_cached_balances,
    save_cached_month,
)

_NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
TODAY = date(2026, 6, 26)
JUNE = date(2026, 6, 1)
JUNE_END = date(2026, 6, 30)
MAY = date(2026, 5, 1)
MAY_END = date(2026, 5, 31)


def _txn(day: str, tid: str = "t") -> dict[str, object]:
    return {"transaction_id": tid, "booking_date": day}


# --------------------------------------------------------------------------- months


def test_months_in_covers_every_touched_month() -> None:
    assert months_in(date(2026, 5, 15), date(2026, 7, 2)) == [MAY, JUNE, date(2026, 7, 1)]
    assert months_in(JUNE, JUNE) == [JUNE]
    # A window inside one month is one month, not none.
    assert months_in(date(2026, 6, 10), date(2026, 6, 11)) == [JUNE]


def test_month_chunks_roundtrip(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [_txn("2026-06-10")], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert (chunk.covered_from, chunk.covered_to) == (JUNE, JUNE_END)
    assert chunk.transactions == [_txn("2026-06-10")]
    assert chunk.fetched_at == _NOW


def test_a_chunk_of_an_unknown_version_is_ignored(tmp_path: Path) -> None:
    """Treated exactly as an unreadable file: a miss, not a crash and not a wrong answer."""
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=_NOW)
    path = month_chunk_path(tmp_path, "acc-1", JUNE)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = CHUNK_VERSION + 99
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW) is None


def test_a_corrupt_chunk_is_a_miss(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=_NOW)
    month_chunk_path(tmp_path, "acc-1", JUNE).write_text("{ not json", encoding="utf-8")
    assert load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW) is None


def test_a_missing_chunk_is_a_miss(tmp_path: Path) -> None:
    assert load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW) is None


def test_chunks_are_written_atomically(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=_NOW)
    assert not list(tmp_path.glob("*.tmp"))


# --------------------------------------------------------------------------- age-based TTL


def test_the_recent_tail_still_expires() -> None:
    """The current month is a moving target, so it keeps the short TTL."""
    assert chunk_ttl(JUNE, TODAY) == timedelta(hours=6)
    # May is only just settled from TODAY, but not from a day close to its end.
    assert chunk_ttl(MAY, date(2026, 6, 5)) == timedelta(hours=6)


def test_a_settled_month_does_not_expire() -> None:
    """No TTL anyone would pick catches a two-month-late aggregator backfill, so paying to
    re-fetch every settled month everywhere buys nothing. --refresh is the instrument for that.

    AGENTS.md: "A settled month does not expire."
    """
    assert chunk_ttl(MAY, TODAY) is None
    assert chunk_ttl(date(2026, 1, 1), TODAY) is None
    assert month_end(MAY) < TODAY - LATE_BOOKING_MARGIN


# --------------------------------------------------------------------------- serving a window


def test_a_window_covered_by_cached_months_needs_no_fetch(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [_txn("2026-06-10")], now=_NOW)
    served, missing, oldest = cached_window(
        tmp_path, "acc-1", date(2026, 6, 5), date(2026, 6, 20), today=TODAY, now=_NOW
    )
    assert missing == []
    assert served == [_txn("2026-06-10")]
    assert oldest == _NOW


def test_a_narrower_window_hits_what_a_wider_one_stored(tmp_path: Path) -> None:
    """The whole point. The old exact-triple key made ``--to 2026-05-31`` then ``--to 2026-06-01``
    a total miss even though the first is a strict subset of what was already on disk."""
    save_cached_month(tmp_path, "acc-1", MAY, MAY, MAY_END, [], now=_NOW)
    _, missing, _ = cached_window(tmp_path, "acc-1", MAY, date(2026, 5, 20), today=TODAY, now=_NOW)
    assert missing == []


def test_only_the_months_not_held_are_reported_missing(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", MAY, MAY, MAY_END, [], now=_NOW)
    _, missing, _ = cached_window(tmp_path, "acc-1", MAY, date(2026, 6, 20), today=TODAY, now=_NOW)
    assert missing == [JUNE]


def test_a_chunk_that_stops_short_is_a_miss_for_the_days_beyond(tmp_path: Path) -> None:
    """Partial coverage is not a partial answer: the window either is served or it is not."""
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, date(2026, 6, 15), [], now=_NOW)
    _, missing, _ = cached_window(tmp_path, "acc-1", JUNE, date(2026, 6, 20), today=TODAY, now=_NOW)
    assert missing == [JUNE]
    # ...but a window inside what it does cover is served.
    _, missing, _ = cached_window(tmp_path, "acc-1", JUNE, date(2026, 6, 15), today=TODAY, now=_NOW)
    assert missing == []


def test_a_stale_tail_is_refetched_but_a_settled_month_is_not(tmp_path: Path) -> None:
    # AGENTS.md: "A settled month does not expire... only the moving tail expires."
    long_ago = _NOW - timedelta(days=30)
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=long_ago)
    save_cached_month(tmp_path, "acc-1", MAY, MAY, MAY_END, [], now=long_ago)
    _, missing, _ = cached_window(tmp_path, "acc-1", MAY, JUNE_END, today=TODAY, now=_NOW)
    assert missing == [JUNE]  # May is settled and served despite being a month old


# --------------------------------------------------------------------------- balances


def test_balances_are_cached_per_account_not_per_window(tmp_path: Path) -> None:
    balances = {"balances": [{"balance_amount": {"currency": "PLN", "amount": "1.00"}}]}
    save_cached_balances(tmp_path, "acc-1", balances, now=_NOW)
    cached = load_cached_balances(tmp_path, "acc-1", ttl=timedelta(hours=6), now=_NOW)
    assert cached is not None
    assert cached.balances == balances
    assert load_cached_balances(tmp_path, "acc-2", ttl=timedelta(hours=6), now=_NOW) is None


def test_stale_balances_are_not_served_for_a_ledger_balance(tmp_path: Path) -> None:
    save_cached_balances(tmp_path, "acc-1", {"balances": []}, now=_NOW)
    later = _NOW + timedelta(hours=7)
    assert load_cached_balances(tmp_path, "acc-1", ttl=timedelta(hours=6), now=later) is None


def test_a_balance_of_any_age_is_served_when_no_ttl_is_given(tmp_path: Path) -> None:
    """``ttl=None`` is for currency discovery, not for LEDGERBAL: a currency does not change."""
    save_cached_balances(tmp_path, "acc-1", {"balances": [{"x": 1}]}, now=_NOW)
    assert load_cached_balances(tmp_path, "acc-1", ttl=None, now=_NOW + timedelta(days=400))


def test_corrupt_balance_file_returns_none(tmp_path: Path) -> None:
    save_cached_balances(tmp_path, "acc-1", {"balances": []}, now=_NOW)
    balance_cache_path(tmp_path, "acc-1").write_text("{ not valid json", encoding="utf-8")
    assert load_cached_balances(tmp_path, "acc-1", ttl=None, now=_NOW) is None


def test_a_missing_balance_file_returns_none(tmp_path: Path) -> None:
    assert load_cached_balances(tmp_path, "acc-1", ttl=None, now=_NOW) is None


def test_a_naive_fetched_at_is_treated_as_utc(tmp_path: Path) -> None:
    # A naive timestamp must not raise on the aware/naive comparison.
    save_cached_balances(tmp_path, "acc-1", {"balances": []}, now=datetime(2026, 6, 26, 12, 0))
    assert load_cached_balances(tmp_path, "acc-1", ttl=timedelta(hours=6), now=_NOW) is not None


def test_balance_and_month_entries_never_collide(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [_txn("2026-06-02")])
    save_cached_balances(tmp_path, "acc-1", {"balances": [{"y": 2}]})
    assert balance_cache_path(tmp_path, "acc-1") != month_chunk_path(tmp_path, "acc-1", JUNE)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None)
    assert chunk is not None and chunk.transactions == [_txn("2026-06-02")]


# --------------------------------------------------------------------------- legacy conversion


def _write_legacy(
    path: Path, uid: str, d_from: str, d_to: str, txns: list[dict[str, object]]
) -> None:
    path.write_text(
        json.dumps(
            {
                "uid": uid,
                "date_from": d_from,
                "date_to": d_to,
                "fetched_at": _NOW.isoformat(),
                "balances": {},
                "transactions": txns,
            }
        ),
        encoding="utf-8",
    )


def test_legacy_entries_become_month_chunks(tmp_path: Path) -> None:
    """Discarding them would make the upgrade itself cost a full re-fetch, at the bank that can
    least afford one."""
    _write_legacy(
        tmp_path / "deadbeef.json",
        "acc-1",
        "2026-05-15",
        "2026-06-20",
        [_txn("2026-05-20", "a"), _txn("2026-06-10", "b")],
    )
    assert migrate_legacy_entries(tmp_path) == 1

    may = load_cached_month(tmp_path, "acc-1", MAY, ttl=None, now=_NOW)
    assert may is not None
    # Partial coverage is recorded honestly rather than rounded out to the month.
    assert (may.covered_from, may.covered_to) == (date(2026, 5, 15), MAY_END)
    assert may.transactions == [_txn("2026-05-20", "a")]

    june = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert june is not None
    assert (june.covered_from, june.covered_to) == (JUNE, date(2026, 6, 20))
    assert june.transactions == [_txn("2026-06-10", "b")]

    # The converted file is removed, so the scan does not run again on every fetch forever.
    assert not (tmp_path / "deadbeef.json").exists()


def test_conversion_keeps_the_widest_coverage_of_a_month(tmp_path: Path) -> None:
    _write_legacy(tmp_path / "a.json", "acc-1", "2026-06-10", "2026-06-15", [])
    _write_legacy(tmp_path / "b.json", "acc-1", "2026-06-01", "2026-06-30", [_txn("2026-06-02")])
    migrate_legacy_entries(tmp_path)
    june = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert june is not None
    assert (june.covered_from, june.covered_to) == (JUNE, JUNE_END)
    assert june.transactions == [_txn("2026-06-02")]


def test_conversion_ignores_files_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / "junk.json").write_text("{ not json", encoding="utf-8")
    _write_legacy(tmp_path / "ok.json", "acc-1", "2026-06-01", "2026-06-30", [])
    assert migrate_legacy_entries(tmp_path) == 1
    assert (tmp_path / "junk.json").exists()  # left alone rather than deleted on a guess


def test_conversion_leaves_the_new_layout_alone(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=_NOW)
    save_cached_balances(tmp_path, "acc-1", {"balances": []}, now=_NOW)
    assert migrate_legacy_entries(tmp_path) == 0
    assert load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW) is not None


def test_conversion_on_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    assert migrate_legacy_entries(tmp_path / "nope") == 0


def test_conversion_prefers_the_wider_entry_whichever_it_reads_first(tmp_path: Path) -> None:
    # The narrow one sorts first here, so the "keep what we already have" branch is the one taken.
    _write_legacy(tmp_path / "a.json", "acc-1", "2026-06-01", "2026-06-30", [_txn("2026-06-02")])
    _write_legacy(tmp_path / "b.json", "acc-1", "2026-06-10", "2026-06-15", [])
    migrate_legacy_entries(tmp_path)
    june = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert june is not None
    assert (june.covered_from, june.covered_to) == (JUNE, JUNE_END)


def test_conversion_keeps_transactions_it_cannot_date(tmp_path: Path) -> None:
    """An undateable transaction stays, so the mapper still raises for it and the problem stays
    visible - filtering it away here would hide a data bug behind a cache layer."""
    _write_legacy(
        tmp_path / "a.json",
        "acc-1",
        "2026-06-01",
        "2026-06-30",
        [{"transaction_id": "no-date"}, {"transaction_id": "bad", "booking_date": "not-a-date"}],
    )
    migrate_legacy_entries(tmp_path)
    june = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert june is not None
    assert [t["transaction_id"] for t in june.transactions] == ["no-date", "bad"]


# --------------------------------------------------------------------------- merge on save
#
# save_cached_month merges rather than overwrites: the incoming fetch is authoritative for the
# days it claims, and never lossy outside them. The overwrite it replaces was measured destroying
# a fuller month twice in two days on the default resume path — full-July data surviving only in
# a stranded pre-re-link chunk. See docs/adr-transaction-date-window-margin.md decision 3.


def test_a_narrow_save_no_longer_discards_what_a_wide_one_stored(tmp_path: Path) -> None:
    """The headline loss of issue #35: window 07-30..08-13 used to replace a full-July chunk
    with a two-day remnant, and covers() then certified the wreck as complete forever."""
    save_cached_month(
        tmp_path,
        "acc-1",
        JUNE,
        JUNE,
        JUNE_END,
        [_txn("2026-06-05", "early"), _txn("2026-06-28", "late")],
        now=_NOW,
    )
    save_cached_month(
        tmp_path, "acc-1", JUNE, date(2026, 6, 25), JUNE_END, [_txn("2026-06-28", "late")], now=_NOW
    )
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert {t["transaction_id"] for t in chunk.transactions} == {"early", "late"}
    assert (chunk.covered_from, chunk.covered_to) == (JUNE, JUNE_END)


def test_the_incoming_set_is_authoritative_inside_its_claim(tmp_path: Path) -> None:
    """A bank-side withdrawal or amendment still lands: inside the days the new fetch claims,
    what it returned replaces what the chunk held."""
    save_cached_month(
        tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [_txn("2026-06-10", "withdrawn")], now=_NOW
    )
    save_cached_month(tmp_path, "acc-1", JUNE, date(2026, 6, 5), date(2026, 6, 15), [], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert chunk.transactions == []


def test_the_incoming_copy_wins_on_a_key_collision_outside_its_claim(tmp_path: Path) -> None:
    """Keyed on the identity FITID rests on, so a bank that returns full history on every fetch
    (Millennium) merges idempotently instead of duplicating its history on every save."""
    txn_v1 = {"transaction_id": "t-1", "booking_date": "2026-06-10", "note": "v1"}
    txn_v2 = {"transaction_id": "t-1", "booking_date": "2026-06-10", "note": "v2"}
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [txn_v1], now=_NOW)
    # The second save claims late June only; the collision is outside that claim.
    save_cached_month(tmp_path, "acc-1", JUNE, date(2026, 6, 20), JUNE_END, [txn_v2], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert chunk.transactions == [txn_v2]


def test_an_undateable_entry_is_kept_unless_the_incoming_set_replaces_it(tmp_path: Path) -> None:
    save_cached_month(
        tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [{"transaction_id": "no-date"}], now=_NOW
    )
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [_txn("2026-06-01")], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert {t["transaction_id"] for t in chunk.transactions} == {"no-date", "t"}


def test_touching_claims_union(tmp_path: Path) -> None:
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, date(2026, 6, 10), [], now=_NOW)
    save_cached_month(tmp_path, "acc-1", JUNE, date(2026, 6, 11), date(2026, 6, 20), [], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert (chunk.covered_from, chunk.covered_to) == (JUNE, date(2026, 6, 20))


def test_disjoint_claims_keep_the_wider_and_never_claim_the_gap(tmp_path: Path) -> None:
    """The one thing a coverage record may never do is claim days it lacks: two disjoint claims
    must not union across the unanswered days between them."""
    save_cached_month(
        tmp_path, "acc-1", JUNE, JUNE, date(2026, 6, 10), [_txn("2026-06-02", "a")], now=_NOW
    )
    save_cached_month(
        tmp_path,
        "acc-1",
        JUNE,
        date(2026, 6, 25),
        JUNE_END,
        [_txn("2026-06-26", "b")],
        now=_NOW,
    )
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    # The wider claim (June 1-10) stands; both fetches' entries survive regardless.
    assert (chunk.covered_from, chunk.covered_to) == (JUNE, date(2026, 6, 10))
    assert not chunk.covers(date(2026, 6, 25), JUNE_END)
    assert {t["transaction_id"] for t in chunk.transactions} == {"a", "b"}


def test_an_empty_claim_holds_data_but_never_covers(tmp_path: Path) -> None:
    """A crosser the ASPSP returned for a span that never claimed its month: held (data paid for
    with a counted request is never discarded), served to no window, refetched when asked for."""
    save_cached_month(tmp_path, "acc-1", JUNE, None, None, [_txn("2026-06-02", "x")], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert (chunk.covered_from, chunk.covered_to) == (None, None)
    assert not chunk.covers(date(2026, 6, 2), date(2026, 6, 2))
    _, missing, _ = cached_window(
        tmp_path, "acc-1", date(2026, 6, 1), date(2026, 6, 3), today=TODAY, now=_NOW
    )
    assert missing == [JUNE]


def test_a_claiming_fetch_takes_over_an_empty_claim_chunk(tmp_path: Path) -> None:
    """The crosser is replaced (not duplicated, not resurrected) the first time a fetch actually
    claims its days; the claim becomes the fetch's own."""
    save_cached_month(tmp_path, "acc-1", JUNE, None, None, [_txn("2026-06-02", "x")], now=_NOW)
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [_txn("2026-06-02", "x")], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert (chunk.covered_from, chunk.covered_to) == (JUNE, JUNE_END)
    assert [t["transaction_id"] for t in chunk.transactions] == ["x"]


def test_a_poisoned_chunk_degrades_the_merge_instead_of_crashing_the_run(tmp_path: Path) -> None:
    """Keying an id-less entry hashes its amount fields, so a wrong-typed entry already on disk
    (cached before the mapper's guard rejected its account) raises on every later save touching
    its chunk. One account's bad data costs that account only: the merge degrades to the incoming
    set - the old overwrite, authoritative for exactly the claim it writes - never a run abort."""
    poisoned = {"booking_date": "2026-06-10", "transaction_amount": "100"}  # not an object
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, date(2026, 6, 15), [poisoned], now=_NOW)
    save_cached_month(
        tmp_path, "acc-1", JUNE, date(2026, 6, 20), JUNE_END, [_txn("2026-06-21")], now=_NOW
    )
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert chunk.transactions == [_txn("2026-06-21")]
    assert (chunk.covered_from, chunk.covered_to) == (date(2026, 6, 20), JUNE_END)


def test_a_save_retaining_old_claim_days_keeps_the_old_timestamp(tmp_path: Path) -> None:
    """A crosser-only save (or a narrow re-claim of a fuller chunk) retains days only the old
    fetch answered. Stamping those saves as fresh would make a stale moving-tail claim read as
    inside the 6-hour TTL without anyone re-asking about its days."""
    early = datetime(2026, 6, 26, 6, 0, tzinfo=UTC)
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=early)
    save_cached_month(tmp_path, "acc-1", JUNE, None, None, [_txn("2026-06-02", "x")], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert chunk.fetched_at == early  # the claim is still the old fetch's answer

    # A save re-claiming the whole span re-answered every claimed day: fresh again.
    save_cached_month(tmp_path, "acc-1", JUNE, JUNE, JUNE_END, [], now=_NOW)
    chunk = load_cached_month(tmp_path, "acc-1", JUNE, ttl=None, now=_NOW)
    assert chunk is not None
    assert chunk.fetched_at == _NOW
