"""Tests for tools/evidence/state_census.py."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from tools.evidence.state_census import discover_banks, state_census

from gnucash_ofx.state import LinkedAccount, SessionState, save_session

TODAY = date(2026, 8, 9)


def test_state_census_counts_identity_fields_and_expiry(tmp_path: Path) -> None:
    session = SessionState(
        bank="alior",
        session_id="s1",
        valid_until=date(2026, 10, 27),
        accounts=(
            # synthetic: all-zeros IBAN, per AGENTS.md's Data section - not a real account.
            LinkedAccount(uid="u1", iban="PL00000000000000000000001111"),
            LinkedAccount(uid="u2", identification_hash="h2", currency="PLN"),
            LinkedAccount(uid="u3"),  # nothing captured: resolves to the bare uid
        ),
    )
    save_session(tmp_path, session)

    census = state_census(tmp_path, "alior", today=TODAY)

    assert census is not None
    assert census.bank == "alior"
    assert census.schema_version == 2
    assert census.account_count == 3
    assert census.with_iban == 1
    assert census.with_identification_hash == 1
    assert census.distinct_identification_hashes == 1
    assert census.with_currency == 1
    assert census.would_resolve_to_bare_uid == 1  # only u3: no iban, no identification_hash
    assert census.days_until_expiry == (date(2026, 10, 27) - TODAY).days
    assert census.with_raw_account_entry == 0  # no session_raw was captured
    assert census.all_account_ids_lengths == {}


def test_state_census_separates_carrying_a_hash_from_carrying_a_distinct_one(
    tmp_path: Path,
) -> None:
    """Every account has one; two of them have the same one, which presence cannot report."""
    session = SessionState(
        bank="revolut",
        session_id="s1",
        valid_until=date(2026, 11, 10),
        accounts=(
            LinkedAccount(uid="u1", identification_hash="h1"),
            LinkedAccount(uid="u2", identification_hash="shared"),
            LinkedAccount(uid="u3", identification_hash="shared"),
        ),
    )
    save_session(tmp_path, session)

    census = state_census(tmp_path, "revolut", today=TODAY)

    assert census is not None
    assert census.with_identification_hash == 3
    assert census.distinct_identification_hashes == 2


def test_state_census_counts_the_identification_shape(tmp_path: Path) -> None:
    session = SessionState(
        bank="revolut",
        session_id="s1",
        valid_until=date(2026, 11, 10),
        accounts=(
            LinkedAccount(uid="u1", name="Main", product="Standard", usage="PRIV"),
            LinkedAccount(uid="u2", identification_scheme="BBAN", name="Pocket"),
            LinkedAccount(uid="u3", identification_scheme="CPAN"),
            LinkedAccount(uid="u4", identification_scheme="BBAN"),
        ),
    )
    save_session(tmp_path, session)

    census = state_census(tmp_path, "revolut", today=TODAY)

    assert census is not None
    assert census.with_name == 2
    assert census.with_product == 1
    assert census.with_usage == 1
    assert census.identification_scheme_kinds == {"BBAN": 2, "CPAN": 1}


@pytest.mark.parametrize(
    "scheme",
    [
        # All synthetic, per AGENTS.md's Data section - each in the wrong field on purpose.
        "PL00000000000000000000001111",  # a 28-character Polish IBAN
        "NO0000000000000",  # 15 characters, letter-initial: a Norwegian IBAN is token-length
        "BE00000000000000",  # 16 characters, likewise Belgian
        "XXXXXXXXXXXX0000",  # a masked PAN
        "ACCT00001",  # a short proprietary id
    ],
)
def test_state_census_never_emits_a_scheme_name_shaped_like_an_identifier(
    tmp_path: Path, scheme: str
) -> None:
    """A bank could put anything in ``scheme_name``; the census counts it without repeating it.

    Length is not what the gate rests on - two of these are shorter than a real code list allows
    for - so the cases that a length-and-charset rule would wave through are pinned by name.
    """
    session = SessionState(
        bank="odd",
        session_id="s1",
        valid_until=date(2026, 11, 10),
        accounts=(LinkedAccount(uid="u1", identification_scheme=scheme),),
    )
    save_session(tmp_path, session)

    census = state_census(tmp_path, "odd", today=TODAY)

    assert census is not None
    assert census.identification_scheme_kinds == {"<non-conforming>": 1}


def test_state_census_admits_an_unfamiliar_all_letters_scheme_name(tmp_path: Path) -> None:
    """The gate excludes identifiers, not unknown vocabulary - reporting the latter is the point.

    ``PLKNR`` is in this repo's own session fixture and is in no ISO 20022 code list, so an
    allow-list of the four names seen so far would have bucketed a real bank's real scheme.
    """
    session = SessionState(
        bank="odd",
        session_id="s1",
        valid_until=date(2026, 11, 10),
        accounts=(LinkedAccount(uid="u1", identification_scheme="PLKNR"),),
    )
    save_session(tmp_path, session)

    census = state_census(tmp_path, "odd", today=TODAY)

    assert census is not None
    assert census.identification_scheme_kinds == {"PLKNR": 1}


def test_state_census_reports_all_account_ids_lengths_from_the_raw_body(tmp_path: Path) -> None:
    session = SessionState(
        bank="revolut",
        session_id="s1",
        valid_until=date(2026, 11, 10),
        accounts=(
            LinkedAccount(uid="u1"),
            LinkedAccount(uid="u2"),
            LinkedAccount(uid="u3"),
            LinkedAccount(uid="u4"),  # linked, but missing from the raw body
        ),
        raw={
            "accounts": [
                {
                    "uid": "u1",
                    "all_account_ids": [{"scheme_name": "IBAN"}, {"scheme_name": "BBAN"}],
                },
                {"uid": "u2", "all_account_ids": [{"scheme_name": "IBAN"}]},
                {"uid": "u3"},  # present, but the key is absent: counted as zero identifiers
            ]
        },
    )
    save_session(tmp_path, session)

    census = state_census(tmp_path, "revolut", today=TODAY)

    assert census is not None
    assert census.account_count == 4
    # The denominator for the map below is this, not account_count: u4 has no raw entry at all.
    assert census.with_raw_account_entry == 3
    assert census.all_account_ids_lengths == {0: 1, 1: 1, 2: 1}


def test_state_census_reads_v1_layout_as_schema_version_none(tmp_path: Path) -> None:
    payload = {
        "bank": "erste",
        "session_id": "s2",
        "valid_until": "2026-11-05",
        "account_ids": ["u1", "u2"],
        "account_ibans": {"u1": "PL00000000000000000000000001"},
        "account_bics": {},
    }
    (tmp_path / "erste.json").write_text(json.dumps(payload), encoding="utf-8")

    census = state_census(tmp_path, "erste", today=TODAY)

    assert census is not None
    assert census.schema_version is None
    assert census.account_count == 2
    assert census.with_iban == 1
    assert census.would_resolve_to_bare_uid == 1  # u2 has neither iban nor identification_hash


def test_state_census_is_none_for_an_unlinked_bank(tmp_path: Path) -> None:
    assert state_census(tmp_path, "nobody", today=TODAY) is None


def test_state_census_is_none_for_an_unreadable_file(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    assert state_census(tmp_path, "broken", today=TODAY) is None


def test_discover_banks_lists_session_files_by_stem(tmp_path: Path) -> None:
    save_session(
        tmp_path, SessionState(bank="alior", session_id="s1", valid_until=date(2026, 10, 27))
    )
    save_session(
        tmp_path, SessionState(bank="erste", session_id="s2", valid_until=date(2026, 11, 5))
    )
    (tmp_path / "alior.json.bak").write_text("{}", encoding="utf-8")

    assert discover_banks(tmp_path) == ["alior", "erste"]


def test_discover_banks_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert discover_banks(tmp_path / "does-not-exist") == []
