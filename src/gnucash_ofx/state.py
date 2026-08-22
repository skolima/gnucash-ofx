"""Persist per-bank Open Banking session info (id + consent expiry) between runs.

Stored as one JSON file per bank under the state dir (gitignored). Used so monthly ``fetch``
runs reuse an existing consent and so ``status`` can report days until re-consent is needed.

The file also carries everything ``POST /sessions`` told us about each account at link time.
That response is the **only** place the full account resource appears — ``GET /accounts/{uid}``
is 404 in Restricted Mode and ``GET /sessions/{id}`` returns bare UID strings — so anything not
captured here is unrecoverable until the next browser SCA dance, ~180 days away. See
``docs/enable-banking.md``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from gnucash_ofx.models import currency_or_none
from gnucash_ofx.naming import safe_component

# Bumped when the on-disk shape changes. Absent means the original (v1) flat layout, which is
# still read; see _accounts_from_payload.
SCHEMA_VERSION = 2


class StateError(Exception):
    """Raised when a ``state/<bank>.json`` file exists but cannot be read as one.

    A distinct type (rather than letting json.JSONDecodeError/KeyError/etc. propagate raw) so
    callers can treat "this bank's state is unreadable" the same way they already treat "this
    bank is not linked" or "this bank's consent expired" — a per-bank problem, not one that
    should abort an entire run or crash a read-only command like ``status``.
    """


@dataclass(frozen=True, slots=True)
class LinkedAccount:
    """What ``POST /sessions`` reported about one account, captured at link time.

    Every field beyond ``uid`` is optional: ASPSPs populate wildly different subsets, and a
    session linked under the v1 schema has none of them. Consumers must degrade rather than
    assume presence.
    """

    uid: str
    # Account number in IBAN form (via ``account_identifier``), from ``account_id``.
    iban: str | None = None
    # ``account_servicer.bic_fi``. Informational only — Enable Banking documents BICs as
    # unreliable identifiers, so this is never used as BANKID.
    bic: str | None = None
    currency: str | None = None
    # Documented as stable "for matching accounts between multiple sessions", unlike ``uid``,
    # which is regenerated on every re-link. Also available from ``GET /sessions`` at fetch time.
    identification_hash: str | None = None
    # ``account_id.other.scheme_name`` (BBAN / CPAN / ...): what the number claims to be. The
    # signal that distinguishes a counterparty account number from a card PAN.
    identification_scheme: str | None = None
    # Human labels, for progress output and for matching an output file to a GnuCash account.
    name: str | None = None
    product: str | None = None
    usage: str | None = None
    cash_account_type: str | None = None


@dataclass(frozen=True, slots=True)
class SessionState:
    bank: str
    session_id: str
    valid_until: date
    accounts: tuple[LinkedAccount, ...] = ()
    # The verbatim POST /sessions body. Kept because it is the only copy of any field we did not
    # think to name above — including ones added by future API versions. Holds account numbers
    # and account names: same sensitivity as the cache dir, never logged.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(a.uid for a in self.accounts)

    @property
    def by_uid(self) -> dict[str, LinkedAccount]:
        return {a.uid: a for a in self.accounts}

    @property
    def account_ibans(self) -> dict[str, str]:
        return {a.uid: a.iban for a in self.accounts if a.iban}

    @property
    def account_bics(self) -> dict[str, str]:
        return {a.uid: a.bic for a in self.accounts if a.bic}


def session_path(state_dir: Path, bank: str) -> Path:
    return state_dir / f"{safe_component(bank, 'bank key')}.json"


def _account_payload(account: LinkedAccount) -> dict[str, Any]:
    # Omit absent fields rather than writing a wall of nulls; load fills them back as None.
    return {
        key: value
        for key, value in {
            "uid": account.uid,
            "iban": account.iban,
            "bic": account.bic,
            "currency": account.currency,
            "identification_hash": account.identification_hash,
            "identification_scheme": account.identification_scheme,
            "name": account.name,
            "product": account.product,
            "usage": account.usage,
            "cash_account_type": account.cash_account_type,
        }.items()
        if value is not None
    }


def save_session(state_dir: Path, state: SessionState) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = session_path(state_dir, state.bank)
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "bank": state.bank,
        "session_id": state.session_id,
        "valid_until": state.valid_until.isoformat(),
        "accounts": [_account_payload(a) for a in state.accounts],
        # Written for one release so a downgraded build still resolves ACCTID from the IBAN
        # instead of falling back to the uid — which would orphan already-imported accounts.
        "account_ids": list(state.account_ids),
        "account_ibans": state.account_ibans,
        "account_bics": state.account_bics,
    }
    if state.raw:
        payload["session_raw"] = state.raw
    _write_atomic(path, json.dumps(payload, indent=2))
    return path


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same dir + ``os.replace``.

    Losing a state file costs a browser SCA dance to recover, so a torn write (crash, full disk)
    must leave the previous file intact rather than a truncated one.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _accounts_from_payload(data: dict[str, Any]) -> tuple[LinkedAccount, ...]:
    """Read the account records, reconstructing them from the v1 flat layout when needed.

    ``currency`` is re-normalized on every load, not just at link time: a session linked before
    this fix may have already persisted ISO 4217's ``XXX`` placeholder to disk
    (see docs/adr-xxx-currency-placeholder.md), and re-reading it verbatim forever would keep
    ``currency_known`` true and the /balances discovery fallback permanently skipped for that
    account. Normalizing here means a file already holding ``XXX`` self-heals on its next fetch
    without needing a re-link.
    """
    entries = data.get("accounts")
    if isinstance(entries, list) and all(isinstance(e, dict) for e in entries):
        return tuple(
            LinkedAccount(
                uid=str(e["uid"]),
                iban=e.get("iban"),
                bic=e.get("bic"),
                currency=currency_or_none(e.get("currency")),
                identification_hash=e.get("identification_hash"),
                identification_scheme=e.get("identification_scheme"),
                name=e.get("name"),
                product=e.get("product"),
                usage=e.get("usage"),
                cash_account_type=e.get("cash_account_type"),
            )
            for e in entries
        )
    # v1: account_ids plus two parallel uid-keyed dicts, everything else unknown until re-link.
    ibans = data.get("account_ibans") or {}
    bics = data.get("account_bics") or {}
    return tuple(
        LinkedAccount(uid=str(uid), iban=ibans.get(uid), bic=bics.get(uid))
        for uid in data.get("account_ids", [])
    )


def load_session(state_dir: Path, bank: str) -> SessionState | None:
    path = session_path(state_dir, bank)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionState(
            bank=data["bank"],
            session_id=data["session_id"],
            valid_until=date.fromisoformat(data["valid_until"]),
            accounts=_accounts_from_payload(data),
            raw=data.get("session_raw") or {},
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"{path} is not a valid session file: {exc}") from exc


def days_until_expiry(state: SessionState, today: date | None = None) -> int:
    """Days until consent expires; negative once expired."""
    today = today or date.today()
    return (state.valid_until - today).days
