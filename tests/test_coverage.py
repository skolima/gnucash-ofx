"""The coverage ledger: interval arithmetic, persistence, and what it refuses to claim."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from gnucash_ofx.coverage import (
    AccountCoverage,
    BankCoverage,
    account_key,
    coverage_path,
    load_coverage,
    merge_spans,
    missing_days,
    save_coverage,
    with_span,
)

D = date.fromisoformat
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

# A published per-country documentation example for Poland: the UNFCCC banking-form
# instructions list it as the Polish row of their IBAN-examples table (the one that gives
# GB29NWBK... for the UK), and tcllib's iban.test carries it as a valid case. Verified against
# those two sources, 2026-08-22; NOT claimed to be the SWIFT IBAN Registry's own example, whose
# Poland row differs. A documented example, not a real account: the 11402004 bank code is real
# (mBank), the account part is the example's. Kept per AGENTS.md's documented-example
# exception — do not re-sanitize into a nines value; the provenance is the point.
EXAMPLE_IBAN = "PL27114020040000300201355387"


# --------------------------------------------------------------------------- merge_spans


def test_merge_spans_joins_overlapping_ranges() -> None:
    assert merge_spans(
        [(D("2026-01-01"), D("2026-01-20")), (D("2026-01-10"), D("2026-01-31"))]
    ) == ((D("2026-01-01"), D("2026-01-31")),)


def test_merge_spans_joins_touching_ranges() -> None:
    """A fetch ending on the 31st and the next starting on the 1st leave no gap."""
    assert merge_spans(
        [(D("2026-01-01"), D("2026-01-31")), (D("2026-02-01"), D("2026-02-28"))]
    ) == ((D("2026-01-01"), D("2026-02-28")),)


def test_merge_spans_keeps_a_real_gap_apart() -> None:
    """One uncovered day is the whole point; it must survive merging."""
    assert merge_spans(
        [(D("2026-01-01"), D("2026-01-30")), (D("2026-02-01"), D("2026-02-28"))]
    ) == (
        (D("2026-01-01"), D("2026-01-30")),
        (D("2026-02-01"), D("2026-02-28")),
    )


def test_merge_spans_sorts_unordered_input() -> None:
    assert merge_spans(
        [(D("2026-03-01"), D("2026-03-31")), (D("2026-01-01"), D("2026-01-31"))]
    ) == (
        (D("2026-01-01"), D("2026-01-31")),
        (D("2026-03-01"), D("2026-03-31")),
    )


def test_merge_spans_swallows_a_contained_range() -> None:
    assert merge_spans(
        [(D("2026-01-01"), D("2026-01-31")), (D("2026-01-10"), D("2026-01-12"))]
    ) == ((D("2026-01-01"), D("2026-01-31")),)


# --------------------------------------------------------------------------- missing_days


def test_missing_days_with_no_record_is_the_whole_window() -> None:
    assert missing_days([], D("2026-05-01"), D("2026-05-31")) == (
        (D("2026-05-01"), D("2026-05-31")),
    )


def test_missing_days_when_fully_covered_is_nothing() -> None:
    spans = [(D("2026-01-01"), D("2026-12-31"))]
    assert missing_days(spans, D("2026-05-01"), D("2026-05-31")) == ()


def test_missing_days_finds_an_interior_hole() -> None:
    """The case a high-water mark would forget: covered either side, not in the middle."""
    spans = [(D("2026-05-01"), D("2026-06-29")), (D("2026-07-01"), D("2026-08-08"))]
    assert missing_days(spans, D("2026-05-01"), D("2026-08-08")) == (
        (D("2026-06-30"), D("2026-06-30")),
    )


def test_missing_days_reports_both_ends_of_the_window() -> None:
    spans = [(D("2026-05-10"), D("2026-05-20"))]
    assert missing_days(spans, D("2026-05-01"), D("2026-05-31")) == (
        (D("2026-05-01"), D("2026-05-09")),
        (D("2026-05-21"), D("2026-05-31")),
    )


def test_missing_days_ignores_coverage_outside_the_window() -> None:
    spans = [(D("2025-01-01"), D("2025-12-31")), (D("2026-05-01"), D("2026-05-31"))]
    assert missing_days(spans, D("2026-05-05"), D("2026-05-10")) == ()


def test_missing_days_ignores_coverage_starting_after_the_window() -> None:
    spans = [(D("2026-09-01"), D("2026-09-30"))]
    assert missing_days(spans, D("2026-05-01"), D("2026-05-31")) == (
        (D("2026-05-01"), D("2026-05-31")),
    )


def test_account_coverage_reports_its_own_gaps() -> None:
    entry = AccountCoverage(spans=((D("2026-05-01"), D("2026-05-20")),))
    assert entry.missing(D("2026-05-01"), D("2026-05-31")) == ((D("2026-05-21"), D("2026-05-31")),)


def test_missing_days_on_a_backwards_window_is_empty() -> None:
    assert missing_days([], D("2026-05-31"), D("2026-05-01")) == ()


def test_missing_days_on_a_single_day() -> None:
    assert (
        missing_days([(D("2026-05-01"), D("2026-05-31"))], D("2026-05-15"), D("2026-05-15")) == ()
    )
    assert missing_days([], D("2026-05-15"), D("2026-05-15")) == (
        (D("2026-05-15"), D("2026-05-15")),
    )


# --------------------------------------------------------------------------- keying


def test_account_key_is_a_stable_short_digest() -> None:
    key = account_key(EXAMPLE_IBAN)
    assert key == account_key(EXAMPLE_IBAN)
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


def test_account_key_separates_accounts() -> None:
    # Synthetic: unassigned 99999999 bank code, checksum-valid (AGENTS.md's Data section recipe).
    assert account_key("PL46999999990000000000000007") != account_key("eb-0123456789abcdef")


# --------------------------------------------------------------------------- with_span


def test_with_span_records_a_new_account() -> None:
    coverage = with_span(
        BankCoverage(bank="alior"), "PL61", D("2026-05-01"), D("2026-05-31"), now=NOW
    )
    entry = coverage.for_account("PL61")
    assert entry is not None
    assert entry.spans == ((D("2026-05-01"), D("2026-05-31")),)
    assert entry.last_fetch == NOW


def test_with_span_merges_into_an_existing_record() -> None:
    coverage = with_span(BankCoverage(bank="a"), "acct", D("2026-05-01"), D("2026-05-31"), now=NOW)
    coverage = with_span(coverage, "acct", D("2026-06-01"), D("2026-06-30"), now=NOW)
    entry = coverage.for_account("acct")
    assert entry is not None
    assert entry.spans == ((D("2026-05-01"), D("2026-06-30")),)


def test_with_span_leaves_siblings_alone() -> None:
    coverage = with_span(BankCoverage(bank="a"), "one", D("2026-05-01"), D("2026-05-31"), now=NOW)
    coverage = with_span(coverage, "two", D("2026-07-01"), D("2026-07-31"), now=NOW)
    first = coverage.for_account("one")
    assert first is not None
    assert first.spans == ((D("2026-05-01"), D("2026-05-31")),)


def test_covered_through_is_the_end_of_the_last_span() -> None:
    entry = AccountCoverage(
        spans=((D("2026-05-01"), D("2026-05-31")), (D("2026-07-01"), D("2026-07-31")))
    )
    assert entry.covered_through == D("2026-07-31")
    assert AccountCoverage().covered_through is None


def test_earliest_covered_through_takes_the_least_covered_account() -> None:
    coverage = with_span(BankCoverage(bank="a"), "one", D("2026-01-01"), D("2026-07-31"), now=NOW)
    coverage = with_span(coverage, "two", D("2026-01-01"), D("2026-05-31"), now=NOW)
    assert coverage.earliest_covered_through(["one", "two"]) == D("2026-05-31")


def test_earliest_covered_through_is_unknown_when_any_account_is() -> None:
    """A never-fetched account is the least covered one there is, so the answer is not a date."""
    coverage = with_span(BankCoverage(bank="a"), "one", D("2026-01-01"), D("2026-07-31"), now=NOW)
    assert coverage.earliest_covered_through(["one", "unfetched"]) is None


def test_earliest_covered_through_of_nothing_is_unknown() -> None:
    assert BankCoverage(bank="a").earliest_covered_through([]) is None


# --------------------------------------------------------------------------- persistence


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    coverage = with_span(
        BankCoverage(bank="alior"), "PL61", D("2026-05-01"), D("2026-05-31"), now=NOW
    )
    save_coverage(tmp_path, coverage)
    loaded = load_coverage(tmp_path, "alior")
    assert loaded.bank == "alior"
    assert not loaded.unreadable
    entry = loaded.for_account("PL61")
    assert entry is not None
    assert entry.spans == ((D("2026-05-01"), D("2026-05-31")),)
    assert entry.last_fetch == NOW


def test_a_bank_with_no_ledger_is_unknown_not_a_gap(tmp_path: Path) -> None:
    """Every existing bank starts here; the first run must not warn about anything."""
    loaded = load_coverage(tmp_path, "never-fetched")
    assert loaded.accounts == {}
    assert not loaded.unreadable
    assert loaded.for_account("PL61") is None


def test_a_corrupt_ledger_reads_as_unknown_and_says_so(tmp_path: Path) -> None:
    path = coverage_path(tmp_path, "alior")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    loaded = load_coverage(tmp_path, "alior")
    assert loaded.accounts == {}
    assert loaded.unreadable


def test_a_future_schema_version_reads_as_unknown(tmp_path: Path) -> None:
    path = coverage_path(tmp_path, "alior")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "bank": "alior", "accounts": {}}), encoding="utf-8")
    loaded = load_coverage(tmp_path, "alior")
    assert loaded.accounts == {}
    assert loaded.unreadable


def test_the_ledger_holds_no_account_number(tmp_path: Path) -> None:
    """It is a file of dates and will look pasteable. It has to actually be pasteable."""
    acctid = EXAMPLE_IBAN
    save_coverage(
        tmp_path,
        with_span(BankCoverage(bank="alior"), acctid, D("2026-05-01"), D("2026-05-31"), now=NOW),
    )
    text = coverage_path(tmp_path, "alior").read_text(encoding="utf-8")
    assert acctid not in text
    assert account_key(acctid) in text


def test_saving_replaces_without_leaving_a_temp_file(tmp_path: Path) -> None:
    coverage = with_span(
        BankCoverage(bank="alior"), "acct", D("2026-05-01"), D("2026-05-31"), now=NOW
    )
    save_coverage(tmp_path, coverage)
    save_coverage(tmp_path, with_span(coverage, "acct", D("2026-06-01"), D("2026-06-30"), now=NOW))
    files = sorted(p.name for p in (tmp_path / "coverage").iterdir())
    assert files == ["alior.json"]


def test_a_torn_write_leaves_the_previous_ledger_intact(tmp_path: Path, monkeypatch) -> None:
    coverage = with_span(
        BankCoverage(bank="alior"), "acct", D("2026-05-01"), D("2026-05-31"), now=NOW
    )
    save_coverage(tmp_path, coverage)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("gnucash_ofx.coverage.os.replace", boom)
    with contextlib.suppress(OSError):
        save_coverage(
            tmp_path, with_span(coverage, "acct", D("2026-06-01"), D("2026-06-30"), now=NOW)
        )
    reloaded = load_coverage(tmp_path, "alior")
    entry = reloaded.for_account("acct")
    assert entry is not None
    assert entry.spans == ((D("2026-05-01"), D("2026-05-31")),)
    assert sorted(p.name for p in (tmp_path / "coverage").iterdir()) == ["alior.json"]


def test_a_naive_last_fetch_is_read_as_utc(tmp_path: Path) -> None:
    path = coverage_path(tmp_path, "alior")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bank": "alior",
                "accounts": {
                    account_key("acct"): {
                        "covered": [["2026-05-01", "2026-05-31"]],
                        "last_fetch": "2026-08-09T12:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    entry = load_coverage(tmp_path, "alior").for_account("acct")
    assert entry is not None
    assert entry.last_fetch == NOW


def test_an_unparseable_last_fetch_does_not_lose_the_coverage(tmp_path: Path) -> None:
    """The dates are the load-bearing part; a bad timestamp must not discard them."""
    path = coverage_path(tmp_path, "alior")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bank": "alior",
                "accounts": {
                    account_key("acct"): {
                        "covered": [["2026-05-01", "2026-05-31"]],
                        "last_fetch": "not a time",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    entry = load_coverage(tmp_path, "alior").for_account("acct")
    assert entry is not None
    assert entry.spans == ((D("2026-05-01"), D("2026-05-31")),)
    assert entry.last_fetch is None


def test_a_ledger_entry_with_no_timestamp_round_trips(tmp_path: Path) -> None:
    coverage = BankCoverage(
        bank="alior",
        accounts={
            account_key("acct"): AccountCoverage(spans=((D("2026-05-01"), D("2026-05-31")),))
        },
    )
    save_coverage(tmp_path, coverage)
    entry = load_coverage(tmp_path, "alior").for_account("acct")
    assert entry is not None
    assert entry.last_fetch is None
    assert entry.spans == ((D("2026-05-01"), D("2026-05-31")),)


def test_the_ledger_is_not_mistaken_for_a_session_file(tmp_path: Path) -> None:
    """state/*.json means 'session file' to ad-hoc tooling; the ledger stays out of that glob."""
    save_coverage(
        tmp_path,
        with_span(BankCoverage(bank="alior"), "acct", D("2026-05-01"), D("2026-05-31"), now=NOW),
    )
    assert list(tmp_path.glob("*.json")) == []
    assert coverage_path(tmp_path, "alior").exists()
