"""Tests for tools/evidence/filename_prediction.py."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tools.evidence.filename_prediction import filename_prediction

from gnucash_ofx.config import BankConfig
from gnucash_ofx.state import LinkedAccount, SessionState, save_session

BANK_CONFIG = BankConfig(key="millennium", source="enablebanking", options={})


def _link(tmp_path: Path, accounts: tuple[LinkedAccount, ...]) -> None:
    session = SessionState(
        bank="millennium", session_id="s1", valid_until=date(2026, 10, 27), accounts=accounts
    )
    save_session(tmp_path, session)


def test_a_single_account_needs_no_disambiguator(tmp_path: Path) -> None:
    _link(tmp_path, (LinkedAccount(uid="u1", iban="PL00000000000000000000005387", currency="PLN"),))

    prediction = filename_prediction(tmp_path, "millennium", BANK_CONFIG)

    assert prediction is not None
    assert prediction.bank == "millennium"
    assert prediction.predictable_count == 1
    assert prediction.unpredictable_count == 0
    assert prediction.disambiguator_kind == {}


def test_same_currency_distinguishable_ibans_use_the_tail(tmp_path: Path) -> None:
    _link(
        tmp_path,
        (
            LinkedAccount(uid="u1", iban="PL00000000000000000000115387", currency="PLN"),
            LinkedAccount(uid="u2", iban="PL00000000000000000000226942", currency="PLN"),
        ),
    )

    prediction = filename_prediction(tmp_path, "millennium", BANK_CONFIG)

    assert prediction is not None
    assert prediction.predictable_count == 2
    assert prediction.disambiguator_kind == {"iban_tail": 2}


def test_same_currency_colliding_tails_fall_back_to_a_digest(tmp_path: Path) -> None:
    _link(
        tmp_path,
        (
            LinkedAccount(uid="u1", iban="PL00000000000000000000115387", currency="PLN"),
            LinkedAccount(uid="u2", iban="PL00000000000000000000225387", currency="PLN"),
        ),
    )

    prediction = filename_prediction(tmp_path, "millennium", BANK_CONFIG)

    assert prediction is not None
    assert prediction.predictable_count == 2
    assert prediction.disambiguator_kind == {"digest": 2}


def test_one_unknown_currency_suppresses_the_whole_bank(tmp_path: Path) -> None:
    _link(
        tmp_path,
        (
            LinkedAccount(uid="u1", iban="PL00000000000000000000115387", currency="PLN"),
            LinkedAccount(uid="u2"),  # linked under the v1 schema: currency was never captured
        ),
    )

    prediction = filename_prediction(tmp_path, "millennium", BANK_CONFIG)

    assert prediction is not None
    assert prediction.predictable_count == 0
    assert prediction.unpredictable_count == 2
    assert prediction.disambiguator_kind == {}


def test_filename_prediction_is_none_for_an_unlinked_bank(tmp_path: Path) -> None:
    assert filename_prediction(tmp_path, "nobody", BANK_CONFIG) is None
