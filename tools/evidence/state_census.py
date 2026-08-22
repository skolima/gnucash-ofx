"""Census of ``state/``: schema-version distribution, identity counts and expiry, per bank.

Never account-keyed: :class:`StateCensus` reports counts, not which account is which — see
``docs/adr-evidence-tool.md`` decision 3.

**Identification shape, not just identification presence.** Onboarding a new bank asks what its
accounts are *identified by*, not only how many carry an IBAN: which ``scheme_name`` vocabulary it
uses for the ``other.identification`` form, whether it labels accounts at all (``name`` /
``product`` / ``usage``), and how many entries each account's ``all_account_ids`` holds — a length
above one is a second number for the same account, which is where an ``ACCTID`` collision between
sibling accounts can be resolved from. Each of those was hand-scripted on every previous new-bank
pass; they are counts here so the next one is a function call.

``all_account_ids`` is read from ``SessionState.raw`` because nothing lifts it into
:class:`~gnucash_ofx.state.LinkedAccount` — and a session linked under the v1 schema has no raw
body at all, so ``with_raw_account_entry`` is the honest denominator for that map rather than
``account_count``.

``distinct_identification_hashes`` is the one field here that counts *values* rather than
presences, and it is here because presence answered the wrong question. Where accounts share an
account number, whether ``identification_hash`` can tell them apart is the thing worth knowing, and
``with_identification_hash`` cannot say: five accounts each carrying one may be carrying the same
one. Measured 2026-08-13 on the connection that motivated this: 5 accounts, 2 distinct IBANs, 5
distinct hashes. The count never leaves as a value — see
:mod:`tools.evidence.coverage_reconciliation` for the same shape applied to the ledger's own keys.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from gnucash_ofx.run import _stored_acctids
from gnucash_ofx.state import (
    LinkedAccount,
    SessionState,
    StateError,
    days_until_expiry,
    load_session,
    session_path,
)

# invariant: a census field keyed on an ASPSP string is gated and bucketed, never counted as
# received. AGENTS.md#invariants
#
# A scheme name is an ISO 20022 code-list token (IBAN, BBAN, CPAN, PLKNR - all four observed in
# this project), and this census reports the vocabulary itself rather than a count per shape. So
# the one field here whose *keys* come off the wire gets a gate, and anything failing it is counted
# under a fixed placeholder: decision 2's guarantee is that a raw identifier is *structurally*
# unable to reach a census's output, and a bare dict keyed on an ASPSP string would rest that on
# the ASPSP instead.
#
# **Letters only is the load-bearing part, not the length.** Every account-identifier form carries
# digits - IBAN, BBAN, masked PAN, proprietary id - so excluding digits excludes the whole class,
# while a length-and-charset shape does not: a Norwegian IBAN is 15 characters and a Belgian one
# 16, both letter-initial. Same correction AGENTS.md already made for reference prefixes ("an
# explicit set with per-prefix id patterns, not a generic <PREFIX>-<digits> rule"). A genuinely new
# all-letters code from a future ASPSP is still admitted - reporting an unknown bank's vocabulary
# is the point of the field - and one carrying a digit is bucketed and counted, never lost.
_SCHEME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z_]{1,11}$")
_NON_CONFORMING_SCHEME = "<non-conforming>"


@dataclass(frozen=True, slots=True)
class StateCensus:
    bank: str
    schema_version: int | None  # None = v1 fallback (key absent)
    account_count: int
    with_iban: int
    with_identification_hash: int
    # Presence is not distinctness, and the difference is the whole question when accounts share
    # an account number: fewer distinct hashes than accounts carrying one means the fallback
    # ACCTID would collapse too. Counted for the same reason distinct_coverage_keys is.
    distinct_identification_hashes: int
    with_currency: int
    with_name: int
    with_product: int
    with_usage: int
    with_raw_account_entry: int  # live accounts whose AccountResource survives in session_raw
    would_resolve_to_bare_uid: int  # a lower bound - see the module docstring on _known_acctid use
    days_until_expiry: int
    # scheme_name -> accounts using it; only accounts carrying one appear (an IBAN needs no scheme).
    identification_scheme_kinds: dict[str, int] = field(default_factory=dict)
    # len(all_account_ids) -> accounts with that many; denominator is with_raw_account_entry.
    all_account_ids_lengths: dict[int, int] = field(default_factory=dict)


def discover_banks(state_dir: Path) -> list[str]:
    """Bank keys with a session file, from the filenames actually present.

    Not from ``config.toml``: a stale or renamed bank's leftover state file is itself something a
    census should be able to see, not something config presence would filter out first.
    """
    if not state_dir.is_dir():
        return []
    return sorted(p.stem for p in state_dir.glob("*.json"))


def _schema_version(state_dir: Path, bank: str) -> int | None:
    path = session_path(state_dir, bank)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version")
    return version if isinstance(version, int) else None


def _scheme_kinds(accounts: tuple[LinkedAccount, ...]) -> dict[str, int]:
    """The ``scheme_name`` vocabulary in use, gated by :data:`_SCHEME_TOKEN`.

    An unrecognised value is counted, never emitted: a bank that put something account-sized in
    ``scheme_name`` would otherwise put it in this census's output.
    """
    kinds: dict[str, int] = {}
    for account in accounts:
        scheme = account.identification_scheme
        if not scheme:
            continue
        key = scheme if _SCHEME_TOKEN.match(scheme) else _NON_CONFORMING_SCHEME
        kinds[key] = kinds.get(key, 0) + 1
    return dict(sorted(kinds.items()))


def _raw_accounts_by_uid(session: SessionState) -> dict[str, dict[str, Any]]:
    """The ``POST /sessions`` account objects still in ``session_raw``, keyed on ``uid``.

    Empty for a v1-schema session, which stores no raw body — absent, not zero-length, and the
    caller has to keep those two apart.
    """
    entries = session.raw.get("accounts")
    if not isinstance(entries, list):
        return {}
    return {
        str(entry["uid"]): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("uid")
    }


def _all_account_ids_lengths(session: SessionState) -> tuple[int, dict[int, int]]:
    """How many identifiers each account carries: ``(accounts with a raw entry, length -> count)``.

    The length only, never an identifier. A missing or non-list ``all_account_ids`` counts as 0
    rather than being dropped, so the map's values sum to the count returned beside it.
    """
    raw_accounts = _raw_accounts_by_uid(session)
    matched = 0
    lengths: dict[int, int] = {}
    for account in session.accounts:
        entry = raw_accounts.get(account.uid)
        if entry is None:
            continue
        matched += 1
        ids = entry.get("all_account_ids")
        length = len(ids) if isinstance(ids, list) else 0
        lengths[length] = lengths.get(length, 0) + 1
    return matched, dict(sorted(lengths.items()))


def state_census(state_dir: Path, bank: str, *, today: date | None = None) -> StateCensus | None:
    """One bank's state-file census, or ``None`` if there is no readable session for it.

    ``would_resolve_to_bare_uid`` calls :func:`gnucash_ofx.run._stored_acctids`, the real
    resolution the no-fetch callers use — connection-wide shared-IBAN policy included
    (docs/adr-revolut-onboarding.md decision 1) — with the fetch-time hashes necessarily absent.
    It is a **lower bound**, not the fetch-time answer: the real check also consults
    ``identification_hash`` values only a live ``GET /sessions`` call produces, and this tool
    never touches the network (see ``docs/adr-coverage-ledger-and-warnings.md`` §6).
    """
    try:
        session = load_session(state_dir, bank)
    except StateError:
        return None
    if session is None:
        return None
    accounts = session.accounts
    with_raw_entry, id_lengths = _all_account_ids_lengths(session)
    return StateCensus(
        bank=bank,
        schema_version=_schema_version(state_dir, bank),
        account_count=len(accounts),
        with_iban=sum(1 for a in accounts if a.iban),
        with_identification_hash=sum(1 for a in accounts if a.identification_hash),
        distinct_identification_hashes=len({a.identification_hash for a in accounts} - {None}),
        with_currency=sum(1 for a in accounts if a.currency),
        with_name=sum(1 for a in accounts if a.name),
        with_product=sum(1 for a in accounts if a.product),
        with_usage=sum(1 for a in accounts if a.usage),
        with_raw_account_entry=with_raw_entry,
        would_resolve_to_bare_uid=sum(
            1
            for a, acctid in zip(accounts, _stored_acctids(accounts), strict=True)
            if acctid == a.uid
        ),
        days_until_expiry=days_until_expiry(session, today),
        identification_scheme_kinds=_scheme_kinds(accounts),
        all_account_ids_lengths=id_lengths,
    )
