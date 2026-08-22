"""Orchestration: tie config + secrets + Enable Banking client + state + OFX writer together.

These functions are deliberately free of CLI/IO concerns (argument parsing, ``print``,
``input``) so they can be unit-tested with a ``MockTransport`` client. The thin command layer
lives in :mod:`gnucash_ofx.cli`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, MutableSequence, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from gnucash_ofx import __version__
from gnucash_ofx.cache import (
    DEFAULT_TTL,
    LATE_BOOKING_MARGIN,
    TRANSACTION_DATE_MARGIN,
    cached_window,
    entry_booking_date,
    load_cached_balances,
    migrate_legacy_entries,
    month_end,
    month_start,
    months_in,
    next_month,
    save_cached_balances,
    save_cached_month,
)
from gnucash_ofx.config import AppConfig, BankConfig, get_secret
from gnucash_ofx.conversions import pair_conversions
from gnucash_ofx.coverage import (
    ONE_DAY,
    BankCoverage,
    DaySpan,
    load_coverage,
    missing_days,
    save_coverage,
    with_span,
)

# The per-account bad-data containment set lives beside the other data-boundary policy in
# models.py (see the comment there) so the cache's merge can share it without an import cycle.
from gnucash_ofx.models import _BAD_DATA_ERRORS, Account, Txn, currency_or_none
from gnucash_ofx.ofxout import (
    UncarryableAccountType,
    account_disambiguators,
    accttype_for,
    bank_id_for,
    batch_statements,
    combined_filename,
    ofx_filename,
    validate_raw_fields,
    write_account_ofx,
    write_combined_ofx,
)
from gnucash_ofx.runlog import RunLog
from gnucash_ofx.sources.enablebanking import (
    EnableBankingClient,
    PageBudget,
    RequestObserver,
    ResponseLimitExceeded,
    api_error_code,
    finite_decimal,
    identification_hashes,
    linked_accounts,
    map_transactions,
    scrub_session,
)
from gnucash_ofx.state import (
    LinkedAccount,
    SessionState,
    StateError,
    days_until_expiry,
    load_session,
    save_session,
)

# Several independent services, tried in order until one answers with something that parses as an
# address. One service was a single point of failure for the whole run's rate-limit mode: if it was
# down, every fetch silently dropped to the ~4/day background cap.
_PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)
_PUBLIC_IP_TIMEOUT = 5.0
# Used only when the bank does not advertise a maximum consent validity.
DEFAULT_VALID_DAYS = 90
_CONSENT_SAFETY_MARGIN = timedelta(hours=1)
_PSU_USER_AGENT = f"gnucash-ofx/{__version__}"

# A progress sink: receives short human-readable status lines. Defaults to a no-op so the
# orchestration layer stays free of IO; the CLI passes a callback that prints to stderr.
ProgressFn = Callable[[str], None]


def _noop_progress(_message: str) -> None:
    pass


ENABLEBANKING = "enablebanking"
# Closing booked first, then closing/interim available/booked; best-effort for LEDGERBAL.
_BALANCE_TYPE_PREFERENCE = ["CLBD", "CLAV", "ITBD", "ITAV", "XPCD", "OPBD", "PRCD"]


class RunError(Exception):
    """User-facing error that aborts the whole run (missing secret, bad config, ...)."""


class BankError(RunError):
    """A failure scoped to one bank, which the other banks in a run can survive.

    A distinct type rather than a well-placed ``try``: it keeps "global" and "per-bank" a
    property of the error itself, so a genuinely global :class:`RunError` raised deeper in the
    call stack still aborts instead of being silently downgraded to one bank's problem.
    """


@dataclass(frozen=True, slots=True)
class BankFailure:
    """One bank's (or one account's) failure, with everything needed to explain it.

    ``payload`` holds the bank's response verbatim — that is the most useful thing to see when
    debugging, so it is never summarised away. ``account`` is already redacted.
    """

    bank_key: str
    operation: str  # consent | session | balances | transactions | mapping | account-type
    scope: str  # "bank" (the rest of it was abandoned) or "account" (siblings continued)
    message: str
    account: str | None = None
    window: tuple[date, date] | None = None
    status_code: int | None = None
    api_code: str | None = None
    payload: str | None = None


@dataclass(frozen=True, slots=True)
class FetchWarning:
    """Something worth acting on that did **not** fail the fetch.

    A distinct type rather than a reused :class:`BankFailure`, so that "this did not fail" is a
    property of the value and cannot be lost by a later edit to the reporting layer — the same
    reason :class:`BankError` is a type rather than a well-placed ``try``. Nothing here may reach
    :attr:`FetchReport.failures`, and none of it moves the exit code: the files were written and
    they are importable.

    ``kind`` groups the warning for rendering (``coverage``, ``consent``, ``identity``, ``ledger``);
    ``account`` is already redacted where it is set.
    """

    bank_key: str
    kind: str
    message: str
    account: str | None = None


# A warning sink, mirroring :data:`ProgressFn`. A sink rather than another return value because
# ``fetch_bank`` already returns two things and the callers that only want files should not have to
# care; the CLI passes one that renders to stderr.
WarnFn = Callable[[FetchWarning], None]


def _noop_warn(_warning: FetchWarning) -> None:
    pass


@dataclass(frozen=True, slots=True)
class FetchReport:
    """What a fetch produced: files written, failures that did not stop it, and warnings."""

    written: list[Path]
    failures: list[BankFailure]
    warnings: list[FetchWarning] = field(default_factory=list)

    @property
    def failed_banks(self) -> list[str]:
        seen: dict[str, None] = {}
        for failure in self.failures:
            seen.setdefault(failure.bank_key, None)
        return list(seen)


def _is_rate_limited(exc: BaseException) -> bool:
    """True for a 429 that survived the client's own retry/backoff.

    The allowance is per ASPSP, so once it is exhausted every remaining account of that bank
    fails identically — continuing would spend minutes of backoff per account to learn nothing.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _response_details(exc: BaseException) -> tuple[int | None, str | None, str | None]:
    """``(status_code, api_code, payload)`` from an HTTP error; ``(None, None, str(exc))`` else."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None, None, str(exc) or None
    response = exc.response
    api_code: str | None = None
    try:
        body = response.json()
        payload = str(body)
        if isinstance(body, dict):
            # The machine-readable code lives in `error` (ASPSP_ERROR, ASPSP_RATE_LIMIT_EXCEEDED);
            # `message` is prose for a human ("Error interacting with ASPSP") and `code` repeats
            # the HTTP status. Verified against live 400 and 429 responses, August 2026.
            api_code = api_error_code(response) or (
                str(body["message"]) if isinstance(body.get("message"), str) else None
            )
    except ValueError:
        payload = response.text
    return response.status_code, api_code, payload


# --------------------------------------------------------------------------- client construction


def require_credentials() -> tuple[str, bytes]:
    """``(app_id, private_key)`` from ``EB_APP_ID`` / ``EB_PRIVATE_KEY``, or :class:`RunError`.

    Separate from :func:`build_enablebanking_client` so a command that will never send a request —
    ``fetch --dry-run`` — can still fail on a credential problem, which is a genuinely global one
    and exactly the kind of thing a dry run exists to surface early.
    """
    app_id = get_secret("EB_APP_ID")
    key_path = get_secret("EB_PRIVATE_KEY")
    if not app_id:
        raise RunError("EB_APP_ID is not set (see .env.example)")
    if not key_path:
        raise RunError("EB_PRIVATE_KEY is not set (see .env.example)")
    key_file = Path(key_path)
    if not key_file.is_file():
        raise RunError(f"private key file not found: {key_file}")
    try:
        private_key = key_file.read_bytes()
    except OSError as exc:
        raise RunError(f"could not read private key {key_file}: {exc}") from exc
    return app_id, private_key


def build_enablebanking_client(
    *,
    sleep: Callable[[float], None] = time.sleep,
    psu_headers: dict[str, str] | None = None,
    observer: RequestObserver | None = None,
) -> EnableBankingClient:
    """Construct a client from ``EB_APP_ID`` / ``EB_PRIVATE_KEY`` (loaded from env/.env).

    ``sleep`` is the function the client calls between rate-limit retries; the CLI passes one
    that reports the wait so a 429 backoff doesn't look like a hang. ``psu_headers`` are sent on
    data-retrieval calls to mark them as online fetches (see :func:`build_psu_headers`).
    ``observer`` receives every response, which is how the run log records what was spent where.
    """
    app_id, private_key = require_credentials()
    return EnableBankingClient(
        application_id=app_id,
        private_key=private_key,
        sleep=sleep,
        psu_headers=psu_headers,
        observer=observer,
    )


def _detect_public_ip() -> str | None:
    """The current public IP for the Psu-Ip-Address header; None if no service could supply one.

    Each service is validated before being accepted, so one answering with an error page or a
    captive-portal redirect is skipped rather than becoming a malformed header.
    """
    for service in _PUBLIC_IP_SERVICES:
        try:
            response = httpx.get(service, timeout=_PUBLIC_IP_TIMEOUT)
            response.raise_for_status()
        except (httpx.HTTPError, OSError):
            continue
        ip = _valid_ip(response.text)
        if ip:
            return ip
    return None


def _valid_ip(value: str | None) -> str | None:
    """Return ``value`` if it is a valid IPv4/IPv6 address (whitespace-trimmed), else None."""
    if not value:
        return None
    candidate = value.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def build_psu_headers(
    progress: ProgressFn = _noop_progress, *, allow_background: bool = False
) -> dict[str, str] | None:
    """Assemble PSU headers so the CLI's user-triggered fetch counts as an *online* request.

    **The detected IP wins; ``EB_PSU_IP`` is the fallback for when detection fails.** The header
    is meant to carry the address the user is actually connecting from, and on a dynamic IP a
    configured value is stale within a day — so a pin that overrode detection would turn a
    correct value into a confidently wrong one. A static-IP user loses nothing by the inversion,
    since detection returns the same address they configured.

    Without a valid IP the whole run would fall back to the ~4/day *background* cap. That is the
    scarce resource this project spends its cache and its 429 handling protecting, so exhausting
    it by accident is not something to do quietly: raise, unless ``allow_background`` says the
    user asked for it. Either way we never send a partial or garbage set, which would trigger
    ``PSU_HEADER_NOT_PROVIDED`` or a hard failure at the ASPSP.
    """
    detected = _detect_public_ip()
    configured = _valid_ip(get_secret("EB_PSU_IP"))
    if detected and configured and detected != configured:
        # Neither address is printed: this line can end up pasted into an issue, and both are the
        # user's own public IP. Which one is stale is not the useful part - that it is, is.
        progress(
            "EB_PSU_IP does not match your current public IP and is being ignored "
            "(unset it if your address is dynamic)"
        )
    ip = detected or configured
    if not ip:
        message = (
            "could not determine your public IP, so this fetch would run in background mode, "
            "where the bank allows only ~4 requests a day. Retry, or set EB_PSU_IP, or pass "
            "--allow-background to spend the smaller allowance deliberately."
        )
        if not allow_background:
            raise RunError(message)
        progress(f"{message.split(',')[0]}; continuing in background mode as requested")
        return None
    return {"Psu-Ip-Address": ip, "Psu-User-Agent": _PSU_USER_AGENT}


# --------------------------------------------------------------------------- helpers


def parse_auth_code(redirected_url: str) -> str:
    """Extract the ``code`` query parameter from the post-SCA redirect URL."""
    codes = parse_qs(urlparse(redirected_url).query).get("code")
    if not codes:
        raise ValueError("redirect URL has no 'code' parameter")
    return codes[0]


def extract_balance(balances: dict[str, Any], currency: str) -> Decimal | None:
    """Best-effort closing balance for ``currency`` from a ``/balances`` response."""
    matching = [
        bal
        for bal in balances.get("balances", [])
        if str(bal.get("balance_amount", {}).get("currency")) == currency
    ]
    if not matching:
        return None

    def rank(bal: dict[str, Any]) -> int:
        btype = str(bal.get("balance_type") or "")
        return _BALANCE_TYPE_PREFERENCE.index(btype) if btype in _BALANCE_TYPE_PREFERENCE else 99

    best = min(matching, key=rank)
    amount = best.get("balance_amount", {}).get("amount")
    # Same rejection as the transaction amounts: a NaN LEDGERBAL would make GnuCash's reconcile
    # dialog pre-fill from a value that compares False against everything, and an infinite one
    # would serialize verbatim. See finite_decimal's docstring for why not clamped.
    if amount is None:
        return None
    return finite_decimal(str(amount), field="balance_amount.amount")


def _currency_from_balances(balances: dict[str, Any]) -> str | None:
    """Return the first real currency among the balance entries, or None if there is none.

    A balance entry reporting ISO 4217's ``XXX`` placeholder is skipped rather than returned, so
    a bogus entry does not shadow a real currency reported by a sibling balance entry.
    """
    for bal in balances.get("balances", []):
        c = currency_or_none(bal.get("balance_amount", {}).get("currency"))
        if c:
            return c
    return None


def _acctid_from_hash(identification_hash: str) -> str:
    """A short, stable ``ACCTID`` derived from Enable Banking's ``identification_hash``.

    The raw hash is ~100 characters of base64 with ``+/=.`` in it — unusable as an OFX
    ``ACCTID``. Digesting it keeps the one property that matters: Enable Banking documents the
    hash as stable "for matching accounts between multiple sessions", unlike ``uid``, which is
    regenerated on every re-link and would make GnuCash see a brand-new account each time.
    """
    return "eb-" + hashlib.sha1(identification_hash.encode("utf-8")).hexdigest()[:16]


def _operation_of(exc: httpx.HTTPError) -> str:
    """Which data call failed, read off the request URL rather than tracked by the caller."""
    request = getattr(exc, "request", None)
    path = str(request.url.path) if request is not None else ""
    if path.endswith("/balances"):
        return "balances"
    if path.endswith("/transactions"):
        return "transactions"
    return "session" if "/sessions" in path else "fetch"


def _iban_shared_within(accounts: Iterable[LinkedAccount]) -> bool:
    """Whether any IBAN is reported for more than one account of this connection.

    The trigger for docs/adr-revolut-onboarding.md decision 1: Revolut reports one master IBAN
    on four of its five currency pockets, so an IBAN-derived ``ACCTID`` names a group, not an
    account. This is a property of the connection's whole account set — ``_known_acctid`` sees
    one account at a time and cannot decide it alone — so it is computed once per connection and
    threaded to every resolution site. A site left out would silently disagree with the fetch
    about the same account's identity, which is worse than the collapse being fixed, because it
    is invisible from either side.

    ``LinkedAccount.iban`` may hold a BBAN or other identifier (``account_identifier`` stores
    ``other.identification`` when ``account_id.iban`` is null), and this check deliberately does
    **not** scheme-validate it: whatever the field holds is what ``ACCTID`` would resolve to, so
    two accounts sharing it *is* the collapse — the coverage conflation and the per-account
    ``FITID`` de-duplication loss follow from the shared value, not from its scheme. Gating on
    "is it really an IBAN" would reintroduce the defect for any connection sharing a non-IBAN
    identifier. Measured across this deployment's coverage keys (which digest the resolved
    ``ACCTID``, any scheme): no connection but Revolut shares one.
    """
    seen: set[str] = set()
    for account in accounts:
        if not account.iban:
            continue
        if account.iban in seen:
            return True
        seen.add(account.iban)
    return False


def _known_acctid(
    stored: LinkedAccount | None,
    uid: str,
    id_hashes: Mapping[str, str],
    *,
    iban_shared: bool,
) -> str:
    """The account's ``ACCTID`` from what is known without a successful fetch.

    Same resolution order as the happy path, minus the balances-derived branch, so a failure can
    name the account the user recognises instead of an opaque UID.

    ``iban_shared`` is the connection-level fact :func:`_iban_shared_within` computes, and it is
    deliberately **required**: a caller that forgot it would silently disagree with the fetch
    about the same account's identity, so forgetting must not type-check. When any IBAN in the
    connection is shared, **every** account of it resolves through ``identification_hash`` — the
    unique-IBAN pocket included, so no account's identity can flip later because of an ASPSP-side
    change to a *sibling* (docs/adr-revolut-onboarding.md decision 1, the uniform policy). The
    floor is part of the rule: an account with no hash keeps its IBAN and is warned about
    (:func:`_warn_shared_iban`), because a shared-but-stable identifier mis-files statements
    recoverably while the per-link ``uid`` would orphan the account every consent cycle.
    """
    iban = stored.iban if stored else None
    if iban and iban_shared:
        # Only the *stored* hash counts in this branch. Consulting the fetch-time map here would
        # make a floored account's ACCTID depend on GET /sessions still reporting a hash — a
        # value the state-only callers (_stored_acctids) can never see — so fetch and status
        # would diverge about the same account, and the floor's warning would stay silent.
        # invariant: an identifier shared within a connection is not an ACCTID for any of its
        # accounts; a hashless account keeps its IBAN, never the re-link-volatile uid.
        # AGENTS.md#invariants
        link_hash = stored.identification_hash if stored else None
        return _acctid_from_hash(link_hash) if link_hash else iban
    stored_hash = (stored.identification_hash if stored else None) or id_hashes.get(uid)
    return iban or (_acctid_from_hash(stored_hash) if stored_hash else uid)


def _stored_acctids(accounts: Sequence[LinkedAccount]) -> list[str]:
    """Resolve every stored account's ``ACCTID``, connection-wide policy included.

    The one wrapper for the callers that resolve from ``state/<bank>.json`` alone (window
    resolution, the dry run, ``status``), so none of them can skip the shared-IBAN determination
    and drift from what the fetch writes about the same accounts.
    """
    iban_shared = _iban_shared_within(accounts)
    return [_known_acctid(a, a.uid, {}, iban_shared=iban_shared) for a in accounts]


def _redact_account(value: str) -> str:
    """Mask the middle of an account identifier for safe display in progress logs.

    ``acct_id`` may be a full IBAN; printing it to stderr/CI would leak the account number.
    Keep a short prefix and suffix so accounts stay distinguishable (e.g. ``PL61***1234``).
    """
    if len(value) <= 8:
        return value
    return f"{value[:4]}***{value[-4:]}"


def _enablebanking_banks(config: AppConfig) -> dict[str, BankConfig]:
    return {k: v for k, v in config.banks.items() if v.source == ENABLEBANKING}


def require_valid_batch_size(batch_size: int | None) -> None:
    """Reject a batch size that is not a positive count, from every entry point.

    ``None`` is "no batching" and is always valid. Anything below 1 is a typo, not an off-switch:
    ``--batch-size 0`` would otherwise reach :func:`~gnucash_ofx.ofxout.split_into_batches`, where
    every day exceeds the cap and the run quietly writes one file per booking date instead of
    failing (docs/adr-ofx-batch-splitting.md decision 9 — ``--no-batch-size`` is how a run opts
    out).

    Lives here rather than in ``cli.py`` for the same reason :func:`require_ordered_window` does:
    a global precondition belongs in every entry point, not only the one its author was looking
    at, so the library and the CLI cannot come to different verdicts about what is valid.
    """
    if batch_size is not None and batch_size < 1:
        raise RunError(f"batch size must be 1 or more (got {batch_size})")


def require_ordered_window(date_from: date | None, date_to: date | None) -> None:
    """Reject a window that runs backwards, before it can cost anything to discover.

    Both dates parse, so nothing upstream catches this — but no bank has transactions between the
    31st and the 1st, and asking spends a counted request per account at ASPSPs that allow a few a
    day. It is a typo, and the bank is the wrong place to learn about a typo.

    Called from :func:`fetch_enablebanking`, :func:`dry_run_enablebanking` *and* the CLI, which
    needs it before it looks up the public IP and opens the run log — otherwise a reversed window
    is answered with an error about background mode rather than about the dates. One function so
    the dry run cannot come to a different verdict than the fetch it is predicting; equal dates are
    a one-day window and stay valid.

    A date that was not given cannot be out of order, so an omitted end is simply not checked here;
    a resolved default is ordered by construction (:func:`resolve_window`).
    """
    if date_from is not None and date_to is not None and date_from > date_to:
        raise RunError(f"invalid date range: --from {date_from} is after --to {date_to}")


# How far back a window with nothing to resume from reaches. Measured 2026-08-09: Alior and Erste
# both serve `date_from` at exactly today-90 and Alior refuses today-120, so 90 is the last day
# served rather than the first refused. The day held back is **not** a guess at that boundary - it
# covers the difference between the local `date.today()` this tool computes from and whatever day
# the ASPSP thinks it is, which west of UTC runs in the direction that makes the local answer one
# day too old. See docs/adr-coverage-ledger-and-warnings.md §9.
DEFAULT_LOOKBACK = timedelta(days=89)

# What we tell the user is still recoverable: the measured 90, not the 89 above. That extra day is a
# margin on what we *ask for* and has no business shortening what we report as reachable. It is the
# PSD2 floor every ASPSP guarantees, so it is true everywhere; banks serving more (Millennium, Wise)
# simply have gaps that stay recoverable past the date we name.
RECOVERABLE_HORIZON = timedelta(days=90)

# Warn this far ahead of a consent expiring. Derived, not picked: a warning has to be seen at least
# one run before expiry, the longest observed gap between runs was 38 days, and a re-link needs the
# user at a browser - so 38 plus a week. Larger is not free: acting on the first warning means
# re-linking every `180 - N` days, and every re-link strands that bank's fetch cache. Re-derive this
# from `state/fetch-log.jsonl` once it holds a few months of real intervals.
CONSENT_WARNING_DAYS = 45


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    """The window a bank will actually be asked for, and how it was arrived at."""

    date_from: date
    date_to: date
    # True when either end was defaulted rather than typed, so the caller can say so.
    implied: bool
    # The start the ledger implied, when it was older than DEFAULT_LOOKBACK allows. Set only when
    # something was actually given up, so a first run (which has nothing to give up) stays quiet.
    clamped_from: date | None = None
    # True only when a recorded coverage end actually decided the start. Distinguishing this from
    # the fixed lookback matters for what is *said*: "resuming from coverage" on a bank that has no
    # ledger claims a provenance the window does not have, which is how a user learns not to trust
    # the line. Caught in QA against the real state directory, where all six banks said it.
    resumed: bool = False

    @property
    def unrecoverable_days(self) -> int:
        """How many days the clamp gave up; 0 when nothing was clamped."""
        if self.clamped_from is None:
            return 0
        return (self.date_from - self.clamped_from).days


def resolve_window(
    *,
    date_from: date | None,
    date_to: date | None,
    coverage: BankCoverage,
    acctids: Sequence[str],
    today: date,
) -> ResolvedWindow:
    """Work out one bank's window from what was typed and what has already been fetched.

    Retyping the range every month is itself the main source of the mistake this whole feature
    exists to catch — a window that starts on the 11th because that is what the last one ended on.
    So an omitted ``--from`` resumes from the ledger, and an omitted ``--to`` is today.

    **The resume point steps back by ``LATE_BOOKING_MARGIN``**, the constant defined in ``cache.py``
    and imported rather than re-picked here. A bank can book a transaction with a booking date a few
    days before it appears, so resuming exactly where coverage ended can skip one; re-fetching the
    margin is close to free (a settled month does not expire, so a well-covered account pays the
    same requests either way) while a gap is permanent. Asymmetric cost, so bias hard toward
    overlap.

    **The earliest account of the bank decides**, and an account with no record makes the answer
    unknown rather than skipping it — the default has to be safe for the *least* covered account,
    and one nobody has a record for is the least covered there is.

    An **explicit** ``--from`` is used exactly as given and never clamped: the user knows whether
    their bank serves more than the PSD2 floor. A defaulted one is clamped to
    :data:`DEFAULT_LOOKBACK`, because a default that earns a ``400`` at the three banks that age out
    is not a default. The clamp is reported through :attr:`ResolvedWindow.clamped_from` rather than
    applied silently.

    Identity note: ``acctids`` here are resolved from **link-time** state alone, because this runs
    before ``GET /sessions``. An account whose ``ACCTID`` can only be resolved at fetch time (no
    stored IBAN, hash only in ``accounts_data``) therefore reads as unknown and gets the full
    lookback — over-fetching, which is the safe direction.
    """
    resolved_to = date_to if date_to is not None else today
    if date_from is not None:
        return ResolvedWindow(date_from=date_from, date_to=resolved_to, implied=date_to is None)

    floor = today - DEFAULT_LOOKBACK
    covered_through = coverage.earliest_covered_through(acctids)
    wanted = covered_through + ONE_DAY - LATE_BOOKING_MARGIN if covered_through else floor
    clamped_from = wanted if wanted < floor else None
    # `min` with the end: coverage can already reach past an explicitly requested `--to`, and a
    # window that runs backwards is refused everywhere else in this codebase. It collapses to a
    # single day, which _report_window says out loud rather than leaving to be noticed.
    resolved_from = min(max(wanted, floor), resolved_to)
    return ResolvedWindow(
        date_from=resolved_from,
        date_to=resolved_to,
        implied=True,
        clamped_from=clamped_from,
        resumed=covered_through is not None,
    )


def _selected_banks(config: AppConfig, only: str | None) -> dict[str, BankConfig]:
    """The Enable Banking banks a run covers, narrowed to ``only`` when one was named.

    An explicit single-bank request that cannot be satisfied is a usage error, not a failure to
    report: there is nothing to degrade to, so it raises before anything is attempted.
    """
    banks = _enablebanking_banks(config)
    if only is None:
        return banks
    if only not in banks:
        raise RunError(f"'{only}' is not a configured Enable Banking bank")
    return {only: banks[only]}


# --------------------------------------------------------------------------- aspsps discovery


def aspsp_lines(client: EnableBankingClient, country: str) -> list[str]:
    """One line per available ASPSP for a country: the exact name to put in config.toml."""
    lines: list[str] = []
    for aspsp in client.list_aspsps(country):
        name = aspsp.get("name", "?")
        raw_psu_types = aspsp.get("psu_types")
        psu_types = [str(p) for p in raw_psu_types if p] if isinstance(raw_psu_types, list) else []
        suffix = f"  [{', '.join(psu_types)}]" if psu_types else ""
        lines.append(f"{name} ({aspsp.get('country', country)}){suffix}")
    return lines


# --------------------------------------------------------------------------- link


def _max_consent_days(client: EnableBankingClient, aspsp_name: str, country: str) -> int | None:
    """The ASPSP's advertised maximum consent validity, in whole days.

    Returns ``None`` when the bank is not in the catalog or does not advertise a limit; a
    discovery failure must never block linking, so lookup errors degrade to the default.
    """
    try:
        aspsps = client.list_aspsps(country)
    except (httpx.HTTPError, ResponseLimitExceeded):
        # An over-cap catalog is a discovery failure like any other, and this function's contract is
        # that discovery never blocks linking - so it degrades to the default rather than raising.
        return None
    for aspsp in aspsps:
        if aspsp.get("name") == aspsp_name and aspsp.get("country") == country:
            seconds = aspsp.get("maximum_consent_validity")
            if isinstance(seconds, int) and seconds > 0:
                return seconds // 86400
            return None
    return None


def start_link(
    client: EnableBankingClient,
    bank: BankConfig,
    *,
    valid_days: int | None = None,
) -> tuple[str, date]:
    """Begin authorization; return the SCA URL to open and the requested consent expiry date.

    By default the consent is requested for as long as the bank allows
    (``maximum_consent_validity`` from ``GET /aspsps`` — 180 days for every bank we use, versus
    the 90 we used to hardcode), which halves how often the browser SCA dance is needed. An
    explicit ``valid_days`` is still capped to the bank's maximum.
    """
    try:
        aspsp_name = bank.options["aspsp"]
        country = bank.options["country"]
    except KeyError as exc:
        raise RunError(
            f"bank '{bank.key}' is missing required option {exc} in config.toml"
        ) from exc
    psu_type = bank.options.get("psu_type", "personal")

    max_days = _max_consent_days(client, aspsp_name, country)
    if valid_days is None:
        valid_days = max_days if max_days is not None else DEFAULT_VALID_DAYS
    elif max_days is not None:
        valid_days = min(valid_days, max_days)

    application = client.get_application()
    redirect_urls = application.get("redirect_urls") or []
    if not redirect_urls:
        raise RunError("the Enable Banking application has no redirect URLs configured")
    # Shave a margin off the request: asking for the exact maximum can trip the ASPSP's own
    # check if our clock runs slightly ahead of theirs, which would fail the whole SCA flow.
    valid_until_dt = datetime.now(UTC) + timedelta(days=valid_days) - _CONSENT_SAFETY_MARGIN
    url = client.start_authorization(
        aspsp_name=aspsp_name,
        country=country,
        redirect_url=redirect_urls[0],
        valid_until=valid_until_dt.isoformat(),
        psu_type=psu_type,
    )
    return url, valid_until_dt.date()


def complete_link(
    client: EnableBankingClient,
    bank: BankConfig,
    code: str,
    requested_valid_until: date,
    state_dir: Path,
) -> SessionState:
    """Create the session from the redirect ``code`` and persist it."""
    session = client.create_session(code)
    granted = session.get("access", {}).get("valid_until")
    valid_until = _parse_valid_until(granted) or requested_valid_until
    # This response is the only place the full account resource appears, so capture all of it —
    # the typed fields we have consumers for, and the whole body for the ones we do not.
    state = SessionState(
        bank=bank.key,
        session_id=str(session["session_id"]),
        valid_until=valid_until,
        accounts=linked_accounts(session),
        raw=scrub_session(session),
    )
    save_session(state_dir, state)
    return state


def _parse_valid_until(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- fetch

_CHUNK_DAYS = 90  # Most PSD2 banks reject ranges longer than 90 days in one request.


def _txn_in_window(t: dict[str, Any], date_from: date, date_to: date) -> bool:
    """Return True if the transaction's booking/value/transaction date is within [from, to].

    Uses the same field precedence as the mapper. Transactions with no parseable date are
    kept (the mapper will raise for them later, making the error explicit).
    """
    booked = entry_booking_date(t)
    if booked is None:
        return True
    return date_from <= booked <= date_to


@dataclass(frozen=True, slots=True)
class RequestSpan:
    """One wire request, alongside the un-widened span whose days its response may claim.

    ``wire_from`` opens :data:`TRANSACTION_DATE_MARGIN` earlier than ``claim_from``: Alior
    filters the requested window by ``transaction_date``, so an entry booked inside the span but
    purchased just before it is only returned by a request that opens early (measured live —
    docs/adr-transaction-date-window-margin.md decision 1). The claim stays the un-widened span,
    because a request opening at ``from − margin`` is only booking-complete from ``from`` —
    claiming the margin days would repeat the original bug one level down (decision 4).
    """

    wire_from: date
    claim_from: date
    claim_to: date


def _request_spans(
    months: list[date],
    date_from: date,
    date_to: date,
    chunk_days: int = _CHUNK_DAYS,
    *,
    request_floor: date | None = None,
) -> list[RequestSpan]:
    """Group months needing a fetch into as few requests as the bank's window limit allows.

    Two constraints pull in opposite directions and are resolved here rather than conflated.
    Requests want to be **large**, because many ASPSPs reject windows over ~90 days and every call
    is counted against a daily allowance. Storage wants to be **monthly**, because that is what
    makes a chunk reusable by a differently-shaped window. So a span covers as many consecutive
    months as fit, and the response is sliced into months afterwards.

    Spans never split a month, which is what keeps per-chunk saving simple: a month belongs to
    exactly one request, so there is no partial chunk to merge into. That costs at most one extra
    request per ~90 days versus flat day-counting.

    Every span's **wire** request opens :data:`TRANSACTION_DATE_MARGIN` early (see
    :class:`RequestSpan`) — every span, not only the first: where the window-size cap splits one
    contiguous group, the later request's margin is what re-returns an entry transacted before
    the cut but booked after it, so the merge (authoritative inside its claim) cannot delete what
    only the earlier request returned. The margin counts against ``chunk_days``, so a widened
    request never exceeds what the bank accepts.

    **Claims** are still clamped to the window and never widened past ``date_from``. The wire is
    bounded on both sides: ``request_floor`` (the 89-day lookback clamp) absorbs the margin at
    the horizon, because Alior and Erste refuse any window starting more than ~90 days back and
    a margin must never turn a working fetch into a ``400`` at exactly the banks it protects —
    an explicit ``--from`` included, or the ADR's own repair command (decision 6, ``--from`` at
    the horizon) would fail; and the wire never rises **above** ``claim_from``, or a floor above
    the whole window (a defaulted ``--from`` against an old explicit ``--to`` resolves below the
    clamp) would invert the request — asked with ``date_from`` after ``date_to``, whose empty
    answer would then claim days no request answered. ``request_floor=None`` widens
    unconditionally.
    """

    def wire(claim_from: date) -> date:
        widened = claim_from - TRANSACTION_DATE_MARGIN
        if request_floor is None:
            return widened
        return min(max(widened, request_floor), claim_from)

    spans: list[RequestSpan] = []
    group: list[date] = []

    def flush() -> None:
        if not group:
            return
        claim_from = max(date_from, group[0])
        claim_to = min(date_to, month_end(group[-1]))
        spans.append(RequestSpan(wire(claim_from), claim_from, claim_to))

    for month in months:
        if group:
            contiguous = month == next_month(group[-1])
            candidate_start = max(date_from, group[0])
            candidate_end = min(date_to, month_end(month))
            if not contiguous or (candidate_end - wire(candidate_start)).days + 1 > chunk_days:
                flush()
                group = []
        group.append(month)
    flush()
    return spans


def _account_balances(
    client: EnableBankingClient,
    uid: str,
    *,
    cache_dir: Path | None,
    cache_ttl: timedelta,
    refresh: bool,
    ledger_balance_wanted: bool,
    currency_known: bool,
) -> dict[str, Any]:
    """The account's ``/balances`` response, or ``{}`` when the call is not worth making.

    The call answers two questions, and only one of them is window-sensitive.

    **The ledger balance.** ``/balances`` takes no date and there is no historical-balance
    endpoint: Enable Banking reports the balance *now*, with ``reference_date`` equal to the fetch
    date in every record measured. But ``OfxWriter`` dates ``LEDGERBAL`` from the statement's end
    date, and GnuCash feeds both into ``recnWindowWithBalance()`` — so for a window that closed
    weeks ago we were handing the user today's balance as the figure to reconcile a past statement
    against, and back-computing a wrong opening balance from it. There is no correct value to
    fetch, so for a past window the call is skipped and the statement carries the period's own
    running total instead. See ``docs/adr-aspsp-rate-limit-domain.md`` §1 and decision 4.

    **The account's currency.** ``GET /accounts/{uid}`` is 404 in Restricted Mode, so for a session
    linked before the currency was captured at link time this response is the only source — and
    without it the account would be skipped entirely, writing no file. So the call still happens
    when the currency is unknown, whatever the window. A *stale* balance answers this one perfectly
    (a currency does not change), which is why the cache lookup drops the TTL in that case: after
    one fetch, even a legacy session stops paying for it.
    """
    if not ledger_balance_wanted and currency_known:
        return {}
    if cache_dir is not None and not refresh:
        cached = load_cached_balances(
            cache_dir, uid, ttl=cache_ttl if ledger_balance_wanted else None
        )
        if cached is not None:
            return cached.balances
    data = client.get_balances(uid)
    if cache_dir is not None:
        save_cached_balances(cache_dir, uid, data)
    return data


def _file_span_months(
    cache_dir: Path, uid: str, span: RequestSpan, fetched: list[dict[str, Any]]
) -> None:
    """File everything a span's response returned into booking-month chunks; claim only the span.

    Every returned entry is filed into its booking month, **including months outside the
    requested span**: the ASPSP windows on ``transaction_date`` where that is observable at all,
    so a July request legitimately returns entries booked in early August, and data paid for with
    a counted request must never be thrown away by our own slicing — the old
    ``months_in(span_from, span_to)`` narrowing did exactly that
    (docs/adr-transaction-date-window-margin.md decision 2).

    An entry filed outside the span never widens a claim: it lands beyond it (an empty claim when
    no chunk exists yet — a crosser proves its own existence, not that its day was served in
    full). An undateable entry is kept in every claimed month, as the old window filter kept it,
    so the mapper still raises for it and the problem stays visible.
    """
    claimed = months_in(span.claim_from, span.claim_to)
    claimed_months = set(claimed)
    by_month: dict[date, list[dict[str, Any]]] = {month: [] for month in claimed}
    for txn in fetched:
        booked = entry_booking_date(txn)
        if booked is None:
            for month in claimed:
                by_month[month].append(txn)
            continue
        by_month.setdefault(month_start(booked), []).append(txn)
    for month, txns in by_month.items():
        if month in claimed_months:
            covered_from: date | None = max(span.claim_from, month)
            covered_to: date | None = min(span.claim_to, month_end(month))
        else:
            covered_from = covered_to = None
        save_cached_month(cache_dir, uid, month, covered_from, covered_to, txns)


def _warn_booking_lag(
    fetched: list[dict[str, Any]],
    *,
    bank_key: str,
    account: str,
    warn: WarnFn,
) -> None:
    """Warn when an observed booking−transaction lag exceeds the request margin.

    The margin is a policy value; this warning is its named revisit trigger
    (docs/adr-transaction-date-window-margin.md decision 5) — a warning and never a failure,
    because *this* run's files are correct: it is some other, narrower window that may have a
    hole. It costs zero requests, and it self-arms at any bank that starts populating
    ``transaction_date``. It can only see entries that were returned, so its reach is bounded
    (roughly the two margins' sum on the resumed cadence); silence is not proof.
    """
    worst = 0
    for txn in fetched:
        booked = entry_booking_date(txn)
        raw_transacted = txn.get("transaction_date")
        if booked is None or not raw_transacted:
            continue
        try:
            transacted = date.fromisoformat(str(raw_transacted))
        except ValueError:
            continue
        worst = max(worst, (booked - transacted).days)
    if worst > TRANSACTION_DATE_MARGIN.days:
        warn(
            FetchWarning(
                bank_key=bank_key,
                kind="coverage",
                message=(
                    f"a booking trailed its transaction date by {worst} day(s), past the "
                    f"{TRANSACTION_DATE_MARGIN.days}-day request margin - an earlier narrow "
                    "window may have missed such entries; re-fetch the range with --refresh "
                    "if a statement looks short"
                ),
                account=account,
            )
        )


def _account_raw_data(
    client: EnableBankingClient,
    uid: str,
    date_from: date,
    date_to: date,
    *,
    cache_dir: Path | None,
    cache_ttl: timedelta,
    refresh: bool,
    progress: ProgressFn,
    label: str,
    ledger_balance_wanted: bool,
    currency_known: bool,
    today: date,
    request_floor: date | None,
    bank_key: str,
    account: str,
    warn: WarnFn,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(balances, raw_transactions)`` for one account, filling only what is missing.

    The cache is consulted a month at a time, so a window overlapping one already fetched pays
    only for the months it adds — the ordinary "``--to`` today, then again tomorrow" pattern used
    to miss entirely. ``refresh`` forces every month to be re-fetched.

    Requests open :data:`TRANSACTION_DATE_MARGIN` earlier than the spans they answer for (see
    :class:`RequestSpan`; ``request_floor`` bounds the widening on a defaulted window). The
    margin days are a wire concern only — they never widen what is claimed, and entries they
    return that book outside the window are trimmed from the statement by the caller's existing
    window filter.
    """
    balances_data = _account_balances(
        client,
        uid,
        cache_dir=cache_dir,
        cache_ttl=cache_ttl,
        refresh=refresh,
        ledger_balance_wanted=ledger_balance_wanted,
        currency_known=currency_known,
    )
    # One budget for this account-window, shared by every span below: the cached path fetches
    # span by span so each month can be persisted as it lands, and a per-call budget would make the
    # cap `cap x spans`. See _fetch_spans.
    budget = PageBudget()
    if cache_dir is None:
        spans = _request_spans(
            months_in(date_from, date_to), date_from, date_to, request_floor=request_floor
        )
        fetched = _fetch_spans(
            client, uid, [(span.wire_from, span.claim_to) for span in spans], budget
        )
        _warn_booking_lag(fetched, bank_key=bank_key, account=account, warn=warn)
        return balances_data, fetched

    if refresh:
        served: list[dict[str, Any]] = []
        missing = months_in(date_from, date_to)
        oldest = None
    else:
        served, missing, oldest = cached_window(
            cache_dir, uid, date_from, date_to, today=today, ttl=cache_ttl
        )
    if not missing:
        progress(f"{label}: using cached data from {oldest:%Y-%m-%d %H:%M} UTC")
        return balances_data, served

    if served:
        progress(f"{label}: fetching {len(missing)} month(s) not already cached...")
    else:
        progress(f"{label}: fetching transactions...")
    raw = list(served)
    fetched_total: list[dict[str, Any]] = []
    for span in _request_spans(missing, date_from, date_to, request_floor=request_floor):
        fetched = _fetch_spans(client, uid, [(span.wire_from, span.claim_to)], budget)
        # Persist each span's months as they land. Saving once at the end meant a 429 on the last
        # chunk discarded everything already paid for, on the accounts least able to afford it.
        _file_span_months(cache_dir, uid, span, fetched)
        raw.extend(fetched)
        fetched_total.extend(fetched)
    _warn_booking_lag(fetched_total, bank_key=bank_key, account=account, warn=warn)
    return balances_data, raw


def _fetch_spans(
    client: EnableBankingClient,
    uid: str,
    spans: list[tuple[date, date]],
    budget: PageBudget,
) -> list[dict[str, Any]]:
    """Fetch these spans under the caller's page budget.

    ``budget`` is a parameter and not a local, because the cached path calls this once **per span**
    (each span's months are persisted as they land) — so a budget created here would hand every span
    a fresh allowance and the real bound would be `cap x spans`, not the per-account-window one
    docs/adr-input-hardening.md decision 5 states. One budget per account-window lives in
    :func:`_account_raw_data`.
    """
    raw: list[dict[str, Any]] = []
    for span_from, span_to in spans:
        raw.extend(
            client.iter_transactions(
                uid,
                date_from=span_from.isoformat(),
                date_to=span_to.isoformat(),
                budget=budget,
            )
        )
    return raw


def _span_text(span: DaySpan) -> str:
    start, end = span
    return str(start) if start == end else f"{start} -> {end}"


def _warn_stale_identity(
    bank_key: str,
    acctids: Mapping[str, str],
    *,
    warn: WarnFn,
    progress: ProgressFn,
) -> None:
    """Warn when an account's ``ACCTID`` is still Enable Banking's opaque UID.

    **Not a state-schema check.** A session file predating the ``accounts`` layout is not the
    problem — most of them carry ``account_ibans``, so ``ACCTID`` resolves exactly as it does under
    the current schema, and warning on the schema would send the user through a browser SCA dance
    per bank to fix nothing. The problem is one account resolving to the bare UID, which Enable
    Banking regenerates on every re-link: GnuCash would then see a brand-new account each consent
    cycle and the imported history would orphan itself.

    This is why the check runs here rather than in the dry run — the fetch-time ``accounts_data``
    hashes are already folded into ``acctids``, and they rescue most v1-schema accounts. A dry run
    has no such array and would over-report.

    What it must **not** do is change what ``ACCTID`` resolves to. That would orphan precisely the
    accounts this warning exists to protect; the warning is the whole intervention.
    """
    stale = [uid for uid, acctid in acctids.items() if acctid == uid]
    if not stale:
        return
    progress(f"{bank_key}: {len(stale)} account(s) still identified by an opaque id")
    warn(
        FetchWarning(
            bank_key=bank_key,
            kind="identity",
            message=(
                f"{len(stale)} account(s) have no stable identifier, so their OFX ACCTID is an id "
                "the bank regenerates on every re-link - GnuCash will see new accounts next time "
                "and the imported history will orphan itself. Fix it once with: "
                f"gnucash-ofx link {bank_key}"
            ),
        )
    )


def _warn_shared_iban(
    bank_key: str,
    acctids: Mapping[str, str],
    linked: Mapping[str, LinkedAccount],
    *,
    iban_shared: bool,
    warn: WarnFn,
    progress: ProgressFn,
) -> None:
    """Warn when the shared-IBAN floor left an account on its IBAN for want of a hash.

    In a shared-IBAN connection every account resolves through ``identification_hash``
    (docs/adr-revolut-onboarding.md decision 1); one that cannot — no hash captured at link time
    or reported at fetch time — keeps its IBAN. Where that IBAN is itself the shared one, the
    sharing accounts' statements still land under one GnuCash account identity: recoverable
    mis-filing, chosen over the self-orphaning a ``uid`` fallback would buy. Not silent, though —
    re-linking captures the hash and fixes it once, the same remedy `_warn_stale_identity`
    points at.

    Keyed on the symptom for the same reason that warning is: it fires only when the floor was
    actually taken, never on the connection's shape alone, so a shared-IBAN connection whose
    accounts all carry hashes (Revolut, measured 5/5) fetches without a word.
    """
    if not iban_shared:
        return
    floored = [
        uid
        for uid, acctid in acctids.items()
        if (stored := linked.get(uid)) is not None and stored.iban and acctid == stored.iban
    ]
    if not floored:
        return
    progress(f"{bank_key}: {len(floored)} account(s) kept an IBAN despite the shared-IBAN policy")
    warn(
        FetchWarning(
            bank_key=bank_key,
            kind="identity",
            message=(
                f"{len(floored)} account(s) of a shared-IBAN connection have no stable "
                "identification_hash, so their OFX ACCTID stays on the IBAN - where that IBAN "
                "is the shared one, those accounts' statements land under a single GnuCash "
                f"account. Capture the hash once with: gnucash-ofx link {bank_key}"
            ),
        )
    )


def _warn_coverage_gaps(
    bank_key: str,
    coverage: BankCoverage,
    acctids: Iterable[str],
    *,
    date_from: date,
    date_to: date,
    today: date,
    warn: WarnFn,
    progress: ProgressFn,
) -> None:
    """Warn about days this window leaves uncovered, escalating the ones that are about to age out.

    Reported per account, because accounts already fail independently within a bank and coverage
    has to match that granularity or a failed account silently inherits its sibling's record.

    An account with **no** record is skipped entirely: absent means unknown, never "gap since the
    epoch". Every bank in existence starts there, and a first run that printed a wall of warnings
    nobody can act on would teach the user to ignore the ones that matter.

    **The window being fetched counts as covered while the gaps are worked out.** It is about to
    be, and reporting it as a gap would fire on every single run — the fastest way to teach someone
    to ignore this. So the question is not "what does the record miss?" but "what will still be
    missing when this run finishes?", and the answer falls out of merging the window in first.
    """
    if coverage.unreadable:
        warn(
            FetchWarning(
                bank_key=bank_key,
                kind="ledger",
                message=(
                    "the coverage record could not be read, so gaps cannot be reported for this "
                    "bank; it will be rebuilt from this run onwards"
                ),
            )
        )
        return
    oldest_recoverable = today - RECOVERABLE_HORIZON
    for acctid in acctids:
        entry = coverage.for_account(acctid)
        if entry is None:
            continue
        covered_from, covered_through = entry.covered_from, entry.covered_through
        if covered_from is None or covered_through is None:
            continue
        # Scan everything this account either claims or is about to claim. Below the earliest of
        # those there is no record at all, and unknown is not a gap.
        gaps = missing_days(
            [*entry.spans, (date_from, date_to)],
            min(covered_from, date_from),
            max(covered_through, date_to),
        )
        if not gaps:
            continue
        redacted = _redact_account(acctid)
        for gap in gaps:
            start, end = gap
            if end < oldest_recoverable:
                detail = (
                    "already past the 90 days PSD2 guarantees, so it may no longer be fetchable "
                    "at all"
                )
            elif start < oldest_recoverable:
                detail = (
                    f"partly past the 90 days PSD2 guarantees; the rest ages out from "
                    f"{oldest_recoverable}"
                )
            else:
                detail = f"still recoverable, but ages out from {start + RECOVERABLE_HORIZON}"
            progress(f"  {bank_key} {redacted}: not fetched {_span_text(gap)} ({detail})")
            warn(
                FetchWarning(
                    bank_key=bank_key,
                    kind="coverage",
                    account=redacted,
                    message=(
                        f"never fetched {_span_text(gap)} - {detail}. To close it: "
                        f"gnucash-ofx fetch --bank {bank_key} --from {start} --to {end}"
                    ),
                )
            )


def _warn_consent_expiry(
    bank_key: str, session: SessionState, *, today: date, warn: WarnFn
) -> None:
    """Warn when a consent is close enough to expiry that the next run might be too late.

    ``status`` already answers this, but it has to be *remembered*. On a bank whose history ages
    out, a consent that lapses unnoticed does not cost access — it costs the months between the
    last fetch and whenever somebody notices, permanently.
    """
    remaining = days_until_expiry(session, today=today)
    if remaining > CONSENT_WARNING_DAYS:
        return
    warn(
        FetchWarning(
            bank_key=bank_key,
            kind="consent",
            message=(
                f"consent expires in {remaining} day(s), on {session.valid_until}. Re-link before "
                "it lapses: a bank serving only ~90 days of history cannot backfill what is missed "
                f"while nobody is looking. Run: gnucash-ofx link {bank_key}"
            ),
        )
    )


def _report_window(
    bank_key: str, window: ResolvedWindow, *, today: date, progress: ProgressFn, warn: WarnFn
) -> None:
    """Say which window this bank is actually getting, and what the clamp gave up.

    An implied window has to be visible: it is the difference between "this run fetched what I
    meant" and "this run fetched what some file remembered", and the user did not type it.
    """
    if window.implied:
        origin = (
            "resuming from recorded coverage"
            if window.resumed
            else "nothing fetched yet on record, so the last 89 days"
        )
        progress(f"{bank_key}: window {window.date_from} -> {window.date_to} ({origin})")
    if window.resumed and window.date_from == window.date_to and window.date_to < today:
        progress(
            f"{bank_key}: coverage already reaches past {window.date_to}, so this is a one-day "
            "window - pass --from to fetch a range ending there"
        )
    if window.clamped_from is None:
        return
    progress(
        f"{bank_key}: coverage ends before {window.date_from}, which is as far back as this "
        "defaults to"
    )
    warn(
        FetchWarning(
            bank_key=bank_key,
            kind="coverage",
            message=(
                f"coverage resumes at {window.clamped_from}, but the default window starts at "
                f"{window.date_from} - {window.unrecoverable_days} day(s) are not fetched by "
                f"default. PSD2 guarantees only ~90 days; if this bank serves more, ask for it: "
                f"gnucash-ofx fetch --bank {bank_key} --from {window.clamped_from}"
            ),
        )
    )


def _persist_coverage(
    state_dir: Path, coverage: BankCoverage, bank_key: str, *, warn: WarnFn
) -> BankCoverage:
    """Write the ledger, downgrading a write failure to a warning.

    A fetch that worked must not fail because a bookkeeping file could not be written — the same
    rule the run log follows. The difference is that the run log swallows the error silently,
    because a lost diagnostic costs nothing, while a lost coverage write makes every later run
    re-fetch the same window. That has a running cost, so it is said out loud.
    """
    try:
        save_coverage(state_dir, coverage)
    except OSError as exc:
        warn(
            FetchWarning(
                bank_key=bank_key,
                kind="ledger",
                message=(
                    f"this run's coverage could not be recorded ({exc}), so the next one cannot "
                    "resume from it and will re-fetch this window"
                ),
            )
        )
    return coverage


def fetch_bank(
    client: EnableBankingClient,
    *,
    bank_key: str,
    session_id: str,
    date_from: date,
    date_to: date,
    output_dir: Path,
    booked_only: bool = True,
    accounts: Mapping[str, LinkedAccount] | None = None,
    progress: ProgressFn = _noop_progress,
    cache_dir: Path | None = None,
    cache_ttl: timedelta = DEFAULT_TTL,
    refresh: bool = False,
    bank_id: str | None = None,
    today: date | None = None,
    warn: WarnFn = _noop_warn,
    state_dir: Path | None = None,
    collect: MutableSequence[tuple[Account, list[Txn], date, date]] | None = None,
    request_floor: date | None = None,
    batch_size: int | None = None,
) -> tuple[list[Path], list[BankFailure]]:
    """Fetch all accounts in a session and write one OFX file per account/currency.

    ``request_floor`` is the deepest day a margin-widened wire request may open at (see
    :class:`RequestSpan` and :func:`_request_spans` for both bounds). ``fetch_enablebanking``
    passes the 89-day lookback clamp for every window, explicit or defaulted, so the margin
    cannot turn a working fetch into a ``400`` at the banks that age out; ``None`` — the
    default — widens unconditionally.

    Returns the paths written and any per-account failures. A failing account is recorded and
    skipped so its siblings still get their files; a rate-limited one abandons the rest of the
    bank, because that allowance is per ASPSP and the siblings would fail identically.

    ``warn`` receives anything worth acting on that did not fail the fetch — a coverage gap, an
    account whose ``ACCTID`` still resolves to an opaque UID. It is a sink rather than a third
    return value so that callers wanting only files are unaffected. When ``state_dir`` is set the
    coverage ledger is read (to find gaps, before any account data is requested) and advanced as
    each account succeeds.

    ``accounts`` maps Enable Banking UIDs to what ``link`` captured about them. The stored IBAN
    is used as ``ACCTID`` in the OFX so GnuCash shows a recognisable account number rather than
    an opaque UUID, and the stored currency stands in when ``/balances`` does not report one.
    Sessions linked under the old state schema have neither, and fall back as before. The one
    exception is a connection in which any IBAN is shared between accounts (Revolut's currency
    pockets): there every account resolves through ``identification_hash`` instead — see
    :func:`_known_acctid` and docs/adr-revolut-onboarding.md decision 1.

    ``progress`` receives a status line per account as it is fetched. When ``cache_dir`` is set,
    fresh cached fetches (within ``cache_ttl``) are reused unless ``refresh`` is True.

    ``collect`` is another sink, same shape as ``warn``: when given, this bank's accounts and
    their transactions are appended to it — alongside **this call's own** ``date_from``/
    ``date_to``, not the run's — instead of being written to per-account files, and ``fetch_bank``
    returns ``([], failures)`` — no paths, because nothing was written here. The per-bank window
    travels with each entry because per-bank windows routinely differ (:func:`resolve_window` is
    resolved per bank, not per run) and a combined file must date each statement's
    ``BANKTRANLIST``/``LEDGERBAL`` from the window it was actually fetched with, not a sibling
    bank's. ``fetch_enablebanking`` passes this when composing one combined file for the whole run
    (docs/adr-combined-ofx-file.md decision 2); every disambiguation and filename concern below
    is per-account output and does not apply to that path.
    """
    linked = accounts or {}
    today = today or date.today()
    # A balance is only ever the balance *now*, so it can only be this statement's closing balance
    # when the statement ends now. For an earlier window there is nothing correct to fetch.
    ledger_balance_wanted = date_to >= today
    failures: list[BankFailure] = []
    try:
        session = client.get_session(session_id)
    except ResponseLimitExceeded as exc:
        # There is no account to blame for an oversized session response, and this call happens
        # before the account loop - so it becomes bank-scoped, the type `fetch_enablebanking`
        # already catches. Left as a bare exception it escaped every handler and cost the *other*
        # banks their files, replacing the stdout file list with a traceback.
        raise BankError(f"{bank_key}: the session response went past a local safety limit") from exc
    id_hashes = identification_hashes(session)
    raw_accounts = session.get("accounts", [])
    progress(f"{bank_key}: {len(raw_accounts)} account(s) in session")

    # Everything below is decided from the session response, which has already been paid for, and
    # from local state - so both warnings land before the first account's data is requested. A
    # coverage gap the user did not intend is worth knowing about while there is still a chance to
    # widen the window, not after the run has spent the allowance on the wrong one.
    # Computed from the stored account set - the same set the no-fetch callers (_stored_acctids)
    # decide from - not from the live session's uid list, so the dry run and the fetch cannot
    # disagree about the regime when the ASPSP adds or drops an account between them.
    iban_shared = _iban_shared_within(linked.values())
    acctids = {
        str(raw): _known_acctid(linked.get(str(raw)), str(raw), id_hashes, iban_shared=iban_shared)
        for raw in raw_accounts
    }
    _warn_stale_identity(bank_key, acctids, warn=warn, progress=progress)
    _warn_shared_iban(
        bank_key, acctids, linked, iban_shared=iban_shared, warn=warn, progress=progress
    )
    coverage = load_coverage(state_dir, bank_key) if state_dir is not None else None
    if coverage is not None:
        _warn_coverage_gaps(
            bank_key,
            coverage,
            acctids.values(),
            date_from=date_from,
            date_to=date_to,
            today=today,
            warn=warn,
            progress=progress,
        )

    # Collect all accounts first so we can detect same-bank+currency duplicates before writing.
    pending: list[tuple[Account, list[Any]]] = []
    for position, raw_entry in enumerate(raw_accounts, start=1):
        # GET /sessions/{id} always returns account UIDs as plain strings (verified; see
        # docs/enable-banking.md).
        uid = str(raw_entry)
        label = f"  [{position}/{len(raw_accounts)}] {bank_key}"
        stored = linked.get(uid)
        # Refuse before spending a request: the stored type is already local, and an account
        # whose statement cannot honestly be written must not spend the bank's daily allowance
        # learning transactions no file will carry (docs/adr-accttype-mapping.md decision 2).
        # The refused account still counts in _unfetched_siblings below, so its same-currency
        # siblings keep their filenames - the shape of the connection did not change.
        try:
            accttype_for(stored.cash_account_type if stored else None)
        except UncarryableAccountType as exc:
            failures.append(
                BankFailure(
                    bank_key=bank_key,
                    operation="account-type",
                    scope="account",
                    message=str(exc),
                    account=_redact_account(acctids[uid]),
                    window=(date_from, date_to),
                )
            )
            progress(f"{label}: FAILED (account type {exc.cash_account_type})")
            continue
        try:
            balances_data, raw = _account_raw_data(
                client,
                uid,
                date_from,
                date_to,
                cache_dir=cache_dir,
                cache_ttl=cache_ttl,
                refresh=refresh,
                progress=progress,
                label=label,
                ledger_balance_wanted=ledger_balance_wanted,
                currency_known=bool(stored and stored.currency),
                today=today,
                request_floor=request_floor,
                bank_key=bank_key,
                account=_redact_account(acctids[uid]),
                warn=warn,
            )
        except httpx.HTTPError as exc:
            status, api_code, payload = _response_details(exc)
            operation = _operation_of(exc)
            rate_limited = _is_rate_limited(exc)
            failures.append(
                BankFailure(
                    bank_key=bank_key,
                    operation=operation,
                    scope="bank" if rate_limited else "account",
                    message=f"fetching {operation} failed",
                    account=_redact_account(acctids[uid]),
                    window=(date_from, date_to),
                    status_code=status,
                    api_code=api_code,
                    payload=payload,
                )
            )
            progress(f"{label}: FAILED ({status or type(exc).__name__})")
            if rate_limited:
                # Per-ASPSP allowance: the remaining accounts of this bank would fail identically.
                progress(f"{bank_key}: rate limited, skipping its remaining account(s)")
                break
            continue
        except ResponseLimitExceeded as exc:
            # A response past a local bound: too many continuation pages, or too large a body. One
            # account's runaway server must not cost its siblings their files, and it must not be
            # served short either - so this fails the account and advances no coverage.
            failures.append(
                BankFailure(
                    bank_key=bank_key,
                    # Carried on the exception, not hard-coded: an oversized `/balances` body must
                    # not be reported as the transactions endpoint misbehaving. Never invent an
                    # error explanation.
                    operation=exc.operation,
                    scope="account",
                    message="the bank's response went past a local safety limit",
                    account=_redact_account(acctids[uid]),
                    window=(date_from, date_to),
                    payload=str(exc),
                )
            )
            progress(f"{label}: FAILED (response past a safety limit)")
            continue

        # GET /accounts/{uid} is not available in Restricted Mode, so currency is inferred from
        # balances, falling back to what link recorded when the ASPSP returns no usable balance.
        try:
            from_balances = _currency_from_balances(balances_data)
        except _BAD_DATA_ERRORS as exc:
            # Reading the balances sat outside every per-account guard until decision 3: a
            # wrong-typed `balances` entry raised straight past this loop and took the other banks
            # with it. It degrades rather than failing the account, because an unreadable balances
            # body *is* the "no usable balance" case the link-time fallback below exists for - and
            # the transactions this account needs may already be cached and free.
            warn(
                FetchWarning(
                    bank_key=bank_key,
                    kind="ledger",
                    message=f"the bank's balance data could not be read ({type(exc).__name__}); "
                    "falling back to the currency recorded at link time",
                    account=_redact_account(acctids[uid]),
                )
            )
            progress(f"{label}: unreadable balance data, using the link-time currency")
            from_balances = None
            balances_data = {}
        currency = from_balances or (stored.currency if stored else None)
        # Prefer the IBAN stored at link time — unless the connection shares one, in which case
        # every account resolved through the hash when the map above was built. Failing that use
        # Enable Banking's identification_hash, which is stable across sessions — unlike the UID,
        # which is regenerated on every re-link and would make GnuCash see a new account each
        # time. The link-time hash is preferred over the fetch-time one so ACCTID does not depend
        # on GET /sessions still reporting it; the two are documented as the same value.
        acct_id = acctids[uid]
        if not currency:
            progress(f"{label}: no currency, skipping")
            continue

        # Some banks ignore the date range server-side and return full history; filter
        # client-side so the OFX only contains the requested period.
        raw = [t for t in raw if _txn_in_window(t, date_from, date_to)]
        try:
            all_txns = map_transactions(raw, currency=currency, booked_only=booked_only)
        except _BAD_DATA_ERRORS as exc:
            # Bad data in one account (mismatched currency, undateable transaction, an amount that
            # is not a finite number) must not cost the sibling accounts their files. See
            # _BAD_DATA_ERRORS for why `ValueError` alone did not deliver that.
            failures.append(
                BankFailure(
                    bank_key=bank_key,
                    operation="mapping",
                    scope="account",
                    message="the bank's transaction data could not be read",
                    account=_redact_account(acct_id),
                    window=(date_from, date_to),
                    payload=str(exc),
                )
            )
            progress(f"{label}: FAILED (unreadable transaction data)")
            continue
        # Banks that ignore date filters may return the same transaction in multiple chunks;
        # deduplicate by stable id so the OFX never has duplicate FITIDs. The **later** copy's
        # content wins, at the first copy's position: `raw` is cached-then-fetched and the wire
        # margin deliberately re-asks a served month's tail, so a freshly amended entry must
        # replace the cached copy it duplicates - the cache merge already kept the fresh one, and
        # a statement that disagreed with the cache would import stale details under a FITID
        # GnuCash then refuses to correct on any later import.
        by_id: dict[str, Txn] = {}
        for t in all_txns:
            by_id[t.id] = t
        txns = list(by_id.values())
        # The three fields that reach the file raw, checked here and not in the writer: the write
        # loop has no per-account guard and coverage has already advanced by the time it runs, so a
        # rejection there would abort the run *and* leave the ledger claiming a day whose file was
        # never written. Here it is one account's failure, before anything is recorded.
        try:
            validate_raw_fields(
                acct_id, currency, (t.id for t in txns), bank_id_for(bank_key, bank_id)
            )
        except _BAD_DATA_ERRORS as exc:
            failures.append(
                BankFailure(
                    bank_key=bank_key,
                    operation="mapping",
                    scope="account",
                    message="the bank sent an identifier this OFX file cannot carry",
                    account=_redact_account(acct_id),
                    window=(date_from, date_to),
                    payload=str(exc),
                )
            )
            progress(f"{label}: FAILED ({str(exc).split(chr(32))[0]} the file cannot carry)")
            continue
        progress(f"{label} {_redact_account(acct_id)} ({currency}): {len(txns)} transaction(s)")
        # Fetched is not the same as usable. When the window has closed we may still have called
        # /balances - to learn the currency, which is the only way to learn it for a session
        # linked before that was captured - but the answer is today's balance, not this
        # statement's closing one, so it must not become the LEDGERBAL.
        try:
            end_balance = (
                extract_balance(balances_data, currency) if ledger_balance_wanted else None
            )
        except _BAD_DATA_ERRORS as exc:
            # Degraded, not failed. `end_balance=None` is not an invented number - it is the shape
            # every closed window already uses, and `build_statement` then fills LEDGERBAL from the
            # statement's own running total. Failing here would discard a fully mapped, deduplicated
            # statement over a figure the file does not require, and because coverage is (correctly)
            # not advanced on a failure, it would fail again identically on every later run - at a
            # bank serving ~90 days that window can age out first. Decision 3's "an invented amount
            # is worse than a missing file" argues against clamping or zeroing, not against writing
            # the statement.
            warn(
                FetchWarning(
                    bank_key=bank_key,
                    kind="ledger",
                    message=f"the bank's closing balance could not be read "
                    f"({type(exc).__name__}); the statement carries its own running total",
                    account=_redact_account(acct_id),
                )
            )
            progress(f"{label}: unreadable closing balance, using the statement total")
            end_balance = None
        account = Account(
            bank_key=bank_key,
            account_id=acct_id,
            currency=currency,
            end_balance=end_balance,
            bank_id=bank_id,
            cash_account_type=stored.cash_account_type if stored else None,
        )
        pending.append((account, txns))
        # Coverage advances here, not where the file is written: an account with no transactions in
        # the window writes no file (`write_account_ofx` returns None) and is nonetheless fully
        # covered. Recording it at the write would manufacture exactly the gap this exists to find.
        #
        # Written per account as it lands rather than once at the end, for the reason
        # `save_cached_month` is: a rate-limited sibling further down the loop must not discard the
        # coverage of the accounts that already succeeded.
        if coverage is not None and state_dir is not None:
            coverage = with_span(coverage, acct_id, date_from, date_to)
            coverage = _persist_coverage(state_dir, coverage, bank_key, warn=warn)

    # Every account of this bank is collected above before any file is written, so both legs of a
    # same-bank currency conversion are always available together here. See
    # docs/adr-currency-conversion-pairs.md decisions 1-4.
    try:
        pending = pair_conversions(pending)
    except _BAD_DATA_ERRORS as exc:
        # The pairing divides two booked amounts and quantizes the result, so a finite-but-absurd
        # magnitude (1e30 / 1) still raises InvalidOperation - an ArithmeticError, the very class
        # decision 3 widened the mapper guard for. finite_decimal bounds finiteness, not magnitude,
        # so this sits outside its reach. Unpaired statements are the ordinary shape (every
        # non-conversion account) and the annotation is an enrichment, so losing it costs a memo
        # line rather than a file - which beats letting one crafted amount abort every bank's run.
        warn(
            FetchWarning(
                bank_key=bank_key,
                kind="ledger",
                message=f"currency-conversion pairing failed ({type(exc).__name__}); "
                "the legs are written unpaired, without the exchange-rate annotation",
            )
        )
        progress(f"{bank_key}: conversion pairing failed, writing the legs unpaired")

    # Batching happens here, at the write, and nowhere earlier: coverage has already advanced from
    # the requested window above, so however many files an account's window is split into, the
    # ledger sees what it always did (docs/adr-ofx-batch-splitting.md decision 7).
    if collect is not None:
        for account, txns in pending:
            collect.extend(batch_statements(account, txns, date_from, date_to, batch_size))
        return [], failures

    disambiguators = _disambiguators(
        [account for account, _ in pending],
        siblings=_unfetched_siblings(raw_accounts, linked, pending, acctids),
    )

    written: list[Path] = []
    for account, txns in pending:
        for batch_account, batch, start, end in batch_statements(
            account, txns, date_from, date_to, batch_size
        ):
            path = write_account_ofx(
                batch_account,
                batch,
                start,
                end,
                output_dir,
                disambiguator=disambiguators.get(account.account_id),
            )
            if path is not None:
                written.append(path)
    return written, failures


def _unfetched_siblings(
    raw_accounts: list[Any],
    linked: Mapping[str, LinkedAccount],
    pending: list[tuple[Account, list[Any]]],
    acctids: Mapping[str, str],
) -> list[tuple[str, str]]:
    """``(acctid, currency)`` for accounts in the session that produced no ``pending`` entry.

    Only those whose currency was recorded at link time can be named — a session linked under the
    old state schema has none, and those simply do not participate (the pre-existing behaviour).

    Takes the already-resolved ``acctids`` map rather than resolving again, so it cannot fall out
    of step with the shared-IBAN determination made once per connection (decision 1 of
    docs/adr-revolut-onboarding.md).
    """
    done = {account.account_id for account, _ in pending}
    siblings: list[tuple[str, str]] = []
    for raw_entry in raw_accounts:
        uid = str(raw_entry)
        stored = linked.get(uid)
        if stored is None or not stored.currency:
            continue
        acct_id = acctids[uid]
        if acct_id not in done:
            siblings.append((acct_id, stored.currency))
    return siblings


def _disambiguators(accounts: list[Account], *, siblings: list[tuple[str, str]]) -> dict[str, str]:
    """``ACCTID`` → filename suffix, for currencies a bank holds more than one account in.

    Grouping counts every account **in the session**, not only the ones that produced output, so
    a filename depends on the shape of the connection rather than on which accounts happened to
    have transactions this period — or to fail. Without that, one failing account silently
    renames its surviving same-currency sibling, and GnuCash sees an unfamiliar file.
    """
    by_currency: dict[str, list[str]] = defaultdict(list)
    for account in accounts:
        by_currency[account.currency].append(account.account_id)
    for acct_id, currency in siblings:
        by_currency[currency].append(acct_id)

    disambiguators: dict[str, str] = {}
    for acct_ids in by_currency.values():
        if len(acct_ids) < 2:
            continue  # single account in this currency: keep the plain bank_currency name
        for acct_id, suffix in zip(acct_ids, account_disambiguators(acct_ids), strict=True):
            disambiguators[acct_id] = suffix
    return disambiguators


def fetch_enablebanking(
    config: AppConfig,
    client: EnableBankingClient,
    *,
    date_from: date | None,
    date_to: date | None,
    only: str | None = None,
    today: date | None = None,
    progress: ProgressFn = _noop_progress,
    cache_ttl: timedelta = DEFAULT_TTL,
    refresh: bool = False,
    runlog: RunLog | None = None,
    combine: bool = False,
    batch_size: int | None = None,
) -> FetchReport:
    """Fetch every linked Enable Banking bank (or just ``only``) into the output dir.

    One bank's failure does not stop the others: it is recorded and the run continues, so a
    rate-limited or unlinked bank cannot cost the rest of them their files. The caller decides
    how to report the failures and what to exit with.

    Errors that no bank could survive — missing credentials, a bad date range, ``only`` naming a
    bank that is not configured — still raise :class:`RunError` before anything is fetched.

    ``date_from`` / ``date_to`` may be ``None``, in which case each bank's window is resolved from
    its own coverage ledger (:func:`resolve_window`) — **per bank, not per run**, because one
    bank's deeper history would otherwise drag another past the ~90 days it will serve and turn a
    working fetch into a ``400``. An explicitly given window still applies to the whole run.

    ``progress`` receives short status lines as banks and accounts are processed. Fresh cached
    fetches (within ``cache_ttl``) are reused unless ``refresh`` forces a live fetch.

    ``combine`` (docs/adr-combined-ofx-file.md decision 2) makes every bank hand its pending
    accounts to a shared sink instead of writing per-account files; once every bank has been
    tried, one file carrying every statement is written from whatever accumulated, named from the
    union of the windows actually resolved (decision 6) rather than any single bank's. Off by
    default, and it changes nothing about what is fetched — a ``--combine`` run spends exactly
    the same requests as the same run without it.

    ``batch_size`` (docs/adr-ofx-batch-splitting.md) splits each account's transactions into
    review-sized files of at most that many, dated by what each batch actually covers. It composes
    with ``combine`` (decision 8): a batched account contributes its several statements to the one
    combined file instead of one. Like ``combine`` it is packaging only and spends no extra
    request — the batching happens after every account has been fetched.
    """
    require_ordered_window(date_from, date_to)
    require_valid_batch_size(batch_size)
    today = today or date.today()
    banks = _selected_banks(config, only)

    # One-shot: convert any window-keyed entries left by an older version into month chunks.
    # Discarding them instead would make the upgrade itself cost a full re-fetch.
    converted = migrate_legacy_entries(config.cache_dir)
    if converted:
        progress(f"converted {converted} cache entr(ies) to the month-chunk layout")

    written: list[Path] = []
    failures: list[BankFailure] = []
    warnings: list[FetchWarning] = []
    warn = warnings.append
    combined_pending: list[tuple[Account, list[Txn], date, date]] = []
    combined_windows: list[ResolvedWindow] = []
    for position, key in enumerate(banks, start=1):
        progress(f"[{position}/{len(banks)}] {key}: checking consent...")
        # The client issues the requests and does not know which bank it is working for; label
        # them here so the log can show a sibling connection being refused on its first call.
        if runlog is not None:
            runlog.set_context(bank=key, domain=banks[key].rate_limit_domain)
        # Named before the try so a failure that happens before the window is known still reports
        # one (as None) rather than tripping over an unbound name.
        window: ResolvedWindow | None = None
        try:
            session = load_session(config.state_dir, key)
            if session is None:
                raise BankError(f"not linked yet - run: gnucash-ofx link {key}")
            if days_until_expiry(session, today=today) < 0:
                raise BankError(f"consent has expired - run: gnucash-ofx link {key}")
            _warn_consent_expiry(key, session, today=today, warn=warn)
            window = resolve_window(
                date_from=date_from,
                date_to=date_to,
                coverage=load_coverage(config.state_dir, key),
                acctids=_stored_acctids(session.accounts),
                today=today,
            )
            if combine:
                # Counted as soon as it is resolved, not after fetch_bank returns: the combined
                # filename must follow the shape of the run - which banks are configured and
                # linked - not which ones happened to succeed, the same rule per-account
                # filenames already follow ("Filenames follow the shape of the connection,
                # not which accounts succeeded", AGENTS.md). Resolving this before any request
                # for this bank is what keeps it identical to what dry_run_enablebanking(
                # combine=True) predicts, which never calls fetch_bank at all.
                combined_windows.append(window)
            _report_window(key, window, today=today, progress=progress, warn=warn)
            # Now that the window is known, put it on this bank's requests. The run record cannot
            # carry it when it was implied - each bank resolves its own - so this is the only place
            # a later reader can learn what was actually asked for.
            if runlog is not None:
                runlog.set_context(
                    bank=key,
                    domain=banks[key].rate_limit_domain,
                    window=(window.date_from.isoformat(), window.date_to.isoformat()),
                )
            bank_written, bank_failures = fetch_bank(
                client,
                bank_key=key,
                session_id=session.session_id,
                date_from=window.date_from,
                date_to=window.date_to,
                output_dir=config.output_dir,
                accounts=session.by_uid,
                bank_id=banks[key].options.get("bankid"),
                progress=progress,
                cache_dir=config.cache_dir,
                cache_ttl=cache_ttl,
                refresh=refresh,
                today=today,
                warn=warn,
                state_dir=config.state_dir,
                collect=combined_pending if combine else None,
                # The floor applies to explicit windows too: the *claim* uses --from exactly as
                # given, and the wire widens below it "exactly as it does for a resolved window"
                # (ADR decision 1) - which respects the horizon where Alior and Erste 400. An
                # explicit --from already at the horizon (decision 6's repair command) must keep
                # working; one below it gets the request as typed, margin absorbed.
                request_floor=today - DEFAULT_LOOKBACK,
                batch_size=batch_size,
            )
            written.extend(bank_written)
            failures.extend(bank_failures)
        except (BankError, StateError) as exc:
            # A corrupt state/<bank>.json (StateError) is reported the same way as "not linked"
            # or "consent expired": it is this bank's problem alone, and the other banks in the
            # run must not lose their files over it.
            failures.append(
                BankFailure(bank_key=key, operation="consent", scope="bank", message=str(exc))
            )
            progress(f"{key}: FAILED ({exc})")
        except httpx.HTTPError as exc:
            # Reaching here means the bank fell over before any account was attempted (its
            # GET /sessions), so the whole bank is lost rather than one of its accounts.
            status, api_code, payload = _response_details(exc)
            failures.append(
                BankFailure(
                    bank_key=key,
                    operation=_operation_of(exc),
                    scope="bank",
                    message="the bank could not be reached",
                    window=(window.date_from, window.date_to) if window else None,
                    status_code=status,
                    api_code=api_code,
                    payload=payload,
                )
            )
            progress(f"{key}: FAILED ({status or type(exc).__name__})")

    if combine and combined_windows:
        combined_from = min(w.date_from for w in combined_windows)
        combined_to = max(w.date_to for w in combined_windows)
        path = write_combined_ofx(combined_pending, combined_from, combined_to, config.output_dir)
        if path is not None:
            written.append(path)
    return FetchReport(written=written, failures=failures, warnings=warnings)


# --------------------------------------------------------------------------- dry run


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One OFX file a fetch of this window would write, predicted from local state alone.

    ``account`` is already redacted, so this can be printed without further care.
    """

    bank_key: str
    account: str
    currency: str
    path: Path


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """What ``fetch --dry-run`` resolved: the files it predicts, and what it could not predict.

    ``problems`` reuses :class:`BankFailure` so the CLI renders it exactly as it renders a real
    fetch's failures — these are the same conditions (not linked, consent lapsed, unreadable
    state file), found before they cost anything rather than after.

    ``warnings`` is the same channel a real fetch uses, and carrying it here is what makes the
    implied window safe to ship: the answer to "what is a bare ``fetch`` about to ask for, and does
    it leave a gap?" is available without spending a request to find out.
    """

    planned: list[PlannedFile]
    problems: list[BankFailure]
    warnings: list[FetchWarning] = field(default_factory=list)


def _planned_files(
    bank_key: str,
    session: SessionState,
    bank: BankConfig,
    *,
    date_from: date,
    date_to: date,
    output_dir: Path,
    progress: ProgressFn,
    predict_paths: bool,
) -> tuple[list[PlannedFile], list[BankFailure]]:
    """Predict one bank's output files from ``state/<bank>.json``, without contacting anything.

    ``predict_paths=False`` (a batched dry run, docs/adr-ofx-batch-splitting.md decision 6) keeps
    every check — refusals, unknown currencies, unformable filenames are all problems a batched
    fetch hits identically — but announces no path and returns no ``PlannedFile``. Batch
    boundaries depend on transaction counts and dates a dry run structurally never sees, so any
    concrete path printed here would be confidently wrong; suppressing it at the source is what
    keeps stderr from naming files the CLI then says were never predicted.

    The account list, its ``ACCTID`` resolution and the disambiguation grouping all come from the
    same functions the real fetch uses (:func:`_known_acctid`, :func:`_disambiguators`,
    :func:`ofx_filename`) — that shared path is what makes the prediction worth trusting, and it
    is why the grouping counts every account in the session rather than only the ones that would
    produce a file.

    An account whose currency was never captured (a session linked under the v1 state schema) is
    reported as unpredictable: a real fetch learns the currency from ``/balances``, and the
    currency is part of the filename, so there is nothing honest to print for it here.

    **One such account suppresses the whole bank's prediction**, rather than being quietly dropped
    from the grouping. Its currency is unknown, so it might be a same-currency sibling of an
    account that *can* be named — and joining the group does not merely add a suffix, it can flip
    the entire group from IBAN tails to digests (:func:`account_disambiguators`). Predicting the
    others anyway would silently hand back names the fetch will not use, which is worse than
    predicting nothing: the reason to run this is to be able to trust the answer.
    """
    problems: list[BankFailure] = []
    candidates: list[tuple[str, Account, UncarryableAccountType | None]] = []
    unknown_currency = 0
    total = len(session.accounts)
    iban_shared = _iban_shared_within(session.accounts)
    for position, stored in enumerate(session.accounts, start=1):
        label = f"  [{position}/{total}] {bank_key}"
        acct_id = _known_acctid(stored, stored.uid, {}, iban_shared=iban_shared)
        redacted = _redact_account(acct_id)
        # The same function the fetch refuses with, so the prediction cannot disagree with the
        # run (docs/adr-accttype-mapping.md decision 4) - checked ahead of the currency check
        # below, matching the fetch, which refuses before /balances could teach it a currency.
        try:
            accttype_for(stored.cash_account_type)
            refusal = None
        except UncarryableAccountType as exc:
            refusal = exc
        if not stored.currency:
            if refusal is not None:
                # Refused *and* currencyless: report the refusal, exactly as the fetch would.
                # This account does not suppress the bank - the fetch never learns its currency
                # (the refusal comes first), so unlike an ordinary unknown-currency account it
                # cannot join a group mid-run and change its siblings' filenames.
                problems.append(
                    BankFailure(
                        bank_key=bank_key,
                        operation="account-type",
                        scope="account",
                        message=str(refusal),
                        account=redacted,
                    )
                )
                progress(
                    f"{label} {redacted}: no file - the fetch would refuse this account "
                    f"(account type {refusal.cash_account_type})"
                )
                continue
            problems.append(
                BankFailure(
                    bank_key=bank_key,
                    operation="account",
                    scope="account",
                    message=(
                        "no currency was captured at link time, so no filename can be predicted "
                        f"for them - run: gnucash-ofx link {bank_key}"
                    ),
                    account=redacted,
                )
            )
            progress(f"{label} {redacted}: currency unknown, cannot predict a path")
            unknown_currency += 1
            continue
        # A refused account with a known currency stays a candidate: it still shapes its
        # siblings' disambiguators below, exactly as an unfetched sibling does in the real
        # fetch - only its own file is not predicted.
        candidates.append(
            (
                label,
                Account(
                    bank_key=bank_key,
                    account_id=acct_id,
                    currency=stored.currency,
                    bank_id=bank.options.get("bankid"),
                    cash_account_type=stored.cash_account_type,
                ),
                refusal,
            )
        )

    if unknown_currency and candidates:
        # invariant: one unknown-currency account suppresses the whole bank's prediction, not
        # just its own. AGENTS.md#invariants
        # Some accounts could be named, and naming them would be a guess: the unknown ones may
        # share their currency, which changes the suffix of every account in that group.
        problems.append(
            BankFailure(
                bank_key=bank_key,
                operation="account",
                scope="bank",
                message=(
                    f"the other {len(candidates)} account(s) are not predicted either - an "
                    "account of unknown currency can change its siblings' filenames, so nothing "
                    "is predicted here rather than something that may be wrong"
                ),
            )
        )
        progress(
            f"{bank_key}: not predicting its other {len(candidates)} account(s) - an unknown "
            "currency could change their filenames"
        )
        return [], problems

    disambiguators = _disambiguators([account for _, account, _ in candidates], siblings=[])
    planned: list[PlannedFile] = []
    for label, account, refusal in candidates:
        redacted = _redact_account(account.account_id)
        if refusal is not None:
            problems.append(
                BankFailure(
                    bank_key=bank_key,
                    operation="account-type",
                    scope="account",
                    message=str(refusal),
                    account=redacted,
                )
            )
            progress(
                f"{label} {redacted}: no file - the fetch would refuse this account "
                f"(account type {refusal.cash_account_type})"
            )
            continue
        try:
            name = ofx_filename(
                account,
                date_from,
                date_to,
                disambiguator=disambiguators.get(account.account_id),
            )
        except ValueError as exc:
            # A currency (or key) that cannot be a path component would fail the same way mid-run.
            # Finding it here is the whole point, so report it rather than raising a traceback.
            problems.append(
                BankFailure(
                    bank_key=bank_key,
                    operation="account",
                    scope="account",
                    message=f"no output filename could be formed: {exc}",
                    account=redacted,
                )
            )
            progress(f"{label} {redacted}: cannot form a filename ({exc})")
            continue
        if not predict_paths:
            # The filename was still formed above - an unformable one fails a batched fetch the
            # same way - but which dates it would carry is unknowable here, so it is not printed
            # and not planned.
            continue
        progress(f"{label} {redacted} ({account.currency}): {output_dir / name}")
        planned.append(
            PlannedFile(
                bank_key=bank_key,
                account=redacted,
                currency=account.currency,
                path=output_dir / name,
            )
        )
    return planned, problems


# invariant: no client, no cache_dir - a dry run must not be able to spend or populate them.
# AGENTS.md#invariants
def dry_run_enablebanking(
    config: AppConfig,
    *,
    date_from: date | None,
    date_to: date | None,
    only: str | None = None,
    today: date | None = None,
    progress: ProgressFn = _noop_progress,
    combine: bool = False,
    batch_size: int | None = None,
) -> DryRunReport:
    """Report what a fetch of this window would write, spending nothing to find out.

    **No client parameter, by design.** Adding a bank to ``config.toml`` or sanity-checking a date
    range should not cost a request against a daily allowance, and should not fail late — after
    the mistake has already been paid for. Everything here comes from ``config.toml`` and
    ``state/<bank>.json``, both local: the account list, the consent expiry, and the output
    filenames. There is no client to call and no cache directory is touched, so a dry run can
    neither consume nor populate the fetch cache, and it is not recorded in the run log — that log
    is an account of what was spent, and this spends nothing.

    What it cannot know is which accounts have transactions in the window; an account with none
    writes no file. So the paths are what *would* be written, one per account, which is also why
    the disambiguation grouping is right: filenames follow the shape of the connection.

    Credentials are still required, a reversed window is still refused, and ``only`` naming an
    unconfigured bank still raises :class:`RunError` — a dry run is the right place to discover
    that the run cannot happen. A bank whose consent has lapsed is a per-bank ``problem``, not an
    abort: the config resolved.

    ``combine`` (docs/adr-combined-ofx-file.md decision 6, open question "Can ``--dry-run``
    predict the combined file's path?") predicts the one combined path instead of one per
    account. That path needs no account enumeration — it carries no bank key or currency — so
    it is not subject to the unknown-currency suppression :func:`_planned_files` applies below,
    and it is still an upper bound for the same reason a per-account prediction is one: an
    account with no transactions in the window contributes nothing, and if every account is
    empty the combined file is not written at all.
    """
    require_ordered_window(date_from, date_to)
    require_valid_batch_size(batch_size)
    today = today or date.today()
    banks = _selected_banks(config, only)
    require_credentials()

    planned: list[PlannedFile] = []
    problems: list[BankFailure] = []
    warnings: list[FetchWarning] = []
    warn = warnings.append
    combined_windows: list[ResolvedWindow] = []
    # docs/adr-ofx-batch-splitting.md decision 6, suppressed at the source rather than filtered
    # from the report afterwards: a `DryRunReport` carrying paths a batched fetch will never
    # write is a value object that lies, and _planned_files' own progress narration would name
    # those paths on stderr a few lines before the CLI says none were predicted. One local so
    # the per-bank gate and the combined gate below are visibly the same rule. Everything else
    # the dry run finds (a lapsed consent, a coverage gap, an unformable filename) survives,
    # because none of it depends on knowing the filenames.
    predict_paths = batch_size is None
    for position, key in enumerate(banks, start=1):
        progress(f"[{position}/{len(banks)}] {key}: reading local state...")
        try:
            session = load_session(config.state_dir, key)
            if session is None:
                raise BankError(f"not linked yet - run: gnucash-ofx link {key}")
            remaining = days_until_expiry(session, today=today)
            if remaining < 0:
                raise BankError(
                    f"consent has expired ({-remaining} days ago) - run: gnucash-ofx link {key}"
                )
        except (BankError, StateError) as exc:
            problems.append(
                BankFailure(bank_key=key, operation="consent", scope="bank", message=str(exc))
            )
            progress(f"{key}: WOULD FAIL ({exc})")
            continue

        progress(
            f"{key}: {len(session.accounts)} account(s) in session, "
            f"consent valid for {remaining} days"
        )
        _warn_consent_expiry(key, session, today=today, warn=warn)
        acctids = _stored_acctids(session.accounts)
        coverage = load_coverage(config.state_dir, key)
        window = resolve_window(
            date_from=date_from,
            date_to=date_to,
            coverage=coverage,
            acctids=acctids,
            today=today,
        )
        _report_window(key, window, today=today, progress=progress, warn=warn)
        # The gap check is the same function the fetch runs, on the same ledger. What it cannot do
        # is see the fetch-time identification hashes, so an account resolvable only from
        # `accounts_data` reads as unknown here and is simply not warned about - the same direction
        # the stale-identity warning is left out for.
        _warn_coverage_gaps(
            key,
            coverage,
            acctids,
            date_from=window.date_from,
            date_to=window.date_to,
            today=today,
            warn=warn,
            progress=progress,
        )
        if combine:
            combined_windows.append(window)
            continue
        bank_planned, bank_problems = _planned_files(
            key,
            session,
            banks[key],
            date_from=window.date_from,
            date_to=window.date_to,
            output_dir=config.output_dir,
            progress=progress,
            predict_paths=predict_paths,
        )
        planned.extend(bank_planned)
        problems.extend(bank_problems)

    if combine and combined_windows and predict_paths:
        # The combined path alone *is* derivable under --batch-size — the fetch names it from the
        # resolved windows, not from the batch-dated items — but decision 6 predicts no paths at
        # all for the whole run, deliberately: --batch-size is global, and one path predicted
        # while the CLI explains that none could be would be a worse contradiction than the one
        # this suppression exists to remove.
        combined_from = min(w.date_from for w in combined_windows)
        combined_to = max(w.date_to for w in combined_windows)
        path = config.output_dir / combined_filename(combined_from, combined_to)
        progress(f"combined: {path}")
        planned.append(PlannedFile(bank_key="", account="", currency="", path=path))

    return DryRunReport(planned=planned, problems=problems, warnings=warnings)


# --------------------------------------------------------------------------- status


@dataclass(frozen=True, slots=True)
class BankStatus:
    """One bank's answer to "is anything wrong here?", for a human and for an exit code."""

    bank_key: str
    lines: list[str]
    needs_attention: bool


def status_report(config: AppConfig, *, today: date | None = None) -> list[BankStatus]:
    """Per-bank consent and coverage status.

    ``needs_attention`` is what ``status --check`` turns into an exit code: not linked, consent
    expired or within :data:`CONSENT_WARNING_DAYS`, an unreadable state file, or a recorded gap
    still inside the recoverable horizon. Deliberately not "anything unusual" — a scheduled run
    that alerts on things the user cannot act on stops being read.
    """
    today = today or date.today()
    report: list[BankStatus] = []
    for key in _enablebanking_banks(config):
        try:
            session = load_session(config.state_dir, key)
        except StateError as exc:
            report.append(
                BankStatus(key, [f"{key}: state file is corrupted ({exc}) - re-link to fix"], True)
            )
            continue
        if session is None:
            report.append(
                BankStatus(key, [f"{key}: not linked - run: gnucash-ofx link {key}"], True)
            )
            continue
        remaining = days_until_expiry(session, today=today)
        if remaining < 0:
            lines = [f"{key}: consent EXPIRED ({-remaining} days ago) - re-link"]
            attention = True
        elif remaining <= CONSENT_WARNING_DAYS:
            lines = [
                f"{key}: linked, consent valid for {remaining} days - re-link soon, before a lapse "
                "costs history that cannot be backfilled"
            ]
            attention = True
        else:
            lines = [f"{key}: linked, consent valid for {remaining} days"]
            attention = False
        coverage_line, has_gap = _coverage_status(config, key, session, today=today)
        if coverage_line is not None:
            lines.append(coverage_line)
        # The fallback's honesty lives here, in the pull channel, never at fetch time: a session
        # linked before the v2 state schema has no stored account type, no fetch can capture one
        # (POST /sessions is the only source), and a per-fetch warning about a condition no fetch
        # can change is how warnings stop being read (docs/adr-accttype-mapping.md decision 3).
        # Not "needs attention": the files are correct and importable; re-linking merely verifies.
        untyped = sum(1 for a in session.accounts if not a.cash_account_type)
        if untyped:
            lines.append(
                f"  {untyped} of {len(session.accounts)} account(s) carry no stored account "
                "type; ACCTTYPE falls back to CHECKING - re-linking captures the bank's answer"
            )
        report.append(BankStatus(key, lines, attention or has_gap))
    return report


def _coverage_status(
    config: AppConfig, bank_key: str, session: SessionState, *, today: date
) -> tuple[str | None, bool]:
    """``(line, any gap inside the horizon)`` for a bank's coverage, or ``(None, False)``.

    Silent when there is no ledger. A bank nobody has fetched yet is not in trouble; it is new,
    and saying so on every line would bury the banks that are.

    The gap check looks only *inside* what has been recorded — from the horizon (or the first
    covered day, whichever is later) to the last covered day. The stretch between the last covered
    day and today is not a gap; it is the window the user is about to ask for.
    """
    coverage = load_coverage(config.state_dir, bank_key)
    if coverage.unreadable:
        return "  coverage record unreadable - it will be rebuilt on the next fetch", False
    acctids = _stored_acctids(session.accounts)
    # An entry with no recorded days is a ledger that has been hand-edited or half-written. It
    # carries no claim, so it is dropped here rather than defended against three lines later - the
    # version that kept it crashed on ``min()`` of an empty sequence when *every* entry was empty.
    entries = [
        entry
        for acctid in acctids
        if (entry := coverage.for_account(acctid)) is not None and entry.covered_through is not None
    ]
    if not entries:
        return None, False
    oldest_recoverable = today - RECOVERABLE_HORIZON
    with_gaps = 0
    for entry in entries:
        first, last = entry.covered_from, entry.covered_through
        if first is None or last is None:  # pragma: no cover - covered_through filtered above
            continue
        if entry.missing(max(first, oldest_recoverable), last):
            with_gaps += 1
    covered_through = min(
        entry.covered_through for entry in entries if entry.covered_through is not None
    )
    line = f"  fetched through {covered_through} ({len(entries)} of {len(acctids)} account(s))"
    if with_gaps:
        line += f"; {with_gaps} with a gap inside the last 90 days - run fetch --dry-run for detail"
    return line, bool(with_gaps)


def status_lines(config: AppConfig, *, today: date | None = None) -> list[str]:
    """One human-readable line per Enable Banking bank: consent status / expiry."""
    return [line for status in status_report(config, today=today) for line in status.lines]
