"""TDD for the Enable Banking transaction mapper (pure, no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gnucash_ofx.models import Txn
from gnucash_ofx.sources.enablebanking import (
    _iban_checksum_ok,
    account_identifier,
    identification_hashes,
    linked_accounts,
    map_transaction,
    map_transactions,
    normalize_account_number,
    scrub_session,
)

FIXTURE = Path(__file__).parent / "fixtures" / "enablebanking_transactions.json"


def _raw() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["transactions"]


def test_maps_booked_and_excludes_pending() -> None:
    txns = map_transactions(_raw(), currency="PLN")
    # 8 booked, the PDNG one is dropped by default.
    assert [t.id for t in txns] == [
        "tx-eb-1001",
        "tx-eb-1002",
        "2026052500003",
        "TRWI-9000000001-BALANCE-0004",
        "TRWI-9000000002-BALANCE-0005",
        "TRWI-9000000003-BALANCE-0006",
        "TRWI-9000000004-BALANCE-0007",
        "TRWI-9000000005-BALANCE-0008",
        "TRWI-9100000001-BALANCE-0010",
        "TRWI-9100000002-BALANCE-0011",
        "TRWI-9100000003-BALANCE-0012",
        "2026060100006",
    ]


def test_can_include_pending() -> None:
    txns = map_transactions(_raw(), currency="PLN", booked_only=False)
    assert any(t.id == "tx-eb-pending" for t in txns)


def test_debit_is_negative_credit_is_positive() -> None:
    # AGENTS.md: "Signed amounts: credits positive, debits negative."
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    assert txns["tx-eb-1001"].amount == Decimal("-42.50")  # DBIT
    assert txns["tx-eb-1002"].amount == Decimal("3500.00")  # CRDT


def test_payee_is_counterparty_by_direction() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    # Outgoing debit -> creditor is the payee.
    assert txns["tx-eb-1001"].payee == "Biedronka Sp. z o.o."
    # Incoming credit -> debtor is the payer.
    assert txns["tx-eb-1002"].payee == "ACME Sp. z o.o."


def test_memo_joins_remittance_lines() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    assert txns["tx-eb-1001"].memo == "Card payment BIEDRONKA 1234 KRAKOW"
    assert txns["2026052500003"].memo is None  # empty remittance


def test_counterparty_iban_extracted_separately() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    # tx-eb-1001 is a DBIT — creditor_account.iban is the counterparty.
    assert txns["tx-eb-1001"].counterparty_iban == "PL00000000000000000000001"
    # tx-eb-1002 has no debtor_account.
    assert txns["tx-eb-1002"].counterparty_iban is None


def test_counterparty_iban_falls_back_to_other_identification() -> None:
    # Some ASPSPs (e.g. Bank Millennium) leave iban null and put the account number under
    # other.identification (even when it is IBAN-formatted, scheme_name may say "BBAN").
    raw = {
        "transaction_id": "m1",
        "booking_date": "2026-06-19",
        "transaction_amount": {"currency": "PLN", "amount": "26.32"},
        "credit_debit_indicator": "DBIT",
        "creditor_account": {
            "iban": None,
            # Synthetic IBAN (see AGENTS.md): bank code 99999999 is unassigned.
            "other": {"identification": "PL14999999990000000000000001", "scheme_name": "BBAN"},
        },
    }
    assert map_transaction(raw, "PLN").counterparty_iban == "PL14999999990000000000000001"


# -------------------------------------------------------------------- machine reference split


def _wise(*remittance: str) -> dict:
    return {
        "entry_reference": "TRWI-9000000009-BALANCE-0009",
        "booking_date": "2026-05-27",
        "transaction_amount": {"currency": "PLN", "amount": "820.00"},
        "credit_debit_indicator": "DBIT",
        "remittance_information": list(remittance),
    }


def test_machine_reference_leaves_the_remittance() -> None:
    # Wise sends its transfer id as its own remittance element. Only the id survives — the
    # "TRANSFER-" prefix is a constant, and it would not fit CHECKNUM's 12 characters.
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    txn = txns["TRWI-9000000001-BALANCE-0004"]
    assert txn.reference == "9000000001"
    assert txn.memo == "Invoice FS 1/05/2026"  # prose only, no leading machine token
    # And nothing changes for a bank that never sends one.
    assert txns["tx-eb-1001"].reference is None


def test_machine_reference_alone_leaves_no_memo() -> None:
    txn = map_transaction(_wise("TRANSFER-9000000001"), "PLN")
    assert txn.reference == "9000000001"
    assert txn.memo is None


def test_machine_reference_is_matched_by_element_not_by_position() -> None:
    txn = map_transaction(_wise("Invoice FS 1/05/2026", "TRANSFER-9000000001"), "PLN")
    assert txn.reference == "9000000001"
    assert txn.memo == "Invoice FS 1/05/2026"


def test_card_payment_reference_is_lifted_like_a_transfer() -> None:
    # Card payments are the common case on a personal Wise balance and carry the same shape.
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    txn = txns["TRWI-9000000003-BALANCE-0006"]
    assert txn.reference == "9000000003"
    assert txn.memo == "Card payment to EXAMPLE STORE KRAKOW"


def test_cashback_reference_is_lifted_even_though_it_cannot_be_emitted() -> None:
    # The UUID cannot fit CHECKNUM, and it does not stay in the Description because of that: an
    # opaque id is not text. compose_check_number() drops it, so the value is simply gone.
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    txn = txns["TRWI-9000000005-BALANCE-0008"]
    assert txn.reference == "9a1e07a1-fe12-4a3b-8c5d-01cb2dd3ce45"
    assert txn.memo == "Cashback"


def test_a_fee_rows_reference_is_removed_rather_than_kept_or_emitted() -> None:
    # A fee row leads with the id of the transaction it is charged for. It cannot be this row's
    # CHECKNUM — it identifies a different transaction — and it is not text, so it goes. The prose
    # beside it keeps the link, which is why nothing is lost.
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    fee = txns["TRWI-9000000004-BALANCE-0007"]
    assert fee.reference is None
    assert fee.memo == "Wise Charges for: CARD-9000000003"

    transfer_fee = txns["TRWI-9000000002-BALANCE-0005"]
    assert transfer_fee.reference is None
    assert transfer_fee.memo == "Wise Charges for: TRANSFER-9000000001"


def test_reference_inside_prose_is_left_alone() -> None:
    # Only whole elements are matched. Stripping the id out of surrounding prose would gut the one
    # place it reads as part of a sentence.
    txn = map_transaction(_wise("Wise Charges for: CARD-9000000003"), "PLN")
    assert txn.reference is None
    assert txn.memo == "Wise Charges for: CARD-9000000003"


def test_an_entity_id_is_not_a_payment_reference() -> None:
    # BALANCE-<digits> is the same shape as CARD-<digits> but identifies the balance, not the
    # payment — it recurs across transactions, so it is a matcher signal worth keeping. Likewise
    # ACCRUAL_CHECKOUT-invoice-<digits>, whose prefix says something to a reader.
    assert map_transaction(_wise("BALANCE-9000000001"), "PLN").reference is None
    assert map_transaction(_wise("BALANCE-9000000001"), "PLN").memo == "BALANCE-9000000001"
    assert map_transaction(_wise("ACCRUAL_CHECKOUT-invoice-90000001"), "PLN").reference is None


def test_only_a_whole_element_of_the_expected_shape_is_a_reference() -> None:
    assert map_transaction(_wise("TRANSFER-ABC"), "PLN").reference is None
    assert map_transaction(_wise("TRANSFER-9000000001 paid"), "PLN").reference is None
    assert map_transaction(_wise("CARD-"), "PLN").reference is None
    # A UUID shape is only accepted for the prefix that actually uses one.
    assert map_transaction(_wise("CARD-9a1e07a1-fe12-4a3b-8c5d-01cb2dd3ce45"), "PLN").reference is (
        None
    )
    assert map_transaction(_wise("Coffee"), "PLN").reference is None


def test_the_first_own_reference_wins_and_the_second_stays_prose() -> None:
    # Not observed, but the rule has to be defined: one transaction has one id.
    txn = map_transaction(_wise("CARD-9000000003", "CARD-9000000004", "Coffee"), "PLN")
    assert txn.reference == "9000000003"
    assert txn.memo == "CARD-9000000004 Coffee"


# ---------------------------------------------------------------- conversion deal key (#5)


def test_wise_conversion_leg_gets_a_namespaced_key() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    assert txns["TRWI-9100000001-BALANCE-0010"].conversion_key == "wise:BALANCE-9100000001"


def test_wise_fee_row_gets_no_conversion_key() -> None:
    # FEE-BALANCE-<n> never fullmatches BALANCE-(\d+), so the fee leg is excluded without any
    # separate flag — and its remittance is untouched by _FOREIGN_REFERENCE either way.
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    fee = txns["TRWI-9100000002-BALANCE-0011"]
    assert fee.conversion_key is None
    assert fee.memo == "Wise Charges for: BALANCE-9100000001"


def test_conversion_code_with_a_non_matching_reference_gets_no_key() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    assert txns["TRWI-9100000003-BALANCE-0012"].conversion_key is None


def test_balance_reference_without_the_conversion_code_gets_no_key() -> None:
    # BALANCE-<digits> alone recurs on ordinary Wise transactions (it names the balance, not the
    # deal) and stays matcher signal in the memo — only the CONVERSION code turns it into a key.
    txn = map_transaction(_wise("BALANCE-9000000001"), "PLN")
    assert txn.conversion_key is None
    assert txn.memo == "BALANCE-9000000001"


def test_kantor_prose_gets_a_key_and_the_remittance_is_left_intact() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    txn = txns["2026060100006"]
    assert txn.conversion_key == "kantor:220"
    # Extraction is read-only: nothing is lifted out of the remittance, unlike _split_reference.
    assert (
        txn.memo
        == "Rozliczenie transakcji Kantor Walutowy 220 Rozliczenie transakcji wymiany walut"
    )


def test_an_ordinary_transaction_has_no_conversion_key() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    assert txns["tx-eb-1001"].conversion_key is None


# ---------------------------------------------------------------- Revolut deal key (#50)

_REVOLUT_FIXTURE = Path(__file__).parent / "fixtures" / "revolut_transactions.json"


def _revolut_pockets() -> dict[str, list[dict]]:
    data = json.loads(_REVOLUT_FIXTURE.read_text(encoding="utf-8"))
    return {uid: body["transactions"] for uid, body in data.items() if uid != "_comment"}


def test_revolut_exchange_legs_share_a_namespaced_key() -> None:
    # Both legs of a deal carry one byte-identical entry_reference (fixture property 2); the key
    # is that reference namespaced, so it can never collide with a wise:/kantor: key. The same
    # field is also the FITID source — extraction is read-only, so id and key simply coincide.
    pockets = _revolut_pockets()
    eur = {t.id: t for t in map_transactions(pockets["acc-rev-eur"], currency="EUR")}
    usd = {t.id: t for t in map_transactions(pockets["acc-rev-usd"], currency="USD")}
    shared = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
    assert eur[shared].conversion_key == f"revolut:{shared}"
    assert usd[shared].conversion_key == f"revolut:{shared}"


def test_revolut_non_exchange_rows_get_no_key() -> None:
    # TOPUP and TRANSFER are not conversion legs — and the vocabulary is not closed, so an
    # unknown code simply does not fire the rule (docs/adr-revolut-exchange-pairing.md
    # decision 1); this test asserts nothing about which codes exist.
    pockets = _revolut_pockets()
    aud = map_transactions(pockets["acc-rev-aud"], currency="AUD")  # TOPUP
    pln = {t.id: t for t in map_transactions(pockets["acc-rev-pln"], currency="PLN")}
    assert [t.conversion_key for t in aud] == [None]
    assert pln["eeeeeeee-5555-4eee-8eee-eeeeeeeeeeee"].conversion_key is None  # TRANSFER


def test_an_exchange_row_without_an_entry_reference_gets_no_key() -> None:
    # There is nothing to join on, and an empty key must not become one: two reference-less
    # EXCHANGE rows sharing "revolut:" would be a false group handed to pair_conversions.
    # No key means the leg is left untouched — today's behaviour.
    raw = {
        "booking_date": "2026-06-10",
        "transaction_amount": {"currency": "EUR", "amount": "100.00"},
        "credit_debit_indicator": "DBIT",
        "bank_transaction_code": {"code": "EXCHANGE", "sub_code": None},
        "remittance_information": ["Exchanged to USD"],
    }
    assert map_transaction(raw, "EUR").conversion_key is None


def test_fitid_hash_basis_keeps_the_machine_reference() -> None:
    # The fallback hash must not shift because the reference moved out of the memo: a source with
    # no ids of its own would see every FITID change, and GnuCash would re-import everything.
    # Golden values, not a self-consistency check — that is the whole point.
    no_ids = _wise("TRANSFER-9000000001", "Invoice FS 1/05/2026")
    del no_ids["entry_reference"]
    assert map_transaction(no_ids, "PLN").id == "gen-e24388a9b3601c8c"

    without = _wise("Invoice FS 1/05/2026")
    del without["entry_reference"]
    assert map_transaction(without, "PLN").id == "gen-17e7bd6783547838"

    # A dropped *foreign* reference counts toward the hash too. It leaves the file entirely, so
    # this is the only place it still matters — and the FITID must not depend on that decision.
    fee = _wise("FEE-CARD-9000000003", "Wise Charges for: CARD-9000000003")
    del fee["entry_reference"]
    assert map_transaction(fee, "PLN").id == "gen-829ff43014304c50"


# --------------------------------------------------------------- account number normalization


def test_normalizes_polish_nrb_to_iban() -> None:
    # Alior reports counterparty accounts as a bare 26-digit NRB; the IBAN is the same with a
    # "PL" prefix. Normalizing lets the token match the same account's ACCTID elsewhere.
    # Synthetic, checksum-valid (see AGENTS.md) — the prefix is only added when mod-97 passes.
    assert normalize_account_number("84999999990000000000000002") == "PL84999999990000000000000002"


def test_leaves_iban_untouched() -> None:
    assert (
        normalize_account_number("PL57999999990000000000000003") == "PL57999999990000000000000003"
    )


def test_strips_whitespace_and_upcases() -> None:
    assert (
        normalize_account_number(" pl57 9999 9999 0000 0000 0000 0003 ")
        == "PL57999999990000000000000003"
    )


def test_does_not_prefix_when_checksum_fails() -> None:
    # 26 digits but not a valid Polish NRB -> returned as-is rather than mislabelled an IBAN.
    bogus = "12345678901234567890123456"
    assert normalize_account_number(bogus) == bogus


def test_does_not_prefix_wrong_length_digits() -> None:
    assert normalize_account_number("1234567890") == "1234567890"


def test_account_identifier_is_none_without_iban_or_other_identification() -> None:
    assert account_identifier({"iban": None, "other": {}}) is None
    assert account_identifier({}) is None
    assert account_identifier(None) is None


# _iban_checksum_ok has exactly one caller (normalize_account_number above), which only ever
# passes it a 28-char "PL" + 26-digit candidate - so these two guards are unreachable through any
# public path. They are tested directly because they protect a general ISO 13616 checksum
# algorithm's correctness (a short or non-alphanumeric input must not coincidentally "pass"),
# not because the branch is expected to fire in this project's own call graph.
def test_iban_checksum_ok_rejects_a_too_short_value() -> None:
    assert _iban_checksum_ok("PL1") is False


def test_iban_checksum_ok_rejects_a_non_alphanumeric_character() -> None:
    assert _iban_checksum_ok("PL03999999990000000000000!") is False


def test_counterparty_nrb_is_normalized_end_to_end() -> None:
    raw = {
        "transaction_id": "a1",
        "booking_date": "2026-08-05",
        "transaction_amount": {"currency": "EUR", "amount": "16100.00"},
        "credit_debit_indicator": "DBIT",
        "creditor_account": {
            "iban": None,
            # Synthetic, checksum-valid (see AGENTS.md).
            "other": {"identification": "30999999990000000000000004", "scheme_name": "BANK"},
        },
    }
    assert map_transaction(raw, "EUR").counterparty_iban == "PL30999999990000000000000004"


def test_date_and_currency() -> None:
    txns = {t.id: t for t in map_transactions(_raw(), currency="PLN")}
    assert txns["tx-eb-1001"].date == date(2026, 5, 4)
    assert txns["tx-eb-1001"].currency == "PLN"


def test_mapping_raises_when_no_date_is_present_at_all() -> None:
    # run.py's _txn_in_window docstring says such a transaction is "kept" (not silently dropped)
    # and that "the mapper will raise for them later, making the error explicit" - this is that.
    raw = {
        "transaction_id": "no-date",
        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
        "credit_debit_indicator": "DBIT",
    }
    with pytest.raises(ValueError, match="no booking/value/transaction date"):
        map_transaction(raw, "PLN")


def test_id_prefers_transaction_id_then_entry_reference() -> None:
    with_tid = {
        "transaction_id": "T1",
        "entry_reference": "E1",
        "booking_date": "2026-05-01",
        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
        "credit_debit_indicator": "DBIT",
    }
    assert map_transaction(with_tid, "PLN").id == "T1"

    only_ref = dict(with_tid)
    del only_ref["transaction_id"]
    assert map_transaction(only_ref, "PLN").id == "E1"


def test_currency_uses_account_argument_when_matching() -> None:
    raw = {
        "transaction_id": "T1",
        "booking_date": "2026-05-01",
        "transaction_amount": {"currency": "PLN", "amount": "1.00"},
        "credit_debit_indicator": "DBIT",
    }
    assert map_transaction(raw, "PLN").currency == "PLN"


def test_currency_mismatch_raises() -> None:
    raw = {
        "transaction_id": "T1",
        "booking_date": "2026-05-01",
        "transaction_amount": {"currency": "EUR", "amount": "1.00"},
        "credit_debit_indicator": "DBIT",
    }
    with pytest.raises(ValueError, match="does not match account currency"):
        map_transaction(raw, "PLN")


def test_zero_amount_debit_is_not_treated_as_credit() -> None:
    raw = {
        "transaction_id": "T0",
        "booking_date": "2026-05-01",
        "transaction_amount": {"currency": "PLN", "amount": "0.00"},
        "credit_debit_indicator": "DBIT",
        "creditor": {"name": "Fee Waiver"},
        "debtor": {"name": "Should Not Be Used"},
    }
    txn = map_transaction(raw, "PLN")
    # Direction is from the indicator: a DBIT -> payee is the creditor, even at zero amount.
    assert txn.payee == "Fee Waiver"


def test_id_falls_back_to_stable_hash_when_no_reference() -> None:
    raw = {
        "booking_date": "2026-05-01",
        "transaction_amount": {"currency": "PLN", "amount": "1.23"},
        "credit_debit_indicator": "DBIT",
        "remittance_information": ["Coffee"],
    }
    first = map_transaction(raw, "PLN").id
    second = map_transaction(dict(raw), "PLN").id
    assert first == second  # deterministic across runs
    assert first.startswith("gen-")


# --------------------------------------------------------------------------- linked_accounts

SESSION_FIXTURE = Path(__file__).parent / "fixtures" / "enablebanking_session.json"


def _session() -> dict:
    return json.loads(SESSION_FIXTURE.read_text(encoding="utf-8"))


def test_linked_accounts_captures_every_field() -> None:
    accounts = linked_accounts(_session())
    assert [a.uid for a in accounts] == ["acc-with-iban", "acc-with-nrb", "acc-no-identifier"]
    first = accounts[0]
    assert first.iban == "PL03999999990000000000000005"
    assert first.bic == "EXMPPLPW"
    assert first.currency == "PLN"
    assert first.identification_hash == "hash-with-iban"
    assert first.name == "Konto Osobiste"
    assert first.product == "Personal Current Account"
    assert first.usage == "PRIV"
    assert first.cash_account_type == "CACC"
    # An IBAN needs no scheme; the field describes the other.identification form only.
    assert first.identification_scheme is None


def test_linked_accounts_reads_domestic_number_and_its_scheme() -> None:
    """Several ASPSPs leave iban null and put a bare NRB under other.identification."""
    account = linked_accounts(_session())[1]
    # Normalized to IBAN form, because the checksum proves it is one.
    assert account.iban == "PL19999999990000000000009999"
    assert account.identification_scheme == "BBAN"
    assert account.currency == "EUR"
    assert account.product is None


def test_linked_accounts_tolerates_missing_identifiers() -> None:
    """A Wise balance can have no account_id, no servicer and no hash at all."""
    account = linked_accounts(_session())[2]
    assert account.iban is None
    assert account.bic is None
    assert account.currency == "USD"
    assert account.identification_hash is None  # ACCTID falls back to the uid for this one


def test_linked_accounts_scheme_is_none_without_other_or_a_scheme_name() -> None:
    """No iban and no other block, and no iban with an other block missing scheme_name."""
    session = {
        "accounts": [
            {"uid": "a1", "account_id": {"iban": None}},
            {"uid": "a2", "account_id": {"iban": None, "other": {"identification": "123"}}},
        ]
    }
    accounts = linked_accounts(session)
    assert accounts[0].identification_scheme is None
    assert accounts[1].identification_scheme is None


def test_identification_hashes_skips_non_dict_entries() -> None:
    session = {"accounts_data": ["not-a-dict", {"uid": "a1", "identification_hash": "h1"}]}
    assert identification_hashes(session) == {"a1": "h1"}


def test_linked_accounts_reads_hash_from_accounts_data_when_absent_on_the_account() -> None:
    """Defensive: the ``accounts_data`` shape belongs to GET /sessions, not POST.

    A real POST /sessions carries ``identification_hash`` on the account object and has no
    ``accounts_data`` array (verified August 2026). Reading both costs nothing and means a
    session response in either shape still yields a stable ACCTID.
    """
    session = {
        "accounts": [{"uid": "a1", "currency": "PLN"}],
        "accounts_data": [{"uid": "a1", "identification_hash": "hash-from-accounts-data"}],
    }
    assert linked_accounts(session)[0].identification_hash == "hash-from-accounts-data"


def test_linked_accounts_skips_entries_without_a_uid() -> None:
    session = {"accounts": [{"currency": "PLN"}, "bare-string", {"uid": "ok"}]}
    assert [a.uid for a in linked_accounts(session)] == ["ok"]


def test_linked_accounts_empty_when_no_accounts() -> None:
    assert linked_accounts({}) == ()
    assert linked_accounts({"accounts": None}) == ()


def test_linked_accounts_treats_xxx_currency_as_absent() -> None:
    """ISO 4217's "no currency" placeholder, seen from a real Alior re-link (2026-08-10).

    Trusting "XXX" as a known currency skips the /balances discovery fallback and corrupts
    every transaction fetched for the account — see docs/adr-xxx-currency-placeholder.md.
    """
    session = {"accounts": [{"uid": "a1", "currency": "XXX"}]}
    assert linked_accounts(session)[0].currency is None


def test_scrub_session_keeps_the_consent_block_but_drops_credentials() -> None:
    session = {"session_id": "s", "access": {"valid_until": "x"}, "refresh_token": "SECRET"}
    scrubbed = scrub_session(session)
    assert scrubbed == {"session_id": "s", "access": {"valid_until": "x"}}


# --------------------------------------------------------------------------- N26 shape (#33)

# N26's wire shape, measured 2026-08-13 over a real fetch (one account, a very small same-day
# sample — every "always" below means "in every transaction observed"): `transaction_id` is null
# with `entry_reference` a bare UUID, BOTH account sides carry an `iban` (own side included, like
# Millennium but under `iban` rather than `other.identification`), the remittance is a single
# prose element, and `bank_transaction_code` carries ISO 20022 tokens (`PMNT`/`ICDT`/`ESCT`)
# rather than Wise-style mechanism words. The fixture mirrors the full 24-key payload, explicit
# nulls included, so the mapper is proven against the wire shape rather than a trimmed one.
# Every value in it is invented: the DE IBANs are checksum-valid synthetics on all-zero BBAN
# stems, the UUIDs are fabricated, and the payees, amounts and dates match nothing real.

N26_FIXTURE = Path(__file__).parent / "fixtures" / "enablebanking_n26_transactions.json"

_N26_OWN_IBAN = "DE09000000000000000001"


def _n26_raw() -> list[dict]:
    return json.loads(N26_FIXTURE.read_text(encoding="utf-8"))["transactions"]


def _n26_txns() -> list[Txn]:
    return map_transactions(_n26_raw(), currency="EUR")


def test_n26_fitid_is_the_bare_uuid_entry_reference() -> None:
    # transaction_id is null on every N26 transaction, so _transaction_id falls through to
    # entry_reference — the FITID is a bare UUID, stable but uncallable against the
    # transaction-details endpoint (docs/enable-banking.md: entry_reference is not a substitute).
    assert [t.id for t in _n26_txns()] == [
        "0a1b2c3d-0001-4000-8000-000000000001",
        "0a1b2c3d-0002-4000-8000-000000000002",
    ]


def test_n26_own_account_never_becomes_the_counterparty() -> None:
    # N26 populates BOTH creditor_account and debtor_account on every transaction, own side
    # included. Reading the side the direction names is what keeps our own IBAN out of the memo.
    debit, credit = _n26_txns()
    assert debit.counterparty_iban == "DE79000000000000000002"
    assert credit.counterparty_iban == "DE52000000000000000003"
    assert _N26_OWN_IBAN not in {debit.counterparty_iban, credit.counterparty_iban}


def test_n26_payee_by_direction() -> None:
    # Only the counterparty's party object is populated (creditor on DBIT, debtor on CRDT);
    # the own-side object is null, so direction-based reading cannot pick up our own name.
    debit, credit = _n26_txns()
    assert debit.payee == "Example Payee GmbH"
    assert credit.payee == "Example Payer"


def test_n26_prose_remittance_is_memo_and_lifts_no_reference() -> None:
    # A single prose element, no Wise-style machine token — nothing for _split_reference to
    # lift, so CHECKNUM stays empty and the memo carries the whole remittance.
    debit, credit = _n26_txns()
    assert debit.memo == "Test transfer out"
    assert credit.memo == "Incoming test"
    assert debit.reference is None
    assert credit.reference is None


def test_n26_iso_bank_transaction_code_gets_no_conversion_key() -> None:
    # ICDT/RCDT are ISO 20022 transfer codes, not Wise's CONVERSION mechanism word.
    assert [t.conversion_key for t in _n26_txns()] == [None, None]


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_n26_fixture_roundtrips_to_ofx() -> None:
    # End to end for the shape: fixture -> Txn -> statement -> OFX -> ofxtools. The FITID is the
    # bare UUID, and the counterparty IBAN lands in BANKACCTTO with the country-code BANKID
    # fallback (DE has no dedicated bank-code branch like PL).
    import io

    from ofxtools.Parser import OFXTree

    from gnucash_ofx.models import Account
    from gnucash_ofx.ofxout import build_statement, statement_to_ofx

    account = Account(bank_key="n26", account_id=_N26_OWN_IBAN, currency="EUR", bank_id="NTSBDEBB")
    stmt = build_statement(account, _n26_txns(), date(2026, 7, 31), date(2026, 8, 13))
    tree = OFXTree()
    tree.parse(io.BytesIO(statement_to_ofx(stmt).encode("utf-8")))
    parsed = tree.convert().statements[0]

    assert parsed.curdef == "EUR"
    assert parsed.account.bankid == "NTSBDEBB"
    debit, credit = parsed.transactions
    assert debit.fitid == "0a1b2c3d-0001-4000-8000-000000000001"
    assert debit.checknum is None  # no machine reference was lifted
    assert debit.bankacctto.acctid == "DE79000000000000000002"
    assert debit.bankacctto.bankid == "DE"
    assert credit.trnamt == Decimal("56.78")
