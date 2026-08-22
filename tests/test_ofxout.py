"""TDD for the OFX writer: normalized Txn -> ofxstatement Statement -> OFX file."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from ofxtools.Parser import OFXTree

from gnucash_ofx.models import Account, Conversion, Txn
from gnucash_ofx.ofxout import (
    UncarryableAccountType,
    account_disambiguators,
    accttype_for,
    bank_id_for,
    build_statement,
    combine_statements,
    combined_filename,
    compose_check_number,
    compose_conversion,
    compose_memo,
    compose_name,
    ofx_filename,
    statement_to_ofx,
    to_ascii,
    validate_raw_fields,
    write_account_ofx,
    write_combined_ofx,
)

PERIOD_START = date(2026, 5, 1)
PERIOD_END = date(2026, 5, 31)


def _txns() -> list[Txn]:
    return [
        Txn(
            id="tx-001",
            date=date(2026, 5, 4),
            amount=Decimal("-42.50"),
            currency="PLN",
            payee="Biedronka",
            memo="Grocery store",
        ),
        Txn(
            id="tx-002",
            date=date(2026, 5, 20),
            amount=Decimal("3500.00"),
            currency="PLN",
            payee="ACME Sp. z o.o.",
            memo="Salary",
        ),
    ]


def test_build_statement_signs_and_types_transactions() -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)

    assert stmt.account_id == "PL123"
    assert stmt.currency == "PLN"
    assert len(stmt.lines) == 2
    debit, credit = stmt.lines
    assert debit.amount == Decimal("-42.50")
    assert debit.trntype == "DEBIT"
    assert credit.amount == Decimal("3500.00")
    assert credit.trntype == "CREDIT"


def test_build_statement_derives_balance_from_transactions() -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)
    # No known balance -> start 0, end = sum of lines.
    assert stmt.start_balance == Decimal("0")
    assert stmt.end_balance == Decimal("3457.50")


def test_build_statement_uses_known_closing_balance() -> None:
    account = Account(
        bank_key="alior", account_id="PL123", currency="PLN", end_balance=Decimal("10000.00")
    )
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)
    # Start balance is back-computed so LEDGERBAL stays consistent with the lines.
    assert stmt.end_balance == Decimal("10000.00")
    assert stmt.start_balance == Decimal("6542.50")


def test_build_statement_keeps_explicit_zero_start_balance() -> None:
    # An explicit opening balance must not be treated as "unset".
    account = Account(
        bank_key="alior", account_id="PL123", currency="PLN", start_balance=Decimal("100.00")
    )
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)
    assert stmt.start_balance == Decimal("100.00")
    assert stmt.end_balance == Decimal("3557.50")


def test_ofx_roundtrips_through_ofxtools() -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)
    ofx_text = statement_to_ofx(stmt)

    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    parsed = tree.convert()
    statement = parsed.statements[0]

    assert statement.curdef == "PLN"
    assert statement.account.acctid == "PL123"
    txn = statement.transactions[0]
    assert txn.fitid == "tx-001"
    assert txn.trnamt == Decimal("-42.50")
    # NAME is the composed Description: remittance first, then the counterparty.
    assert txn.name == "Grocery store; Biedronka"
    # MEMO keeps the remittance in full — it is the untruncated copy.
    assert txn.memo == "Grocery store"


# Synthetic 28-char Polish IBAN (see AGENTS.md); the 99999999 bank code is unassigned. Note IBANs
# legitimately exceed OFX's ACCTID max length (A-22); GnuCash/libofx tolerate this and ofxtools
# keeps the full value (warning only), so the warning below is expected and harmless. The matcher
# signal is the memo (A-255), unaffected.
_PL_IBAN = "PL03999999990000000000000005"

# The account's own ACCTID in the raw-passthrough tests; same synthetic convention as above.
_OWN_ACCTID = "PL19999999990000000000000008"


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_counterparty_iban_in_memo_and_bankacctto() -> None:
    # IBAN must be a token in the memo (the signal GnuCash's matcher tokenizes) AND in the
    # standard BANKACCTTO aggregate (for auditing / other OFX tools).
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-xfer",
        date=date(2026, 5, 10),
        amount=Decimal("-390.00"),
        currency="PLN",
        memo="Przelew wlasny",
        counterparty_iban=_PL_IBAN,
    )
    stmt = build_statement(account, [txn], PERIOD_START, PERIOD_END)
    ofx_text = statement_to_ofx(stmt)

    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    parsed = tree.convert()
    parsed_txn = parsed.statements[0].transactions[0]
    # IBAN appended to the remittance so the matcher sees it as a token.
    assert parsed_txn.memo == f"Przelew wlasny {_PL_IBAN}"
    # Standard field also present, with the IBAN as ACCTID and a derived (PL) BANKID.
    assert parsed_txn.bankacctto is not None
    assert parsed_txn.bankacctto.acctid == _PL_IBAN
    assert parsed_txn.bankacctto.bankid == "9999"  # PL bank code = IBAN positions 5-8
    # Direction/type stays correct — not forced to XFER.
    assert parsed_txn.trntype == "DEBIT"


# Every European IBAN country's 2-letter code and total IBAN length (ISO 13616 / IBAN registry,
# cross-checked against Wikipedia's "International Bank Account Number" table, August 2026).
# _bankid_from_iban only has a dedicated branch for Poland (tested above); every other country
# falls back to its code, and this is the data that lets that be tested without one near-identical
# function per country.
_EUROPEAN_IBAN_LENGTHS = [
    ("AL", 28), ("AD", 24), ("AT", 20), ("BY", 28), ("BE", 16), ("BA", 20), ("BG", 22),
    ("HR", 21), ("CY", 28), ("CZ", 24), ("DK", 18), ("EE", 20), ("FO", 18), ("FI", 18),
    ("FR", 27), ("GE", 22), ("DE", 22), ("GI", 23), ("GR", 27), ("GL", 18), ("HU", 28),
    ("IS", 26), ("IE", 22), ("IT", 27), ("XK", 20), ("LV", 21), ("LI", 21), ("LT", 20),
    ("LU", 20), ("MT", 31), ("MD", 24), ("MC", 27), ("ME", 22), ("NL", 18), ("MK", 19),
    ("NO", 15), ("PT", 25), ("RO", 24), ("RU", 33), ("SM", 27), ("RS", 22), ("SK", 24),
    ("SI", 19), ("ES", 24), ("SE", 24), ("CH", 21), ("TR", 26), ("UA", 29), ("GB", 22),
    ("VA", 22),
]  # fmt: skip


def _synthetic_iban(country: str, length: int) -> str:
    """A structurally valid, checksum-passing IBAN for ``country``, with an all-zero BBAN.

    Synthetic per AGENTS.md: no real bank/branch/account code, just enough structure (correct
    length, correct ISO 7064 MOD97-10 check digits) to look like a real IBAN of that country.
    Implements the standard check-digit algorithm - the same one _iban_checksum_ok() verifies,
    generalized from the single-country snippet in AGENTS.md.
    """
    bban = "0" * (length - 4)
    rearranged = bban + country + "00"
    numeric = "".join(str(ord(c) - ord("A") + 10) if c.isalpha() else c for c in rearranged)
    check_digits = 98 - int(numeric) % 97
    return f"{country}{check_digits:02d}{bban}"


@pytest.mark.parametrize(
    "country,length", _EUROPEAN_IBAN_LENGTHS, ids=[c for c, _ in _EUROPEAN_IBAN_LENGTHS]
)
def test_bankacctto_bankid_falls_back_to_the_country_code(country: str, length: int) -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="EUR")
    txn = Txn(
        id="tx-1",
        date=date(2026, 5, 10),
        amount=Decimal("-10.00"),
        currency="EUR",
        counterparty_iban=_synthetic_iban(country, length),
    )
    stmt = build_statement(account, [txn], PERIOD_START, PERIOD_END)
    bank_account_to = stmt.lines[0].bank_account_to
    assert bank_account_to is not None
    assert bank_account_to.bank_id == country


def test_bankacctto_bankid_falls_back_to_na_for_a_whitespace_only_iban() -> None:
    # counterparty_iban="" never reaches _bankid_from_iban at all (build_statement's `if
    # txn.counterparty_iban:` guard is falsy for it) - but a source could report one that is
    # entirely whitespace, which passes that guard and only turns empty after .strip() inside
    # the function. BANKID must never end up blank; OFX requires it in a bank-account aggregate.
    account = Account(bank_key="alior", account_id="PL123", currency="EUR")
    txn = Txn(
        id="tx-1",
        date=date(2026, 5, 10),
        amount=Decimal("-10.00"),
        currency="EUR",
        counterparty_iban="   ",
    )
    stmt = build_statement(account, [txn], PERIOD_START, PERIOD_END)
    bank_account_to = stmt.lines[0].bank_account_to
    assert bank_account_to is not None
    assert bank_account_to.bank_id == "NA"


def test_counterparty_iban_alone_becomes_memo() -> None:
    # When there is no remittance, the IBAN alone is the memo.
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-1",
        date=date(2026, 5, 10),
        amount=Decimal("100.00"),
        currency="PLN",
        memo=None,
        counterparty_iban=_PL_IBAN,
    )
    stmt = build_statement(account, [txn], PERIOD_START, PERIOD_END)
    assert stmt.lines[0].memo == _PL_IBAN


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_long_bank_key_still_produces_valid_ofx() -> None:
    # OFX types BANKID as A-9. Config keys like "wise_personal" (13) or "alior_kantor" (12) are
    # longer, which made real exports fail strict validation.
    account = Account(bank_key="wise_personal", account_id=_PL_IBAN, currency="EUR")
    ofx_text = statement_to_ofx(build_statement(account, _txns(), PERIOD_START, PERIOD_END))

    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    parsed = tree.convert()  # raises OFXSpecError if BANKID is over-long
    assert parsed.statements[0].account.bankid == "wise_pers"


def test_bank_id_prefers_configured_bic() -> None:
    assert bank_id_for("alior", "ALBPPLPW") == "ALBPPLPW"
    assert bank_id_for("alior", " albpplpw ") == "ALBPPLPW"  # normalized
    # An 11-char BIC exceeds OFX's A-9 limit; reduce to the 8-char primary-office form.
    assert bank_id_for("alior", "ALBPPLPWXXX") == "ALBPPLPW"
    # Empty/blank override falls back to the key rather than emitting an empty BANKID.
    assert bank_id_for("wise_personal", "   ") == "wise_pers"
    assert bank_id_for("wise_personal", None) == "wise_pers"


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_configured_bank_id_reaches_the_ofx() -> None:
    account = Account(
        bank_key="alior_kantor", account_id=_PL_IBAN, currency="PLN", bank_id="ALBPPLPW"
    )
    ofx_text = statement_to_ofx(build_statement(account, _txns(), PERIOD_START, PERIOD_END))
    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    assert tree.convert().statements[0].account.bankid == "ALBPPLPW"


def test_bank_id_is_truncated_deterministically() -> None:
    # Stable across runs: GnuCash derives online_id from BANKID+ACCTID.
    assert bank_id_for("wise_personal") == "wise_pers"
    assert bank_id_for("wise_business") == "wise_busi"
    assert bank_id_for("alior_kantor") == "alior_kan"
    assert bank_id_for("alior") == "alior"  # short keys untouched


def test_to_ascii_folds_diacritics_and_special_letters() -> None:
    # GnuCash's OFX parser (libofx/OpenSP) drops non-ASCII outright, so we fold first.
    assert to_ascii("Kämpf OÜ") == "Kampf OU"
    assert to_ascii("Przelew własny") == "Przelew wlasny"  # 'ł' does not NFKD-decompose
    assert to_ascii("ZAŻÓŁĆ GĘŚLĄ JAŹŃ") == "ZAZOLC GESLA JAZN"
    assert to_ascii("Zielona Góra") == "Zielona Gora"
    assert to_ascii("plain ascii") == "plain ascii"


def test_compose_name_leads_with_the_remittance() -> None:
    # GnuCash shows NAME as the register Description. The remittance identifies the transaction,
    # the counterparty name usually repeats across many, so the remittance comes first — the
    # same order GnuCash's own AqBanking importer emits.
    assert compose_name("Kampf OU", "Monthly payment for hosting") == (
        "Monthly payment for hosting; Kampf OU"
    )


def test_compose_name_drops_a_component_already_contained() -> None:
    # AqBanking's gnc_g_list_stringjoin_nodups() skips a component that is already a substring
    # of what it has accumulated, which is what stops "Sent money to ACME; ACME".
    assert compose_name("ACME Sp. z o.o.", "Sent money to ACME Sp. z o.o.") == (
        "Sent money to ACME Sp. z o.o."
    )


def test_compose_name_containment_only_looks_backwards() -> None:
    # AqBanking parity: a component is dropped when it is contained in what came *before* it,
    # never retroactively when a later component subsumes it. Harmless here — the direction that
    # occurs in practice is a remittance quoting the counterparty name, covered above; a
    # counterparty name quoting the whole remittance does not happen.
    assert compose_name("Monthly payment for hosting", "Monthly payment") == (
        "Monthly payment; Monthly payment for hosting"
    )


def test_compose_name_containment_is_case_sensitive() -> None:
    # Matching AqBanking's utf8_strstr(), which compares bytes. Card-payment remittances are
    # typically upper-cased by the acquirer while the counterparty name is not, so the two are
    # different strings and both are kept — they carry different information (the branch/city).
    assert compose_name("Biedronka Sp. z o.o.", "Card payment BIEDRONKA 1234 KRAKOW") == (
        "Card payment BIEDRONKA 1234 KRAKOW; Biedronka Sp. z o.o."
    )


def test_compose_name_handles_a_missing_component() -> None:
    assert compose_name("Spotify", None) == "Spotify"
    assert compose_name(None, "Salary May 2026") == "Salary May 2026"


def test_compose_name_is_none_when_there_is_nothing_to_say() -> None:
    # None leaves NAME unset, so GnuCash falls back to MEMO for the Description as before.
    assert compose_name(None, None) is None
    assert compose_name("", "") is None
    assert compose_name("   ", "\t") is None


def test_compose_name_folds_to_ascii_before_comparing() -> None:
    # Folding first means the containment check sees one alphabet: "Kämpf OÜ" would not be found
    # inside an already-folded "Kampf OU" and the name would be repeated.
    assert compose_name("Kämpf OÜ", "Przelew do Kampf OU") == "Przelew do Kampf OU"


def test_compose_name_truncates_at_a_word_boundary() -> None:
    # AGENTS.md: "NAME is capped at 96 characters and MEMO at 390." — libofx's NAME buffer is
    # 96+1 and it copies with strncpy, which does not NUL-terminate at exactly the buffer size —
    # so 96 is a hard ceiling. Cut at a space, because a partial token would be dead weight in
    # the Bayesian matcher.
    composed = compose_name(None, f"{'A' * 50} {'B' * 50}")
    assert composed == "A" * 50


def test_compose_name_keeps_a_name_that_exactly_fills_the_budget() -> None:
    assert compose_name(None, "A" * 96) == "A" * 96


def test_compose_name_hard_cuts_a_single_over_long_token() -> None:
    # No boundary to prefer; the ceiling still has to hold.
    assert compose_name(None, "A" * 120) == "A" * 96


def test_compose_name_never_ends_on_a_dangling_separator() -> None:
    # A cut landing inside "; " would otherwise leave a trailing ';'.
    composed = compose_name("Y", "X" * 95)
    assert composed == "X" * 95


def test_compose_check_number_keeps_a_plain_token() -> None:
    assert compose_check_number("9000000001") == "9000000001"
    assert compose_check_number(" 9000000001 ") == "9000000001"
    assert compose_check_number(None) is None
    assert compose_check_number("   ") is None


def test_compose_check_number_drops_an_over_long_reference() -> None:
    # AGENTS.md: "CHECKNUM is capped at 12, and an over-long value is dropped, not truncated." —
    # libofx's check_number buffer is 12+1 and it copies with the same non-terminating strncpy as
    # NAME, so 13 fills it with no NUL and anything longer comes back cut — measured: a 19-char
    # "TRANSFER-<id>" arrives as "TRANSFER-1234". ofxtools rejects over-12 outright (strict
    # String(12), unlike the NagString it uses for ACCTID). Dropping beats truncating: a cut
    # identifier is no longer unique but still looks like one.
    assert compose_check_number("A" * 12) == "A" * 12
    assert compose_check_number("A" * 13) is None
    assert compose_check_number("TRANSFER-9000000001") is None


def test_compose_check_number_drops_anything_that_is_not_a_token() -> None:
    # The Num column is for an identifier, not prose, and the value goes through the same SGML
    # path as everything else.
    assert compose_check_number("ref 9000") is None
    assert compose_check_number("-9000") is None
    assert compose_check_number("ref/9000") is None
    assert compose_check_number("zażółć gęślą") is None


def test_compose_check_number_folds_to_ascii_like_every_other_field() -> None:
    # It reaches GnuCash through the same OpenSP path, which drops non-ASCII on the Windows build.
    assert compose_check_number("REF-Ä12") == "REF-A12"


def test_compose_check_number_drops_a_reference_carrying_a_control() -> None:
    """Decided on the *raw* reference, because folding now deletes controls.

    `R` + chr(2) + `7` folds to the valid-looking `R7` — a cut identifier in the register's Num
    column, which is exactly what "dropped, not truncated" exists to refuse. Not reachable from
    Enable Banking today (`_OWN_REFERENCE` fullmatches digit/UUID shapes), so this pins the
    guarantee at the gate whose comment claims it rather than in another module's regex.
    """
    assert compose_check_number("R" + chr(0x02) + "7") is None
    assert compose_check_number("REF" + chr(0x7F) + "123") is None
    # A separator control folds to a space, fails _CHECKNUM_ALLOWED, and is dropped as before.
    assert compose_check_number("CHK" + chr(0x09) + "123456789") is None


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_reference_reaches_checknum_and_refnum() -> None:
    # CHECKNUM is what GnuCash reads into the register's Num column; REFNUM is the semantically
    # correct field but cannot stand alone, because GnuCash's reference_number_valid branch
    # assigns check_number by mistake. Both are written, same as MEMO + BANKACCTTO.
    account = Account(bank_key="wise_personal", account_id="PL123", currency="EUR")
    txn = Txn(
        id="TRWI-9000000001-BALANCE-0004",
        date=date(2026, 5, 27),
        amount=Decimal("-820.00"),
        currency="EUR",
        payee="ACME Sp. z o.o.",
        memo="Invoice FS 1/05/2026",
        reference="9000000001",
    )
    ofx_text = statement_to_ofx(build_statement(account, [txn], PERIOD_START, PERIOD_END))

    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    parsed_txn = tree.convert().statements[0].transactions[0]  # raises if CHECKNUM is over-long
    assert parsed_txn.checknum == "9000000001"
    assert parsed_txn.refnum == "9000000001"
    # The reference is not repeated in the fields GnuCash tokenizes — that is the point of it.
    assert parsed_txn.name == "Invoice FS 1/05/2026; ACME Sp. z o.o."
    assert parsed_txn.memo == "Invoice FS 1/05/2026"


def test_an_unemittable_reference_is_lost_rather_than_left_in_the_text() -> None:
    # Wise's cashback id is a 36-character UUID: it cannot reach CHECKNUM under any encoding. It
    # still leaves NAME/MEMO, because an opaque id is not text a person reads — losing it beats
    # showing it. Nothing is at risk in the import: FITID keeps these rows distinct.
    account = Account(bank_key="wise_personal", account_id="PL123", currency="PLN")
    txn = Txn(
        id="TRWI-9000000005-BALANCE-0008",
        date=date(2026, 5, 29),
        amount=Decimal("1.35"),
        currency="PLN",
        memo="Cashback",
        reference="9a1e07a1-fe12-4a3b-8c5d-01cb2dd3ce45",
    )
    ofx_text = statement_to_ofx(build_statement(account, [txn], PERIOD_START, PERIOD_END))
    assert "9a1e07a1" not in ofx_text
    assert "<CHECKNUM>" not in ofx_text
    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    assert tree.convert().statements[0].transactions[0].name == "Cashback"


def test_no_reference_emits_neither_tag() -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    ofx_text = statement_to_ofx(build_statement(account, _txns(), PERIOD_START, PERIOD_END))
    assert "<CHECKNUM>" not in ofx_text
    assert "<REFNUM>" not in ofx_text


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_composed_name_exceeds_the_advisory_a_32_and_survives() -> None:
    # OFX types NAME as A-32, but libofx's buffer is 96 and GnuCash keeps the whole value. Like
    # the over-long ACCTID, ofxtools warns rather than failing, and the text round-trips intact.
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-long",
        date=date(2026, 5, 10),
        amount=Decimal("-100.00"),
        currency="PLN",
        payee="ACME Sp. z o.o.",
        memo="Invoice FS 1/05/2026 for consulting services rendered in May",
    )
    ofx_text = statement_to_ofx(build_statement(account, [txn], PERIOD_START, PERIOD_END))
    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    parsed_txn = tree.convert().statements[0].transactions[0]
    assert parsed_txn.name == (
        "Invoice FS 1/05/2026 for consulting services rendered in May; ACME Sp. z o.o."
    )
    assert len(parsed_txn.name) > 32


# -------------------------------------------------------- currency conversion annotation (#5)

_CONVERSION = Conversion(
    from_amount=Decimal("12000.00"),
    from_currency="EUR",
    to_amount=Decimal("51720.00"),
    to_currency="PLN",
    rate=Decimal("4.310000"),
)


def test_compose_conversion_format() -> None:
    assert compose_conversion(_CONVERSION) == "12000.00 EUR -> 51720.00 PLN @ 4.310000"


def test_compose_conversion_is_none_without_a_pairing() -> None:
    assert compose_conversion(None) is None


def test_compose_name_leads_with_the_conversion_annotation() -> None:
    # A deliberate exception to remittance-first (adr-currency-conversion-pairs.md decision 4):
    # the annotation is the substantive remittance for a conversion, so it leads even the memo.
    name = compose_name("ACME Sp. z o.o.", "Kantor Walutowy 220", _CONVERSION)
    assert name == "12000.00 EUR -> 51720.00 PLN @ 4.310000; Kantor Walutowy 220; ACME Sp. z o.o."


def test_compose_name_without_a_conversion_is_unchanged() -> None:
    assert compose_name("ACME Sp. z o.o.", "Kantor Walutowy 220") == (
        compose_name("ACME Sp. z o.o.", "Kantor Walutowy 220", None)
    )


def test_compose_name_truncation_eats_boilerplate_never_the_annotation() -> None:
    # The source's duplicated boilerplate is the budget to reclaim, not the annotation.
    boilerplate = "Rozliczenie transakcji wymiany walut " * 5
    name = compose_name(None, boilerplate, _CONVERSION)
    assert name is not None
    assert name.startswith("12000.00 EUR -> 51720.00 PLN @ 4.310000")
    assert len(name) <= 96
    assert boilerplate.strip() not in name  # the boilerplate itself got cut


def test_compose_memo_leads_with_the_conversion_annotation() -> None:
    memo = compose_memo("Kantor Walutowy 220", None, _CONVERSION)
    assert memo == "12000.00 EUR -> 51720.00 PLN @ 4.310000; Kantor Walutowy 220"


def test_compose_memo_without_a_conversion_is_unchanged() -> None:
    assert compose_memo("Kantor Walutowy 220", None) == (
        compose_memo("Kantor Walutowy 220", None, None)
    )


def test_compose_memo_keeps_the_iban_last_with_a_conversion_leading() -> None:
    memo = compose_memo("Kantor Walutowy 220", _PL_IBAN, _CONVERSION)
    assert memo is not None
    assert memo.startswith("12000.00 EUR -> 51720.00 PLN @ 4.310000")
    assert memo.endswith(f" {_PL_IBAN}")
    assert len(memo) <= 390


def test_conversion_annotation_is_identical_on_both_legs() -> None:
    # Both legs carry the same Conversion and the same source-sent remittance text
    # (docs/adr-currency-conversion-pairs.md decision 4: identical text on both files).
    eur_name = compose_name(None, "Kantor Walutowy 220", _CONVERSION)
    pln_name = compose_name(None, "Kantor Walutowy 220", _CONVERSION)
    assert eur_name == pln_name
    eur_memo = compose_memo("Kantor Walutowy 220", None, _CONVERSION)
    pln_memo = compose_memo("Kantor Walutowy 220", None, _CONVERSION)
    assert eur_memo == pln_memo


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_write_account_ofx_carries_the_conversion_annotation_through_libofx_shaped_fields(
    tmp_path: object,
) -> None:
    from pathlib import Path

    tmp_path = Path(str(tmp_path))
    account = Account(bank_key="alior_kantor", account_id="PL123", currency="EUR")
    txn = Txn(
        id="tx-conv",
        date=date(2026, 5, 18),
        amount=Decimal("-12000.00"),
        currency="EUR",
        memo="Kantor Walutowy 220",
        conversion=_CONVERSION,
    )
    path = write_account_ofx(account, [txn], PERIOD_START, PERIOD_END, tmp_path)
    assert path is not None
    tree = OFXTree()
    tree.parse(_as_bytes(path.read_text(encoding="utf-8")))
    parsed_txn = tree.convert().statements[0].transactions[0]
    assert parsed_txn.name == "12000.00 EUR -> 51720.00 PLN @ 4.310000; Kantor Walutowy 220"
    assert parsed_txn.memo == "12000.00 EUR -> 51720.00 PLN @ 4.310000; Kantor Walutowy 220"


def test_compose_memo_caps_at_the_libofx_buffer() -> None:
    # AGENTS.md: "NAME is capped at 96 characters and MEMO at 390." — libofx's memo buffer is
    # 390+1 and is filled by the same strncpy that does not terminate at exactly the buffer size.
    assert compose_memo("A" * 500, None) == "A" * 390
    assert compose_memo("A" * 390, None) == "A" * 390


def test_compose_memo_reserves_room_for_the_iban() -> None:
    # The IBAN is appended last, so a naive cut would drop the one token account routing needs.
    # The remittance yields the space instead; the IBAN always survives whole.
    memo = compose_memo("word " * 100, _PL_IBAN)
    assert memo is not None
    assert memo.endswith(f" {_PL_IBAN}")
    assert len(memo) <= 390


def test_compose_memo_keeps_the_iban_when_the_remittance_fills_the_budget() -> None:
    # A single unbreakable run longer than the remittance budget must not push the IBAN out.
    memo = compose_memo("A" * 500, _PL_IBAN)
    assert memo == "A" * (390 - len(_PL_IBAN) - 1) + f" {_PL_IBAN}"


def test_compose_memo_survives_a_pathological_account_number() -> None:
    # normalize_account_number() does not bound length, so a garbage "IBAN" longer than the whole
    # memo budget is reachable from bad source data. It must still not overrun the buffer.
    memo = compose_memo("Salary", "X" * 500)
    assert memo == "X" * 390


def test_compose_memo_folds_to_ascii() -> None:
    # Folding happens before the cap, since folding can change the length either way.
    assert compose_memo("Przelew własny", None) == "Przelew wlasny"


def test_compose_memo_is_none_when_empty() -> None:
    assert compose_memo(None, None) is None
    assert compose_memo("   ", None) is None


@pytest.mark.filterwarnings("ignore::ofxtools.Types.OFXTypeWarning")
def test_counterparty_iban_stays_out_of_the_name() -> None:
    # The IBAN is already a token via MEMO, which GnuCash tokenizes alongside the description.
    # Repeating it in NAME would spend the 96-character budget for no extra matching signal.
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-xfer",
        date=date(2026, 5, 10),
        amount=Decimal("-390.00"),
        currency="PLN",
        payee="ACME Sp. z o.o.",
        memo="Przelew wlasny",
        counterparty_iban=_PL_IBAN,
    )
    line = build_statement(account, [txn], PERIOD_START, PERIOD_END).lines[0]
    assert line.payee == "Przelew wlasny; ACME Sp. z o.o."
    assert line.memo == f"Przelew wlasny {_PL_IBAN}"


def test_payee_and_memo_are_ascii_folded_in_output() -> None:
    account = Account(bank_key="wise", account_id=_PL_IBAN, currency="EUR")
    txn = Txn(
        id="t1",
        date=date(2026, 8, 5),
        amount=Decimal("-10.00"),
        currency="EUR",
        payee="Kämpf OÜ",
        memo="Przelew własny",
    )
    ofx_text = statement_to_ofx(build_statement(account, [txn], PERIOD_START, PERIOD_END))
    assert ofx_text.isascii()  # nothing for OpenSP to drop
    assert "<NAME>Przelew wlasny; Kampf OU</NAME>" in ofx_text
    assert "<MEMO>Przelew wlasny</MEMO>" in ofx_text


def test_ofx_filename_is_bank_currency_then_period() -> None:
    # Account prefix first so every file for one account sorts together; fixed-width dates so
    # lexicographic order is chronological within that group.
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    assert ofx_filename(account, PERIOD_START, PERIOD_END) == "alior_PLN_2026_05_01-2026_05_31.ofx"


def test_ofx_filename_covers_a_single_day_range() -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    day = date(2026, 5, 4)
    assert ofx_filename(account, day, day) == "alior_PLN_2026_05_04-2026_05_04.ofx"


def test_ofx_filename_spells_out_a_multi_month_range() -> None:
    # The old "{period_end:%Y-%m}" name hid the range: two fetches with different --from
    # collided on one filename and one silently overwrote the other.
    account = Account(bank_key="wise_personal", account_id=_PL_IBAN, currency="EUR")
    name = ofx_filename(account, date(2026, 1, 1), date(2026, 3, 31))
    assert name == "wise_personal_EUR_2026_01_01-2026_03_31.ofx"


def test_ofx_filename_disambiguator_sits_between_account_and_period() -> None:
    account = Account(bank_key="millennium", account_id=_PL_IBAN, currency="PLN")
    name = ofx_filename(account, PERIOD_START, PERIOD_END, disambiguator="5387")
    assert name == "millennium_PLN_5387_2026_05_01-2026_05_31.ofx"


def test_ofx_filename_rejects_traversal() -> None:
    account = Account(bank_key="../evil", account_id="PL123", currency="PLN")
    with pytest.raises(ValueError, match="invalid bank_key"):
        ofx_filename(account, PERIOD_START, PERIOD_END)

    safe = Account(bank_key="alior", account_id="PL123", currency="PLN")
    with pytest.raises(ValueError, match="invalid disambiguator"):
        ofx_filename(safe, PERIOD_START, PERIOD_END, disambiguator="../evil")


def test_account_disambiguators_use_the_acctid_tail() -> None:
    assert account_disambiguators([_PL_IBAN, "PL03999999990000000000009999"]) == ["0005", "9999"]


def test_account_disambiguators_do_not_depend_on_order() -> None:
    ids = [_PL_IBAN, "PL03999999990000000000009999"]
    forward = dict(zip(ids, account_disambiguators(ids), strict=True))
    reversed_ = dict(zip(ids[::-1], account_disambiguators(ids[::-1]), strict=True))
    assert forward == reversed_


def test_account_disambiguators_fall_back_to_a_digest() -> None:
    # 'eb-…' ACCTIDs (and bare UIDs) can end in '-' or '_', which safe_component permits but
    # which would blur the boundary against the date range that follows.
    suffixes = account_disambiguators(["acc-a", "acc-b"])
    assert all(s.startswith("eb-") and len(s) == len("eb-") + 8 for s in suffixes)
    assert len(set(suffixes)) == 2
    assert account_disambiguators(["acc-a", "acc-b"]) == suffixes  # stable across calls


def test_account_disambiguators_avoid_a_shared_tail() -> None:
    # Two accounts ending in the same four characters would still collide; the whole group
    # falls back to the digest form rather than half of it.
    # Both synthetic (see AGENTS.md); different unassigned bank codes, same trailing digits.
    ids = [_PL_IBAN, "PL03888888880000000000000005"]
    suffixes = account_disambiguators(ids)
    assert len(set(suffixes)) == 2
    assert all(s.startswith("eb-") for s in suffixes)


def test_write_account_ofx_writes_file(tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path))
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    path = write_account_ofx(account, _txns(), PERIOD_START, PERIOD_END, out)
    assert path is not None
    assert path.name == "alior_PLN_2026_05_01-2026_05_31.ofx"
    assert path.read_text(encoding="utf-8").startswith("OFXHEADER:100")


def test_written_file_has_clean_crlf_line_endings(tmp_path: object) -> None:
    # AGENTS.md: "Write OFX with newline=''." — OfxWriter emits CRLF; writing in default text
    # mode on Windows turned every "\r\n" into "\r\r\n", producing malformed OFX. The file must
    # contain CRLF and never CR CR LF.
    from pathlib import Path

    out = Path(str(tmp_path))
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    path = write_account_ofx(account, _txns(), PERIOD_START, PERIOD_END, out)
    assert path is not None
    raw = path.read_bytes()
    assert b"\r\r\n" not in raw
    assert b"\r\n" in raw


def test_write_account_ofx_skips_when_no_transactions(tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path))
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    assert write_account_ofx(account, [], PERIOD_START, PERIOD_END, out) is None
    assert list(out.iterdir()) == []


# --------------------------------------------------------------------------- combine_statements


def _statement(bank_key: str, account_id: str, currency: str) -> object:
    account = Account(bank_key=bank_key, account_id=account_id, currency=currency)
    txn = Txn(
        id=f"{account_id}-t1",
        date=date(2026, 5, 10),
        amount=Decimal("10.00"),
        currency=currency,
        payee="Payee",
        memo="Memo",
    )
    return build_statement(account, [txn], PERIOD_START, PERIOD_END)


def test_combine_statements_at_n1_is_byte_identical_to_statement_to_ofx() -> None:
    """docs/adr-combined-ofx-file.md decision 3: the flag must degrade exactly at N=1."""
    stmt = _statement("alior", "PL111", "PLN")
    assert combine_statements([stmt]) == statement_to_ofx(stmt)


def test_combine_statements_writes_one_message_set_with_n_statements() -> None:
    """docs/adr-combined-ofx-file.md decision 2: one <BANKMSGSRSV1>, N <STMTTRNRS>."""
    stmts = [
        _statement("alior", "PL111", "PLN"),
        _statement("alior", "PL222", "PLN"),  # shares a BANKID, differs only by ACCTID
        _statement("wise", "PL333", "EUR"),
    ]
    ofx_text = combine_statements(stmts)

    # Exactly one message set and one signon, however many statements.
    assert ofx_text.count("<BANKMSGSRSV1>") == 1
    assert ofx_text.count("</BANKMSGSRSV1>") == 1
    assert ofx_text.count("<SIGNONMSGSRSV1>") == 1

    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    parsed = tree.convert()
    assert [s.account.acctid for s in parsed.statements] == ["PL111", "PL222", "PL333"]
    assert [s.curdef for s in parsed.statements] == ["PLN", "PLN", "EUR"]


def test_combine_statements_rejects_an_empty_sequence() -> None:
    with pytest.raises(ValueError, match="at least one statement"):
        combine_statements([])


def test_combined_filename_carries_only_the_window() -> None:
    assert combined_filename(PERIOD_START, PERIOD_END) == "combined_2026_05_01-2026_05_31.ofx"


def test_write_combined_ofx_omits_accounts_with_no_transactions(tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path))
    empty_account = Account(bank_key="alior", account_id="PL999", currency="PLN")
    funded_account = Account(bank_key="alior", account_id="PL111", currency="PLN")
    txns = [
        Txn(id="t1", date=date(2026, 5, 10), amount=Decimal("10.00"), currency="PLN"),
    ]

    path = write_combined_ofx(
        [
            (empty_account, [], PERIOD_START, PERIOD_END),
            (funded_account, txns, PERIOD_START, PERIOD_END),
        ],
        PERIOD_START,
        PERIOD_END,
        out,
    )

    assert path is not None
    assert path.name == "combined_2026_05_01-2026_05_31.ofx"
    content = path.read_text(encoding="utf-8")
    assert "PL111" in content
    assert "PL999" not in content


def test_write_combined_ofx_writes_nothing_when_every_account_is_empty(tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path))
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    entries = [(account, [], PERIOD_START, PERIOD_END)]
    assert write_combined_ofx(entries, PERIOD_START, PERIOD_END, out) is None
    assert list(out.iterdir()) == []


def test_write_combined_ofx_dates_each_statement_from_its_own_window(tmp_path: object) -> None:
    """A statement's BANKTRANLIST/LEDGERBAL must reflect the window it was fetched with, not the
    run's - two banks resolving different windows must not have one bank's dates leak into the
    other's statement, even though the *file* is named from the union of both."""
    from pathlib import Path

    out = Path(str(tmp_path))
    early_account = Account(bank_key="alior", account_id="PL111", currency="PLN")
    late_account = Account(bank_key="millennium", account_id="PL222", currency="PLN")
    txn = Txn(id="t1", date=date(2026, 5, 10), amount=Decimal("10.00"), currency="PLN")
    early_window = (date(2026, 3, 1), date(2026, 5, 31))
    late_window = (date(2026, 5, 20), date(2026, 5, 31))

    path = write_combined_ofx(
        [(early_account, [txn], *early_window), (late_account, [txn], *late_window)],
        early_window[0],  # the filename uses the union - unrelated to each statement's own dates
        early_window[1],
        out,
    )

    assert path is not None
    tree = OFXTree()
    tree.parse(_as_bytes(path.read_text(encoding="utf-8")))
    statements = {s.account.acctid: s for s in tree.convert().statements}
    assert statements["PL111"].dtstart.date() == early_window[0]
    assert statements["PL222"].dtstart.date() == late_window[0]  # not early_window's start


def test_ofx_filename_allows_a_bank_literally_keyed_combined() -> None:
    """No actual collision to guard against: a per-account name always carries a currency segment
    a combined one never has, so the two shapes cannot collide even sharing this prefix."""
    account = Account(bank_key="combined", account_id="PL123", currency="PLN")
    assert (
        ofx_filename(account, PERIOD_START, PERIOD_END) == "combined_PLN_2026_05_01-2026_05_31.ofx"
    )


def _as_bytes(ofx_text: str):
    import io

    return io.BytesIO(ofx_text.encode("utf-8"))


# ------------------------------------------------------------------ ACCTTYPE mapping (issue #22)


@pytest.mark.parametrize(
    ("cash_account_type", "accttype"),
    [
        ("CACC", "CHECKING"),
        ("SVGS", "SAVINGS"),
        # The explicit fallback: absence (a pre-v2-schema session) and OTHR (the bank explicitly
        # answering "not otherwise specified") carry the same information content - a shrug.
        (None, "CHECKING"),
        ("OTHR", "CHECKING"),
    ],
)
def test_accttype_for_maps_the_carryable_vocabulary(
    cash_account_type: str | None, accttype: str
) -> None:
    assert accttype_for(cash_account_type) == accttype


@pytest.mark.parametrize("cash_account_type", ["CARD", "CASH", "LOAN"])
def test_accttype_for_refuses_a_documented_uncarryable_type(cash_account_type: str) -> None:
    """CARD/CASH/LOAN have no honest ACCTTYPE in the bank message set - never guess one."""
    with pytest.raises(UncarryableAccountType) as excinfo:
        accttype_for(cash_account_type)
    assert excinfo.value.cash_account_type == cash_account_type
    assert cash_account_type in str(excinfo.value)


def test_accttype_for_card_says_what_to_do() -> None:
    """The standing exit-1 for a card account must tell the user their options, not just fail."""
    with pytest.raises(UncarryableAccountType, match="unlink the account or wait"):
        accttype_for("CARD")


def test_accttype_for_refuses_an_undocumented_value() -> None:
    """TRAN is real ISO 20022 but outside Enable Banking's documented six-value vocabulary -
    seeing it means the premise the mapping is built on is broken, which must be loud."""
    with pytest.raises(UncarryableAccountType, match="TRAN"):
        accttype_for("TRAN")


def test_build_statement_maps_the_stored_account_type() -> None:
    account = Account(
        bank_key="alior", account_id="PL123", currency="PLN", cash_account_type="SVGS"
    )
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)
    assert stmt.account_type == "SAVINGS"
    assert "<ACCTTYPE>SAVINGS" in statement_to_ofx(stmt)


def test_build_statement_falls_back_to_checking_explicitly() -> None:
    """Same value as ofxstatement's default, but chosen by accttype_for(), not inherited."""
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    stmt = build_statement(account, _txns(), PERIOD_START, PERIOD_END)
    assert stmt.account_type == "CHECKING"
    assert "<ACCTTYPE>CHECKING" in statement_to_ofx(stmt)


def test_build_statement_raises_for_an_uncarryable_type() -> None:
    """run.py refuses such an account before it gets here; a raise here is the backstop."""
    account = Account(
        bank_key="alior", account_id="PL123", currency="PLN", cash_account_type="CARD"
    )
    with pytest.raises(UncarryableAccountType):
        build_statement(account, _txns(), PERIOD_START, PERIOD_END)


def test_savings_statement_roundtrips_through_ofxtools() -> None:
    account = Account(
        bank_key="alior", account_id="PL123", currency="PLN", cash_account_type="SVGS"
    )
    ofx_text = statement_to_ofx(build_statement(account, _txns(), PERIOD_START, PERIOD_END))
    tree = OFXTree()
    tree.parse(_as_bytes(ofx_text))
    assert tree.convert().statements[0].account.accttype == "SAVINGS"


def test_to_ascii_strips_control_characters() -> None:
    """docs/adr-input-hardening.md decision 4a: the `< 128` filter passed C0 controls and DEL.

    A memo holding `\x02` aborted the writer's minidom step with an uncaught ExpatError — the whole
    run, not the account. Zero occurrences were measured in real data, so stripping breaks nothing.
    """
    assert to_ascii("a\x00b\x02c\x1fd\x7fe") == "abcde"
    # Only the C whitespace controls stand in for a space, keeping the word boundary they replaced:
    # two tokens for GnuCash's matcher rather than one run-together one. U+001F is deliberately not
    # one of them even though `str.isspace()` says otherwise — see `_WHITESPACE_CONTROLS`, and note
    # the first assertion above expects it dropped.
    assert to_ascii("ACME\tInvoice\n7") == "ACME Invoice 7"
    assert to_ascii("a\x0bb\x0cc\rd") == "a b c d"
    # Ordinary text is untouched, and the existing folding still folds.
    assert to_ascii("Kämpf OÜ") == "Kampf OU"


def test_hostile_text_survives_the_real_writer() -> None:
    """The end-to-end version: what matters is the writer, not the folded string in isolation.

    An earlier version of this test called `xml.sax.saxutils.escape` and claimed it "would raise on
    a control character". It does not — it only substitutes `& < >` — so the test touched no writer
    and its comment advertised coverage that did not exist. This drives the real
    `build_statement` -> `statement_to_ofx` path, where the uncaught `ExpatError` came from.
    """
    # Built with chr() so no editor or tool can quietly normalise away the bytes this test
    # exists to carry: every C0 code point, plus DEL.
    hostile = "".join(chr(code) for code in range(0x00, 0x21)) + chr(0x7F)
    txn = Txn(
        id="t-hostile",
        date=date(2026, 6, 10),
        amount=Decimal("-1.00"),
        currency="PLN",
        payee=f"ACME{hostile}Sp. z o.o.",
        memo=f"Invoice{hostile}7",
        # The fourth raw-passthrough field: this one reaches BANKACCTTO unfolded, and a control in
        # it aborted the whole run rather than the account.
        counterparty_iban="PL00" + chr(0x02) + "00000000000000000002",
    )
    account = Account(bank_key="alior", account_id=_OWN_ACCTID, currency="PLN")
    xml = statement_to_ofx(build_statement(account, [txn], date(2026, 6, 1), date(2026, 6, 30)))
    # Serialization is the assertion: it raised ExpatError before decision 4.
    assert "<OFX>" in xml
    # No control byte in the document at all beyond the CR and LF of its own line endings.
    assert not any(ord(char) < 0x20 and ord(char) not in (0x0D, 0x0A) for char in xml)
    # The unusable counterparty number is omitted from the aggregate rather than failing the
    # account - libofx does not parse BANKACCTTO, so the folded MEMO copy is the load-bearing one.
    assert "BANKACCTTO" not in xml
    # The memo survived with its word boundary intact rather than running the tokens together. The
    # hostile run holds real spaces and several separator controls, so it folds to a whitespace run.
    assert "Invoice" in xml and "Invoice7" not in xml
    # And the *folded* copy of the counterparty number is still in the memo, control-free: that is
    # the copy GnuCash's matcher actually tokenizes, so the gate above must not have cost it.
    assert "PL0000000000000000000002" in xml


def test_a_printable_counterparty_number_still_reaches_bankacctto() -> None:
    """The gate must not cost the ordinary case the aggregate it exists to emit."""
    txn = Txn(
        id="t-1",
        date=date(2026, 6, 10),
        amount=Decimal("-1.00"),
        currency="PLN",
        payee="ACME",
        memo="Invoice 7",
        counterparty_iban="PL00000000000000000000002",
    )
    account = Account(bank_key="alior", account_id=_OWN_ACCTID, currency="PLN")
    xml = statement_to_ofx(build_statement(account, [txn], date(2026, 6, 1), date(2026, 6, 30)))
    assert "BANKACCTTO" in xml and "PL00000000000000000000002" in xml


def test_validate_raw_fields_accepts_every_real_fitid_alphabet() -> None:
    """rejected option D: `[A-Za-z0-9-]` would reject real FITIDs at three of six banks.

    `entry_reference` is the majority FITID source and carries `- . _ / |` in the measured data, so
    the check is "printable ASCII, no controls" rather than a tight alphanumeric whitelist.
    """
    validate_raw_fields(
        "PL00000000000000000000001",
        "PLN",
        ["abc123", "a-b", "a.b", "a_b", "a/b", "a|b", "2026-06-10/1", "REF 7"],
    )


@pytest.mark.parametrize(
    ("account_id", "currency", "txn_ids", "expect"),
    [
        ("acct-1", "PLN", ["ok", "bad\x02id"], "FITID"),
        ("acct\x001", "PLN", ["ok"], "ACCTID"),
        ("acct-1", "XX", ["ok"], "CURDEF"),
        ("acct-1", "pln", ["ok"], "CURDEF"),
        ("acct-1", "PLN", ["ok", "café"], "FITID"),
        ("acct-1", "PLN", [""], "FITID"),
    ],
)
def test_validate_raw_fields_rejects_what_the_file_cannot_carry(
    account_id: str, currency: str, txn_ids: list[str], expect: str
) -> None:
    """Validated, never repaired: a mutated FITID re-imports as new, a mutated ACCTID orphans."""
    with pytest.raises(ValueError, match=expect):
        validate_raw_fields(account_id, currency, txn_ids)


def test_a_rejected_identifier_is_described_not_quoted() -> None:
    """An ACCTID *is* the account number, so it must not reach the summary users paste.

    The offending code points are named, because that is what a bug report needs and none of them
    is anybody's account number.
    """
    with pytest.raises(ValueError) as caught:
        validate_raw_fields("PL9999\x0299999", "PLN", ["ok"])
    message = str(caught.value)
    assert "U+0002" in message and "12 chars" in message
    assert "PL9999" not in message  # the value itself never appears


def test_validate_raw_fields_rejects_an_unusable_bank_id() -> None:
    """BANKID is a fifth field written raw, and `bank_id_for` does not sanitize it.

    It only strips whitespace, upper-cases and truncates — so a control character in a
    hand-written `bankid` option reaches the file exactly as an ASPSP's would: aborting
    serialization after coverage advanced, or (on Windows) coming back from libofx mutated and
    orphaning the account. Config is a likelier source of a typo than a hostile server, not a
    rarer one.
    """
    from gnucash_ofx.ofxout import bank_id_for

    assert bank_id_for("alior", "ALBP" + chr(0x02) + "PLPW") == "ALBP" + chr(0x02) + "PLPW"
    with pytest.raises(ValueError, match="BANKID"):
        validate_raw_fields(
            _OWN_ACCTID,
            "PLN",
            ["t1"],
            bank_id_for("alior", "ALBP" + chr(0x02) + "PLPW"),
        )
    # The ordinary configured BIC, and the truncated-key fallback, both pass.
    validate_raw_fields(_OWN_ACCTID, "PLN", ["t1"], bank_id_for("alior", "ALBPPLPW"))
    validate_raw_fields(_OWN_ACCTID, "PLN", ["t1"], bank_id_for("alior"))


@pytest.mark.parametrize("code", range(0x80, 0xA0))
def test_no_c1_control_survives_folding(code: int) -> None:
    """Settles docs/adr-input-hardening.md open question 3 with an assertion, not a prediction.

    The ADR guessed the C1 range was "likely moot" because the `< 128` filter already excludes it,
    and #45 shipped without pinning that — while deliberately treating one C1 character, NEL, as a
    separator. So the range is now asserted rather than assumed: NEL keeps the word boundary it
    stands for, every other C1 control vanishes, and nothing in the range reaches the file.
    """
    folded = to_ascii("A" + chr(code) + "B")
    assert folded == ("A B" if code == 0x85 else "AB")
    assert all(ord(char) < 0x80 for char in folded)
