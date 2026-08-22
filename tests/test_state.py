"""TDD for session-state persistence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gnucash_ofx.state import (
    LinkedAccount,
    SessionState,
    StateError,
    days_until_expiry,
    load_session,
    save_session,
    session_path,
)

# Synthetic Polish IBAN (see AGENTS.md); the 99999999 bank code is unassigned.
_IBAN = "PL03999999990000000000000005"


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    state = SessionState(
        bank="alior",
        session_id="sess-123",
        valid_until=date(2026, 11, 1),
        accounts=(LinkedAccount("acc-a"), LinkedAccount("acc-b")),
    )
    save_session(tmp_path, state)
    loaded = load_session(tmp_path, "alior")
    assert loaded == state


def test_save_then_load_roundtrips_full_account_details(tmp_path: Path) -> None:
    """Everything captured at link time must survive the round trip — it is unrecoverable."""
    account = LinkedAccount(
        uid="acc-a",
        iban=_IBAN,
        bic="EXMPPLPW",
        currency="PLN",
        identification_hash="hash-a",
        identification_scheme="BBAN",
        name="Konto Osobiste",
        product="Personal Current Account",
        usage="PRIV",
        cash_account_type="CACC",
    )
    state = SessionState(
        bank="alior",
        session_id="sess-123",
        valid_until=date(2026, 11, 1),
        accounts=(account,),
        raw={"session_id": "sess-123", "aspsp": {"name": "Example Bank", "country": "PL"}},
    )
    save_session(tmp_path, state)
    assert load_session(tmp_path, "alior") == state


def test_derived_accessors(tmp_path: Path) -> None:
    state = SessionState(
        bank="alior",
        session_id="s",
        valid_until=date(2026, 11, 1),
        accounts=(
            LinkedAccount("acc-a", iban=_IBAN, bic="EXMPPLPW"),
            LinkedAccount("acc-b"),
        ),
    )
    assert state.account_ids == ("acc-a", "acc-b")
    assert state.account_ibans == {"acc-a": _IBAN}
    assert state.account_bics == {"acc-a": "EXMPPLPW"}
    assert state.by_uid["acc-b"].iban is None


def _write_v1(state_dir: Path, bank: str) -> None:
    """A state file in the original flat layout, as written before account records existed."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{bank}.json").write_text(
        json.dumps(
            {
                "bank": bank,
                "session_id": "sess-old",
                "valid_until": "2026-11-01",
                "account_ids": ["acc-a", "acc-b"],
                "account_ibans": {"acc-a": _IBAN},
                "account_bics": {"acc-a": "EXMPPLPW"},
            }
        ),
        encoding="utf-8",
    )


def test_loads_v1_state_file(tmp_path: Path) -> None:
    """Sessions linked before the schema change must keep working without a re-link."""
    _write_v1(tmp_path, "alior")
    loaded = load_session(tmp_path, "alior")
    assert loaded is not None
    assert loaded.session_id == "sess-old"
    # The v1 fields are exposed exactly as they were.
    assert loaded.account_ids == ("acc-a", "acc-b")
    assert loaded.account_ibans == {"acc-a": _IBAN}
    assert loaded.account_bics == {"acc-a": "EXMPPLPW"}
    # Everything only POST /sessions could tell us is simply unknown until the next link.
    assert loaded.by_uid["acc-a"].currency is None
    assert loaded.by_uid["acc-a"].identification_hash is None
    assert loaded.raw == {}


def _write_xxx_currency(state_dir: Path, bank: str) -> None:
    """A state file as persisted by a session linked before the XXX-placeholder fix."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{bank}.json").write_text(
        json.dumps(
            {
                "bank": bank,
                "session_id": "sess-old",
                "valid_until": "2026-11-01",
                "accounts": [{"uid": "acc-a", "currency": "XXX"}],
            }
        ),
        encoding="utf-8",
    )


def test_load_session_heals_a_currency_already_persisted_as_xxx(tmp_path: Path) -> None:
    """A file written before this fix must not keep re-trusting the placeholder forever.

    ``linked_accounts()`` only runs at link time, so a bogus "XXX" already on disk (Alior,
    2026-08-10 — see docs/adr-xxx-currency-placeholder.md) would otherwise never self-heal
    without a re-link: every load would keep handing it out as a known currency. Re-normalizing
    on load means the very next fetch treats it as unknown and rediscovers the real value from
    ``/balances`` instead.
    """
    _write_xxx_currency(tmp_path, "alior")
    loaded = load_session(tmp_path, "alior")
    assert loaded is not None
    assert loaded.by_uid["acc-a"].currency is None


def test_save_keeps_writing_legacy_keys(tmp_path: Path) -> None:
    """A downgraded build must still find the IBAN, or it would fall back to uid as ACCTID."""
    state = SessionState(
        bank="alior",
        session_id="s",
        valid_until=date(2026, 11, 1),
        accounts=(LinkedAccount("acc-a", iban=_IBAN, bic="EXMPPLPW"),),
    )
    path = save_session(tmp_path, state)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["account_ids"] == ["acc-a"]
    assert payload["account_ibans"] == {"acc-a": _IBAN}
    assert payload["account_bics"] == {"acc-a": "EXMPPLPW"}


def test_save_does_not_leave_a_temp_file_behind(tmp_path: Path) -> None:
    state = SessionState(bank="alior", session_id="s", valid_until=date(2026, 11, 1))
    save_session(tmp_path, state)
    assert [p.name for p in tmp_path.iterdir()] == ["alior.json"]


def test_save_replaces_previous_file_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write must leave the previous consent intact — losing it costs a browser SCA."""
    first = SessionState(bank="alior", session_id="sess-1", valid_until=date(2026, 11, 1))
    save_session(tmp_path, first)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("gnucash_ofx.state.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_session(tmp_path, SessionState("alior", "sess-2", date(2027, 1, 1)))

    assert load_session(tmp_path, "alior") == first
    assert [p.name for p in tmp_path.iterdir()] == ["alior.json"]


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load_session(tmp_path, "nope") is None


def test_load_session_rejects_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "alior.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(StateError, match="not a valid session file"):
        load_session(tmp_path, "alior")


def test_load_session_rejects_missing_required_fields(tmp_path: Path) -> None:
    # Valid JSON, but missing "session_id" - a truncated or hand-edited file, not a parse error.
    (tmp_path / "alior.json").write_text(json.dumps({"bank": "alior"}), encoding="utf-8")
    with pytest.raises(StateError, match="not a valid session file"):
        load_session(tmp_path, "alior")


def test_load_session_rejects_unparseable_valid_until(tmp_path: Path) -> None:
    (tmp_path / "alior.json").write_text(
        json.dumps({"bank": "alior", "session_id": "s", "valid_until": "not-a-date"}),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="not a valid session file"):
        load_session(tmp_path, "alior")


def test_days_until_expiry(tmp_path: Path) -> None:
    state = SessionState(bank="alior", session_id="s", valid_until=date(2026, 6, 30))
    assert days_until_expiry(state, today=date(2026, 6, 20)) == 10
    assert days_until_expiry(state, today=date(2026, 7, 5)) == -5


def test_session_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid bank key"):
        session_path(tmp_path, "../../etc/passwd")
