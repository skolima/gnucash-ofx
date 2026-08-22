"""What `fetch` now says before it costs you data: implied windows, gaps, consent, stale identity.

Kept apart from `test_run.py` because these all share one question — does the tool warn about the
thing it already knows? — and because every one of them must prove the warning did *not* become a
failure.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gnucash_ofx.config import AppConfig, BankConfig
from gnucash_ofx.coverage import BankCoverage, load_coverage, save_coverage, with_span
from gnucash_ofx.run import (
    CONSENT_WARNING_DAYS,
    DEFAULT_LOOKBACK,
    FetchWarning,
    fetch_bank,
    fetch_enablebanking,
    resolve_window,
    status_report,
)
from gnucash_ofx.sources.enablebanking import EnableBankingClient
from gnucash_ofx.state import LinkedAccount, SessionState, save_session

D = date.fromisoformat
TODAY = D("2026-08-09")
_ALIOR = BankConfig("alior", "enablebanking", {"aspsp": "Alior Bank", "country": "PL"})
# Synthetic, checksum-valid (see AGENTS.md) - never a real account number.
_IBAN = "PL69999999990000000000000001"


@pytest.fixture(scope="module")
def private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _client(private_key_pem: bytes, handler) -> EnableBankingClient:
    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    return EnableBankingClient(application_id="app", private_key=private_key_pem, http=http)


def _config(base: Path, banks: dict[str, BankConfig] | None = None) -> AppConfig:
    return AppConfig(
        output_dir=base / "out",
        state_dir=base,
        banks=banks or {"alior": _ALIOR},
        cache_dir=base / "cache",
    )


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/sessions/sess-1":
        return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-pln"]})
    if path == "/accounts/acc-pln/transactions":
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "t1",
                        "booking_date": "2026-08-01",
                        "transaction_amount": {"currency": "PLN", "amount": "100.00"},
                        "credit_debit_indicator": "DBIT",
                        "creditor": {"name": "ACME Sp. z o.o."},
                        "remittance_information": ["Coffee"],
                    }
                ],
                "continuation_key": None,
            },
        )
    if path == "/accounts/acc-pln/balances":
        return httpx.Response(
            200,
            json={
                "balances": [
                    {
                        "balance_amount": {"currency": "PLN", "amount": "900.00"},
                        "balance_type": "CLBD",
                    }
                ]
            },
        )
    return httpx.Response(404)


def _empty_handler(request: httpx.Request) -> httpx.Response:
    """A bank with nothing in the window - the case that writes no file but is fully covered."""
    if request.url.path == "/accounts/acc-pln/transactions":
        return httpx.Response(200, json={"transactions": [], "continuation_key": None})
    return _handler(request)


def _linked(uid: str = "acc-pln", **kwargs: object) -> dict[str, LinkedAccount]:
    defaults: dict[str, object] = {"iban": _IBAN, "currency": "PLN"}
    defaults.update(kwargs)
    return {uid: LinkedAccount(uid=uid, **defaults)}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- resolve_window


def test_an_explicit_window_is_used_exactly_as_given() -> None:
    window = resolve_window(
        date_from=D("2026-01-01"),
        date_to=D("2026-01-31"),
        coverage=BankCoverage(bank="alior"),
        acctids=[_IBAN],
        today=TODAY,
    )
    assert (window.date_from, window.date_to) == (D("2026-01-01"), D("2026-01-31"))
    assert not window.implied
    assert window.clamped_from is None


def test_an_explicit_from_is_never_clamped() -> None:
    """The user typed it; they know whether their bank serves more than the PSD2 floor."""
    window = resolve_window(
        date_from=D("2025-01-01"),
        date_to=None,
        coverage=BankCoverage(bank="alior"),
        acctids=[_IBAN],
        today=TODAY,
    )
    assert window.date_from == D("2025-01-01")
    assert window.clamped_from is None


def test_an_omitted_to_is_today() -> None:
    window = resolve_window(
        date_from=D("2026-08-01"),
        date_to=None,
        coverage=BankCoverage(bank="alior"),
        acctids=[_IBAN],
        today=TODAY,
    )
    assert window.date_to == TODAY
    assert window.implied


def test_with_no_record_the_window_is_the_default_lookback() -> None:
    window = resolve_window(
        date_from=None,
        date_to=None,
        coverage=BankCoverage(bank="alior"),
        acctids=[_IBAN],
        today=TODAY,
    )
    assert window.date_from == TODAY - DEFAULT_LOOKBACK
    assert window.date_to == TODAY
    # A first run has nothing to give up, so nothing is reported as given up.
    assert window.clamped_from is None


def test_the_window_resumes_from_coverage_with_an_overlap() -> None:
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), D("2026-07-31"))
    window = resolve_window(
        date_from=None, date_to=None, coverage=coverage, acctids=[_IBAN], today=TODAY
    )
    # Resumes the day after coverage ended, then steps back the late-booking margin.
    assert window.date_from < D("2026-08-01")
    assert window.date_from == D("2026-07-18")
    assert window.implied


def test_the_least_covered_account_decides_the_window() -> None:
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), D("2026-07-31"))
    coverage = with_span(coverage, "other", D("2026-06-01"), D("2026-06-30"))
    window = resolve_window(
        date_from=None, date_to=None, coverage=coverage, acctids=[_IBAN, "other"], today=TODAY
    )
    assert window.date_from == D("2026-06-17")


def test_an_account_with_no_record_makes_the_window_the_full_lookback() -> None:
    """Unknown is the least covered there is; the default has to be safe for it."""
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-08-01"), D("2026-08-08"))
    window = resolve_window(
        date_from=None,
        date_to=None,
        coverage=coverage,
        acctids=[_IBAN, "never-fetched"],
        today=TODAY,
    )
    assert window.date_from == TODAY - DEFAULT_LOOKBACK


def test_a_stale_record_is_clamped_and_the_clamp_is_reported() -> None:
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-03-01"))
    window = resolve_window(
        date_from=None, date_to=None, coverage=coverage, acctids=[_IBAN], today=TODAY
    )
    assert window.date_from == TODAY - DEFAULT_LOOKBACK
    assert window.clamped_from == D("2026-02-16")
    assert window.unrecoverable_days > 0


def test_the_window_never_runs_backwards_when_coverage_is_current() -> None:
    """Coverage through today (or later) must not resolve to a from after the to."""
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-12-31"))
    window = resolve_window(
        date_from=None, date_to=D("2026-08-01"), coverage=coverage, acctids=[_IBAN], today=TODAY
    )
    assert window.date_from <= window.date_to


# --------------------------------------------------------------------------- recording


def test_a_successful_fetch_advances_coverage(private_key_pem: bytes, tmp_path: Path) -> None:
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
    )
    entry = load_coverage(tmp_path, "alior").for_account(_IBAN)
    assert entry is not None
    assert entry.spans == ((D("2026-08-01"), D("2026-08-09")),)


def test_an_account_with_no_transactions_is_still_covered(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """It writes no file. Recording coverage at the write would manufacture a gap."""
    written, failures = fetch_bank(
        _client(private_key_pem, _empty_handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
    )
    assert written == []
    assert failures == []
    entry = load_coverage(tmp_path, "alior").for_account(_IBAN)
    assert entry is not None
    assert entry.spans == ((D("2026-08-01"), D("2026-08-09")),)


def test_a_failed_account_does_not_advance_coverage(private_key_pem: bytes, tmp_path: Path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/acc-pln/transactions":
            return httpx.Response(400, json={"error": "ASPSP_ERROR", "message": "no"})
        return _handler(request)

    _written, failures = fetch_bank(
        _client(private_key_pem, failing),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
    )
    assert failures
    assert load_coverage(tmp_path, "alior").for_account(_IBAN) is None


def test_no_state_dir_records_nothing(private_key_pem: bytes, tmp_path: Path) -> None:
    """fetch_bank is callable without a ledger, and then it simply has no opinion."""
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        warn=warnings.append,
    )
    assert not (tmp_path / "coverage").exists()
    assert [w for w in warnings if w.kind == "coverage"] == []


def test_a_ledger_write_failure_warns_but_does_not_fail_the_fetch(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("gnucash_ofx.run.save_coverage", boom)
    warnings: list[FetchWarning] = []
    written, failures = fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert written and not failures
    assert [w.kind for w in warnings] == ["ledger"]
    assert "could not be recorded" in warnings[0].message


# --------------------------------------------------------------------------- gap warnings


def test_a_window_leaving_a_gap_warns_without_failing(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    save_coverage(
        tmp_path, with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), D("2026-06-30"))
    )
    warnings: list[FetchWarning] = []
    written, failures = fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert written and not failures
    gaps = [w for w in warnings if w.kind == "coverage"]
    assert len(gaps) == 1
    assert "2026-07-01 -> 2026-07-31" in gaps[0].message
    assert "--from 2026-07-01 --to 2026-07-31" in gaps[0].message


def test_a_gap_still_inside_the_horizon_says_when_it_ages_out(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    save_coverage(
        tmp_path, with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), D("2026-06-30"))
    )
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    gap = next(w for w in warnings if w.kind == "coverage")
    assert "ages out from 2026-09-29" in gap.message


def test_a_gap_past_the_horizon_is_reported_as_probably_gone(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    # A hole in February, with March onwards covered: entirely older than the 90-day horizon.
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-01-31"))
    coverage = with_span(coverage, _IBAN, D("2026-03-01"), D("2026-07-31"))
    save_coverage(tmp_path, coverage)
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    gap = next(w for w in warnings if w.kind == "coverage")
    assert "may no longer be fetchable" in gap.message


def test_an_account_with_no_record_is_not_warned_about(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Absent means unknown. The first run after upgrade must be quiet."""
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert [w for w in warnings if w.kind == "coverage"] == []


def test_an_unreadable_ledger_warns_once_and_keeps_going(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    (tmp_path / "coverage").mkdir(parents=True)
    (tmp_path / "coverage" / "alior.json").write_text("{ broken", encoding="utf-8")
    warnings: list[FetchWarning] = []
    written, failures = fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert written and not failures
    assert [w.kind for w in warnings] == ["ledger"]


# --------------------------------------------------------------------------- stale identity


def test_an_account_resolving_to_the_bare_uid_warns(private_key_pem: bytes, tmp_path: Path) -> None:
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts={"acc-pln": LinkedAccount(uid="acc-pln", currency="PLN")},
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    identity = [w for w in warnings if w.kind == "identity"]
    assert len(identity) == 1
    assert "gnucash-ofx link alior" in identity[0].message


def test_a_v1_schema_account_with_an_iban_is_not_warned_about(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """5 of the 6 banks measured are on the old layout and none of them is the orphaning case.

    Warning on the schema would send the user through a browser SCA dance per bank to fix nothing.
    """
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts={"acc-pln": LinkedAccount(uid="acc-pln", iban=_IBAN)},
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert [w for w in warnings if w.kind == "identity"] == []


def test_a_fetch_time_hash_rescues_an_account_with_no_iban(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """`accounts_data` is why this check cannot run before GET /sessions."""

    def with_hashes(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions/sess-1":
            return httpx.Response(
                200,
                json={
                    "session_id": "sess-1",
                    "accounts": ["acc-pln"],
                    "accounts_data": [{"uid": "acc-pln", "identification_hash": "stable-hash"}],
                },
            )
        return _handler(request)

    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, with_hashes),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts={"acc-pln": LinkedAccount(uid="acc-pln", currency="PLN")},
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert [w for w in warnings if w.kind == "identity"] == []


# --------------------------------------------------------------------------- consent expiry


def _linked_session(tmp_path: Path, valid_until: date) -> None:
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=valid_until,
            accounts=(LinkedAccount(uid="acc-pln", iban=_IBAN, currency="PLN"),),
        ),
    )


def test_a_consent_close_to_expiry_warns(private_key_pem: bytes, tmp_path: Path) -> None:
    _linked_session(tmp_path, TODAY + DEFAULT_LOOKBACK * 0 + (D("2026-09-01") - TODAY))
    report = fetch_enablebanking(
        _config(tmp_path),
        _client(private_key_pem, _handler),
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        today=TODAY,
    )
    consent = [w for w in report.warnings if w.kind == "consent"]
    assert len(consent) == 1
    assert "gnucash-ofx link alior" in consent[0].message
    assert report.failures == []


def test_a_consent_with_plenty_of_time_is_quiet(private_key_pem: bytes, tmp_path: Path) -> None:
    _linked_session(tmp_path, TODAY + (D("2027-01-01") - TODAY))
    report = fetch_enablebanking(
        _config(tmp_path),
        _client(private_key_pem, _handler),
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        today=TODAY,
    )
    assert [w for w in report.warnings if w.kind == "consent"] == []


def test_warnings_do_not_make_the_report_a_failure(private_key_pem: bytes, tmp_path: Path) -> None:
    _linked_session(tmp_path, D("2026-09-01"))
    report = fetch_enablebanking(
        _config(tmp_path),
        _client(private_key_pem, _handler),
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        today=TODAY,
    )
    assert report.warnings
    assert report.failures == []
    assert report.failed_banks == []
    assert report.written


# --------------------------------------------------------------------------- end to end


def test_an_implied_window_resumes_from_the_previous_run(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The point of the whole feature: run it twice, and the second run knows where it got to."""
    _linked_session(tmp_path, D("2027-01-01"))
    config = _config(tmp_path)
    fetch_enablebanking(
        _config(tmp_path),
        _client(private_key_pem, _handler),
        date_from=D("2026-07-01"),
        date_to=D("2026-07-31"),
        today=TODAY,
    )
    entry = load_coverage(tmp_path, "alior").for_account(_IBAN)
    assert entry is not None and entry.covered_through == D("2026-07-31")

    windows: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/acc-pln/transactions":
            windows.append(f"{request.url.params['date_from']}..{request.url.params['date_to']}")
        return _handler(request)

    fetch_enablebanking(
        config,
        _client(private_key_pem, recording),
        date_from=None,
        date_to=None,
        today=TODAY,
        refresh=True,  # bypass the cache so the request is actually made and observable
    )
    # Resumed the day after coverage ended, less the late-booking margin (window 07-18..), and
    # the wire opens the 7-day transaction-date margin below the resolved start on top of that.
    assert windows == ["2026-07-11..2026-08-09"]


def test_status_check_flags_a_bank_needing_attention(tmp_path: Path) -> None:
    _linked_session(tmp_path, D("2026-09-01"))  # 23 days out, inside the warning threshold
    report = status_report(_config(tmp_path), today=TODAY)
    assert len(report) == 1
    assert report[0].needs_attention
    assert "re-link soon" in report[0].lines[0]


def test_status_is_quiet_about_a_healthy_bank(tmp_path: Path) -> None:
    _linked_session(tmp_path, TODAY + (D("2027-01-01") - TODAY))
    save_coverage(tmp_path, with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), TODAY))
    report = status_report(_config(tmp_path), today=TODAY)
    assert not report[0].needs_attention
    assert any("fetched through 2026-08-09" in line for line in report[0].lines)


def test_status_flags_a_gap_inside_the_horizon(tmp_path: Path) -> None:
    _linked_session(tmp_path, TODAY + (D("2027-01-01") - TODAY))
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), D("2026-06-30"))
    coverage = with_span(coverage, _IBAN, D("2026-08-01"), TODAY)
    save_coverage(tmp_path, coverage)
    report = status_report(_config(tmp_path), today=TODAY)
    assert report[0].needs_attention
    assert any("gap inside the last 90 days" in line for line in report[0].lines)


def test_consent_warning_threshold_is_the_documented_one() -> None:
    """Derived in the ADR from the longest observed interval between runs, plus a week."""
    assert CONSENT_WARNING_DAYS == 45


def test_unrecoverable_days_is_zero_when_nothing_was_clamped() -> None:
    window = resolve_window(
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        coverage=BankCoverage(bank="alior"),
        acctids=[_IBAN],
        today=TODAY,
    )
    assert window.unrecoverable_days == 0


def test_a_clamped_window_warns_about_what_it_gave_up(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    _linked_session(tmp_path, D("2027-01-01"))
    save_coverage(
        tmp_path, with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-03-01"))
    )
    report = fetch_enablebanking(
        _config(tmp_path),
        _client(private_key_pem, _handler),
        date_from=None,
        date_to=None,
        today=TODAY,
    )
    clamp = next(w for w in report.warnings if "not fetched by default" in w.message)
    assert "--from 2026-02-16" in clamp.message
    assert report.failures == []


def test_a_gap_straddling_the_horizon_says_so(private_key_pem: bytes, tmp_path: Path) -> None:
    # Covered to the end of January, then nothing until the window: the hole starts before the
    # 90-day horizon and ends after it.
    save_coverage(
        tmp_path, with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-01-31"))
    )
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    gap = next(w for w in warnings if w.kind == "coverage")
    assert "partly past the 90 days" in gap.message
    assert "the rest ages out from 2026-05-11" in gap.message


def test_an_entry_with_no_recorded_days_is_ignored(private_key_pem: bytes, tmp_path: Path) -> None:
    """A hand-edited ledger can hold an account with an empty range; it must read as unknown."""
    from gnucash_ofx.coverage import AccountCoverage, account_key

    save_coverage(
        tmp_path,
        BankCoverage(bank="alior", accounts={account_key(_IBAN): AccountCoverage()}),
    )
    warnings: list[FetchWarning] = []
    fetch_bank(
        _client(private_key_pem, _handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=D("2026-08-01"),
        date_to=D("2026-08-09"),
        output_dir=tmp_path / "out",
        accounts=_linked(),
        today=TODAY,
        state_dir=tmp_path,
        warn=warnings.append,
    )
    assert [w for w in warnings if w.kind == "coverage"] == []
    _linked_session(tmp_path, D("2027-01-01"))
    assert status_report(_config(tmp_path), today=TODAY)[0].lines


def test_status_says_when_the_ledger_cannot_be_read(tmp_path: Path) -> None:
    _linked_session(tmp_path, D("2027-01-01"))
    (tmp_path / "coverage").mkdir(parents=True, exist_ok=True)
    (tmp_path / "coverage" / "alior.json").write_text("{ broken", encoding="utf-8")
    report = status_report(_config(tmp_path), today=TODAY)
    assert any("unreadable" in line for line in report[0].lines)


def test_status_ignores_a_ledger_entry_with_no_recorded_days(tmp_path: Path) -> None:
    from gnucash_ofx.coverage import AccountCoverage, account_key

    _linked_session(tmp_path, D("2027-01-01"))
    save_coverage(
        tmp_path, BankCoverage(bank="alior", accounts={account_key(_IBAN): AccountCoverage()})
    )
    report = status_report(_config(tmp_path), today=TODAY)
    assert not report[0].needs_attention


def test_a_window_with_no_record_does_not_claim_to_come_from_coverage() -> None:
    """Caught in QA: every bank printed "(from coverage)" with no ledger anywhere on disk."""
    window = resolve_window(
        date_from=None,
        date_to=None,
        coverage=BankCoverage(bank="alior"),
        acctids=[_IBAN],
        today=TODAY,
    )
    assert not window.resumed


def test_a_window_from_a_record_says_it_resumed() -> None:
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-06-01"), D("2026-07-31"))
    window = resolve_window(
        date_from=None, date_to=None, coverage=coverage, acctids=[_IBAN], today=TODAY
    )
    assert window.resumed


def test_coverage_reaching_past_an_explicit_to_collapses_to_one_day() -> None:
    """Degenerate but not backwards: `require_ordered_window` holds for resolved windows too."""
    coverage = with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-08-08"))
    window = resolve_window(
        date_from=None, date_to=D("2026-06-30"), coverage=coverage, acctids=[_IBAN], today=TODAY
    )
    assert window.date_from == window.date_to == D("2026-06-30")


def test_the_one_day_window_is_announced(
    private_key_pem: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _linked_session(tmp_path, D("2027-01-01"))
    save_coverage(
        tmp_path, with_span(BankCoverage(bank="alior"), _IBAN, D("2026-01-01"), D("2026-08-08"))
    )
    lines: list[str] = []
    fetch_enablebanking(
        _config(tmp_path),
        _client(private_key_pem, _handler),
        date_from=None,
        date_to=D("2026-06-30"),
        today=TODAY,
        progress=lines.append,
    )
    assert any("coverage already reaches past 2026-06-30" in line for line in lines)


# --------------------------------------------------------------------------- acceptance


def test_the_skipped_month_story(private_key_pem: bytes, tmp_path: Path) -> None:
    """The scenario the whole feature exists for, start to finish.

    Fetch June. Come back in August and type the wrong window, skipping July. The tool has to
    notice, keep working, and print the command that closes it - and once that has been run, stop
    mentioning it.
    """
    _linked_session(tmp_path, D("2027-01-01"))
    config = _config(tmp_path)

    def run(date_from: date | None, date_to: date | None):
        return fetch_enablebanking(
            config,
            _client(private_key_pem, _handler),
            date_from=date_from,
            date_to=date_to,
            today=TODAY,
            refresh=True,
        )

    # June: nothing on record before it, so nothing to warn about.
    june = run(D("2026-06-01"), D("2026-06-30"))
    assert [w for w in june.warnings if w.kind == "coverage"] == []

    # August, July forgotten. The files are still written and the run still succeeds.
    august = run(D("2026-08-01"), D("2026-08-09"))
    assert august.written
    assert august.failures == []
    gap = next(w for w in august.warnings if w.kind == "coverage")
    assert "never fetched 2026-07-01 -> 2026-07-31" in gap.message

    # The warning names the command that closes it. Run exactly that.
    assert "--from 2026-07-01 --to 2026-07-31" in gap.message
    repair = run(D("2026-07-01"), D("2026-07-31"))
    assert repair.failures == []

    # Closed: a bare fetch now resumes from the merged record and says nothing about July.
    after = run(None, None)
    assert [w for w in after.warnings if w.kind == "coverage"] == []
    entry = load_coverage(tmp_path, "alior").for_account(_IBAN)
    assert entry is not None
    assert entry.spans == ((D("2026-06-01"), TODAY),)
