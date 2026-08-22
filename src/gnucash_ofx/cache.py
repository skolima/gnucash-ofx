"""Local cache of raw Enable Banking fetch responses.

PSD2 ASPSPs cap *background* data fetches (typically 4x/day). Even in online mode the allowance is
finite, so re-running ``fetch`` for the same account and dates — during a re-import, a retry, or
debugging — should not re-spend it.

**The unit of storage is a calendar month, not a request.** Keying on the exact
``(uid, date_from, date_to)`` triple made the cache a memo of questions once asked rather than a
record of what is known, so ``--to 2026-05-31`` followed by ``--to 2026-06-01`` was a total miss
even though the first window is a strict subset of data already on disk. Over the cache that
produced, more than half of all entries were fully covered by another entry for the same account.
A month is a fact about the world: it is the same month whatever window asked for it, so chunks are
identical between runs and independently reusable, and serving a window becomes "do I hold every
month it touches, far enough, fresh enough?" — no interval algebra.

**Request size and storage size are deliberately different things.** How much can be asked for in
one call is a bank constraint (many ASPSPs reject windows over ~90 days); how finely it is stored
is a reuse concern. Requests therefore stay as large as the bank allows and the response is sliced
into months before writing, which is why monthly granularity costs no extra requests. The slicing
is the same client-side filtering Millennium already forces on us.

Balances are stored separately, keyed by ``uid`` alone: a transaction list belongs to the window
requested, but a balance belongs to the account and the moment of the call — Enable Banking reports
``reference_date`` as the fetch date in every record, never the window end. Holding both under one
key meant two fetches of different ranges each paid for an identical balance.

Cached files hold financial data and live under a gitignored cache dir.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from gnucash_ofx.models import _BAD_DATA_ERRORS

# The merge in save_cached_month must key entries on the same identity FITID rests on, or an
# entry would survive itself under a second key. That function is deliberately not re-derived
# here — one definition of "the same transaction", however private its home.
from gnucash_ofx.sources.enablebanking import _transaction_id

DEFAULT_TTL = timedelta(hours=6)  # Matches Enable Banking's "continue after 6 hours" guidance.

# How far back a booking may still appear after the fact. A month older than this is treated as
# settled and served from cache regardless of age; the recent tail keeps DEFAULT_TTL.
#
# **This is a policy value, not a measured one, and must not be read as a finding.** Looking for it
# in the local cache found no few-days lag at all: across every pair of fetches re-covering an
# already-covered window, exactly two transactions ever arrived late, and both were an
# aggregator-side backfill about two *months* after booking. So there was nothing to derive a
# number from, and no TTL anyone would pick catches a backfill like that anyway — which is the
# argument for not expiring settled months at all rather than for expiring them sooner.
# ``fetch --refresh`` is the instrument for "I have reason to think the bank changed something".
LATE_BOOKING_MARGIN = timedelta(days=14)

# How much earlier than a planned span's start every wire request opens. Alior filters the
# requested window by ``transaction_date`` — the purchase date — not by ``booking_date``
# (measured live: probe-txn-date-window, 2026-08-13; docs/probes.md), so a card purchase made
# before ``date_from`` but booked inside the window is invisible to the exact-window request,
# and only reachable by asking for days *before* the window. The margin compensates at the
# request boundary; nothing about what a window *means* (month bucketing, coverage, DTSTART/
# DTEND) moves off booking date. See docs/adr-transaction-date-window-margin.md decision 1.
#
# **A policy value, not a measured one**, like LATE_BOOKING_MARGIN above: the measured maximum
# booking−transaction lag is 3 days over a sample holding no holiday settlement cycle, and 7 is
# max-observed + 4 days of slack — one full calendar week. The revisit trigger is the lag
# warning (decision 5), not a re-guess: an observed lag past this margin announces itself.
TRANSACTION_DATE_MARGIN = timedelta(days=7)

# Bumped when the on-disk shape changes incompatibly; anything else is ignored exactly as an
# unreadable file is. The nullable claim (#35) deliberately did **not** bump it: a bump discards
# every existing chunk — a full re-buy at the banks serving ~90 days — while an *older* reader
# meeting a null-claim chunk merely fails to parse it and refetches: a miss, never a wrong answer.
CHUNK_VERSION = 2


def _as_utc(dt: datetime) -> datetime:
    """Treat a timezone-naive datetime as UTC so aware/naive comparisons never raise."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + replace, as ``save_session`` does.

    Chunks are now written one per month as each arrives rather than once per fetch, so a torn
    write is a more frequent opportunity. A corrupt chunk degrades safely — it reads as a miss —
    but a miss costs the rate-limit allowance, which is the thing this module exists to protect.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- months


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def next_month(day: date) -> date:
    return month_end(day) + timedelta(days=1)


def months_in(date_from: date, date_to: date) -> list[date]:
    """The first day of every calendar month the window touches, in order."""
    months: list[date] = []
    cursor = month_start(date_from)
    while cursor <= date_to:
        months.append(cursor)
        cursor = next_month(cursor)
    return months


# --------------------------------------------------------------------------- transaction chunks


@dataclass(frozen=True, slots=True)
class CachedMonth:
    """One account-month: the transactions, and how much of the month they actually cover.

    The claim (``covered_from``/``covered_to``) may be empty (both ``None``): a chunk created
    only to hold entries booked outside any requested span — a cross-month crosser the ASPSP
    returned and no request has claimed yet. Such a chunk holds correct data, and an empty
    claim can never make :meth:`covers` answer true, so the month is still fetched (and the
    entries merged, not lost) the first time a window actually asks for it.

    A chunk may also hold entries booked *outside* a non-empty claim — kept by the merge in
    :func:`save_cached_month` — for the same reason: data paid for with a counted request is
    never discarded by our own bookkeeping (docs/adr-transaction-date-window-margin.md
    decisions 2 and 3).
    """

    month: date  # first day of the month
    covered_from: date | None
    covered_to: date | None
    transactions: list[dict[str, Any]]
    fetched_at: datetime

    def covers(self, date_from: date, date_to: date) -> bool:
        # An empty claim never covers: it marks held data, not answered days.
        if self.covered_from is None or self.covered_to is None:
            return False
        return self.covered_from <= date_from and self.covered_to >= date_to


def _uid_digest(uid: str) -> str:
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:16]


def month_chunk_path(cache_dir: Path, uid: str, month: date) -> Path:
    return cache_dir / f"m-{_uid_digest(uid)}-{month:%Y-%m}.json"


def balance_cache_path(cache_dir: Path, uid: str) -> Path:
    """Balances are keyed by account alone — they have no window (see the module docstring)."""
    return cache_dir / f"bal-{_uid_digest(uid)}.json"


def chunk_ttl(month: date, today: date, ttl: timedelta = DEFAULT_TTL) -> timedelta | None:
    """How long a month's chunk stays usable; ``None`` means it does not expire.

    A closed month is immutable apart from late bookings, so once it is more than
    :data:`LATE_BOOKING_MARGIN` behind, re-fetching it buys nothing and costs the allowance. The
    recent tail keeps the short TTL, because it is still moving.
    """
    # invariant: a settled month never expires - None here is intentional, not a missing case.
    # AGENTS.md#invariants
    return ttl if month_end(month) >= today - LATE_BOOKING_MARGIN else None


def load_cached_month(
    cache_dir: Path,
    uid: str,
    month: date,
    *,
    ttl: timedelta | None,
    now: datetime | None = None,
) -> CachedMonth | None:
    """Return a usable chunk, or ``None`` if missing, stale, or unreadable."""
    path = month_chunk_path(cache_dir, uid, month)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != CHUNK_VERSION:
            return None
        fetched_at = _as_utc(datetime.fromisoformat(str(data["fetched_at"])))
        # ``null`` is an empty claim (see CachedMonth), not a corrupt chunk.
        raw_from, raw_to = data["covered_from"], data["covered_to"]
        covered_from = date.fromisoformat(str(raw_from)) if raw_from is not None else None
        covered_to = date.fromisoformat(str(raw_to)) if raw_to is not None else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if ttl is not None and _as_utc(now or datetime.now(UTC)) - fetched_at > ttl:
        return None
    return CachedMonth(
        month=month,
        covered_from=covered_from,
        covered_to=covered_to,
        transactions=data.get("transactions", []),
        fetched_at=fetched_at,
    )


def _merge_claims(
    existing: tuple[date, date] | None, incoming: tuple[date, date] | None
) -> tuple[date, date] | None:
    """Combine two claimed spans; the result may never claim a day neither span answered.

    Touching or overlapping spans union. Disjoint spans keep the wider one (the incoming on a
    tie — those days were answered more recently) and **never claim the gap between them** —
    the one thing a coverage record may never do is claim days it lacks.
    """
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    if existing[0] <= incoming[1] + timedelta(days=1) and incoming[0] <= existing[1] + timedelta(
        days=1
    ):
        return min(existing[0], incoming[0]), max(existing[1], incoming[1])
    existing_days = (existing[1] - existing[0]).days
    incoming_days = (incoming[1] - incoming[0]).days
    return incoming if incoming_days >= existing_days else existing


def _merge_transactions(
    existing: CachedMonth,
    incoming_claim: tuple[date, date] | None,
    transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The incoming fetch is authoritative for the days it claims; nothing else is lost.

    Inside the incoming claim the incoming set replaces what the chunk held, so a bank-side
    withdrawal or amendment still lands. Outside it the chunk keeps what it had. The merge is
    keyed on the identity ``FITID`` already rests on (:func:`_transaction_id`), and the incoming
    copy wins on any key collision, inside or outside the claim — the key is what keeps this
    idempotent at a bank that returns full history on every fetch (Millennium), where an unkeyed
    preserve would duplicate that history on every save.
    """
    incoming_keys = {_transaction_id(t) for t in transactions}
    kept = []
    for txn in existing.transactions:
        if _transaction_id(txn) in incoming_keys:
            continue
        if incoming_claim is not None and _booking_date_in(txn, *incoming_claim) is True:
            continue
        kept.append(txn)
    return kept + list(transactions)


def save_cached_month(
    cache_dir: Path,
    uid: str,
    month: date,
    covered_from: date | None,
    covered_to: date | None,
    transactions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> Path:
    """Merge one account-month into its chunk. Called as each span lands, not once per fetch.

    ``save_cached_fetch`` used to run after the whole chunk loop, so a 429 on the last chunk
    discarded every chunk already paid for — on exactly the accounts that were rate-limited.

    This **merges** rather than overwrites (docs/adr-transaction-date-window-margin.md
    decision 3): the old overwrite let a narrower fetch replace a fuller month wholesale —
    measured doing exactly that on the default resume path, twice in two days — re-paying in
    counted requests, and past the ~90-day history horizon in unrecoverable data, for entries
    the chunk already held. See :func:`_merge_transactions` for the rule and its key.

    ``covered_from``/``covered_to`` are the days this fetch actually claims — the planned,
    un-widened span intersected with the month, never the margin-widened wire request (a request
    opening at ``from − margin`` is only booking-complete from ``from``). ``None``/``None``
    claims nothing: entries booked outside every requested span land that way (decision 2).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    incoming_claim = (
        (covered_from, covered_to) if covered_from is not None and covered_to is not None else None
    )
    existing = load_cached_month(cache_dir, uid, month, ttl=None)
    merged = transactions
    claim = incoming_claim
    fetched_at = _as_utc(now or datetime.now(UTC))
    if existing is not None:
        existing_claim = (
            (existing.covered_from, existing.covered_to)
            if existing.covered_from is not None and existing.covered_to is not None
            else None
        )
        try:
            merged = _merge_transactions(existing, incoming_claim, transactions)
            claim = _merge_claims(existing_claim, incoming_claim)
        except _BAD_DATA_ERRORS:
            # Keying an id-less entry hashes its amount fields, so a wrong-typed entry — already
            # on disk, because caching happens before the mapper's guard rejects the account —
            # raises here on every later save touching its chunk. That must stay this chunk's
            # problem: an escape would abort the whole run, after every bank's requests were
            # spent. Degrade to the incoming set (the old overwrite), which is authoritative for
            # exactly the claim it writes; the mapper's per-account guard still reports the data.
            merged = transactions
            claim = incoming_claim
        if claim != incoming_claim:
            # The merged claim retains days only the *old* fetch answered, so the chunk keeps the
            # old fetch's timestamp. Stamping now would let a crosser-only save — or a narrow
            # re-claim of a fuller chunk — make a stale moving-tail claim read as fresh and
            # suppress the 6-hour TTL for days nobody re-asked about.
            fetched_at = existing.fetched_at
    path = month_chunk_path(cache_dir, uid, month)
    payload = {
        "version": CHUNK_VERSION,
        "uid": uid,
        "month": f"{month:%Y-%m}",
        "covered_from": claim[0].isoformat() if claim is not None else None,
        "covered_to": claim[1].isoformat() if claim is not None else None,
        "fetched_at": fetched_at.isoformat(),
        "transactions": merged,
    }
    _write_atomic(path, json.dumps(payload, indent=2))
    return path


def cached_window(
    cache_dir: Path,
    uid: str,
    date_from: date,
    date_to: date,
    *,
    today: date,
    ttl: timedelta = DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[date], datetime | None]:
    """What the cache can serve for a window, and which months it cannot.

    Returns ``(transactions, missing months, oldest fetch time used)``. A month counts as served
    only if its stored coverage reaches across the whole part of the window that falls inside it —
    a chunk that stops short is a miss for the days beyond, not a partial answer.
    """
    served: list[dict[str, Any]] = []
    missing: list[date] = []
    oldest: datetime | None = None
    for month in months_in(date_from, date_to):
        need_from = max(date_from, month)
        need_to = min(date_to, month_end(month))
        chunk = load_cached_month(cache_dir, uid, month, ttl=chunk_ttl(month, today, ttl), now=now)
        if chunk is None or not chunk.covers(need_from, need_to):
            missing.append(month)
            continue
        served.extend(chunk.transactions)
        oldest = chunk.fetched_at if oldest is None else min(oldest, chunk.fetched_at)
    return served, missing, oldest


# --------------------------------------------------------------------------- balances


@dataclass(frozen=True, slots=True)
class CachedBalances:
    """A previously fetched account's raw ``/balances`` response, with its fetch time."""

    balances: dict[str, Any]
    fetched_at: datetime


def load_cached_balances(
    cache_dir: Path,
    uid: str,
    *,
    ttl: timedelta | None = DEFAULT_TTL,
    now: datetime | None = None,
) -> CachedBalances | None:
    """Return a cached ``/balances`` response, or ``None`` if missing, stale, or unreadable.

    ``ttl=None`` serves an entry of any age. That is not laziness about freshness — it is for the
    *other* thing this response is used for. The call answers two questions: what the ledger
    balance is (which goes stale immediately) and what currency the account is in (which never
    changes, and which ``GET /accounts/{uid}`` cannot answer at all in Restricted Mode). Only the
    first needs a fresh answer.
    """
    path = balance_cache_path(cache_dir, uid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = _as_utc(datetime.fromisoformat(str(data["fetched_at"])))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if ttl is not None and _as_utc(now or datetime.now(UTC)) - fetched_at > ttl:
        return None
    return CachedBalances(balances=data.get("balances", {}), fetched_at=fetched_at)


def save_cached_balances(
    cache_dir: Path, uid: str, balances: dict[str, Any], *, now: datetime | None = None
) -> Path:
    """Persist an account's ``/balances`` response; return the file path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = balance_cache_path(cache_dir, uid)
    payload = {
        "uid": uid,
        "fetched_at": _as_utc(now or datetime.now(UTC)).isoformat(),
        "balances": balances,
    }
    _write_atomic(path, json.dumps(payload, indent=2))
    return path


# --------------------------------------------------------------------------- legacy conversion


def migrate_legacy_entries(cache_dir: Path) -> int:
    """Convert window-keyed entries into month chunks in place; return how many were converted.

    Shipping the new layout without this would make the upgrade itself cost a full re-fetch, at the
    bank that can least afford one — a strange way to deliver a rate-limit fix. Old entries carry
    everything needed: the window they covered, when they were fetched, and the transactions.

    Best-effort by nature. Where several old entries touch one month, the widest coverage wins
    (newest first on a tie) rather than being merged; anything that leaves uncovered is simply
    re-fetched, which is the pre-existing behaviour and not a regression.
    """
    if not cache_dir.is_dir():
        return 0
    best: dict[tuple[str, date], tuple[int, datetime, date, date, list[dict[str, Any]]]] = {}
    legacy: list[Path] = []
    for path in sorted(cache_dir.glob("*.json")):
        if path.name.startswith(("m-", "bal-")):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            uid = str(data["uid"])
            entry_from = date.fromisoformat(str(data["date_from"]))
            entry_to = date.fromisoformat(str(data["date_to"]))
            fetched_at = _as_utc(datetime.fromisoformat(str(data["fetched_at"])))
            transactions = data["transactions"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        legacy.append(path)
        for month in months_in(entry_from, entry_to):
            covered_from = max(entry_from, month)
            covered_to = min(entry_to, month_end(month))
            span = (covered_to - covered_from).days
            key = (uid, month)
            if key in best and (span, fetched_at) <= best[key][:2]:
                continue
            in_month = [
                t
                for t in transactions
                if _booking_date_in(t, covered_from, covered_to) is not False
            ]
            best[key] = (span, fetched_at, covered_from, covered_to, in_month)

    for (uid, month), (_span, fetched_at, covered_from, covered_to, transactions) in best.items():
        save_cached_month(
            cache_dir, uid, month, covered_from, covered_to, transactions, now=fetched_at
        )
    for path in legacy:
        # A converted entry is dead weight: nothing reads the old shape any more, and leaving it
        # would make this scan run again on every fetch, forever.
        path.unlink(missing_ok=True)
    return len(legacy)


def entry_booking_date(txn: dict[str, Any]) -> date | None:
    """A raw transaction's booking date — same field precedence as the mapper — or ``None``."""
    raw = txn.get("booking_date") or txn.get("value_date") or txn.get("transaction_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _booking_date_in(txn: dict[str, Any], date_from: date, date_to: date) -> bool | None:
    """Whether a raw transaction falls in a range; ``None`` when it carries no usable date.

    An undateable transaction is kept rather than dropped, so the mapper still raises for it and
    the problem stays visible instead of being silently filtered away here.
    """
    booked = entry_booking_date(txn)
    if booked is None:
        return None
    return date_from <= booked <= date_to
