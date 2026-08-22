"""Cross-reference the coverage ledger against what the run log says was actually requested.

See ``docs/adr-evidence-tool.md`` decision 6: the two sides key on different things - a truncated
``uid`` in the log (:func:`gnucash_ofx.runlog.redact_uid_path`), ``sha1(ACCTID)`` in the ledger
(:func:`gnucash_ofx.coverage.account_key`) - so the join routes through the live session's own
``uid -> ACCTID`` mapping (:func:`gnucash_ofx.run._known_acctid`) rather than deriving one key from
the other directly. Whether the truncated form is collision-free within one bank is not yet known
(open question 1); :func:`coverage_reconciliation` reports the collision count so that assumption
never gets to fail silently.

A per-account, day-level cross-check ("does the ledger show this exact day as covered") is out of
scope here - open question 2 of the ADR: a coverage-write failure is never persisted anywhere
today, so a mismatch cannot yet be told apart from that known, silent case.

**Two live accounts can resolve to one ledger key, and that is the thing counting accounts cannot
see.** ``coverage.account_key`` digests the ``ACCTID``, and resolution follows the shared identity
fields - so a connection whose accounts share an account number could collapse N accounts onto
fewer than N keys, and each of them then finds a ledger entry that is not its own.
``covered_accounts`` reports every one of them as covered, because each lookup succeeds; what it
cannot report is that they succeeded against the same entry, so fetching one advances
``covered_through`` for the others. ``distinct_coverage_keys`` and ``accounts_sharing_a_key`` make
the collapse a number rather than something a scratch script has to go and find. Measured on this
machine 2026-08-13: one connection with 5 live accounts, 2 distinct keys, 4 accounts sharing a key.
``docs/adr-revolut-onboarding.md`` decision 1 (accepted 2026-08-14) now forbids exactly that:
resolution goes through ``run._stored_acctids``, the same connection-wide policy the fetch and
``status`` use, so this module measures with the fix's own resolution rather than mis-measuring
the fix. A collapse reported here is therefore a real defect again, not the known Revolut shape.

**Both key counts are a lower bound on the collapse, for the same reason
``StateCensus.would_resolve_to_bare_uid`` is one** (``docs/adr-evidence-tool.md`` decision 3):
resolution runs with ``id_hashes`` empty, because the fetch-time hashes come from a live
``GET /sessions`` and this tool never touches the network. An account with neither a stored
``iban`` nor a stored ``identification_hash`` therefore resolves to its own ``uid`` here - unique
by construction, so it reports as keying alone - while at fetch time it can resolve through a hash
its siblings share. ``accounts_sharing_a_key == 0`` is "no collapse *visible from stored state*",
never "no collapse". The direction is the safe one for a warning and the unsafe one for a
conclusion: do not read a zero here as evidence that a bank's accounts are distinct.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from gnucash_ofx.coverage import account_key, load_coverage
from gnucash_ofx.run import _stored_acctids
from gnucash_ofx.runlog import load_requests, redact_uid_path
from gnucash_ofx.state import StateError, load_session

_PATH_ACCOUNT = re.compile(r"^/accounts/([^/]+)/")


@dataclass(frozen=True, slots=True)
class CoverageReconciliation:
    bank: str
    live_accounts: int
    # Live accounts that find an entry in the ledger - NOT a count of entries, and not a health
    # signal on its own: read it against distinct_coverage_keys, or a many-to-one collapse reads
    # as full coverage. See the module docstring.
    covered_accounts: int
    # Keys the live accounts resolve to, not entries the ledger holds: fewer than live_accounts
    # means a collapse, equal means each account keys on its own value (covered or not).
    # A lower bound on the collapse, like would_resolve_to_bare_uid - id_hashes is empty here.
    distinct_coverage_keys: int
    accounts_sharing_a_key: int  # live accounts whose key is not theirs alone (0 when none is)
    ledger_unreadable: bool
    ambiguous_truncated_uids: int  # see the module docstring - open question 1
    requests_total: int
    requests_matched_to_live_account: int
    requests_unmatched: int


def _truncated_uid(uid: str) -> str:
    """The form ``uid`` takes inside a redacted run-log path, via the log's own function.

    Reuses :func:`redact_uid_path` on a synthetic path rather than re-implementing its ``[:4]``/
    ``[-4:]`` rule, so the two truncations can never drift apart.
    """
    match = _PATH_ACCOUNT.match(redact_uid_path(f"/accounts/{uid}/x"))
    assert match is not None  # redact_uid_path always emits this shape for /accounts/<uid>/...
    return match.group(1)


def coverage_reconciliation(state_dir: Path, bank: str) -> CoverageReconciliation | None:
    try:
        session = load_session(state_dir, bank)
    except StateError:
        return None
    if session is None:
        return None

    truncated: dict[str, str] = {}
    ambiguous: set[str] = set()
    for account in session.accounts:
        form = _truncated_uid(account.uid)
        if form in truncated and truncated[form] != account.uid:
            ambiguous.add(form)
        truncated[form] = account.uid

    coverage = load_coverage(state_dir, bank)
    # Resolved once and kept: the same values answer both "does the ledger know this account" and
    # "do two accounts arrive at one key", and the second question is invisible from the first.
    resolved = _stored_acctids(session.accounts)
    covered_accounts = sum(1 for acctid in resolved if coverage.for_account(acctid) is not None)
    key_counts = Counter(account_key(acctid) for acctid in resolved)

    _runs, requests, _skipped = load_requests(state_dir)
    bank_requests = [r for r in requests if r.bank == bank and r.path.startswith("/accounts/")]
    matched = 0
    unmatched = 0
    for record in bank_requests:
        match = _PATH_ACCOUNT.match(record.path)
        requested_form = match.group(1) if match else None
        if (
            requested_form is not None
            and requested_form in truncated
            and requested_form not in ambiguous
        ):
            matched += 1
        else:
            unmatched += 1

    return CoverageReconciliation(
        bank=bank,
        live_accounts=len(session.accounts),
        covered_accounts=covered_accounts,
        distinct_coverage_keys=len(key_counts),
        accounts_sharing_a_key=sum(n for n in key_counts.values() if n > 1),
        ledger_unreadable=coverage.unreadable,
        ambiguous_truncated_uids=len(ambiguous),
        requests_total=len(bank_requests),
        requests_matched_to_live_account=matched,
        requests_unmatched=unmatched,
    )
