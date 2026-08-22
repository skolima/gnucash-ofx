"""Tests for tools/evidence/coverage_reconciliation.py."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tools.evidence.coverage_reconciliation import coverage_reconciliation

from gnucash_ofx.coverage import BankCoverage, save_coverage, with_span
from gnucash_ofx.runlog import RunLog, redact_uid_path
from gnucash_ofx.state import LinkedAccount, SessionState, save_session

UID1 = "11112222-uid-one-77778888"
UID2 = "aaaa1111-uid-two-2222bbbb"
ACCTID1 = "PL00000000000000000000001111"  # synthetic: all-zeros IBAN, per AGENTS.md's Data section
ACCTID2 = "PL00000000000000000000002222"  # synthetic, likewise


def _link(tmp_path: Path) -> None:
    session = SessionState(
        bank="alior",
        session_id="s1",
        valid_until=date(2026, 10, 27),
        accounts=(
            LinkedAccount(uid=UID1, iban=ACCTID1),
            LinkedAccount(uid=UID2),
        ),
    )
    save_session(tmp_path, session)


def test_coverage_reconciliation_matches_requests_and_reads_the_ledger(tmp_path: Path) -> None:
    _link(tmp_path)
    coverage = with_span(BankCoverage(bank="alior"), ACCTID1, date(2026, 7, 1), date(2026, 7, 31))
    save_coverage(tmp_path, coverage)

    log = RunLog(tmp_path)
    log.set_context(bank="alior", domain="d")
    log.record_request(
        method="GET",
        path=redact_uid_path(f"/accounts/{UID1}/transactions"),
        status=200,
        attempt=0,
        elapsed=0.1,
    )
    log.record_request(
        method="GET",
        path=redact_uid_path(f"/accounts/{UID2}/balances"),
        status=200,
        attempt=0,
        elapsed=0.1,
    )
    log.record_request(
        method="GET",
        path="/accounts/zzzz***0000/transactions",
        status=200,
        attempt=0,
        elapsed=0.1,
    )

    result = coverage_reconciliation(tmp_path, "alior")

    assert result is not None
    assert result.bank == "alior"
    assert result.live_accounts == 2
    assert result.covered_accounts == 1  # only UID1's ACCTID has a recorded span
    assert result.ledger_unreadable is False
    assert result.ambiguous_truncated_uids == 0
    assert result.requests_total == 3
    assert result.requests_matched_to_live_account == 2
    assert result.requests_unmatched == 1
    assert result.distinct_coverage_keys == 2  # UID2 has no iban, so it keys on its own uid
    assert result.accounts_sharing_a_key == 0


def test_coverage_reconciliation_with_no_ledger_yet(tmp_path: Path) -> None:
    _link(tmp_path)
    result = coverage_reconciliation(tmp_path, "alior")
    assert result is not None
    assert result.covered_accounts == 0
    assert result.ledger_unreadable is False  # absent is "never fetched", not "unreadable"
    assert result.requests_total == 0


def test_coverage_reconciliation_counts_accounts_collapsing_onto_one_ledger_key(
    tmp_path: Path,
) -> None:
    """Currency pockets sharing one master IBAN resolve to one key - the Revolut shape.

    ``covered_accounts`` reads as fully healthy here, because every one of the three sharers finds
    an entry; the entry it finds is the same one, so the fetch that wrote it advanced coverage for
    two accounts nobody fetched. Only the key counts can say so.
    """
    session = SessionState(
        bank="revolut",
        session_id="s1",
        valid_until=date(2026, 11, 10),
        accounts=(
            LinkedAccount(uid="pocket-eur-1111", iban=ACCTID1, currency="EUR"),
            LinkedAccount(uid="pocket-pln-2222", iban=ACCTID1, currency="PLN"),
            LinkedAccount(uid="pocket-usd-3333", iban=ACCTID1, currency="USD"),
            LinkedAccount(uid="pocket-own-4444", iban=ACCTID2, currency="GBP"),
        ),
    )
    save_session(tmp_path, session)
    # One fetch, of one pocket, writing one span.
    save_coverage(
        tmp_path,
        with_span(BankCoverage(bank="revolut"), ACCTID1, date(2026, 7, 1), date(2026, 7, 31)),
    )

    result = coverage_reconciliation(tmp_path, "revolut")

    assert result is not None
    assert result.live_accounts == 4
    assert result.covered_accounts == 3  # the misleading number this pair exists to qualify
    assert result.distinct_coverage_keys == 2
    assert result.accounts_sharing_a_key == 3  # every member of the group, not the surplus


def test_coverage_reconciliation_reports_no_sharing_when_every_account_has_its_own_key(
    tmp_path: Path,
) -> None:
    _link(tmp_path)
    result = coverage_reconciliation(tmp_path, "alior")
    assert result is not None
    assert result.distinct_coverage_keys == result.live_accounts
    assert result.accounts_sharing_a_key == 0


def test_the_key_counts_are_a_lower_bound_when_state_stores_no_identity(tmp_path: Path) -> None:
    """Accounts with nothing captured key on their own ``uid``, which is unique by construction.

    So they report as keying alone even though a fetch-time ``identification_hash`` could collapse
    them - the same ``id_hashes={}`` limit ``would_resolve_to_bare_uid`` carries. Pinned so a zero
    here is never read as "this bank's accounts are distinct".
    """
    session = SessionState(
        bank="erste",
        session_id="s1",
        valid_until=date(2026, 11, 5),
        accounts=(LinkedAccount(uid=UID1), LinkedAccount(uid=UID2)),  # v1: no iban, no hash
    )
    save_session(tmp_path, session)

    result = coverage_reconciliation(tmp_path, "erste")

    assert result is not None
    assert result.distinct_coverage_keys == 2
    assert result.accounts_sharing_a_key == 0


def test_coverage_reconciliation_flags_a_truncated_uid_collision(tmp_path: Path) -> None:
    session = SessionState(
        bank="alior",
        session_id="s1",
        valid_until=date(2026, 10, 27),
        accounts=(
            LinkedAccount(uid="abcd-aaaa-bbbb-cccc-efgh"),
            LinkedAccount(uid="abcd-xxxx-yyyy-zzzz-efgh"),
        ),
    )
    save_session(tmp_path, session)

    result = coverage_reconciliation(tmp_path, "alior")

    assert result is not None
    assert result.ambiguous_truncated_uids == 1


def test_coverage_reconciliation_is_none_for_an_unlinked_bank(tmp_path: Path) -> None:
    assert coverage_reconciliation(tmp_path, "nobody") is None
