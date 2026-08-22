"""Tests for the orchestration layer (config + state + client + OFX writer)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gnucash_ofx.cache import load_cached_month, months_in, save_cached_balances
from gnucash_ofx.config import AppConfig, BankConfig
from gnucash_ofx.coverage import BankCoverage, load_coverage, save_coverage, with_span
from gnucash_ofx.diagnose import diagnose
from gnucash_ofx.run import (
    _PSU_USER_AGENT,
    _PUBLIC_IP_SERVICES,
    DEFAULT_LOOKBACK,
    BankError,
    FetchWarning,
    RequestSpan,
    RunError,
    _currency_from_balances,
    _iban_shared_within,
    _known_acctid,
    _noop_warn,
    _redact_account,
    _request_spans,
    _stored_acctids,
    aspsp_lines,
    build_enablebanking_client,
    build_psu_headers,
    complete_link,
    dry_run_enablebanking,
    extract_balance,
    fetch_bank,
    fetch_enablebanking,
    parse_auth_code,
    start_link,
    status_lines,
    status_report,
)
from gnucash_ofx.sources.enablebanking import EnableBankingClient, linked_accounts
from gnucash_ofx.state import (
    LinkedAccount,
    SessionState,
    load_session,
    save_session,
    session_path,
)


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


# --------------------------------------------------------------------------- parse_auth_code


def test_parse_auth_code() -> None:
    url = "https://localhost/callback?code=abc123&state=xyz"
    assert parse_auth_code(url) == "abc123"


def test_parse_auth_code_missing_raises() -> None:
    with pytest.raises(ValueError, match="no 'code'"):
        parse_auth_code("https://localhost/callback?state=xyz")


# --------------------------------------------------------------------------- extract_balance


def test_extract_balance_prefers_closing_booked() -> None:
    balances = {
        "balances": [
            {"balance_amount": {"currency": "PLN", "amount": "10.00"}, "balance_type": "ITAV"},
            {"balance_amount": {"currency": "PLN", "amount": "1234.56"}, "balance_type": "CLBD"},
        ]
    }
    assert extract_balance(balances, "PLN") == Decimal("1234.56")


def test_extract_balance_filters_by_currency() -> None:
    balances = {
        "balances": [
            {"balance_amount": {"currency": "EUR", "amount": "5.00"}, "balance_type": "CLBD"},
            {"balance_amount": {"currency": "PLN", "amount": "9.00"}, "balance_type": "ITAV"},
        ]
    }
    assert extract_balance(balances, "PLN") == Decimal("9.00")


def test_extract_balance_none_when_absent() -> None:
    assert extract_balance({"balances": []}, "PLN") is None


def test_extract_balance_none_when_the_matching_entry_carries_no_amount() -> None:
    """A currency with no number beside it is missing data, not a zero balance."""
    balances = {"balances": [{"balance_amount": {"currency": "PLN"}, "balance_type": "CLBD"}]}
    assert extract_balance(balances, "PLN") is None


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "garbage"])
def test_extract_balance_rejects_an_amount_that_is_not_a_finite_number(amount: str) -> None:
    """docs/adr-input-hardening.md decision 3: a NaN LEDGERBAL is worse than no file.

    GnuCash pre-fills its reconcile dialog from this number, and NaN compares False against
    everything while Infinity serializes verbatim. Rejected rather than clamped or zeroed — the
    caller turns it into one account's failure and the siblings still get their files.
    """
    balances = {
        "balances": [
            {"balance_amount": {"currency": "PLN", "amount": amount}, "balance_type": "CLBD"}
        ]
    }
    with pytest.raises(ValueError, match="balance_amount.amount"):
        extract_balance(balances, "PLN")


def test_currency_from_balances_skips_xxx_for_a_real_sibling_entry() -> None:
    """ISO 4217's "no currency" placeholder must not shadow a real currency from another entry."""
    balances = {
        "balances": [
            {"balance_amount": {"currency": "XXX", "amount": "0.00"}, "balance_type": "ITAV"},
            {"balance_amount": {"currency": "PLN", "amount": "9.00"}, "balance_type": "CLBD"},
        ]
    }
    assert _currency_from_balances(balances) == "PLN"


def test_currency_from_balances_none_when_every_entry_is_xxx() -> None:
    balances = {"balances": [{"balance_amount": {"currency": "XXX", "amount": "0.00"}}]}
    assert _currency_from_balances(balances) is None


# --------------------------------------------------------------------------- fetch_bank


def _fetch_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/sessions/sess-1":
        # Real GET /sessions returns account UIDs as plain strings (no currency/IBAN).
        return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-pln"]})
    if path == "/accounts/acc-pln/transactions":
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "t1",
                        "booking_date": "2026-05-10",
                        "transaction_amount": {"currency": "PLN", "amount": "100.00"},
                        "credit_debit_indicator": "DBIT",
                        "creditor": {"name": "Shop"},
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


def test_fetch_bank_writes_ofx_per_account(private_key_pem: bytes, tmp_path: Path) -> None:
    client = _client(private_key_pem, _fetch_handler)
    written, _ = fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        output_dir=tmp_path,
        today=date(2026, 5, 31),  # window ends today, so the bank's balance is this one
    )
    assert [p.name for p in written] == ["alior_PLN_2026_05_01-2026_05_31.ofx"]
    content = written[0].read_text(encoding="utf-8")
    assert "t1" in content  # FITID present
    assert "900.00" in content  # real closing balance used as LEDGERBAL


def _counting_handler(calls: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-pln"]})
        if path == "/accounts/acc-pln/transactions":
            calls["transactions"] += 1
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "t1",
                            "booking_date": "2026-05-10",
                            "transaction_amount": {"currency": "PLN", "amount": "100.00"},
                            "credit_debit_indicator": "DBIT",
                        }
                    ],
                    "continuation_key": None,
                },
            )
        if path == "/accounts/acc-pln/balances":
            calls["balances"] += 1
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

    return handler


def test_fetch_bank_serves_second_run_from_cache(private_key_pem: bytes, tmp_path: Path) -> None:
    calls = {"balances": 0, "transactions": 0}
    handler = _counting_handler(calls)
    cache_dir = tmp_path / "cache"
    kwargs: dict[str, Any] = {
        "bank_key": "alior",
        "session_id": "sess-1",
        "date_from": date(2026, 5, 1),
        "date_to": date(2026, 5, 31),
        "output_dir": tmp_path,
        "cache_dir": cache_dir,
    }

    fetch_bank(_client(private_key_pem, handler), **kwargs)
    assert calls == {"balances": 1, "transactions": 1}

    # A second identical fetch is served from cache: no further balance/transaction calls.
    written, _ = fetch_bank(_client(private_key_pem, handler), **kwargs)
    assert calls == {"balances": 1, "transactions": 1}
    assert [p.name for p in written] == [
        "alior_PLN_2026_05_01-2026_05_31.ofx"
    ]  # still produces output


def test_fetch_bank_refresh_bypasses_cache(private_key_pem: bytes, tmp_path: Path) -> None:
    calls = {"balances": 0, "transactions": 0}
    handler = _counting_handler(calls)
    cache_dir = tmp_path / "cache"
    kwargs: dict[str, Any] = {
        "bank_key": "alior",
        "session_id": "sess-1",
        "date_from": date(2026, 5, 1),
        "date_to": date(2026, 5, 31),
        "output_dir": tmp_path,
        "cache_dir": cache_dir,
    }

    fetch_bank(_client(private_key_pem, handler), **kwargs)
    fetch_bank(_client(private_key_pem, handler), refresh=True, **kwargs)
    assert calls == {"balances": 2, "transactions": 2}  # refresh re-hits the API


def test_build_psu_headers_prefers_the_detected_ip_over_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection wins. On a dynamic IP a configured value is stale within a day, so a pin that
    overrode detection would replace a correct address with a confidently wrong one."""
    monkeypatch.setenv("EB_PSU_IP", "198.51.100.7")
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: "203.0.113.9")
    seen: list[str] = []
    headers = build_psu_headers(seen.append)
    assert headers is not None
    assert headers["Psu-Ip-Address"] == "203.0.113.9"
    assert headers["Psu-User-Agent"].startswith("gnucash-ofx/")
    assert any("EB_PSU_IP does not match" in line for line in seen)
    # Neither address may be echoed: the line can be pasted into an issue and both are the user's.
    assert not any("198.51.100.7" in line or "203.0.113.9" in line for line in seen)


def test_build_psu_headers_is_quiet_when_the_configured_ip_still_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EB_PSU_IP", "203.0.113.9")
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: "203.0.113.9")
    seen: list[str] = []
    assert build_psu_headers(seen.append) == {
        "Psu-Ip-Address": "203.0.113.9",
        "Psu-User-Agent": _PSU_USER_AGENT,
    }
    assert seen == []


def test_build_psu_headers_falls_back_to_the_configured_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pin earns its keep only here: every lookup failed, and a stale address still beats
    # dropping the whole run to the ~4/day background cap.
    monkeypatch.setenv("EB_PSU_IP", "  203.0.113.9  ")
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: None)
    headers = build_psu_headers()
    assert headers is not None
    assert headers["Psu-Ip-Address"] == "203.0.113.9"


def test_build_psu_headers_raises_rather_than_silently_using_background_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background mode is ~4 requests/day. Falling into it unnoticed is the expensive mistake
    this project's cache and 429 handling exist to prevent, so it is never the quiet default."""
    monkeypatch.delenv("EB_PSU_IP", raising=False)
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: None)
    with pytest.raises(RunError, match="background mode"):
        build_psu_headers()


def test_build_psu_headers_allows_background_mode_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EB_PSU_IP", raising=False)
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: None)
    seen: list[str] = []
    assert build_psu_headers(seen.append, allow_background=True) is None
    assert any("background mode" in line for line in seen)


def test_build_psu_headers_rejects_an_invalid_configured_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A garbage EB_PSU_IP is no IP at all, not a bad header to send.
    monkeypatch.setenv("EB_PSU_IP", "not-an-ip\n")
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: None)
    with pytest.raises(RunError, match="background mode"):
        build_psu_headers()


# The tests above replace _detect_public_ip itself, so its own body - including its error
# handling and the walk across services - has never run. These instead patch the one thing it
# actually calls (httpx.get), letting the real function execute end to end.


def test_detect_public_ip_uses_the_first_service_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EB_PSU_IP", raising=False)
    tried: list[str] = []

    def fake_get(url: str, timeout: float = 5.0) -> httpx.Response:
        tried.append(url)
        return httpx.Response(200, text="203.0.113.9\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    headers = build_psu_headers()
    assert headers is not None
    assert headers["Psu-Ip-Address"] == "203.0.113.9"
    assert tried == ["https://api.ipify.org"]  # stops at the first success


def test_detect_public_ip_falls_through_to_the_next_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One service being down must not decide the whole run's rate-limit mode."""
    monkeypatch.delenv("EB_PSU_IP", raising=False)
    tried: list[str] = []

    def fake_get(url: str, timeout: float = 5.0) -> httpx.Response:
        tried.append(url)
        if url == _PUBLIC_IP_SERVICES[0]:
            raise httpx.ConnectError("no network")
        if url == _PUBLIC_IP_SERVICES[1]:
            # A captive portal or error page: a 200 that is not an address at all.
            return httpx.Response(200, text="<html>login</html>", request=httpx.Request("GET", url))
        return httpx.Response(200, text="203.0.113.9\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    headers = build_psu_headers()
    assert headers is not None
    assert headers["Psu-Ip-Address"] == "203.0.113.9"
    assert tried == list(_PUBLIC_IP_SERVICES)


def test_detect_public_ip_gives_up_when_every_service_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EB_PSU_IP", raising=False)

    def fake_get(url: str, timeout: float = 5.0) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(RunError, match="background mode"):
        build_psu_headers()


def test_fetch_bank_redacts_iban_in_progress(private_key_pem: bytes, tmp_path: Path) -> None:
    client = _client(private_key_pem, _fetch_handler)
    lines: list[str] = []
    fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        output_dir=tmp_path,
        # Synthetic, checksum-valid (see AGENTS.md).
        accounts={"acc-pln": LinkedAccount("acc-pln", iban="PL73999999990000000000000006")},
        progress=lines.append,
    )
    joined = "\n".join(lines)
    # The full IBAN must never appear in progress/log output; only the redacted form.
    assert "PL73999999990000000000000006" not in joined
    assert "PL73***0006" in joined


def test_fetch_bank_drops_out_of_range_transactions(private_key_pem: bytes, tmp_path: Path) -> None:
    """Server returning history beyond the requested window must be filtered client-side."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-pln"]})
        if path == "/accounts/acc-pln/transactions":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {  # inside window — must be kept
                            "transaction_id": "keep",
                            "booking_date": "2026-06-15",
                            "transaction_amount": {"currency": "PLN", "amount": "10.00"},
                            "credit_debit_indicator": "CRDT",
                        },
                        {  # outside window — must be dropped
                            "transaction_id": "drop",
                            "booking_date": "2026-05-01",
                            "transaction_amount": {"currency": "PLN", "amount": "5.00"},
                            "credit_debit_indicator": "DBIT",
                        },
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
                            "balance_amount": {"currency": "PLN", "amount": "100.00"},
                            "balance_type": "CLBD",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    written, _ = fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
    )
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert "keep" in content
    assert "drop" not in content


# Synthetic Polish IBANs (see AGENTS.md); the 99999999 bank code is unassigned.
_IBAN_A = "PL03999999990000000000000005"
_IBAN_B = "PL03999999990000000000009999"


def _two_account_handler(uids: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    """A session with two same-currency accounts, listed in the given order."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(200, json={"session_id": "sess-1", "accounts": list(uids)})
        if path in ("/accounts/acc-a/transactions", "/accounts/acc-b/transactions"):
            uid = "a" if "acc-a" in path else "b"
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": f"t-{uid}",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                            "credit_debit_indicator": "CRDT",
                        }
                    ],
                    "continuation_key": None,
                },
            )
        if path in ("/accounts/acc-a/balances", "/accounts/acc-b/balances"):
            return httpx.Response(
                200,
                json={
                    "balances": [
                        {
                            "balance_amount": {"currency": "PLN", "amount": "0.00"},
                            "balance_type": "CLBD",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return handler


def _fetch_two_accounts(private_key_pem: bytes, output_dir: Path, uids: list[str]) -> list[Path]:
    written, _ = fetch_bank(
        _client(private_key_pem, _two_account_handler(uids)),
        bank_key="millennium",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=output_dir,
        accounts={
            "acc-a": LinkedAccount("acc-a", iban=_IBAN_A),
            "acc-b": LinkedAccount("acc-b", iban=_IBAN_B),
        },
    )
    return written


def test_fetch_bank_refuses_an_uncarryable_account_type_before_spending_a_request(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A statement that cannot honestly be written must not spend the bank's daily allowance -
    and the refusal is account-scoped: the sibling still ships (docs/adr-accttype-mapping.md)."""
    touched: list[str] = []
    base = _two_account_handler(["acc-a", "acc-b"])

    def handler(request: httpx.Request) -> httpx.Response:
        touched.append(request.url.path)
        return base(request)

    written, failures = fetch_bank(
        _client(private_key_pem, handler),
        bank_key="millennium",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
        accounts={
            "acc-a": LinkedAccount("acc-a", iban=_IBAN_A, currency="PLN", cash_account_type="CARD"),
            "acc-b": LinkedAccount("acc-b", iban=_IBAN_B, currency="PLN", cash_account_type="CACC"),
        },
    )

    # The sibling ships under its suffixed name: the refused account stays in the disambiguation
    # group, because filenames follow the shape of the connection, not which accounts succeeded.
    assert [p.name for p in written] == ["millennium_PLN_9999_2026_06_01-2026_06_30.ofx"]
    assert [failure.operation for failure in failures] == ["account-type"]
    failure = failures[0]
    assert failure.scope == "account"
    assert failure.account == _redact_account(_IBAN_A)
    assert "CARD" in failure.message
    # Refused before any request: neither transactions nor balances were asked for the account.
    assert not [path for path in touched if "acc-a" in path]


def test_fetch_bank_refuses_a_currencyless_uncarryable_account(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The refusal comes before /balances, so a refused account never learns a currency - it
    therefore cannot join a disambiguation group, and the sibling ships under its plain name."""
    written, failures = fetch_bank(
        _client(private_key_pem, _two_account_handler(["acc-a", "acc-b"])),
        bank_key="millennium",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
        accounts={
            "acc-a": LinkedAccount("acc-a", iban=_IBAN_A, cash_account_type="CARD"),
            "acc-b": LinkedAccount("acc-b", iban=_IBAN_B, currency="PLN", cash_account_type="CACC"),
        },
    )
    assert [p.name for p in written] == ["millennium_PLN_2026_06_01-2026_06_30.ofx"]
    assert [failure.operation for failure in failures] == ["account-type"]


def test_fetch_bank_disambiguates_duplicate_currency_accounts(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Two accounts in the same currency must get distinct filenames, not collide."""
    written = _fetch_two_accounts(private_key_pem, tmp_path, ["acc-a", "acc-b"])

    names = sorted(p.name for p in written)
    assert names == [
        "millennium_PLN_0005_2026_06_01-2026_06_30.ofx",
        "millennium_PLN_9999_2026_06_01-2026_06_30.ofx",
    ]
    # Each file must contain only its own transaction.
    assert "t-a" in (tmp_path / names[0]).read_text(encoding="utf-8")
    assert "t-b" in (tmp_path / names[1]).read_text(encoding="utf-8")


def test_fetch_bank_filenames_do_not_depend_on_account_order(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The disambiguator comes from ACCTID, so reordering the session must not rename files."""

    def owner_by_filename(uids: list[str], output_dir: Path) -> dict[str, str]:
        written = _fetch_two_accounts(private_key_pem, output_dir, uids)
        return {
            p.name: next(t for t in ("t-a", "t-b") if t in p.read_text(encoding="utf-8"))
            for p in written
        }

    forward = owner_by_filename(["acc-a", "acc-b"], tmp_path / "forward")
    backward = owner_by_filename(["acc-b", "acc-a"], tmp_path / "backward")
    # Same names, and each name still belongs to the same account.
    assert forward == backward


def test_fetch_bank_chunks_long_date_range(private_key_pem: bytes, tmp_path: Path) -> None:
    """Ranges > 90 days must be split; transactions from all chunks must appear in the OFX.

    Spans are packed by whole months rather than by counting days, so they never split a month -
    which is what lets each chunk be stored and reused on its own. Flat day-counting used to end
    this range with a pointless one-day request and put June in two different chunks.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-pln"]})
        if path == "/accounts/acc-pln/transactions":
            df = request.url.params["date_from"]
            dt = request.url.params.get("date_to", "")
            calls.append((df, dt))
            # Return one transaction per chunk, ID encodes the chunk start date.
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": f"t-{df}",
                            "booking_date": df,
                            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                            "credit_debit_indicator": "CRDT",
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
                            "balance_amount": {"currency": "PLN", "amount": "0.00"},
                            "balance_type": "CLBD",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    written, _ = fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
    )
    # Six months, packed greedily into spans whose margin-widened wire stays within 90 days,
    # each span ending on a month boundary. Every wire request opens TRANSACTION_DATE_MARGIN
    # (7 days) before the span it answers for.
    assert len(calls) == 3
    assert calls[0] == ("2025-12-25", "2026-02-28")  # claims Jan-Feb; 66 wire days
    assert calls[1] == ("2026-02-22", "2026-04-30")  # claims Mar-Apr; adding May would be 99
    assert calls[2] == ("2026-04-24", "2026-06-30")  # claims May-Jun
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    # Transactions from the middle and last chunks appear; the first chunk's is booked on its
    # wire start - 2025-12-25, before the requested window - so the statement filter drops it:
    # the margin is a wire concern and must never move what the OFX carries.
    assert "t-2026-02-22" in content
    assert "t-2026-04-24" in content
    assert "t-2025-12-25" not in content


def test_fetch_bank_deduplicates_transactions_across_chunks(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Banks that ignore date filters return the same txns in every chunk; deduplicate by id."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-pln"]})
        if path == "/accounts/acc-pln/transactions":
            # Same transaction returned regardless of the requested date range.
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "dup",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "PLN", "amount": "10.00"},
                            "credit_debit_indicator": "CRDT",
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
                            "balance_amount": {"currency": "PLN", "amount": "10.00"},
                            "balance_type": "CLBD",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    written, _ = fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
    )
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert content.count("dup") == 1  # FITID appears exactly once


# --------------------------------------------------------------------------- status_lines


def _config(banks: dict[str, BankConfig], base: Path) -> AppConfig:
    # cache_dir must be set explicitly. Its default is the *relative* "./cache", which under
    # pytest resolves to the developer's real cache directory - and fetch_enablebanking now
    # migrates and prunes what it finds there.
    return AppConfig(output_dir=base / "out", state_dir=base, banks=banks, cache_dir=base / "cache")


_ALIOR = BankConfig("alior", "enablebanking", {"aspsp": "Alior Bank", "country": "PL"})


def test_status_lines_reports_a_corrupt_state_file(tmp_path: Path) -> None:
    """A broken state/<bank>.json must produce a status line, not crash the command."""
    banks = {"alior": _ALIOR}
    (tmp_path / "alior.json").write_text("{ not valid json", encoding="utf-8")
    lines = status_lines(_config(banks, tmp_path))
    assert len(lines) == 1
    assert "alior" in lines[0]
    assert "corrupted" in lines[0]


def test_status_lines_reports_linked_and_unlinked(tmp_path: Path) -> None:
    banks = {
        "alior": BankConfig("alior", "enablebanking", {"aspsp": "Alior Bank", "country": "PL"}),
        "wise_personal": BankConfig("wise_personal", "wise", {"token_env": "X"}),
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    save_session(
        tmp_path,
        SessionState(bank="alior", session_id="s", valid_until=date(2026, 7, 1)),
    )
    lines = status_lines(_config(banks, tmp_path), today=date(2026, 6, 21))
    joined = "\n".join(lines)
    assert "alior" in joined and "10 days" in joined
    assert "millennium" in joined and "not linked" in joined
    # Non-Enable-Banking sources are not consent-based, so they are not listed here.
    assert "wise_personal" not in joined


def test_status_lines_reports_expired_consent(tmp_path: Path) -> None:
    banks = {"alior": _ALIOR}
    save_session(
        tmp_path,
        SessionState(bank="alior", session_id="s", valid_until=date(2026, 6, 1)),
    )
    lines = status_lines(_config(banks, tmp_path), today=date(2026, 6, 15))
    assert lines == ["alior: consent EXPIRED (14 days ago) - re-link"]


def test_status_reports_accounts_without_a_stored_type(tmp_path: Path) -> None:
    """The CHECKING fallback's honesty lives in status - the pull channel - never at fetch time
    (docs/adr-accttype-mapping.md decision 3)."""
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="s",
            valid_until=date(2026, 12, 31),
            accounts=(
                LinkedAccount("acc-a", cash_account_type="CACC"),
                LinkedAccount("acc-b"),
                LinkedAccount("acc-c"),
            ),
        ),
    )
    report = status_report(_config({"alior": _ALIOR}, tmp_path), today=date(2026, 6, 21))
    assert len(report) == 1
    joined = "\n".join(report[0].lines)
    assert "2 of 3 account(s) carry no stored account type" in joined
    assert "falls back to CHECKING" in joined
    # Informational, not "needs attention": the files are correct and importable, and nothing
    # can act on this before the bank's next natural re-link.
    assert report[0].needs_attention is False


def test_status_says_nothing_when_every_account_has_a_stored_type(tmp_path: Path) -> None:
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="s",
            valid_until=date(2026, 12, 31),
            accounts=(LinkedAccount("acc-a", cash_account_type="CACC"),),
        ),
    )
    lines = status_lines(_config({"alior": _ALIOR}, tmp_path), today=date(2026, 6, 21))
    assert not any("account type" in line for line in lines)


# --------------------------------------------------------------------------- aspsp_lines


def test_aspsp_lines(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/aspsps"
        assert request.url.params.get("country") == "PL"
        return httpx.Response(
            200,
            json={
                "aspsps": [
                    {"name": "Alior Bank", "country": "PL", "psu_types": ["personal", "business"]},
                    {"name": "Wise", "country": "PL"},
                ]
            },
        )

    client = _client(private_key_pem, handler)
    assert aspsp_lines(client, "PL") == [
        "Alior Bank (PL)  [personal, business]",
        "Wise (PL)",
    ]


def test_aspsp_lines_tolerates_bad_psu_types(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "aspsps": [
                    {"name": "A", "country": "PL", "psu_types": None},
                    {"name": "B", "country": "PL", "psu_types": ["personal", None]},
                    {"name": "C", "country": "PL"},
                ]
            },
        )

    client = _client(private_key_pem, handler)
    assert aspsp_lines(client, "PL") == ["A (PL)", "B (PL)  [personal]", "C (PL)"]


# --------------------------------------------------------------------------- build client


def test_build_client_requires_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EB_APP_ID", raising=False)
    monkeypatch.delenv("EB_PRIVATE_KEY", raising=False)
    with pytest.raises(RunError, match="EB_APP_ID"):
        build_enablebanking_client()


def test_build_client_reads_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_key_pem: bytes
) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(private_key_pem)
    monkeypatch.setenv("EB_APP_ID", "app-x")
    monkeypatch.setenv("EB_PRIVATE_KEY", str(key))
    client = build_enablebanking_client()
    assert client._application_id == "app-x"
    client.close()


def test_build_client_missing_key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EB_APP_ID", "app-x")
    monkeypatch.setenv("EB_PRIVATE_KEY", str(tmp_path / "nope.pem"))
    with pytest.raises(RunError, match="private key file not found"):
        build_enablebanking_client()


def test_build_client_requires_private_key_when_app_id_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # test_build_client_requires_secrets unsets both, so EB_APP_ID's check always fires first;
    # this is the other secret, checked on its own.
    monkeypatch.setenv("EB_APP_ID", "app-x")
    monkeypatch.delenv("EB_PRIVATE_KEY", raising=False)
    with pytest.raises(RunError, match="EB_PRIVATE_KEY"):
        build_enablebanking_client()


def test_build_client_key_file_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_key_pem: bytes
) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(private_key_pem)
    monkeypatch.setenv("EB_APP_ID", "app-x")
    monkeypatch.setenv("EB_PRIVATE_KEY", str(key))

    def boom(self: Path) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(RunError, match="could not read private key"):
        build_enablebanking_client()


# --------------------------------------------------------------------------- link round trip


def _link_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/application":
        return httpx.Response(200, json={"redirect_urls": ["https://localhost/callback"]})
    if path == "/auth":
        return httpx.Response(200, json={"url": "https://bank.example/sca?x=1"})
    if path == "/sessions":
        return httpx.Response(
            200,
            json={
                "session_id": "sess-9",
                "accounts": [{"uid": "a1"}, {"uid": "a2"}],
                "access": {"valid_until": "2026-09-15T00:00:00+00:00"},
            },
        )
    return httpx.Response(404)


def test_start_and_complete_link(private_key_pem: bytes, tmp_path: Path) -> None:
    client = _client(private_key_pem, _link_handler)
    url, requested = start_link(client, _ALIOR, valid_days=90)
    assert url == "https://bank.example/sca?x=1"

    state = complete_link(client, _ALIOR, "code-1", requested, tmp_path)
    assert state.session_id == "sess-9"
    assert state.account_ids == ("a1", "a2")
    # Expiry comes from the bank's granted access.valid_until, not our requested date.
    assert state.valid_until == date(2026, 9, 15)
    assert load_session(tmp_path, "alior") == state


def test_complete_link_falls_back_to_requested_valid_until_on_a_bad_grant(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A malformed access.valid_until from the bank must not crash link; fall back to requested."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(
                200,
                json={
                    "session_id": "sess-bad-date",
                    "accounts": [{"uid": "a1"}],
                    "access": {"valid_until": "not-a-date"},
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    requested = date(2026, 9, 15)
    state = complete_link(client, _ALIOR, "code-1", requested, tmp_path)
    assert state.valid_until == requested


def test_fetch_bank_uses_identification_hash_when_no_iban(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Without an IBAN, ACCTID must come from the stable hash, never the per-session UID."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(
                200,
                json={
                    "accounts": ["acc-nohash"],
                    "accounts_data": [
                        {"uid": "acc-nohash", "identification_hash": "abc123=="},
                    ],
                },
            )
        if path == "/accounts/acc-nohash/transactions":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "t1",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                            "credit_debit_indicator": "CRDT",
                        }
                    ],
                    "continuation_key": None,
                },
            )
        if path == "/accounts/acc-nohash/balances":
            return httpx.Response(
                200,
                json={
                    "balances": [
                        {
                            "balance_amount": {"currency": "PLN", "amount": "0.00"},
                            "balance_type": "CLBD",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    written, _ = fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
    )
    content = written[0].read_text(encoding="utf-8")
    assert "acc-nohash" not in content  # the volatile UID must not become ACCTID
    assert "<ACCTID>eb-" in content  # stable, derived from identification_hash


def _bare_uid_handler(uid: str, balances: list[dict[str, Any]]):
    """GET /sessions with one bare UID, an accounts_data hash, and the given balances."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(
                200,
                json={
                    "accounts": [uid],
                    "accounts_data": [{"uid": uid, "identification_hash": "abc123=="}],
                },
            )
        if path == f"/accounts/{uid}/transactions":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "t1",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                            "credit_debit_indicator": "CRDT",
                        }
                    ],
                    "continuation_key": None,
                },
            )
        if path == f"/accounts/{uid}/balances":
            return httpx.Response(200, json={"balances": balances})
        return httpx.Response(404)

    return handler


def _fetch_one(
    private_key_pem: bytes,
    output_dir: Path,
    *,
    balances: list[dict[str, Any]],
    accounts: dict[str, LinkedAccount] | None,
    today: date | None = None,
) -> list[Path]:
    written, _ = fetch_bank(
        _client(private_key_pem, _bare_uid_handler("acc-1", balances)),
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=output_dir,
        accounts=accounts,
        today=today,
    )
    return written


_PLN_BALANCE = [{"balance_amount": {"currency": "PLN", "amount": "0.00"}, "balance_type": "CLBD"}]


def test_fetch_bank_falls_back_to_stored_currency(private_key_pem: bytes, tmp_path: Path) -> None:
    """An ASPSP returning no usable balance used to mean 'no currency, skipping' — no file."""
    stored = {"acc-1": LinkedAccount("acc-1", currency="PLN")}
    written = _fetch_one(private_key_pem, tmp_path, balances=[], accounts=stored)
    assert [p.name for p in written] == ["alior_PLN_2026_06_01-2026_06_30.ofx"]


def test_fetch_bank_still_skips_when_currency_is_unknown_everywhere(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    assert _fetch_one(private_key_pem, tmp_path, balances=[], accounts=None) == []


def test_fetch_bank_prefers_balances_currency_over_stored(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Live balances stay authoritative; the stored value is only a fallback.

    Only decides anything where balances are fetched at all, i.e. a window ending today - for an
    earlier one there is no live balance to prefer, and the stored currency is all there is.
    """
    stored = {"acc-1": LinkedAccount("acc-1", currency="EUR")}
    written = _fetch_one(
        private_key_pem, tmp_path, balances=_PLN_BALANCE, accounts=stored, today=date(2026, 6, 30)
    )
    assert [p.name for p in written] == ["alior_PLN_2026_06_01-2026_06_30.ofx"]


def _acctid_of(path: Path) -> str:
    match = re.search(r"<ACCTID>([^\r\n<]+)", path.read_text(encoding="utf-8"))
    assert match is not None
    return match.group(1)


def test_fetch_bank_acctid_unchanged_by_the_state_schema_upgrade(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """ACCTID must not move when a session gains a link-time hash.

    GnuCash derives an account's ``online_id`` from BANKID+ACCTID, so a value that changes
    silently orphans every transaction already imported under the old one. A session linked
    before the schema change has no stored hash and resolves via ``accounts_data``; one linked
    after has the same hash stored. Both must produce the same ACCTID.
    """
    v1 = _fetch_one(
        private_key_pem,
        tmp_path / "v1",
        balances=_PLN_BALANCE,
        accounts={"acc-1": LinkedAccount("acc-1")},  # v1 state: uid only
    )
    v2 = _fetch_one(
        private_key_pem,
        tmp_path / "v2",
        balances=_PLN_BALANCE,
        accounts={"acc-1": LinkedAccount("acc-1", identification_hash="abc123==")},
    )
    assert _acctid_of(v1[0]) == _acctid_of(v2[0])
    assert _acctid_of(v1[0]).startswith("eb-")


def test_complete_link_captures_bic(private_key_pem: bytes, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(
                200,
                json={
                    "session_id": "sess-bic",
                    "accounts": [
                        {
                            "uid": "a1",
                            "account_id": {"iban": "PL12345678"},
                            "account_servicer": {"bic_fi": "ALBPPLPW"},
                        },
                        {"uid": "a2", "account_id": {"iban": "PL99"}},  # no servicer
                    ],
                    "access": {"valid_until": "2026-09-15T00:00:00+00:00"},
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    state = complete_link(client, _ALIOR, "code-1", date(2026, 9, 15), tmp_path)
    assert state.account_bics == {"a1": "ALBPPLPW"}
    assert load_session(tmp_path, "alior") == state  # survives the round trip


def test_complete_link_extracts_ibans_tolerating_bad_account_id(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """account_id may be a dict, missing, or an explicit null — linking must not crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(
                200,
                json={
                    "session_id": "sess-iban",
                    "accounts": [
                        {"uid": "with-iban", "account_id": {"iban": "PL12345678"}},
                        {"uid": "null-acct", "account_id": None},
                        {"uid": "no-acct"},
                    ],
                    "access": {"valid_until": "2026-09-15T00:00:00+00:00"},
                },
            )
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    state = complete_link(client, _ALIOR, "code-1", date(2026, 9, 15), tmp_path)
    assert state.account_ids == ("with-iban", "null-acct", "no-acct")
    # Only the account that actually carries an IBAN is mapped.
    assert state.account_ibans == {"with-iban": "PL12345678"}


_SESSION_FIXTURE = Path(__file__).parent / "fixtures" / "enablebanking_session.json"


def _session_fixture_handler(extra: dict[str, Any] | None = None):
    """A link handler whose POST /sessions returns the full sanitized session fixture."""
    payload = json.loads(_SESSION_FIXTURE.read_text(encoding="utf-8"))
    payload.update(extra or {})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return handler


def test_complete_link_captures_full_account_details(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """POST /sessions is the only source of these fields, so link must persist all of them."""
    client = _client(private_key_pem, _session_fixture_handler())
    state = complete_link(client, _ALIOR, "code-1", date(2026, 9, 15), tmp_path)

    account = state.by_uid["acc-with-iban"]
    assert account.currency == "PLN"
    assert account.identification_hash == "hash-with-iban"
    assert account.name == "Konto Osobiste"
    assert account.cash_account_type == "CACC"
    assert state.by_uid["acc-with-nrb"].identification_scheme == "BBAN"
    # Fields we have no consumer for are still recoverable from the raw body.
    assert state.raw["aspsp"] == {"name": "Example Bank", "country": "PL"}
    assert load_session(tmp_path, "alior") == state  # survives the round trip


def test_complete_link_does_not_persist_credentials(private_key_pem: bytes, tmp_path: Path) -> None:
    """The raw body is kept for ~180 days; a token must never be one of the things kept."""
    client = _client(private_key_pem, _session_fixture_handler({"refresh_token": "SECRET-TOKEN"}))
    state = complete_link(client, _ALIOR, "code-1", date(2026, 9, 15), tmp_path)
    assert "refresh_token" not in state.raw
    assert "SECRET-TOKEN" not in session_path(tmp_path, "alior").read_text(encoding="utf-8")


# ------------------------------------------------------- shared-IBAN connections (#32, Revolut)

_REVOLUT_SESSION_FIXTURE = Path(__file__).parent / "fixtures" / "revolut_session.json"
_REVOLUT_TRANSACTIONS_FIXTURE = Path(__file__).parent / "fixtures" / "revolut_transactions.json"

# Synthetic, checksum-valid, unassigned 99999 bank code (AGENTS.md) — the fixture's master IBAN.
_SHARED_LT_IBAN = "LT449999900000000021"


def _revolut_linked() -> dict[str, LinkedAccount]:
    session = json.loads(_REVOLUT_SESSION_FIXTURE.read_text(encoding="utf-8"))
    return {a.uid: a for a in linked_accounts(session)}


def test_shared_iban_connection_resolves_every_pocket_through_the_hash() -> None:
    """docs/adr-revolut-onboarding.md decision 1, the uniform policy.

    Four of Revolut's five pockets share one master IBAN, so an IBAN-derived ACCTID names a
    group, not an account. Once any IBAN in the connection is shared, *every* account of it
    resolves through ``identification_hash`` — the unique-IBAN PLN pocket included, so its
    identity cannot flip later when an ASPSP-side change touches a sibling.
    """
    linked = _revolut_linked()
    acctids = _stored_acctids(tuple(linked.values()))
    assert len(set(acctids)) == 5  # five pockets, five identities — not the collapsed two
    assert all(acctid.startswith("eb-") for acctid in acctids)  # the PLN pocket is demoted too


def test_unshared_connection_resolution_is_unchanged() -> None:
    """The shared-IBAN branch is a new condition, not a reordering (decision 1's rules-out).

    The baseline session fixture — distinct identifiers throughout — must resolve exactly as it
    always has: IBAN first.
    """
    session = json.loads(_SESSION_FIXTURE.read_text(encoding="utf-8"))
    accounts = tuple(linked_accounts(session))
    assert not _iban_shared_within(accounts)
    resolved = _stored_acctids(accounts)
    assert resolved[0] == "PL03999999990000000000000005"  # the stored IBAN still wins


def test_shared_iban_floor_keeps_the_iban_never_the_uid() -> None:
    """A rejected IBAN must never fall through to ``uid`` (decision 1's floor).

    Enable Banking regenerates uids on every re-link, so a uid-derived ACCTID orphans the
    account each consent cycle. A shared-but-stable IBAN mis-files statements recoverably; the
    floor picks it, and warns, whenever the hash the policy wants is not there to fall to.
    """
    with_hash = LinkedAccount("uid-a", iban=_SHARED_LT_IBAN, identification_hash="h-a")
    without_hash = LinkedAccount("uid-b", iban=_SHARED_LT_IBAN)  # old-schema: no hash anywhere
    assert _iban_shared_within((with_hash, without_hash))
    resolved = _stored_acctids((with_hash, without_hash))
    assert resolved[0].startswith("eb-")
    assert resolved[1] == _SHARED_LT_IBAN  # the floor, not the uid
    assert "uid-b" not in resolved


def test_shared_iban_floor_ignores_the_fetch_time_hash() -> None:
    """In the shared-IBAN branch only the *stored* hash counts.

    A fetch-time rescue (``accounts_data``) would make the floored account's ACCTID depend on
    ``GET /sessions`` still reporting a hash — a value the state-only callers can never see — so
    ``fetch`` and ``status`` would silently diverge about the same account, and the floor's
    warning would never fire. The IBAN it keeps is stable; a re-link stores the hash and moves
    the account once, visibly.
    """
    hashless = LinkedAccount("uid-b", iban=_SHARED_LT_IBAN)
    fetch_view = _known_acctid(hashless, "uid-b", {"uid-b": "fetch-only-hash"}, iban_shared=True)
    state_view = _known_acctid(hashless, "uid-b", {}, iban_shared=True)
    assert fetch_view == state_view == _SHARED_LT_IBAN


def _revolut_handler(
    linked: dict[str, LinkedAccount], responses: dict[str, Any]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-rev":
            return httpx.Response(200, json={"accounts": list(linked)})
        for uid, stored in linked.items():
            if path == f"/accounts/{uid}/transactions":
                return httpx.Response(200, json=responses[uid])
            if path == f"/accounts/{uid}/balances":
                return httpx.Response(
                    200,
                    json={
                        "balances": [
                            {
                                "balance_amount": {"currency": stored.currency, "amount": "0.00"},
                                "balance_type": "CLBD",
                            }
                        ]
                    },
                )
        return httpx.Response(404)

    return handler


# ofxtools nags on values past the spec's A-22/A-32 that the writer deliberately keeps: the
# counterparty number and the composed NAME follow libofx's real 96/390 buffer caps (AGENTS.md).
@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_fetch_bank_shared_iban_connection_writes_five_distinct_accounts(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The regression the GnuCash gate measured (docs/adr-revolut-onboarding.md, open question 1).

    Under the collapsed identities, the two legs of a currency conversion — which share one
    byte-identical FITID by design — landed under one BANKID+ACCTID, and GnuCash's per-account
    FITID de-duplication silently dropped the second leg. Five distinct ACCTIDs are what remove
    the collision; the coverage ledger holding five entries for five pockets (not 2-for-5) is
    the other measured consequence of the same fix.
    """
    linked = _revolut_linked()
    responses = json.loads(_REVOLUT_TRANSACTIONS_FIXTURE.read_text(encoding="utf-8"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    warnings: list[FetchWarning] = []

    written, failures = fetch_bank(
        _client(private_key_pem, _revolut_handler(linked, responses)),
        bank_key="revolut",
        session_id="sess-rev",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=output_dir,
        accounts=linked,
        bank_id="REVOLT21",
        state_dir=state_dir,
        warn=warnings.append,
    )

    assert failures == []
    assert len(written) == 5
    acctids = {_acctid_of(path) for path in written}
    assert len(acctids) == 5
    assert all(acctid.startswith("eb-") for acctid in acctids)

    # The colliding conversion pair: one FITID, now under two distinct account identities, once
    # in each file — unique within every account, exactly what OFX scopes FITID uniqueness to.
    shared_fitid = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
    eur = next(p for p in written if "_EUR_" in p.name).read_text(encoding="utf-8")
    usd = next(p for p in written if "_USD_" in p.name).read_text(encoding="utf-8")
    assert eur.count(f"<FITID>{shared_fitid}") == 1
    assert usd.count(f"<FITID>{shared_fitid}") == 1

    # Revolut's machine reference is entry_reference, a field — never a remittance element — so
    # nothing may surface as a check number (decision 8 property 4).
    everything = "".join(path.read_text(encoding="utf-8") for path in written)
    assert "<CHECKNUM>" not in everything
    assert "<REFNUM>" not in everything

    # All five pockets carry a hash, so neither identity warning fires (the floor was not taken).
    assert [w for w in warnings if w.kind == "identity"] == []

    # The ledger the fetch wrote is readable back through the same resolution status uses: five
    # entries for five pockets, none recorded as covered by a fetch of a different pocket.
    ledger = load_coverage(state_dir, "revolut")
    for acctid in _stored_acctids(tuple(linked.values())):
        entry = ledger.for_account(acctid)
        assert entry is not None
        assert entry.covered_from == date(2026, 6, 1)

    # The written OFX is asserted through ofxtools, per the repo's TDD convention — a malformed
    # document or a mapper regression must fail here, not only at the manual Windows ofxdump gate.
    import io

    from ofxtools.Parser import OFXTree

    statements = {}
    for path in written:
        tree = OFXTree()
        tree.parse(io.BytesIO(path.read_bytes()))
        parsed = tree.convert()
        assert len(parsed.statements) == 1
        statements[parsed.statements[0].curdef] = parsed.statements[0]

    assert set(statements) == {"AUD", "EUR", "GBP", "PLN", "USD"}
    assert {s.account.acctid for s in statements.values()} == acctids
    assert all(s.account.bankid == "REVOLT21" for s in statements.values())

    # The conversion pair's mapped contract: credits positive, debits negative, dated from the
    # booking date, one leg per pocket under the shared FITID.
    eur_leg = next(t for t in statements["EUR"].transactions if t.fitid == shared_fitid)
    usd_leg = next(t for t in statements["USD"].transactions if t.fitid == shared_fitid)
    assert eur_leg.trnamt == Decimal("-100.00")  # DBIT: the source pocket
    assert usd_leg.trnamt == Decimal("110.00")  # CRDT: the target pocket
    assert eur_leg.dtposted.date() == usd_leg.dtposted.date() == date(2026, 6, 10)

    # The counterparty contract on a non-conversion row: composed NAME, and the MEMO copy of the
    # counterparty number — the copy that actually routes GnuCash accounts.
    transfer = next(t for t in statements["PLN"].transactions if t.trnamt == Decimal("-120.00"))
    assert transfer.name == "To Example Utilities S.A."
    assert transfer.memo.endswith("PL00000000000000000000001")


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_fetch_bank_pairs_revolut_exchange_legs_on_entry_reference(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """EXCHANGE legs pair on entry_reference and the annotation reaches both files (#50).

    docs/adr-revolut-exchange-pairing.md decision 1: the deal key is revolut:<entry_reference>,
    so both legs of each fixture deal carry the identical counter-amount-and-rate annotation —
    the rate derived from the two booked amounts, stated nowhere in the source data
    (exchange_rate is a present-but-null key on every fixture row).
    """
    linked = _revolut_linked()
    responses = json.loads(_REVOLUT_TRANSACTIONS_FIXTURE.read_text(encoding="utf-8"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    written, failures = fetch_bank(
        _client(private_key_pem, _revolut_handler(linked, responses)),
        bank_key="revolut",
        session_id="sess-rev",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=output_dir,
        accounts=linked,
        bank_id="REVOLT21",
        state_dir=state_dir,
    )

    assert failures == []
    files = {}
    for currency in ("AUD", "EUR", "GBP", "PLN", "USD"):
        path = next(p for p in written if f"_{currency}_" in p.name)
        files[currency] = path.read_text(encoding="utf-8")

    # "->" is XML-escaped by the writer, so match the pieces either side of it. Identical on
    # both legs (pairing ADR decision 4); the debit pocket is the "from" side.
    for text in (files["EUR"], files["USD"]):
        assert "100.00 EUR" in text
        assert "110.00 USD @ 1.100000" in text
    for text in (files["PLN"], files["GBP"]):
        assert "450.00 PLN" in text
        assert "90.00 GBP @ 0.200000" in text

    # Non-conversion rows are untouched: no annotation arrow anywhere in the TOPUP-only pocket.
    assert "-&gt;" not in files["AUD"]


def test_fetch_bank_warns_when_the_shared_iban_floor_is_taken(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The floor is not silent: a hashless account in a shared-IBAN connection keeps its IBAN,
    and the warning names the once-only remedy (re-link) — mirroring `_warn_stale_identity`."""
    linked = {
        "uid-a": LinkedAccount(
            "uid-a", iban=_SHARED_LT_IBAN, identification_hash="h-a", currency="EUR"
        ),
        "uid-b": LinkedAccount("uid-b", iban=_SHARED_LT_IBAN, currency="USD"),
    }
    responses = {
        uid: {
            "transactions": [
                {
                    "entry_reference": f"ref-{uid}",
                    "transaction_id": None,
                    "booking_date": "2026-06-10",
                    "transaction_amount": {"currency": stored.currency, "amount": "1.00"},
                    "credit_debit_indicator": "CRDT",
                    "status": "BOOK",
                }
            ],
            "continuation_key": None,
        }
        for uid, stored in linked.items()
    }
    warnings: list[FetchWarning] = []

    written, failures = fetch_bank(
        _client(private_key_pem, _revolut_handler(linked, responses)),
        bank_key="revolut",
        session_id="sess-rev",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
        accounts=linked,
        warn=warnings.append,
    )

    assert failures == []
    acctids = {_acctid_of(path) for path in written}
    assert "uid-b" not in acctids  # never the re-link-volatile uid
    assert _SHARED_LT_IBAN in acctids  # the floor kept the IBAN
    assert any(acctid.startswith("eb-") for acctid in acctids)
    identity = [w for w in warnings if w.kind == "identity"]
    assert len(identity) == 1
    assert "shared-IBAN" in identity[0].message
    assert "gnucash-ofx link revolut" in identity[0].message


# --------------------------------------------------------------------------- fetch_enablebanking


def _aspsp_handler(max_consent_seconds: int | None):
    """A link handler whose /aspsps advertises a maximum consent validity."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/aspsps":
            entry: dict = {"name": "Alior Bank", "country": "PL"}
            if max_consent_seconds is not None:
                entry["maximum_consent_validity"] = max_consent_seconds
            return httpx.Response(200, json={"aspsps": [entry]})
        return _link_handler(request)

    return handler


def test_start_link_requests_bank_maximum_consent(private_key_pem: bytes) -> None:
    # 180 days, the real value for every bank we use.
    client = _client(private_key_pem, _aspsp_handler(15552000))
    _url, requested = start_link(client, _ALIOR)
    days = (requested - date.today()).days
    assert 179 <= days <= 180  # exact value depends on the safety margin


def test_start_link_falls_back_when_aspsp_unavailable(private_key_pem: bytes) -> None:
    # /aspsps 404s in _link_handler; discovery failure must not block linking.
    client = _client(private_key_pem, _link_handler)
    _url, requested = start_link(client, _ALIOR)
    days = (requested - date.today()).days
    assert 89 <= days <= 90


def test_start_link_falls_back_when_no_maximum_advertised(private_key_pem: bytes) -> None:
    client = _client(private_key_pem, _aspsp_handler(None))
    _url, requested = start_link(client, _ALIOR)
    days = (requested - date.today()).days
    assert 89 <= days <= 90


def test_start_link_caps_explicit_days_to_bank_maximum(private_key_pem: bytes) -> None:
    client = _client(private_key_pem, _aspsp_handler(15552000))
    _url, requested = start_link(client, _ALIOR, valid_days=365)
    days = (requested - date.today()).days
    assert 179 <= days <= 180  # capped down to the bank's 180


def test_start_link_missing_option(private_key_pem: bytes) -> None:
    client = _client(private_key_pem, _link_handler)
    incomplete = BankConfig("alior", "enablebanking", {"country": "PL"})  # no 'aspsp'
    with pytest.raises(RunError, match="missing required option"):
        start_link(client, incomplete)


def test_start_link_requires_a_redirect_url(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/application":
            return httpx.Response(200, json={"redirect_urls": []})
        return httpx.Response(404)

    client = _client(private_key_pem, handler)
    with pytest.raises(RunError, match="no redirect URLs configured"):
        start_link(client, _ALIOR, valid_days=90)


def test_start_link_passes_psu_type(private_key_pem: bytes) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/application":
            return httpx.Response(200, json={"redirect_urls": ["https://localhost/callback"]})
        if request.url.path == "/auth":
            import json

            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"url": "https://bank.example/sca"})
        return httpx.Response(404)

    business_bank = BankConfig(
        "alior_biz",
        "enablebanking",
        {"aspsp": "Alior Bank", "country": "PL", "psu_type": "business"},
    )
    client = _client(private_key_pem, handler)
    start_link(client, business_bank, valid_days=90)
    assert captured[0]["psu_type"] == "business"


def test_fetch_enablebanking_requires_link(private_key_pem: bytes, tmp_path: Path) -> None:
    """An unlinked bank is a per-bank failure now — it must not abort the run."""
    client = _client(private_key_pem, _fetch_handler)
    config = _config({"alior": _ALIOR}, tmp_path)
    report = fetch_enablebanking(
        config, client, date_from=date(2026, 5, 1), date_to=date(2026, 5, 31)
    )
    assert report.written == []
    assert [(f.bank_key, f.scope) for f in report.failures] == [("alior", "bank")]
    assert "not linked" in report.failures[0].message


def test_fetch_enablebanking_writes_files(private_key_pem: bytes, tmp_path: Path) -> None:
    save_session(
        tmp_path,
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )
    client = _client(private_key_pem, _fetch_handler)
    config = _config({"alior": _ALIOR}, tmp_path)
    report = fetch_enablebanking(
        config, client, date_from=date(2026, 5, 1), date_to=date(2026, 5, 31)
    )
    assert [p.name for p in report.written] == ["alior_PLN_2026_05_01-2026_05_31.ofx"]
    assert report.failures == []


def test_fetch_enablebanking_rejects_expired_consent(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    save_session(
        tmp_path,
        SessionState("alior", "sess-1", date(2026, 5, 1), (LinkedAccount("acc-pln"),)),
    )
    client = _client(private_key_pem, _fetch_handler)
    config = _config({"alior": _ALIOR}, tmp_path)
    report = fetch_enablebanking(
        config,
        client,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 21),  # well past the consent's valid_until
    )
    assert "has expired" in report.failures[0].message
    assert report.written == []


def test_fetch_enablebanking_unknown_only(private_key_pem: bytes, tmp_path: Path) -> None:
    client = _client(private_key_pem, _fetch_handler)
    config = _config({"alior": _ALIOR}, tmp_path)
    with pytest.raises(RunError, match="not a configured Enable Banking bank"):
        fetch_enablebanking(
            config, client, date_from=date(2026, 5, 1), date_to=date(2026, 5, 31), only="ghost"
        )


def test_fetch_enablebanking_rejects_a_reversed_window(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A window that runs backwards is a typo, and the bank is the wrong place to learn that.

    Every ASPSP would be asked for it and the answer would cost a counted request each - at banks
    that allow a few a day. The window is the caller's own argument, so it is checked here.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a reversed window must not reach the bank ({request.url.path})")

    client = _client(private_key_pem, refuse)
    with pytest.raises(RunError, match="--from 2026-07-31 is after --to 2026-07-01"):
        fetch_enablebanking(
            _config({"alior": _ALIOR}, tmp_path),
            client,
            date_from=date(2026, 7, 31),
            date_to=date(2026, 7, 1),
            today=date(2026, 8, 9),
        )


# ------------------------------------------------------------------- failure isolation

_TXNS = {
    "transactions": [
        {
            "transaction_id": "t1",
            "booking_date": "2026-06-10",
            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
            "credit_debit_indicator": "CRDT",
        }
    ],
    "continuation_key": None,
}
_BAL = {
    "balances": [{"balance_amount": {"currency": "PLN", "amount": "5.00"}, "balance_type": "CLBD"}]
}


def _multi_account_handler(
    uids: list[str], failing: dict[str, httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """A session of ``uids``; any uid in ``failing`` returns that response for its data calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": uids})
        for uid in uids:
            if path.startswith(f"/accounts/{uid}/"):
                if uid in failing:
                    return failing[uid]
                return httpx.Response(200, json=_BAL if path.endswith("balances") else _TXNS)
        return httpx.Response(404)

    return handler


def _fetch(
    private_key_pem: bytes,
    out: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    accounts: dict[str, LinkedAccount] | None = None,
    today: date | None = None,
    warnings: list[Any] | None = None,
    state_dir: Path | None = None,
    **client_kwargs: Any,
) -> tuple[list[Path], list[Any]]:
    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app", private_key=private_key_pem, http=http, **client_kwargs
    )
    kwargs: dict[str, Any] = {} if warnings is None else {"warn": warnings.append}
    if state_dir is not None:
        kwargs["state_dir"] = state_dir
    return fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=out,
        accounts=accounts,
        today=today,
        **kwargs,
    )


def test_failing_account_does_not_stop_its_siblings(private_key_pem: bytes, tmp_path: Path) -> None:
    handler = _multi_account_handler(
        ["a", "b", "c"], {"b": httpx.Response(400, json={"code": 400, "message": "ASPSP_ERROR"})}
    )
    # All PLN, matching the shared transaction fixture: the stored currency is authoritative for
    # a closed window now that no balance is fetched, so a mismatch here would be a mapping
    # failure and would test something other than the isolation this is about.
    accounts = {
        "a": LinkedAccount("a", iban=_IBAN_A, currency="PLN"),
        "b": LinkedAccount("b", iban=_IBAN_B, currency="PLN"),
        "c": LinkedAccount("c", iban="PL73999999990000000000000006", currency="PLN"),
    }
    written, failures = _fetch(private_key_pem, tmp_path, handler, accounts)

    assert len(written) == 2  # a and c still got their files
    # The window has closed and every account's currency is known, so no /balances call is made
    # at all (decision 4); "b" fails on the transactions call instead. The isolation is the point.
    assert [(f.scope, f.operation, f.status_code) for f in failures] == [
        ("account", "transactions", 400)
    ]
    assert failures[0].api_code == "ASPSP_ERROR"
    assert "ASPSP_ERROR" in (failures[0].payload or "")


def test_failure_names_the_redacted_account_not_the_uid(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    handler = _multi_account_handler(["a"], {"a": httpx.Response(400, json={"message": "X"})})
    _, failures = _fetch(
        private_key_pem, tmp_path, handler, {"a": LinkedAccount("a", iban=_IBAN_A)}
    )
    assert failures[0].account == _redact_account(_IBAN_A)
    assert _IBAN_A not in (failures[0].account or "")  # never the full number
    assert failures[0].window == (date(2026, 6, 1), date(2026, 6, 30))


def test_rate_limited_account_abandons_the_rest_of_the_bank(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The allowance is per ASPSP, so siblings would only burn backoff to fail identically."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b", "c"]})
        seen.append(path)
        if path.startswith("/accounts/b/"):
            return httpx.Response(429, json={"message": "ASPSP_RATE_LIMIT_EXCEEDED"})
        return httpx.Response(200, json=_BAL if path.endswith("balances") else _TXNS)

    written, failures = _fetch(
        private_key_pem,
        tmp_path,
        handler,
        {u: LinkedAccount(u, currency="PLN") for u in ("a", "b", "c")},
        max_retries=0,
    )
    assert len(written) == 1  # account a, fetched before the limit was hit
    assert [(f.scope, f.status_code) for f in failures] == [("bank", 429)]
    assert not any("/accounts/c/" in p for p in seen)  # c never attempted


def test_unreadable_transaction_data_is_isolated(private_key_pem: bytes, tmp_path: Path) -> None:
    """A currency mismatch is one account's bad data, not the whole bank's problem."""
    bad = {
        "transactions": [
            {
                "transaction_id": "t1",
                "booking_date": "2026-06-10",
                "transaction_amount": {"currency": "SEK", "amount": "1.00"},
                "credit_debit_indicator": "CRDT",
            }
        ],
        "continuation_key": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=bad if path.startswith("/accounts/b/") else _TXNS)

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert len(written) == 1
    assert [(f.scope, f.operation) for f in failures] == [("account", "mapping")]


@pytest.mark.parametrize(
    ("amount", "label"),
    [
        ("garbage", "not a number at all"),
        ("NaN", "compares False against everything, so >= 0 calls a credit a debit"),
        ("Infinity", "serializes verbatim into TRNAMT"),
        ("-Infinity", "the same, signed"),
    ],
)
def test_a_crafted_amount_costs_only_its_own_account(
    private_key_pem: bytes, tmp_path: Path, amount: str, label: str
) -> None:
    """docs/adr-input-hardening.md decision 3: each of these aborted the whole multi-bank run.

    `Decimal("garbage")` raises `decimal.InvalidOperation`, an `ArithmeticError`, which the old
    `except ValueError` did not catch; the two non-finite values parsed cleanly and did their damage
    later, in `build_statement` and in the serialized file.
    """
    bad = {
        "transactions": [
            {
                "transaction_id": "t1",
                "booking_date": "2026-06-10",
                "transaction_amount": {"currency": "PLN", "amount": amount},
                "credit_debit_indicator": "CRDT",
            }
        ],
        "continuation_key": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=bad if path.startswith("/accounts/b/") else _TXNS)

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert len(written) == 1  # the sibling still got its file
    assert [(f.scope, f.operation) for f in failures] == [("account", "mapping")]


def test_a_wrong_typed_amount_field_costs_only_its_own_account(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """`"transaction_amount": "100"` raises AttributeError from the `.get` chain, not ValueError.

    A different escape route from the crafted amounts above, and the reason the guard names
    `AttributeError` explicitly rather than trusting one exception type to cover bad data.
    """
    bad = {
        "transactions": [
            {
                "transaction_id": "t1",
                "booking_date": "2026-06-10",
                "transaction_amount": "100",
                "credit_debit_indicator": "CRDT",
            }
        ],
        "continuation_key": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=bad if path.startswith("/accounts/b/") else _TXNS)

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert len(written) == 1
    assert [(f.scope, f.operation) for f in failures] == [("account", "mapping")]


def test_unreadable_balance_data_degrades_to_the_link_time_currency(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Reading the balances sat outside every per-account guard before decision 3.

    It degrades rather than failing: an unreadable balances body *is* the "no usable balance" case
    the link-time fallback exists for, and the transactions may already be cached and free. Failing
    here would also fail identically on every later run, because coverage is not advanced.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            if path.startswith("/accounts/b/"):
                # A string where the object belongs: `.get` on it is an AttributeError.
                return httpx.Response(200, json={"balances": ["not-an-object"]})
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=_TXNS)

    warnings: list[Any] = []
    written, failures = _fetch(
        private_key_pem,
        tmp_path,
        handler,
        {u: LinkedAccount(u, currency="PLN") for u in ("a", "b")},
        # An open window, so /balances is actually called: with the currency already known from
        # link time and a closed window, the call is skipped and there is nothing to misread.
        today=date(2026, 6, 30),
        warnings=warnings,
    )
    assert len(written) == 2  # both accounts still got their files
    assert failures == []  # a warning, never a BankFailure, and it does not move the exit code
    # Filtered to `ledger`: a stale-identity warning may fire alongside it and is not under test.
    ledger = [w for w in warnings if w.kind == "ledger"]
    assert len(ledger) == 1
    assert "balance data could not be read" in ledger[0].message


def test_an_unreadable_closing_balance_degrades_to_the_statement_total(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """`end_balance=None` is the shape every closed window already uses, not an invented number.

    `build_statement` then fills LEDGERBAL from the statement's own running total, so refusing to
    write over an unreadable figure the file does not require would discard a fully mapped
    statement — and repeat that on every later run, since coverage is not advanced on a failure.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            if path.startswith("/accounts/b/"):
                return httpx.Response(
                    200,
                    json={
                        "balances": [
                            {
                                # The currency reads fine, so the first guard passes and the failure
                                # lands on the closing balance instead.
                                "balance_amount": {"currency": "PLN", "amount": "NaN"},
                                "balance_type": "CLBD",
                            }
                        ]
                    },
                )
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=_TXNS)

    warnings: list[Any] = []
    written, failures = _fetch(
        private_key_pem, tmp_path, handler, today=date(2026, 6, 30), warnings=warnings
    )
    assert len(written) == 2
    assert failures == []
    ledger = [w for w in warnings if w.kind == "ledger"]
    assert len(ledger) == 1
    assert "closing balance could not be read" in ledger[0].message


def test_a_response_past_the_page_cap_costs_only_its_own_account(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """decision 5's cap reports through decision 3's path: account-scoped, coverage unadvanced."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        if path.startswith("/accounts/b/"):
            # Always another page, so the loop would never end on its own.
            return httpx.Response(200, json={"transactions": [], "continuation_key": "more"})
        return httpx.Response(200, json=_TXNS)

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert len(written) == 1
    assert [(f.scope, f.operation) for f in failures] == [("account", "transactions")]


# ------------------------------------------------------------- currency conversion pairing (#5)


def test_conversion_pass_runs_before_the_write_loop_and_pairs_the_legs(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Both legs of a same-bank conversion are collected before any file is written, so the
    pairing pass sees them together (docs/adr-currency-conversion-pairs.md decision 1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["acc-eur", "acc-pln"]})
        if path == "/accounts/acc-eur/transactions":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "eur-leg",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "EUR", "amount": "100.00"},
                            "credit_debit_indicator": "DBIT",
                            "remittance_information": ["Kantor Walutowy 220"],
                        }
                    ],
                    "continuation_key": None,
                },
            )
        if path == "/accounts/acc-pln/transactions":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "pln-leg",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "PLN", "amount": "431.00"},
                            "credit_debit_indicator": "CRDT",
                            "remittance_information": ["Kantor Walutowy 220"],
                        }
                    ],
                    "continuation_key": None,
                },
            )
        return httpx.Response(404)

    written, failures = _fetch(
        private_key_pem,
        tmp_path,
        handler,
        {
            "acc-eur": LinkedAccount("acc-eur", iban=_IBAN_A, currency="EUR"),
            "acc-pln": LinkedAccount("acc-pln", iban=_IBAN_B, currency="PLN"),
        },
    )

    assert failures == []
    assert len(written) == 2
    # "->" is XML-escaped by the writer, so match the pieces either side of it.
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "100.00 EUR" in text
        assert "431.00 PLN @ 4.310000" in text
        assert "Kantor Walutowy 220" in text


def test_a_failed_sibling_leaves_its_partner_unpaired_but_still_written(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A per-account fetch failure can split a pair (docs/adr-currency-conversion-pairs.md §7);
    the surviving leg must still write, with today's plain text - no partial annotation."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["acc-eur", "acc-pln"]})
        if path.startswith("/accounts/acc-eur/"):
            return httpx.Response(400, json={"message": "ASPSP_ERROR"})
        if path == "/accounts/acc-pln/transactions":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "pln-leg",
                            "booking_date": "2026-06-10",
                            "transaction_amount": {"currency": "PLN", "amount": "431.00"},
                            "credit_debit_indicator": "CRDT",
                            "remittance_information": ["Kantor Walutowy 220"],
                        }
                    ],
                    "continuation_key": None,
                },
            )
        return httpx.Response(404)

    written, failures = _fetch(
        private_key_pem,
        tmp_path,
        handler,
        {
            "acc-eur": LinkedAccount("acc-eur", iban=_IBAN_A, currency="EUR"),
            "acc-pln": LinkedAccount("acc-pln", iban=_IBAN_B, currency="PLN"),
        },
    )

    assert len(written) == 1  # only the surviving leg
    assert [(f.scope, f.operation) for f in failures] == [("account", "transactions")]
    text = written[0].read_text(encoding="utf-8")
    assert "Kantor Walutowy 220" in text
    assert "->" not in text  # no annotation without a validated pair


def test_a_dateless_transaction_is_kept_by_the_window_filter_then_reported_as_unmappable(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """_txn_in_window keeps a dateless transaction rather than silently dropping it; the mapper
    then raises for it, which is what actually surfaces the problem (isolated to that account)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "no-date",
                        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                        "credit_debit_indicator": "CRDT",
                    }
                ],
                "continuation_key": None,
            },
        )

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert written == []
    assert [(f.scope, f.operation) for f in failures] == [("account", "mapping")]


def test_a_transaction_with_an_unparseable_date_is_also_kept_then_reported(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "bad-date",
                        "booking_date": "not-a-date",
                        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                        "credit_debit_indicator": "CRDT",
                    }
                ],
                "continuation_key": None,
            },
        )

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert written == []
    assert [(f.scope, f.operation) for f in failures] == [("account", "mapping")]


def test_a_non_json_error_body_is_isolated_like_any_other_failure(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A bank or proxy returning plain text instead of JSON must not break failure reporting."""
    handler = _multi_account_handler(["a"], {"a": httpx.Response(500, text="upstream error")})
    _, failures = _fetch(private_key_pem, tmp_path, handler)
    assert failures[0].status_code == 500
    assert failures[0].api_code is None
    assert failures[0].payload == "upstream error"


def test_transport_error_is_isolated_like_a_status_error(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A connect timeout on one account is exactly what per-account isolation is for."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.startswith("/accounts/b/"):
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json=_BAL if path.endswith("balances") else _TXNS)

    written, failures = _fetch(private_key_pem, tmp_path, handler)
    assert len(written) == 1
    assert failures[0].scope == "account"
    assert failures[0].status_code is None  # no HTTP response to quote


def test_surviving_sibling_keeps_the_filename_it_has_in_a_full_run(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A failing account must not silently rename its same-currency sibling.

    AGENTS.md: "The disambiguation group counts every account in the session, so a failing
    account cannot silently rename its same-currency sibling." Filenames follow the shape of the
    connection, not which accounts happened to succeed — otherwise GnuCash is handed an
    unfamiliar file the first time a bank has a bad day.
    """
    accounts = {
        "a": LinkedAccount("a", iban=_IBAN_A, currency="PLN"),
        "b": LinkedAccount("b", iban=_IBAN_B, currency="PLN"),
    }
    full, _ = _fetch(
        private_key_pem, tmp_path / "full", _multi_account_handler(["a", "b"], {}), accounts
    )
    partial, failures = _fetch(
        private_key_pem,
        tmp_path / "partial",
        _multi_account_handler(["a", "b"], {"b": httpx.Response(400, json={"message": "X"})}),
        accounts,
    )
    assert len(failures) == 1
    surviving = {p.name for p in partial}
    assert surviving == {f"alior_PLN_{_IBAN_A[-4:]}_2026_06_01-2026_06_30.ofx"}
    assert surviving <= {p.name for p in full}


def test_v1_state_cannot_reserve_the_group_slot(private_key_pem: bytes, tmp_path: Path) -> None:
    """Documented degradation: without a stored currency the sibling's slot is unknowable.

    AGENTS.md: "One account with no link-time currency suppresses its whole bank's prediction —
    it may be a same-currency sibling... Never 'improve' this by predicting the accounts that
    are known." The neighboring test above pins the grouping invariant itself; this one pins its
    documented edge case.
    """
    accounts = {
        "a": LinkedAccount("a", iban=_IBAN_A),  # v1 shape: no currency
        "b": LinkedAccount("b", iban=_IBAN_B),
    }
    partial, _ = _fetch(
        private_key_pem,
        tmp_path,
        _multi_account_handler(["a", "b"], {"b": httpx.Response(400, json={"message": "X"})}),
        accounts,
    )
    # No suffix, because nothing knew account b was also PLN. Re-linking the bank fixes it.
    assert {p.name for p in partial} == {"alior_PLN_2026_06_01-2026_06_30.ofx"}


def test_one_failing_bank_does_not_stop_the_others(private_key_pem: bytes, tmp_path: Path) -> None:
    for key in ("alior", "millennium"):
        save_session(
            tmp_path,
            SessionState(key, "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
        )
    banks = {
        "alior": _ALIOR,
        "erste": BankConfig("erste", "enablebanking", {"aspsp": "Erste", "country": "PL"}),
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    report = fetch_enablebanking(
        _config(banks, tmp_path),
        _client(private_key_pem, _fetch_handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
    )
    # erste is not linked; the two that are still produce their files.
    assert len(report.written) == 2
    assert report.failed_banks == ["erste"]


def test_fetch_enablebanking_combine_writes_one_file_for_every_bank(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """docs/adr-combined-ofx-file.md decision 2: one file, composed once, from every bank."""
    for key in ("alior", "millennium"):
        save_session(
            tmp_path,
            SessionState(key, "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
        )
    banks = {
        "alior": _ALIOR,
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    report = fetch_enablebanking(
        _config(banks, tmp_path),
        _client(private_key_pem, _fetch_handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        combine=True,
    )
    assert [p.name for p in report.written] == ["combined_2026_05_01-2026_05_31.ofx"]
    assert report.failures == []
    content = report.written[0].read_text(encoding="utf-8")
    # One statement per bank, sharing the one message set decision 2 requires.
    assert content.count("<BANKMSGSRSV1>") == 1
    assert content.count("<STMTTRNRS>") == 2


def test_fetch_enablebanking_combine_writes_nothing_when_nothing_was_fetched(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """An unlinked bank contributes no pending accounts, so the combined file is never written."""
    report = fetch_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        _client(private_key_pem, _fetch_handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        combine=True,
    )
    assert report.written == []
    assert report.failed_banks == ["alior"]


def test_fetch_enablebanking_combine_writes_the_surviving_bank_when_one_fails_outright(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A bank that never gets as far as fetch_bank (unlinked, here) must not cost the combined
    file its content from the bank that did succeed - the same isolation per-file output has."""
    save_session(
        tmp_path,
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )
    banks = {
        "alior": _ALIOR,
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    report = fetch_enablebanking(
        _config(banks, tmp_path),
        _client(private_key_pem, _fetch_handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        combine=True,
    )
    assert [p.name for p in report.written] == ["combined_2026_05_01-2026_05_31.ofx"]
    assert report.failed_banks == ["millennium"]
    content = report.written[0].read_text(encoding="utf-8")
    assert content.count("<STMTTRNRS>") == 1  # only alior's statement, millennium never ran


def test_fetch_enablebanking_combine_counts_a_resolved_window_even_when_the_bank_then_fails(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A window is resolved locally, before any request for that bank is made - so it must count
    toward the combined filename even if the live fetch that follows falls over.

    Otherwise the combined filename would depend on which banks happened to succeed, breaking the
    same invariant per-account filenames already hold ("Filenames follow the shape of the
    connection, not which accounts succeeded", AGENTS.md) and making a real `--combine` run name
    its file differently than `--dry-run --combine` predicted for the identical local state.

    millennium has no coverage, so it resolves the full 89-day lookback - the earliest possible
    `date_from` in this run - but its session fetch itself fails (`GET /sessions` errors). alior
    has coverage that resumes much later and succeeds normally. If the failing bank's window were
    only counted on success, the union would silently narrow to alior's later start.
    """
    today = date(2026, 5, 31)
    save_session(
        tmp_path,
        # sess-1 matches _fetch_handler's hardcoded path, so alior fetches normally.
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )
    save_session(
        tmp_path,
        SessionState("millennium", "sess-fail", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )
    alior_coverage = with_span(
        BankCoverage(bank="alior"), "acc-pln", date(2026, 4, 1), date(2026, 5, 20)
    )
    save_coverage(tmp_path, alior_coverage)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions/sess-fail":
            return httpx.Response(500, json={"message": "boom"})
        return _fetch_handler(request)

    banks = {
        "alior": _ALIOR,
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    report = fetch_enablebanking(
        _config(banks, tmp_path),
        _client(private_key_pem, handler),
        date_from=None,
        date_to=None,
        today=today,
        combine=True,
    )
    assert report.failed_banks == ["millennium"]
    # millennium's window (the 89-day floor) must still decide the union's start, even though
    # millennium itself never produced a statement.
    assert [p.name for p in report.written] == [
        f"combined_{today - DEFAULT_LOOKBACK:%Y_%m_%d}-{today:%Y_%m_%d}.ofx"
    ]


def test_fetch_enablebanking_combine_names_the_file_from_the_union_of_bank_windows(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Two banks with different implied windows still produce one, correctly-dated file.

    millennium already has coverage through 2026-05-20, so its implied window resumes close to
    there; alior has no coverage at all, so it defaults to the full 89-day lookback - a much
    earlier `date_from`. The combined file must span the earliest start to the latest end across
    both, not just whichever bank happened to run last (decision 6).
    """
    today = date(2026, 5, 31)
    for key in ("alior", "millennium"):
        save_session(
            tmp_path,
            SessionState(key, "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
        )
    millennium_coverage = with_span(
        BankCoverage(bank="millennium"), "acc-pln", date(2026, 4, 1), date(2026, 5, 20)
    )
    save_coverage(tmp_path, millennium_coverage)
    banks = {
        "alior": _ALIOR,
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    report = fetch_enablebanking(
        _config(banks, tmp_path),
        _client(private_key_pem, _fetch_handler),
        date_from=None,
        date_to=None,
        today=today,
        combine=True,
    )
    # alior's floor (today - 89 days) is far earlier than millennium's resumed start, so the
    # union's `date_from` must be alior's, not millennium's.
    assert [p.name for p in report.written] == [
        f"combined_{today - DEFAULT_LOOKBACK:%Y_%m_%d}-{today:%Y_%m_%d}.ofx"
    ]
    # The filename is the union - but each statement inside must still carry its *own* bank's
    # window, not the union's, or millennium's BANKTRANLIST/LEDGERBAL would misreport a range it
    # was never actually asked for. Order follows `banks` (alior, then millennium).
    import io

    from ofxtools.Parser import OFXTree

    tree = OFXTree()
    tree.parse(io.BytesIO(report.written[0].read_bytes()))
    alior_stmt, millennium_stmt = tree.convert().statements
    assert alior_stmt.dtstart.date() == today - DEFAULT_LOOKBACK
    assert millennium_stmt.dtstart.date() > alior_stmt.dtstart.date()


def test_fetch_enablebanking_isolates_a_corrupt_state_file(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """One bank's unreadable state file must not cost the others their files."""
    save_session(
        tmp_path,
        SessionState("millennium", "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )
    (tmp_path / "alior.json").write_text("{ not valid json", encoding="utf-8")
    banks = {
        "alior": _ALIOR,
        "millennium": BankConfig(
            "millennium", "enablebanking", {"aspsp": "Bank Millennium", "country": "PL"}
        ),
    }
    report = fetch_enablebanking(
        _config(banks, tmp_path),
        _client(private_key_pem, _fetch_handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
    )
    assert len(report.written) == 1  # millennium still produced its file
    assert report.failed_banks == ["alior"]
    assert "not a valid session file" in report.failures[0].message


def test_fetch_enablebanking_reports_a_bank_that_fails_before_any_account_is_attempted(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """GET /sessions/{id} itself failing is a bank-level failure, not an account-level one."""
    save_session(
        tmp_path,
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions/sess-1":
            return httpx.Response(500, json={"error": "SOMETHING_WENT_WRONG"})
        return httpx.Response(404)

    report = fetch_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        _client(private_key_pem, handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
    )
    assert report.written == []
    assert [(f.bank_key, f.scope, f.operation) for f in report.failures] == [
        ("alior", "bank", "session")
    ]
    assert report.failures[0].message == "the bank could not be reached"


# Real envelopes, captured from live responses in August 2026. `message` is prose for a human
# and `code` repeats the HTTP status; only `error` names the failure.
_LIVE_400 = {
    "code": 400,
    "message": "Error interacting with ASPSP",
    "error": "ASPSP_ERROR",
    "detail": None,
}
_LIVE_429 = {
    "code": 429,
    "message": "Too many requests",
    "detail": {"message": "Too many requests", "error_name": "RateLimitException"},
    "error": "ASPSP_RATE_LIMIT_EXCEEDED",
}


def test_api_code_comes_from_the_error_field(private_key_pem: bytes, tmp_path: Path) -> None:
    """Reading `message` instead would yield prose, and no explanation would ever match.

    Mirrors the live shape: balances succeeds, the transactions call is the one refused.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a"]})
        if path.endswith("/balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(400, json=_LIVE_400)

    _, failures = _fetch(private_key_pem, tmp_path, handler)
    assert failures[0].operation == "transactions"
    assert failures[0].api_code == "ASPSP_ERROR"
    finding = diagnose(failures[0].status_code, failures[0].api_code, failures[0].operation)
    assert finding is not None and "90 days" in finding.explanation


def test_rate_limit_envelope_is_recognised_by_code_not_just_status(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    handler = _multi_account_handler(["a"], {"a": httpx.Response(429, json=_LIVE_429)})
    _, failures = _fetch(private_key_pem, tmp_path, handler, max_retries=0)
    assert failures[0].api_code == "ASPSP_RATE_LIMIT_EXCEEDED"
    assert failures[0].scope == "bank"


# --------------------------------------------------------------------------- LEDGERBAL scope
#
# /balances answers "what is this account's balance now" - it takes no date, and Enable Banking
# reports reference_date as the fetch date in every record. But OfxWriter dates LEDGERBAL from the
# statement's end date, and GnuCash feeds both into recnWindowWithBalance(). So for a window that
# has closed we were asserting today's balance as the figure to reconcile a past statement
# against. There is nothing correct to fetch, so the call is skipped.
# See docs/adr-aspsp-rate-limit-domain.md §1 and decision 4.


def _recording_handler(uid: str, seen: list[str]):
    inner = _bare_uid_handler(uid, _PLN_BALANCE_5)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return inner(request)

    return handler


_PLN_BALANCE_5 = [{"balance_amount": {"currency": "PLN", "amount": "5.00"}, "balance_type": "CLBD"}]


def _ledgerbal_of(path: Path) -> tuple[str, str]:
    """``(BALAMT, DTASOF date part)`` from the statement's LEDGERBAL aggregate."""
    text = path.read_text(encoding="utf-8")
    ledger = re.search(r"<LEDGERBAL>(.*?)</LEDGERBAL>", text, re.S)
    assert ledger is not None
    amount = re.search(r"<BALAMT>([^\r\n<]+)", ledger.group(1))
    as_of = re.search(r"<DTASOF>([^\r\n<]+)", ledger.group(1))
    assert amount is not None and as_of is not None
    return amount.group(1).strip(), as_of.group(1).strip()[:8]


def _fetch_recording(
    private_key_pem: bytes,
    out: Path,
    *,
    accounts: dict[str, LinkedAccount] | None,
    today: date,
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> tuple[list[Path], list[str]]:
    seen: list[str] = []
    written, _ = fetch_bank(
        _client(private_key_pem, _recording_handler("acc-1", seen)),
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        output_dir=out,
        accounts=accounts,
        today=today,
        cache_dir=cache_dir,
        refresh=refresh,
    )
    return written, seen


_KNOWN_PLN = {"acc-1": LinkedAccount("acc-1", currency="PLN")}
_LEGACY = {"acc-1": LinkedAccount("acc-1")}  # linked before the currency was captured


def test_a_closed_window_spends_no_balance_call(private_key_pem: bytes, tmp_path: Path) -> None:
    # AGENTS.md: "LEDGERBAL carries the bank's balance only when the window ends today or later."
    written, seen = _fetch_recording(
        private_key_pem, tmp_path, accounts=_KNOWN_PLN, today=date(2026, 8, 9)
    )
    assert not [p for p in seen if p.endswith("/balances")]
    # The statement still carries a LEDGERBAL - ofx160.dtd line 921 makes it mandatory in STMTRS -
    # but it is the period's own running total, not a live balance stamped with a past date.
    assert _ledgerbal_of(written[0]) == ("1.00", "20260630")


def test_a_live_window_still_carries_the_banks_ledger_balance(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    # AGENTS.md: "LEDGERBAL carries the bank's balance only when the window ends today or later,
    # and the tag is present either way."
    written, seen = _fetch_recording(
        private_key_pem, tmp_path, accounts=_KNOWN_PLN, today=date(2026, 6, 30)
    )
    assert [p for p in seen if p.endswith("/balances")]
    assert _ledgerbal_of(written[0]) == ("5.00", "20260630")


def test_a_closed_window_still_fetches_balances_when_the_currency_is_unknown(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The call has a second job, and skipping it blindly would cost the file entirely.

    GET /accounts/{uid} is 404 in Restricted Mode, so for a session linked before the currency was
    captured at link time /balances is the only source - and an account with no currency is
    skipped, writing nothing at all.
    """
    written, seen = _fetch_recording(
        private_key_pem, tmp_path, accounts=_LEGACY, today=date(2026, 8, 9)
    )
    assert [p for p in seen if p.endswith("/balances")]
    assert [p.name for p in written] == ["alior_PLN_2026_06_01-2026_06_30.ofx"]
    # ...but it is only used to learn the currency: the balance itself is not this window's.
    assert _ledgerbal_of(written[0]) == ("1.00", "20260630")


def test_currency_discovery_accepts_a_balance_of_any_age(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A currency does not change, so a long-stale balance answers that question perfectly.

    This is what keeps the saving available to legacy sessions: they pay for one balance call ever,
    not one per fetch.
    """
    save_cached_balances(
        tmp_path / "cache",
        "acc-1",
        {"balances": _PLN_BALANCE_5},
        now=datetime(2025, 1, 1, tzinfo=UTC),  # far outside any TTL
    )
    written, seen = _fetch_recording(
        private_key_pem,
        tmp_path,
        accounts=_LEGACY,
        today=date(2026, 8, 9),
        cache_dir=tmp_path / "cache",
    )
    assert not [p for p in seen if p.endswith("/balances")]
    assert [p.name for p in written] == ["alior_PLN_2026_06_01-2026_06_30.ofx"]


def test_a_stale_balance_is_not_reused_for_a_live_windows_ledger_balance(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The other half of the same rule: for LEDGERBAL, an old balance is no better than none."""
    save_cached_balances(
        tmp_path / "cache", "acc-1", {"balances": []}, now=datetime(2025, 1, 1, tzinfo=UTC)
    )
    _, seen = _fetch_recording(
        private_key_pem,
        tmp_path,
        accounts=_KNOWN_PLN,
        today=date(2026, 6, 30),
        cache_dir=tmp_path / "cache",
    )
    assert [p for p in seen if p.endswith("/balances")]


def test_balances_are_not_refetched_for_a_second_window(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """A balance has no window, so two fetches must not each pay for an identical answer."""
    cache = tmp_path / "cache"
    for _ in range(2):
        _, seen = _fetch_recording(
            private_key_pem, tmp_path, accounts=_KNOWN_PLN, today=date(2026, 6, 30), cache_dir=cache
        )
    assert not [p for p in seen if p.endswith("/balances")]  # second run: served from cache


def test_refresh_still_spends_a_balance_call(private_key_pem: bytes, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _fetch_recording(
        private_key_pem, tmp_path, accounts=_KNOWN_PLN, today=date(2026, 6, 30), cache_dir=cache
    )
    _, seen = _fetch_recording(
        private_key_pem,
        tmp_path,
        accounts=_KNOWN_PLN,
        today=date(2026, 6, 30),
        cache_dir=cache,
        refresh=True,
    )
    assert [p for p in seen if p.endswith("/balances")]


# --------------------------------------------------------------------------- month-chunk cache
#
# The cache is a record of what is known, not a memo of questions once asked. See
# docs/adr-aspsp-rate-limit-domain.md decisions 5 and 6.


def test_request_spans_pack_whole_months_up_to_the_bank_limit() -> None:
    """The margin counts against the 90-day request cap, so a widened wire never exceeds it."""
    months = months_in(date(2026, 1, 1), date(2026, 6, 30))
    assert _request_spans(months, date(2026, 1, 1), date(2026, 6, 30)) == [
        RequestSpan(date(2025, 12, 25), date(2026, 1, 1), date(2026, 2, 28)),  # +March = 97 wire
        RequestSpan(date(2026, 2, 22), date(2026, 3, 1), date(2026, 4, 30)),  # +May = 99 wire
        RequestSpan(date(2026, 4, 24), date(2026, 5, 1), date(2026, 6, 30)),
    ]


def test_request_spans_claims_never_widen_past_the_requested_window() -> None:
    """The wire opens TRANSACTION_DATE_MARGIN early (ADR decision 1); the claim must not follow
    it (decision 4) - a request opening at from-margin is only booking-complete from `from`."""
    months = months_in(date(2026, 5, 15), date(2026, 6, 10))
    assert _request_spans(months, date(2026, 5, 15), date(2026, 6, 10)) == [
        RequestSpan(date(2026, 5, 8), date(2026, 5, 15), date(2026, 6, 10))
    ]


def test_request_spans_floor_absorbs_the_margin_at_the_lookback_clamp() -> None:
    """A defaulted window is clamped to 89 days because Alior and Erste 400 on anything deeper -
    so the margin must not step the wire below the clamp, and the capacity check must use the
    floored width or an 89-day cold fetch would split into two requests for no reason."""
    date_from, date_to = date(2026, 5, 12), date(2026, 8, 9)  # 89 days, first fetch shape
    months = months_in(date_from, date_to)
    assert _request_spans(months, date_from, date_to, request_floor=date_from) == [
        RequestSpan(date_from, date_from, date_to)
    ]


def test_the_wire_never_rises_above_the_claim_when_the_floor_sits_above_the_window() -> None:
    """A defaulted --from against an explicit --to older than the clamp resolves to a one-day
    window *below* the floor. max(widened, floor) alone would then put date_from after date_to -
    an inverted request whose empty answer would claim days no request answered. The wire is
    capped at the claim's own start instead: the request goes out exactly as resolved."""
    date_from = date_to = date(2026, 5, 1)  # resolved window, ~100 days before "today"
    floor = date(2026, 5, 12)  # today - DEFAULT_LOOKBACK, above the whole window
    months = months_in(date_from, date_to)
    assert _request_spans(months, date_from, date_to, request_floor=floor) == [
        RequestSpan(date_from, date_from, date_to)
    ]


def test_the_floor_partially_absorbs_the_margin_near_the_horizon() -> None:
    """An explicit --from a few days inside the clamp still widens - but only down to the floor,
    where Alior and Erste 400. This is what keeps the ADR decision-6 repair command (--from at
    the horizon, --refresh) a working command rather than a 400."""
    floor = date(2026, 5, 12)
    date_from, date_to = date(2026, 5, 16), date(2026, 8, 9)  # 4 days inside the clamp
    months = months_in(date_from, date_to)
    assert _request_spans(months, date_from, date_to, request_floor=floor) == [
        RequestSpan(floor, date_from, date_to)  # widened by 4 of the 7 margin days
    ]


def test_request_spans_split_on_a_gap() -> None:
    """A month already held in the middle of a range breaks the span rather than being refetched.

    Each group's wire opens the margin early - including a group that starts after a gap, where
    the margin days reach into the already-cached month; that overlap is what lets the merge
    replace a cached crosser if the bank has since amended it."""
    months = [date(2026, 1, 1), date(2026, 3, 1)]  # February is already cached
    assert _request_spans(months, date(2026, 1, 1), date(2026, 3, 31)) == [
        RequestSpan(date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 31)),
        RequestSpan(date(2026, 2, 22), date(2026, 3, 1), date(2026, 3, 31)),
    ]


def _month_txn_handler(calls: list[tuple[str, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["acc-1"]})
        if path.endswith("/balances"):
            # Only asked for when the window is still open; see the LEDGERBAL section above.
            return httpx.Response(200, json={"balances": _PLN_BALANCE_5})
        if path.endswith("/transactions"):
            df = request.url.params["date_from"]
            calls.append((df, request.url.params.get("date_to", "")))
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": f"t-{df}",
                            "booking_date": df,
                            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                            "credit_debit_indicator": "CRDT",
                        }
                    ],
                    "continuation_key": None,
                },
            )
        return httpx.Response(404)

    return handler


def _windowed_fetch(
    private_key_pem: bytes,
    out: Path,
    cache: Path,
    date_from: date,
    date_to: date,
    today: date,
    refresh: bool = False,
) -> list[tuple[str, str]]:
    """Fetch a window against a shared cache dir; return the transaction requests it sent."""
    calls: list[tuple[str, str]] = []
    fetch_bank(
        _client(private_key_pem, _month_txn_handler(calls)),
        bank_key="alior",
        session_id="sess-1",
        date_from=date_from,
        date_to=date_to,
        output_dir=out,
        accounts={"acc-1": LinkedAccount("acc-1", currency="PLN")},
        cache_dir=cache,
        today=today,
        refresh=refresh,
    )
    return calls


def test_a_window_one_day_wider_refetches_only_what_it_adds(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The pattern that used to miss entirely: ``--to`` a date, then ``--to`` the next day."""
    cache = tmp_path / "cache"
    today = date(2026, 6, 2)
    first = _windowed_fetch(
        private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 5, 31), today
    )
    assert first == [("2026-04-24", "2026-05-31")]  # the wire opens the 7-day margin early

    second = _windowed_fetch(
        private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 6, 1), today
    )
    # May is held in full, so only June is asked for - not the whole window over again. The
    # margin reaches back into cached May on the wire, but claims nothing there.
    assert second == [("2026-05-25", "2026-06-01")]


def test_a_narrower_window_is_served_entirely_from_cache(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    today = date(2026, 6, 2)
    _windowed_fetch(private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 5, 31), today)
    again = _windowed_fetch(
        private_key_pem, tmp_path, cache, date(2026, 5, 10), date(2026, 5, 20), today
    )
    assert again == []


def test_a_settled_month_survives_the_ttl(private_key_pem: bytes, tmp_path: Path) -> None:
    """Fetched while June was still the moving tail, read back long after it has settled."""
    cache = tmp_path / "cache"
    first = _windowed_fetch(
        private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 6, 30), date(2026, 6, 30)
    )
    assert first == [("2026-04-24", "2026-06-30")]
    # Two months on, both months are long settled, so nothing is re-fetched at all.
    later = _windowed_fetch(
        private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 6, 30), date(2026, 8, 30)
    )
    assert later == []


def test_a_failed_span_keeps_the_months_already_paid_for(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Saving once at the end meant a 429 on the last chunk discarded everything already
    fetched, on exactly the accounts that were rate-limited."""
    cache = tmp_path / "cache"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["acc-1"]})
        if path.endswith("/transactions"):
            df = request.url.params["date_from"]
            if df.startswith("2026-04"):  # the second span fails
                return httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": f"t-{df}",
                            "booking_date": df,
                            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                            "credit_debit_indicator": "CRDT",
                        }
                    ],
                    "continuation_key": None,
                },
            )
        return httpx.Response(404)

    _, failures = fetch_bank(
        _client(private_key_pem, handler),
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 6, 30),
        output_dir=tmp_path,
        accounts={"acc-1": LinkedAccount("acc-1", currency="PLN")},
        cache_dir=cache,
        today=date(2026, 8, 9),
    )
    assert [f.status_code for f in failures] == [429]
    # January to April landed before the failure (spans claim Jan-Feb, Mar-Apr, May-Jun once the
    # margin counts against the 90-day cap) and are on disk, so a retry does not re-buy them.
    for month in months_in(date(2026, 1, 1), date(2026, 4, 30)):
        assert load_cached_month(cache, "acc-1", month, ttl=None) is not None
    assert load_cached_month(cache, "acc-1", date(2026, 5, 1), ttl=None) is None


def test_refresh_refetches_a_month_already_held(private_key_pem: bytes, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    today = date(2026, 6, 2)
    _windowed_fetch(private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 5, 31), today)
    again = _windowed_fetch(
        private_key_pem, tmp_path, cache, date(2026, 5, 1), date(2026, 5, 31), today, refresh=True
    )
    assert again == [("2026-04-24", "2026-05-31")]


def test_no_missing_months_needs_no_requests() -> None:
    assert _request_spans([], date(2026, 1, 1), date(2026, 1, 31)) == []


def _crosser_handler(transactions: list[dict[str, object]]):
    """A handler returning a fixed transaction list, whatever window is asked for."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["acc-1"]})
        if path.endswith("/balances"):
            return httpx.Response(200, json={"balances": _PLN_BALANCE_5})
        if path.endswith("/transactions"):
            return httpx.Response(
                200, json={"transactions": transactions, "continuation_key": None}
            )
        return httpx.Response(404)

    return handler


def _fetch_may(
    private_key_pem: bytes,
    out: Path,
    cache: Path,
    transactions: list[dict[str, object]],
    warnings: list[FetchWarning] | None = None,
) -> list[Path]:
    written, _ = fetch_bank(
        _client(private_key_pem, _crosser_handler(transactions)),
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        output_dir=out,
        # A synthetic IBAN (the AGENTS.md recipe: unassigned bank code 99999999, checksum-
        # valid), stable so the stale-identity warning cannot drown the one under test.
        accounts={
            "acc-1": LinkedAccount("acc-1", iban="PL73999999990000000000000006", currency="PLN")
        },
        cache_dir=cache,
        today=date(2026, 6, 2),
        warn=warnings.append if warnings is not None else _noop_warn,
    )
    return written


def test_a_returned_entry_booked_outside_the_span_is_cached_not_discarded(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Issue #35's second edge, ours alone: the ASPSP windows on transaction_date where that is
    observable, so a May request legitimately returns an entry booked June 2nd - and the old
    months_in(span) narrowing threw it away after paying a counted request for it. It is filed
    into its booking month instead, claiming nothing there (a crosser proves its own existence,
    not that its day was served in full), and stays out of this window's statement."""
    crosser = {
        "transaction_id": "crosser",
        "booking_date": "2026-06-02",
        "transaction_date": "2026-05-30",
        "transaction_amount": {"currency": "PLN", "amount": "2.00"},
        "credit_debit_indicator": "CRDT",
    }
    in_may = {
        "transaction_id": "in-may",
        "booking_date": "2026-05-20",
        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
        "credit_debit_indicator": "CRDT",
    }
    written = _fetch_may(private_key_pem, tmp_path, tmp_path / "cache", [in_may, crosser])

    content = written[0].read_text(encoding="utf-8")
    assert "in-may" in content
    assert "crosser" not in content  # booked outside the window; the OFX window did not move

    may = load_cached_month(tmp_path / "cache", "acc-1", date(2026, 5, 1), ttl=None)
    assert may is not None
    assert (may.covered_from, may.covered_to) == (date(2026, 5, 1), date(2026, 5, 31))
    assert [t["transaction_id"] for t in may.transactions] == ["in-may"]

    june = load_cached_month(tmp_path / "cache", "acc-1", date(2026, 6, 1), ttl=None)
    assert june is not None
    assert [t["transaction_id"] for t in june.transactions] == ["crosser"]
    # Held, but claiming nothing: a June window still fetches June (and then merges).
    assert (june.covered_from, june.covered_to) == (None, None)


def test_a_freshly_amended_copy_beats_the_cached_one_in_the_statement(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The wire margin deliberately re-asks a served month's tail, so a cached entry can come
    back amended under the same FITID. The statement must carry the fresh copy: the cache merge
    already keeps it, and a file that disagreed with the cache would import stale details under
    a FITID GnuCash then refuses to correct on any later import."""
    cache = tmp_path / "cache"
    today = date(2026, 6, 2)

    def entry(detail: str) -> dict[str, object]:
        return {
            "transaction_id": "amended",
            "booking_date": "2026-05-28",
            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
            "credit_debit_indicator": "CRDT",
            "remittance_information": [detail],
        }

    def handler_for(detail: str):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.startswith("/sessions/"):
                return httpx.Response(200, json={"accounts": ["acc-1"]})
            if path.endswith("/balances"):
                return httpx.Response(200, json={"balances": _PLN_BALANCE_5})
            if path.endswith("/transactions"):
                return httpx.Response(
                    200, json={"transactions": [entry(detail)], "continuation_key": None}
                )
            return httpx.Response(404)

        return handler

    def fetch(handler, date_to: date) -> list[Path]:
        written, _ = fetch_bank(
            _client(private_key_pem, handler),
            bank_key="alior",
            session_id="sess-1",
            date_from=date(2026, 5, 1),
            date_to=date_to,
            output_dir=tmp_path,
            accounts={"acc-1": LinkedAccount("acc-1", currency="PLN")},
            cache_dir=cache,
            today=today,
        )
        return written

    fetch(handler_for("ORIGINAL-DETAIL"), date(2026, 5, 31))
    # The second window adds June; its span's wire (May 25th on) re-returns the May 28th entry,
    # now amended, while the cached May chunk still serves the original alongside it.
    written = fetch(handler_for("AMENDED-DETAIL"), date(2026, 6, 1))
    content = written[0].read_text(encoding="utf-8")
    assert "AMENDED-DETAIL" in content
    assert "ORIGINAL-DETAIL" not in content


def test_the_deduplicated_copy_keeps_the_first_copys_position(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The other half of the dedup invariant: the later copy's *content* wins while the entry
    stays where the first copy sat. The writer emits transactions in list order, so a
    last-wins-position dedup would visibly reorder the statement whenever the margin re-serves
    a duplicate."""
    cache = tmp_path / "cache"
    today = date(2026, 6, 2)

    def txn(tid: str, day: str, detail: str) -> dict[str, object]:
        return {
            "transaction_id": tid,
            "booking_date": day,
            "transaction_amount": {"currency": "PLN", "amount": "1.00"},
            "credit_debit_indicator": "CRDT",
            "remittance_information": [detail],
        }

    def fetch(transactions: list[dict[str, object]], date_to: date) -> list[Path]:
        written, _ = fetch_bank(
            _client(private_key_pem, _crosser_handler(transactions)),
            bank_key="alior",
            session_id="sess-1",
            date_from=date(2026, 5, 1),
            date_to=date_to,
            output_dir=tmp_path,
            accounts={"acc-1": LinkedAccount("acc-1", currency="PLN")},
            cache_dir=cache,
            today=today,
        )
        return written

    fetch(
        [
            txn("id-before", "2026-05-26", "NEIGHBOUR-BEFORE"),
            txn("id-dup", "2026-05-27", "DUP-ORIGINAL"),
            txn("id-after", "2026-05-28", "NEIGHBOUR-AFTER"),
        ],
        date(2026, 5, 31),
    )
    # The second window's June span re-returns only the duplicate, amended, via the wire margin.
    written = fetch([txn("id-dup", "2026-05-27", "DUP-AMENDED")], date(2026, 6, 1))
    content = written[0].read_text(encoding="utf-8")
    assert "DUP-AMENDED" in content and "DUP-ORIGINAL" not in content
    positions = [content.index(f"<FITID>{tid}") for tid in ("id-before", "id-dup", "id-after")]
    assert positions == sorted(positions)


def test_a_booking_lag_past_the_margin_raises_a_warning(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """TRANSACTION_DATE_MARGIN is a policy value; this warning is its named revisit trigger
    (ADR decision 5). A warning and never a failure - this run's files are correct; it is some
    other, narrower window that may have a hole."""
    laggard = {
        "transaction_id": "laggard",
        "booking_date": "2026-05-20",
        "transaction_date": "2026-05-05",  # 15 days > the 7-day margin
        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
        "credit_debit_indicator": "CRDT",
    }
    warnings: list[FetchWarning] = []
    _fetch_may(private_key_pem, tmp_path, tmp_path / "cache", [laggard], warnings)
    assert [w.kind for w in warnings] == ["coverage"]
    assert "15 day(s)" in warnings[0].message

    # Only a *fetched* entry can testify: the same window served from cache warns again neither.
    warnings.clear()
    _fetch_may(private_key_pem, tmp_path, tmp_path / "cache", [laggard], warnings)
    assert warnings == []


def test_a_booking_lag_within_the_margin_is_silent(private_key_pem: bytes, tmp_path: Path) -> None:
    prompt = {
        "transaction_id": "prompt",
        "booking_date": "2026-05-20",
        "transaction_date": "2026-05-14",  # 6 days, inside the margin
        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
        "credit_debit_indicator": "CRDT",
    }
    warnings: list[FetchWarning] = []
    _fetch_may(private_key_pem, tmp_path, tmp_path / "cache", [prompt], warnings)
    assert warnings == []


def test_a_run_converts_an_older_versions_cache_before_fetching(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """Discarding the old layout would make the upgrade itself cost a full re-fetch, which is a
    strange way to deliver a rate-limit fix. Converting is one-shot and spends nothing."""
    save_session(
        tmp_path,
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("acc-pln"),)),
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "legacy.json").write_text(
        json.dumps(
            {
                "uid": "acc-pln",
                "date_from": "2026-05-01",
                "date_to": "2026-05-31",
                "fetched_at": "2026-05-31T12:00:00+00:00",
                "balances": {},
                "transactions": [],
            }
        ),
        encoding="utf-8",
    )
    config = AppConfig(
        output_dir=tmp_path / "out", state_dir=tmp_path, banks={"alior": _ALIOR}, cache_dir=cache
    )
    seen: list[str] = []
    fetch_enablebanking(
        config,
        _client(private_key_pem, _fetch_handler),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        progress=seen.append,
    )
    assert any("converted 1 cache entr" in line for line in seen)
    assert load_cached_month(cache, "acc-pln", date(2026, 5, 1), ttl=None) is not None
    assert not (cache / "legacy.json").exists()


# --------------------------------------------------------------------------- dry run

# _IBAN_A / _IBAN_B (synthetic, checksum-valid) are reused from the fetch_bank tests above: their
# last four characters are what the filename disambiguator is built from, and using the same pair
# keeps the predicted names directly comparable with the ones a real fetch writes.


def _dry_run_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_key_pem: bytes
) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(private_key_pem)
    monkeypatch.setenv("EB_APP_ID", "app-x")
    monkeypatch.setenv("EB_PRIVATE_KEY", str(key))


def _dry_run_state(tmp_path: Path, *accounts: LinkedAccount) -> None:
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=date(2026, 12, 31),
            accounts=accounts,
        ),
    )


def _two_pln_accounts() -> tuple[LinkedAccount, ...]:
    return (
        LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"),
        LinkedAccount(uid="acc-b", iban=_IBAN_B, currency="PLN"),
    )


def _dry_run_comparison_handler(request: httpx.Request) -> httpx.Response:
    """Two same-currency accounts, only one of which has transactions in the window."""
    path = request.url.path
    if path == "/sessions/sess-1":
        return httpx.Response(200, json={"session_id": "sess-1", "accounts": ["acc-a", "acc-b"]})
    if path == "/accounts/acc-a/transactions":
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "t1",
                        "booking_date": "2026-05-10",
                        "transaction_amount": {"currency": "PLN", "amount": "100.00"},
                        "credit_debit_indicator": "DBIT",
                    }
                ],
                "continuation_key": None,
            },
        )
    if path == "/accounts/acc-b/transactions":
        return httpx.Response(200, json={"transactions": [], "continuation_key": None})
    return httpx.Response(404)


def test_dry_run_predicts_exactly_the_paths_a_real_fetch_writes(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the dry run is that the prediction is the real thing.

    The hard case is the disambiguator, which is grouped over every account *in the session* - so
    a bank with two PLN accounts names both files with a suffix even when only one of them has
    transactions this period. A dry run that grouped over "accounts that would produce a file"
    would predict the un-suffixed name, and be wrong exactly where a prediction is worth having.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, *_two_pln_accounts())
    config = _config({"alior": _ALIOR}, tmp_path)

    report = dry_run_enablebanking(
        config,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )

    client = _client(private_key_pem, _dry_run_comparison_handler)
    written, failures = fetch_bank(
        client,
        bank_key="alior",
        session_id="sess-1",
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        output_dir=config.output_dir,
        accounts={a.uid: a for a in _two_pln_accounts()},
        today=date(2026, 6, 15),
    )

    assert failures == []
    # Only acc-a had transactions, so only its file exists - and the dry run predicted that path.
    assert [p.name for p in written] == ["alior_PLN_0005_2026_05_01-2026_05_31.ofx"]
    assert written[0] in [planned.path for planned in report.planned]
    # Both accounts are predicted: which of them has transactions is not knowable locally, and
    # naming the file that will not be written is more honest than omitting it.
    assert [p.path.name for p in report.planned] == [
        "alior_PLN_0005_2026_05_01-2026_05_31.ofx",
        "alior_PLN_9999_2026_05_01-2026_05_31.ofx",
    ]
    assert [p.currency for p in report.planned] == ["PLN", "PLN"]
    assert report.problems == []


def test_dry_run_reports_the_account_redacted(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned[0].account == _redact_account(_IBAN_A)
    assert _IBAN_A not in report.planned[0].account


def test_dry_run_writes_nothing_and_reads_no_cache(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run must cost nothing: no cache entry, no output directory, no run log.

    The structural guarantee is that ``dry_run_enablebanking`` takes no client at all, so there is
    nothing there to call; this pins the local side effects a real fetch would have had.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    config = _config({"alior": _ALIOR}, tmp_path)

    dry_run_enablebanking(
        config,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )

    assert not config.cache_dir.exists()
    assert not config.output_dir.exists()
    assert not (config.state_dir / "fetch-log.jsonl").exists()


def test_dry_run_reports_a_lapsed_consent_rather_than_predicting_files(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=date(2026, 6, 1),
            accounts=(LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"),),
        ),
    )
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned == []
    assert [f.operation for f in report.problems] == ["consent"]
    assert "expired" in report.problems[0].message


def test_dry_run_reports_an_unlinked_bank(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned == []
    assert "not linked" in report.problems[0].message


def test_dry_run_reports_a_corrupt_state_file(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    (tmp_path / "alior.json").write_text("{ not valid json", encoding="utf-8")
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned == []
    assert "not a valid session file" in report.problems[0].message


def test_dry_run_cannot_predict_a_path_without_a_link_time_currency(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session linked under the v1 schema has no currency, and the currency is in the filename.

    A real fetch learns it from ``/balances``; a dry run may not make that call, so the honest
    answer is that the path is unknown - not a guess the fetch will not honour.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A))
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned == []
    assert [f.scope for f in report.problems] == ["account"]
    assert report.problems[0].account == _redact_account(_IBAN_A)
    assert "currency" in report.problems[0].message


def test_dry_run_rejects_an_unconfigured_bank(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    with pytest.raises(RunError, match="not a configured Enable Banking bank"):
        dry_run_enablebanking(
            _config({"alior": _ALIOR}, tmp_path),
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
            only="nope",
            today=date(2026, 6, 15),
        )


def test_dry_run_rejects_a_reversed_window(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rejection as a real fetch, in the command whose job is to find this first.

    A dry run that faithfully predicted ``..._2026_07_31-2026_07_01.ofx`` and exited 0 would be
    telling the user the window is fine, which is the one answer it must never give: the next
    thing they run is the fetch that spends the allowance to learn otherwise.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    with pytest.raises(RunError, match="--from 2026-07-31 is after --to 2026-07-01"):
        dry_run_enablebanking(
            _config({"alior": _ALIOR}, tmp_path),
            date_from=date(2026, 7, 31),
            date_to=date(2026, 7, 1),
            today=date(2026, 8, 9),
        )


def test_dry_run_accepts_a_single_day_window(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--from`` equal to ``--to`` is one day, not a reversed range: it must still be predicted."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 1),
        today=date(2026, 6, 15),
    )
    assert [p.path.name for p in report.planned] == ["alior_PLN_2026_05_01-2026_05_01.ofx"]
    assert report.problems == []


def test_batched_dry_run_predicts_no_paths_and_narrates_none(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """decision 6, suppressed at the source: neither the report nor stderr may name a path.

    The report half alone is not enough - _planned_files narrates each predicted path through
    ``progress``, so clearing ``planned`` afterwards would leave stderr printing concrete paths a
    few lines before the CLI explains that no paths were predicted. The suppression has to reach
    the narration, which is what this pins.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    lines: list[str] = []
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        progress=lines.append,
        batch_size=25,
    )
    assert report.planned == []
    assert report.problems == []
    assert [line for line in lines if ".ofx" in line] == []


def test_batched_combined_dry_run_does_not_announce_the_combined_path(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """decision 6 is global: no paths at all, so not the combined one either.

    The combined path alone is derivable under --batch-size - the fetch names it from the
    resolved windows, not the batch-dated items - but one path announced while the CLI explains
    that none could be predicted would be the same stderr contradiction in the other direction.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    lines: list[str] = []
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        progress=lines.append,
        combine=True,
        batch_size=25,
    )
    assert report.planned == []
    assert [line for line in lines if "combined" in line or ".ofx" in line] == []


def test_batched_dry_run_still_reports_problems(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppressing paths must not suppress findings: a lapsed consent survives decision 6."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=date(2026, 6, 1),
            accounts=(LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"),),
        ),
    )
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        batch_size=25,
    )
    assert report.planned == []
    assert [f.operation for f in report.problems] == ["consent"]


def test_dry_run_requires_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing credentials is a global RunError even here, where nothing would be sent.

    The dry run is the right place to find out that the run cannot happen at all; it just stops
    before anything is constructed from the secrets.
    """
    monkeypatch.delenv("EB_APP_ID", raising=False)
    monkeypatch.delenv("EB_PRIVATE_KEY", raising=False)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    with pytest.raises(RunError, match="EB_APP_ID"):
        dry_run_enablebanking(
            _config({"alior": _ALIOR}, tmp_path),
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
            today=date(2026, 6, 15),
        )


def test_dry_run_reports_progress_per_bank_and_account(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, *_two_pln_accounts())
    seen: list[str] = []
    dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        progress=seen.append,
    )
    joined = "\n".join(seen)
    assert "2 account(s)" in joined
    assert _redact_account(_IBAN_A) in joined
    assert "(PLN)" in joined
    assert _IBAN_A not in joined


def test_dry_run_only_covers_enable_banking_banks(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    banks = {
        "alior": _ALIOR,
        "wise_personal": BankConfig("wise_personal", "wise", {"token_env": "X"}),
    }
    report = dry_run_enablebanking(
        _config(banks, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    # Only 'alior' is consent-based; the other source is not linked through this command at all.
    assert [p.bank_key for p in report.problems] == ["alior"]


def test_dry_run_uses_the_configured_bankid_in_the_predicted_file(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bankid`` never reaches the filename, but it does reach the Account the path is built from.

    Passing it keeps the dry run's Account identical to the one a fetch builds, so the prediction
    cannot drift if the filename ever starts depending on more of it.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    bank = BankConfig(
        "alior", "enablebanking", {"aspsp": "Alior Bank", "country": "PL", "bankid": "ALBPPLPW"}
    )
    report = dry_run_enablebanking(
        _config({"alior": bank}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned[0].path.name == "alior_PLN_2026_05_01-2026_05_31.ofx"


def test_dry_run_reports_a_currency_that_cannot_be_a_filename(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A currency that is not a safe path component fails the fetch too - just later, and after
    the requests have been paid for. Surfacing it here is the point, so it is a reported problem
    rather than a traceback out of the middle of a run."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="../PLN"))
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned == []
    assert "no output filename could be formed" in report.problems[0].message
    assert report.problems[0].account == _redact_account(_IBAN_A)


def test_dry_run_predicts_nothing_for_a_bank_with_one_unknown_currency(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling of unknown currency changes the names of the accounts that *are* known.

    Caught against real output: a bank with two PLN accounts writes suffixed filenames, but if one
    of them has no link-time currency and is merely dropped, the group shrinks to one and the
    prediction loses the suffix - a name the fetch never writes. Joining a group can also flip it
    from IBAN tails to digests, so the unknown account can change the others' names outright, not
    only add to them. So the whole bank goes unpredicted.
    """
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(
        tmp_path,
        LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"),
        LinkedAccount(uid="acc-b", iban=_IBAN_B),  # v1 record: no currency
    )
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert report.planned == []
    scopes = [problem.scope for problem in report.problems]
    assert scopes == ["account", "bank"]
    assert report.problems[0].account == _redact_account(_IBAN_B)
    assert "can change its siblings' filenames" in report.problems[1].message


def test_dry_run_still_predicts_when_every_currency_is_known(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suppression above is scoped to real doubt: a fully-known bank still gets its paths."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, *_two_pln_accounts())
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert len(report.planned) == 2
    assert report.problems == []


def test_dry_run_predicts_the_refusal_and_keeps_the_siblings_names(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` must not predict a file the fetch would refuse to write - and the refused
    account still shapes its sibling's disambiguator, exactly as an unfetched sibling does in
    the real fetch: the shape of the connection did not change."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(
        tmp_path,
        LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN", cash_account_type="CARD"),
        LinkedAccount(uid="acc-b", iban=_IBAN_B, currency="PLN", cash_account_type="CACC"),
    )
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    # The suffix survives: grouped over both PLN accounts, not only the writable one.
    assert [p.path.name for p in report.planned] == ["alior_PLN_9999_2026_05_01-2026_05_31.ofx"]
    assert [problem.operation for problem in report.problems] == ["account-type"]
    assert report.problems[0].scope == "account"
    assert report.problems[0].account == _redact_account(_IBAN_A)
    assert "CARD" in report.problems[0].message


def test_dry_run_reports_the_refusal_for_a_currencyless_account_too(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused account with no link-time currency is a refusal, not an unknown currency - and
    it must not suppress the bank: the fetch refuses before /balances could teach it a currency,
    so unlike an ordinary unknown-currency account it can never join a group mid-run and change
    its siblings' filenames. The sibling is predicted under its plain name, which is exactly what
    the fetch writes for this shape."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(
        tmp_path,
        LinkedAccount(uid="acc-a", iban=_IBAN_A, cash_account_type="CARD"),  # no currency
        LinkedAccount(uid="acc-b", iban=_IBAN_B, currency="PLN", cash_account_type="CACC"),
    )
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
    )
    assert [p.path.name for p in report.planned] == ["alior_PLN_2026_05_01-2026_05_31.ofx"]
    assert [problem.operation for problem in report.problems] == ["account-type"]
    assert report.problems[0].account == _redact_account(_IBAN_A)


def test_dry_run_combine_predicts_the_single_combined_path(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/adr-combined-ofx-file.md open question: --combine needs no account enumeration."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A, currency="PLN"))
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        combine=True,
    )
    assert [planned.path.name for planned in report.planned] == [
        "combined_2026_05_01-2026_05_31.ofx"
    ]
    assert report.problems == []


def test_dry_run_combine_predicts_even_with_an_unknown_currency(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the per-account name, the combined name carries no currency to get wrong."""
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    _dry_run_state(tmp_path, LinkedAccount(uid="acc-a", iban=_IBAN_A))  # no currency
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        combine=True,
    )
    assert len(report.planned) == 1


def test_dry_run_combine_predicts_nothing_when_no_bank_resolves(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dry_run_credentials(monkeypatch, tmp_path, private_key_pem)
    report = dry_run_enablebanking(
        _config({"alior": _ALIOR}, tmp_path),
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
        today=date(2026, 6, 15),
        combine=True,
    )
    assert report.planned == []
    assert report.problems[0].message.startswith("not linked")


def test_an_oversized_session_response_fails_the_bank_not_the_run(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """There is no account to blame before the account loop, so it must be bank-scoped.

    Left as a bare exception this escaped every handler: it cost the *other* banks their files and
    replaced the stdout file list with a traceback, which is the opposite of "one bank's failure
    must not cost another bank its files".
    """
    from gnucash_ofx.sources.enablebanking import _MAX_RESPONSE_BYTES

    padding = "x" * (_MAX_RESPONSE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a"], "pad": padding})
        return httpx.Response(200, json=_TXNS)

    with pytest.raises(BankError, match="safety limit"):
        _fetch(private_key_pem, tmp_path, handler)


def test_a_failed_conversion_pairing_still_writes_the_legs(
    private_key_pem: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing divides two amounts and quantizes, so an absurd magnitude raises there.

    `finite_decimal` bounds finiteness, not magnitude, so this sits outside its reach — and it runs
    after every per-account guard. The annotation is an enrichment, so losing it costs a memo line
    rather than a file; aborting would cost every bank in the run.
    """

    def boom(pending: Any) -> Any:
        raise ArithmeticError("quantize overflowed")

    monkeypatch.setattr("gnucash_ofx.run.pair_conversions", boom)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=_TXNS)

    warnings: list[Any] = []
    written, failures = _fetch(private_key_pem, tmp_path, handler, warnings=warnings)
    assert len(written) == 1  # the leg was still written
    assert failures == []
    ledger = [w for w in warnings if w.kind == "ledger"]
    assert len(ledger) == 1
    assert "pairing failed" in ledger[0].message


def test_one_page_budget_covers_every_span_of_an_account_window(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """The cached path fetches span by span, so a budget made per call gave each span a fresh 100.

    A gap in the middle of the cache splits one account-window into two spans. With the budget
    created inside `_fetch_spans` the real bound was `cap x spans`; it now lives in
    `_account_raw_data` and is shared, so the total request count stays inside one allowance.
    """
    from gnucash_ofx.cache import save_cached_month
    from gnucash_ofx.run import _account_raw_data
    from gnucash_ofx.sources.enablebanking import _MAX_PAGES_PER_ACCOUNT, ResponseLimitExceeded

    cache_dir = tmp_path / "cache"
    # Cache the middle month so the window splits into two request spans around it.
    save_cached_month(
        cache_dir, "uid-1", date(2026, 5, 1), date(2026, 7, 31), date(2026, 6, 30), []
    )

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        calls["n"] += 1
        # Always another page: without a shared budget this runs the cap once per span.
        return httpx.Response(200, json={"transactions": [], "continuation_key": "more"})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(application_id="app", private_key=private_key_pem, http=http)

    with pytest.raises(ResponseLimitExceeded):
        _account_raw_data(
            client,
            "uid-1",
            date(2026, 5, 1),
            date(2026, 7, 31),
            cache_dir=cache_dir,
            cache_ttl=timedelta(hours=6),
            refresh=False,
            progress=lambda _m: None,
            label="test",
            ledger_balance_wanted=False,
            currency_known=True,
            today=date(2026, 8, 1),
            request_floor=None,
            bank_key="alior",
            account="acc",
            warn=lambda _w: None,
        )

    # One allowance for the whole account-window, not one per span.
    assert calls["n"] == _MAX_PAGES_PER_ACCOUNT


def test_an_unusable_identifier_costs_only_its_own_account(
    private_key_pem: bytes, tmp_path: Path
) -> None:
    """decision 4b: validated at the mapping step, so nothing is recorded and no sibling suffers.

    Checked here rather than in the writer on purpose: the write loop has no per-account guard and
    coverage has already advanced by the time it runs, so a rejection there would abort the run
    *and* leave the ledger claiming a day whose file was never written.
    """
    bad = {
        "transactions": [
            {
                # A control character in the entry reference, which becomes the FITID raw.
                "entry_reference": "ref\x02one",
                "booking_date": "2026-06-10",
                "transaction_amount": {"currency": "PLN", "amount": "1.00"},
                "credit_debit_indicator": "CRDT",
            }
        ],
        "continuation_key": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"accounts": ["a", "b"]})
        if path.endswith("balances"):
            return httpx.Response(200, json=_BAL)
        return httpx.Response(200, json=bad if path.startswith("/accounts/b/") else _TXNS)

    # A real ledger, so "advances no coverage" is actually checked rather than asserted into a void:
    # without state_dir there is nothing to record into, and a regression that moved the validation
    # after _persist_coverage but before the write would still have passed.
    state_dir = tmp_path / "state"
    written, failures = _fetch(
        private_key_pem,
        tmp_path,
        handler,
        {u: LinkedAccount(u, currency="PLN") for u in ("a", "b")},
        state_dir=state_dir,
    )
    assert len(written) == 1  # the sibling still got its file
    assert [(f.scope, f.operation) for f in failures] == [("account", "mapping")]
    # The rejected identifier is described, never quoted, because it reaches the stderr summary.
    assert "U+0002" in (failures[0].payload or "")
    assert "ref" not in (failures[0].payload or "")

    # The good account earned coverage; the refused one earned none, so the next run refetches it.
    from gnucash_ofx.coverage import load_coverage

    coverage = load_coverage(state_dir, "alior")
    assert coverage is not None
    covered = {key: spans for key, spans in coverage.accounts.items() if spans}
    assert len(covered) == 1
