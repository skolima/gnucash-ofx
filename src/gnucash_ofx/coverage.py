"""Which days have actually been fetched, per account, so a gap is visible before it ages out.

Nothing else records this. The fetch cache looks as though it does — it stores ``covered_from`` /
``covered_to`` per account-month — but it is a rate-limit shield, not a ledger: a chunk's claim
cannot hold two disjoint spans (the wider wins and the gap between them is never claimed — #47
replaced the outright overwrite with a merge, which removed the loss but not this limitation), it
is keyed on Enable Banking's account ``uid``, which is regenerated on every re-link, and it lives
in a directory the user is told they may delete.
See ``docs/adr-coverage-ledger-and-warnings.md`` §1.

Three properties follow from what this has to survive.

**Keyed on the same value as ``ACCTID``.** Consent lasts ~180 days and history reaches back ~90, so
a re-link falls *inside* the period a coverage warning exists to protect; anything keyed on the
``uid`` would blank itself exactly when it mattered. Keying on ``ACCTID`` also makes it impossible
for this file to lie: ``ACCTID`` is what GnuCash derives ``online_id`` from, so if it changes,
GnuCash sees a new account and this ledger sees an unknown one — the same event, never one without
the other.

**Days, not months.** The cache stores months because a month is the unit of *retrieval*; a day is
the unit of *loss*. A month map cannot express two disjoint spans inside one month, so it would
have to either widen — claiming coverage it does not have, the one thing a warning system may never
do — or keep the last, which is the cache's forgetting reproduced in the component built to fix it.

**Absent, unreadable or wrong-version means _unknown_, never "gap since the epoch."** Every bank
that exists today has no ledger, so the first run after upgrade must warn about nothing.

The file holds dates and opaque account digests only — no amounts, no transaction data, no account
numbers. That is the run log's rule, for the run log's reason: a file of dates *looks* harmless and
will be pasted into an issue, so it has to actually be harmless.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from gnucash_ofx.naming import safe_component

# Bumped when the on-disk shape changes. Anything else is ignored exactly as a corrupt file is:
# unknown coverage costs a re-fetch, while a misread one costs a wrong warning.
SCHEMA_VERSION = 1

# A subdirectory rather than ``state/<bank>.coverage.json``: ``state/*.json`` already means "the
# session files" to any script or reader that has learned to glob it, and a bank key cannot contain
# a dot, so the two would otherwise sit in one namespace for no benefit.
COVERAGE_DIR = "coverage"

ONE_DAY = timedelta(days=1)

# A closed range of days, both ends inclusive. Inclusive rather than half-open because every date
# the user types, every OFX statement period and every window in this codebase is inclusive; one
# convention that matches the domain beats one that matches arithmetic convenience.
DaySpan = tuple[date, date]


def account_key(acctid: str) -> str:
    """The ledger's key for an account: a short digest of its ``ACCTID``.

    The digest, not the ``ACCTID`` itself, because the ``ACCTID`` is usually the IBAN. Same
    stability (it is a pure function of the value ``ACCTID`` resolves to) with nothing readable on
    disk — the same trade the cache makes with ``_uid_digest`` and the run log with its path
    redaction.
    """
    return hashlib.sha1(acctid.encode("utf-8")).hexdigest()[:16]


def merge_spans(spans: Iterable[DaySpan]) -> tuple[DaySpan, ...]:
    """Sort and coalesce day ranges, joining ones that touch as well as ones that overlap.

    Touching matters: a fetch ending on the 31st and the next starting on the 1st leave no
    uncovered day, and storing them apart would report a gap that does not exist. Overlap is the
    ordinary case, because the default window deliberately re-fetches a margin.
    """
    merged: list[DaySpan] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + ONE_DAY:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def missing_days(spans: Iterable[DaySpan], date_from: date, date_to: date) -> tuple[DaySpan, ...]:
    """The parts of ``[date_from, date_to]`` no span covers.

    Reports interior holes, not only the tail past the last covered day. That is the case a
    high-water mark would forget — fetch May, then July, and June is a gap that no "last covered"
    date can express — and it is what makes a warning survive being ignored once.
    """
    if date_from > date_to:
        return ()
    gaps: list[DaySpan] = []
    cursor = date_from
    for start, end in merge_spans(spans):
        if end < cursor:
            continue
        if start > date_to:
            break
        if start > cursor:
            gaps.append((cursor, start - ONE_DAY))
        cursor = end + ONE_DAY
        if cursor > date_to:
            return tuple(gaps)
    gaps.append((cursor, date_to))
    return tuple(gaps)


@dataclass(frozen=True, slots=True)
class AccountCoverage:
    """One account's fetched days, merged, plus when it was last added to."""

    spans: tuple[DaySpan, ...] = ()
    last_fetch: datetime | None = None

    @property
    def covered_through(self) -> date | None:
        """The last day known to be fetched, or ``None`` when nothing is recorded."""
        return self.spans[-1][1] if self.spans else None

    @property
    def covered_from(self) -> date | None:
        """The first day known to be fetched, or ``None`` when nothing is recorded."""
        return self.spans[0][0] if self.spans else None

    def missing(self, date_from: date, date_to: date) -> tuple[DaySpan, ...]:
        return missing_days(self.spans, date_from, date_to)


@dataclass(frozen=True, slots=True)
class BankCoverage:
    """One bank's ledger. ``unreadable`` distinguishes "never fetched" from "could not be read"."""

    bank: str
    accounts: dict[str, AccountCoverage] = field(default_factory=dict)
    unreadable: bool = False

    def for_account(self, acctid: str) -> AccountCoverage | None:
        return self.accounts.get(account_key(acctid))

    def earliest_covered_through(self, acctids: Iterable[str]) -> date | None:
        """The oldest "covered through" among the named accounts, or ``None`` if any is unknown.

        ``None`` for an unknown account rather than skipping it: the default window has to be safe
        for the *least* covered account of the bank, and an account nobody has a record for is the
        least covered one there is.
        """
        oldest: date | None = None
        for acctid in acctids:
            entry = self.accounts.get(account_key(acctid))
            covered = entry.covered_through if entry else None
            if covered is None:
                return None
            oldest = covered if oldest is None else min(oldest, covered)
        return oldest


def with_span(
    coverage: BankCoverage,
    acctid: str,
    date_from: date,
    date_to: date,
    *,
    now: datetime | None = None,
) -> BankCoverage:
    """``coverage`` plus ``[date_from, date_to]`` on one account. Pure; returns a new value."""
    key = account_key(acctid)
    existing = coverage.accounts.get(key)
    spans = merge_spans([*(existing.spans if existing else ()), (date_from, date_to)])
    accounts = dict(coverage.accounts)
    accounts[key] = AccountCoverage(spans=spans, last_fetch=now or datetime.now(UTC))
    return BankCoverage(bank=coverage.bank, accounts=accounts, unreadable=coverage.unreadable)


def coverage_path(state_dir: Path, bank: str) -> Path:
    return state_dir / COVERAGE_DIR / f"{safe_component(bank, 'bank key')}.json"


def load_coverage(state_dir: Path, bank: str) -> BankCoverage:
    """Read a bank's ledger. **Never raises**: any problem reads as unknown coverage.

    Unlike ``load_session``, an unreadable file here is not a per-bank failure. Losing a session
    costs a browser SCA dance; losing this costs a re-fetch, so the safe direction is to forget and
    carry on. ``unreadable`` is set so the caller can say that it did.
    """
    path = coverage_path(state_dir, bank)
    if not path.exists():
        return BankCoverage(bank=bank)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != SCHEMA_VERSION:
            return BankCoverage(bank=bank, unreadable=True)
        accounts = {
            str(key): AccountCoverage(
                spans=merge_spans(
                    (date.fromisoformat(str(start)), date.fromisoformat(str(end)))
                    for start, end in entry.get("covered", [])
                ),
                last_fetch=_parse_time(entry.get("last_fetch")),
            )
            for key, entry in data["accounts"].items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return BankCoverage(bank=bank, unreadable=True)
    return BankCoverage(bank=bank, accounts=accounts)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def save_coverage(state_dir: Path, coverage: BankCoverage) -> Path:
    """Persist a bank's ledger atomically; raises ``OSError`` for the caller to report.

    Deliberately not silent about failure, which is where this differs from ``RunLog``. A lost log
    line costs nothing, so the log swallows every ``OSError``; a lost coverage write makes every
    later run re-fetch the same window, so it has a running cost and is worth a warning.
    """
    path = coverage_path(state_dir, coverage.bank)
    payload = {
        "version": SCHEMA_VERSION,
        "bank": coverage.bank,
        "accounts": {
            key: {
                "covered": [[start.isoformat(), end.isoformat()] for start, end in entry.spans],
                "last_fetch": entry.last_fetch.isoformat() if entry.last_fetch else None,
            }
            for key, entry in sorted(coverage.accounts.items())
        },
    }
    _write_atomic(path, json.dumps(payload, indent=2))
    return path


def _write_atomic(path: Path, text: str) -> None:
    """Temp file in the same directory plus ``os.replace``, as ``save_session`` does.

    This one is rewritten once per account per fetch rather than once per link, so a torn write is a
    far more frequent opportunity — and a half-written ledger reads as unknown, which silently
    re-fetches forever.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
