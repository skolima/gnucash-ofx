"""Enable Banking source: PSD2 client + transaction mapper.

Auth and call flow follow the official sample
(https://github.com/enablebanking/enablebanking-api-samples): a short-lived RS256 JWT signed
with the application's private key is sent as a Bearer token to ``https://api.enablebanking.com``.

The ``map_*`` functions are pure (no network) so they are unit-tested against fixtures; the
:class:`EnableBankingClient` wraps the REST endpoints and accepts an injected ``httpx.Client``
for offline testing.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

import httpx
import jwt as pyjwt

from gnucash_ofx.models import Txn, currency_or_none
from gnucash_ofx.state import LinkedAccount

API_ORIGIN = "https://api.enablebanking.com"
# A Polish domestic account number (NRB) is 26 digits; the IBAN is the same with a "PL" prefix.
_PL_NRB_DIGITS = 26
_JWT_TTL_SECONDS = 3600
# Enable Banking returns 429 (ASPSP_RATE_LIMIT_EXCEEDED) when an ASPSP is hit too fast; retry
# with exponential backoff, honouring a numeric Retry-After header when the server sends one.
_MAX_RETRIES = 5
_BASE_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0
# The one 429 that is not transient: a per-ASPSP daily cap, documented as recovering in ~6 hours.
# Retrying it cannot win and spends counted requests trying, so it is raised on the first response.
ASPSP_RATE_LIMIT_EXCEEDED = "ASPSP_RATE_LIMIT_EXCEEDED"
# Hard local bounds on what one account's fetch will follow, so a server that answers forever cannot
# hold the run forever (docs/adr-input-hardening.md decision 5). Policy picks with a measured floor
# and no measured ceiling: the largest whole-window response ever observed here is well under 1 MB,
# and provable pagination has never exceeded 2 pages, so both carry >25x headroom. Re-tune them
# against a runlog census, never against a guess -- and never by raising them to make a bank work
# without measuring first, because the point of them is to bound a server acting in bad faith.
_MAX_PAGES_PER_ACCOUNT = 100
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# Wise's machine references, each sent as a remittance element of its own. Both classes below are
# matched whole (see _split_reference), never as a substring.
#
# The id pattern is per prefix rather than one permissive charset, because the shape alone does not
# say what a token is: BALANCE-<digits> looks identical to CARD-<digits> but recurs across
# transactions (it identifies the balance, not the payment), and ACCRUAL_CHECKOUT-invoice-<digits>
# names something a person actually reads. Both must keep failing to match.
_DIGIT_ID = r"\d+"
_UUID_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"

# References Wise attaches to a transaction of its own. The id is lifted into Txn.reference and
# reaches OFX CHECKNUM when it fits there; BALANCE_CASHBACK's UUID never can, and is dropped rather
# than left sitting in the Description (see compose_check_number()).
_OWN_REFERENCES = (("TRANSFER", _DIGIT_ID), ("CARD", _DIGIT_ID), ("BALANCE_CASHBACK", _UUID_ID))
_OWN_REFERENCE = re.compile(
    "|".join(f"(?:{prefix}-({id_pattern}))" for prefix, id_pattern in _OWN_REFERENCES)
)

# The same references on a *fee* row, where the id belongs to the transaction being charged for.
# Recognised only in order to be removed: it is not this transaction's id, so it must never reach
# the Num column, and it is not text anybody reads, so it does not stay in the Description either.
# The prose beside it ("Wise Charges for: CARD-<id>") keeps the link to the payment.
_FOREIGN_REFERENCE = re.compile(rf"FEE-(?:TRANSFER|CARD|BALANCE)-(?:{_DIGIT_ID}|{_UUID_ID})")

# Per-source currency-conversion deal keys (docs/adr-currency-conversion-pairs.md decision 1), in
# the same spirit as _OWN_REFERENCES: a shape rule cannot tell a deal key from an ordinary
# reference, so each source gets an explicit rule. Namespaced by source so a Wise BALANCE-7 and an
# Alior Kantor deal 7 cannot collide. Extraction is read-only — unlike _split_reference, nothing is
# removed from the remittance.
_WISE_CONVERSION_CODE = "CONVERSION"
# Deliberately narrower than _OWN_REFERENCE: BALANCE-<digits> alone recurs on ordinary Wise
# transactions too (it names the balance, not the deal — see the machine-reference invariant in
# AGENTS.md), so only the CONVERSION transaction code turns it into a deal key. The FEE- variant is
# excluded by the fullmatch itself: "FEE-BALANCE-<n>" never matches "BALANCE-(\d+)" wholesale, so no
# extra flag is needed to keep the fee row unpaired.
_WISE_BALANCE_KEY = re.compile(r"BALANCE-(\d+)")
_KANTOR_DEAL = re.compile(r"Kantor Walutowy (\d+)")
# Revolut's conversion legs carry no deal reference in the remittance at all; the join is the
# entry_reference both legs share (docs/adr-revolut-exchange-pairing.md decision 1). That field
# is also this source's FITID (_transaction_id falls through to it — transaction_id is null on
# every measured row), so extraction here must stay read-only: one entry_reference names two
# transactions on this data by construction.
_REVOLUT_EXCHANGE_CODE = "EXCHANGE"


def _conversion_key(raw: dict[str, Any]) -> str | None:
    """A source-specific currency-conversion deal key, or None for an ordinary transaction."""
    code = raw.get("bank_transaction_code")
    code_word = str(code.get("code") or "").upper() if isinstance(code, dict) else ""
    if code_word == _WISE_CONVERSION_CODE:
        for part in _remittance_parts(raw):
            match = _WISE_BALANCE_KEY.fullmatch(part)
            if match:
                return f"wise:BALANCE-{match.group(1)}"
    if code_word == _REVOLUT_EXCHANGE_CODE:
        # A reference-less EXCHANGE row gets no revolut: key — an empty key would falsely group
        # every such row into one cluster for pair_conversions to consider. It falls through to
        # the remaining rules like any other row.
        reference = raw.get("entry_reference")
        if reference:
            return f"revolut:{reference}"
    match = _KANTOR_DEAL.search(_remittance(raw))
    if match:
        return f"kantor:{match.group(1)}"
    return None


# --------------------------------------------------------------------------- mapping (pure)


def _amount_str(raw: dict[str, Any]) -> str:
    return str(raw.get("transaction_amount", {}).get("amount", "0"))


def _is_credit(raw: dict[str, Any]) -> bool:
    """Direction comes from the indicator, never from the amount (which may be zero).

    Only ``CRDT`` is a credit; everything else (``DBIT`` and any spelling variant) is a debit,
    so the sign cannot be flipped by an unexpected value.
    """
    return str(raw.get("credit_debit_indicator") or "").upper() == "CRDT"


def finite_decimal(value: str, *, field: str) -> Decimal:
    """Parse ``value`` as a finite :class:`Decimal`, or raise :class:`ValueError`.

    ``Decimal`` accepts ``"NaN"`` and ``"Infinity"`` — neither is a money amount, and both survive
    far enough to do damage rather than failing where they arrive: ``NaN`` makes every comparison
    False, so ``build_statement``'s ``amount >= 0`` silently calls a credit a debit, and
    ``Infinity`` serializes into ``<TRNAMT>Infinity</TRNAMT>`` for GnuCash to choke on. Rejected
    rather than clamped or zeroed (docs/adr-input-hardening.md decision 3): an invented amount in a
    financial file is worse than a missing file, and the caller turns this into one account's
    failure while its siblings still get theirs.

    ``InvalidOperation`` is re-raised as ``ValueError`` so every bad-amount path out of the mapper
    is one type, whichever way the value was malformed.

    **The rejected value is never quoted**, only its length and character classes. This message
    reaches ``BankFailure.payload`` and the stderr failure summary a user is asked to paste into an
    issue, and ``InvalidOperation`` fires on merely *non-canonical* real money —
    ``"1 234,56"``, ``"1,234.56"``, ``"100 PLN"`` all raise while carrying a real balance
    (``AGENTS.md#data``: never include balances).
    """
    try:
        amount = Decimal(value)
    except ArithmeticError as exc:  # decimal.InvalidOperation is an ArithmeticError
        raise ValueError(f"{field} is not a number ({_value_shape(value)})") from exc
    if not amount.is_finite():
        # Described, not quoted - the same rule as the branch above, now with no exception.
        # `Decimal` accepts a NaN *payload* (`NaN0000123456789`), so echoing this would put
        # attacker-chosen digits into the stderr summary users are asked to paste. Knowing which
        # non-finite spelling arrived is not worth that.
        raise ValueError(f"{field} is not finite ({_value_shape(value)})")
    return amount


def _value_shape(value: str) -> str:
    """Describe a rejected value without quoting it: ``"11 chars, digits+punctuation"``."""
    classes = []
    if any(char.isdigit() for char in value):
        classes.append("digits")
    if any(char.isalpha() for char in value):
        classes.append("letters")
    if any(char.isspace() for char in value):
        classes.append("whitespace")
    if any(not char.isalnum() and not char.isspace() for char in value):
        classes.append("punctuation")
    return f"{len(value)} chars, {'+'.join(classes) or 'empty'}"


def _signed_amount(raw: dict[str, Any]) -> Decimal:
    """Enable Banking reports an unsigned amount; the indicator gives the sign."""
    amount = finite_decimal(_amount_str(raw), field="transaction_amount.amount")
    return amount if _is_credit(raw) else -amount


def _remittance_parts(raw: dict[str, Any]) -> list[str]:
    """The remittance elements, stripped, with the empty ones dropped.

    Enable Banking reports ``remittance_information`` as a list, and the split between its
    elements is real structure — Wise puts its transfer reference in an element of its own. Keep
    the list intact here; the callers decide what to do with the pieces.
    """
    info = raw.get("remittance_information") or []
    parts = info if isinstance(info, list) else [info]
    return [text for text in (str(part).strip() for part in parts if part) if text]


def _remittance(raw: dict[str, Any]) -> str:
    """The whole remittance as one string, machine reference included.

    This is the FITID hash basis (see :func:`_transaction_id`), which is why it keeps every
    element: a source with no transaction ids of its own must not see its FITIDs churn because we
    changed our mind about which parts of the remittance are prose.
    """
    return " ".join(_remittance_parts(raw))


def _split_reference(parts: list[str]) -> tuple[str | None, list[str]]:
    """Split machine references out of the remittance: ``(bare reference, prose parts)``.

    Wise tags transfers, card payments and cashback with a reference of its own, each sent as a
    separate remittance element. Such a reference is opaque to a reader and near-useless to the
    matcher — it recurs on at most the payment and its fee, so it can never earn a Bayesian match —
    while in ``NAME`` it takes the leading characters that the remittance-first ordering exists to
    protect. Lifted out here, the transaction's *own* id reaches the OFX ``CHECKNUM``
    (see :func:`gnucash_ofx.ofxout.compose_check_number`), where GnuCash shows it in the Num column.

    A **foreign** reference — the parent's id on a fee row — is removed and goes nowhere. It cannot
    be this row's ``CHECKNUM``, because it identifies a different transaction, and it does not stay
    in the text, because it is not text. The prose beside it (``Wise Charges for: CARD-<id>``)
    already carries the link to the payment being charged for, so no token is lost.

    Only an element that is *entirely* a reference is taken, and only the id itself: the prefix is a
    constant carrying no information, and it would not fit ``CHECKNUM`` anyway. Matching a substring
    would be wrong rather than merely loose — it would strip the id out of that surrounding prose,
    which is the one place it reads as part of a sentence.
    """
    reference: str | None = None
    prose: list[str] = []
    for part in parts:
        own = _OWN_REFERENCE.fullmatch(part)
        if own and reference is None:
            # Exactly one group can have matched: the alternation is one branch per prefix.
            reference = next(group for group in own.groups() if group is not None)
            continue
        if _FOREIGN_REFERENCE.fullmatch(part):
            # invariant: a fee row's foreign reference is dropped, never relocated to its own
            # CHECKNUM. AGENTS.md#invariants
            continue
        prose.append(part)
    return reference, prose


def _counterparty(raw: dict[str, Any], *, is_credit: bool) -> str | None:
    # Incoming credit -> the payer is the debtor; outgoing debit -> the payee is the creditor.
    # invariant: the side the direction names, never "whichever side is populated" - see
    # _counterparty_iban; the same fallback here puts our own name in NAME. AGENTS.md#invariants
    party = raw.get("debtor") if is_credit else raw.get("creditor")
    if isinstance(party, dict):
        name = party.get("name")
        return str(name) if name else None
    return None


def _iban_checksum_ok(value: str) -> bool:
    """True if ``value`` satisfies the ISO 13616 mod-97 IBAN check."""
    if len(value) < 5:
        return False
    # Move the country code + check digits to the end, then map letters to numbers (A=10..Z=35).
    rearranged = value[4:] + value[:4]
    digits: list[str] = []
    for char in rearranged:
        if char.isdigit():
            digits.append(char)
        elif char.isalpha():
            digits.append(str(ord(char) - ord("A") + 10))
        else:
            return False
    return int("".join(digits)) % 97 == 1


def normalize_account_number(value: str) -> str:
    """Return an account number in IBAN form when it can be proven to be one.

    ASPSPs report counterparty accounts inconsistently: some send a proper IBAN, others the
    domestic number — for Poland a 26-digit NRB with no country prefix. GnuCash matches on exact
    tokens, so a counterparty reported as a bare NRB would not match the same account's
    IBAN-formatted ``ACCTID`` elsewhere, defeating own-account transfer detection.

    A ``PL`` prefix is added only when the result passes the IBAN checksum, so non-Polish or
    malformed values are returned as-is (merely stripped of whitespace and upper-cased).
    """
    cleaned = "".join(value.split()).upper()
    if cleaned.isdigit() and len(cleaned) == _PL_NRB_DIGITS:
        candidate = f"PL{cleaned}"
        if _iban_checksum_ok(candidate):
            return candidate
    return cleaned


def account_identifier(acct: Any) -> str | None:
    """Best account number from an Enable Banking account object, normalized to IBAN form.

    Prefer ``iban``; fall back to ``other.identification``. Some ASPSPs (e.g. Bank Millennium,
    and Alior for counterparties) leave ``iban`` null and put the account number under
    ``other.identification``, sometimes as a domestic NRB rather than an IBAN.
    """
    if not isinstance(acct, dict):
        return None
    iban = acct.get("iban")
    if iban:
        return normalize_account_number(str(iban))
    other = acct.get("other")
    if isinstance(other, dict) and other.get("identification"):
        return normalize_account_number(str(other["identification"]))
    return None


def _identification_scheme(acct: Any) -> str | None:
    """``account_id.other.scheme_name`` — what the account number claims to be (BBAN, CPAN, ...).

    Only meaningful for the ``other.identification`` form; an ``iban`` needs no scheme.
    """
    if not isinstance(acct, dict) or acct.get("iban"):
        return None
    other = acct.get("other")
    if isinstance(other, dict) and other.get("scheme_name"):
        return str(other["scheme_name"])
    return None


def identification_hashes(session: dict[str, Any]) -> dict[str, str]:
    """uid → identification_hash from a session response's ``accounts_data`` array."""
    hashes: dict[str, str] = {}
    for entry in session.get("accounts_data", []) or []:
        if not isinstance(entry, dict):
            continue
        uid, value = entry.get("uid"), entry.get("identification_hash")
        if uid and value:
            hashes[str(uid)] = str(value)
    return hashes


def _str_or_none(value: Any) -> str | None:
    return str(value) if value else None


def linked_accounts(session: dict[str, Any]) -> tuple[LinkedAccount, ...]:
    """Capture everything ``POST /sessions`` reports per account.

    This response is the only place the full account resource appears (``GET /accounts/{uid}``
    is 404 in Restricted Mode, ``GET /sessions/{id}`` returns bare UID strings), so a field not
    read here is lost until the next re-link. Every field is optional — ASPSPs populate very
    different subsets — and anything unrecognised survives in ``SessionState.raw``.
    """
    # identification_hash arrives on the account object in some API versions and in a sibling
    # accounts_data array in others; take it from wherever it is present.
    hashes = identification_hashes(session)
    accounts: list[LinkedAccount] = []
    for entry in session.get("accounts", []) or []:
        if not isinstance(entry, dict) or not entry.get("uid"):
            continue
        uid = str(entry["uid"])
        acct_id = entry.get("account_id")
        servicer = entry.get("account_servicer")
        bic = servicer.get("bic_fi") if isinstance(servicer, dict) else None
        accounts.append(
            LinkedAccount(
                uid=uid,
                iban=account_identifier(acct_id),
                bic=_str_or_none(bic),
                currency=currency_or_none(entry.get("currency")),
                identification_hash=_str_or_none(entry.get("identification_hash"))
                or hashes.get(uid),
                identification_scheme=_identification_scheme(acct_id),
                name=_str_or_none(entry.get("name")),
                product=_str_or_none(entry.get("product")),
                usage=_str_or_none(entry.get("usage")),
                cash_account_type=_str_or_none(entry.get("cash_account_type")),
            )
        )
    return tuple(accounts)


# Not observed in any session response; dropped defensively so that a future API version which
# starts returning a credential does not land it in a file we keep for the ~180-day consent life.
# Matched exactly, so the legitimate ``access`` (consent scope) block is untouched.
_CREDENTIAL_KEYS = frozenset(
    {"access_token", "refresh_token", "id_token", "token", "client_secret", "secret", "password"}
)


def scrub_session(session: dict[str, Any]) -> dict[str, Any]:
    """The session response with any credential-shaped top-level key removed."""
    return {k: v for k, v in session.items() if k not in _CREDENTIAL_KEYS}


def _counterparty_iban(raw: dict[str, Any], *, is_credit: bool) -> str | None:
    # Incoming credit -> the sender is the debtor; outgoing debit -> the recipient is the creditor.
    # invariant: the side the direction names, never "whichever side is populated" - N26 fills
    # both sides, and a populated-side fallback puts our own IBAN in MEMO. AGENTS.md#invariants
    key = "debtor_account" if is_credit else "creditor_account"
    return account_identifier(raw.get(key))


def _booking_date(raw: dict[str, Any]) -> date:
    value = raw.get("booking_date") or raw.get("value_date") or raw.get("transaction_date")
    if not value:
        raise ValueError("transaction has no booking/value/transaction date")
    return date.fromisoformat(str(value))


def _transaction_id(raw: dict[str, Any]) -> str:
    """A stable FITID. Prefer the bank's ids; otherwise a deterministic hash of stable fields."""
    for key in ("transaction_id", "entry_reference"):
        value = raw.get(key)
        if value:
            return str(value)
    basis = "|".join(
        [
            str(raw.get("booking_date") or raw.get("value_date") or ""),
            _amount_str(raw),
            str(raw.get("credit_debit_indicator") or ""),
            _remittance(raw),
        ]
    )
    return "gen-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def map_transaction(raw: dict[str, Any], currency: str) -> Txn:
    """Map one Enable Banking transaction object to a normalized :class:`Txn`.

    ``currency`` (the account's currency) is authoritative — one OFX statement is
    single-currency. A transaction reporting a different currency means it was grouped under
    the wrong account, so we raise rather than silently mix currencies.
    """
    raw_currency = raw.get("transaction_amount", {}).get("currency")
    if raw_currency and str(raw_currency) != currency:
        raise ValueError(
            f"transaction currency {raw_currency!r} does not match account currency {currency!r}"
        )
    is_credit = _is_credit(raw)
    # The machine reference leaves the remittance here, so NAME/MEMO only ever see prose.
    reference, prose = _split_reference(_remittance_parts(raw))
    return Txn(
        id=_transaction_id(raw),
        date=_booking_date(raw),
        amount=_signed_amount(raw),
        currency=currency,
        payee=_counterparty(raw, is_credit=is_credit),
        memo=" ".join(prose) or None,
        counterparty_iban=_counterparty_iban(raw, is_credit=is_credit),
        reference=reference,
        conversion_key=_conversion_key(raw),
    )


def map_transactions(
    raw_txns: list[dict[str, Any]],
    *,
    currency: str,
    booked_only: bool = True,
) -> list[Txn]:
    """Map a list of raw transactions, dropping pending entries unless ``booked_only`` is False."""
    result: list[Txn] = []
    for raw in raw_txns:
        status = str(raw.get("status") or "BOOK").upper()
        if booked_only and not status.startswith("BOOK"):
            continue
        result.append(map_transaction(raw, currency))
    return result


# --------------------------------------------------------------------------- HTTP client


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429, never more than ``_MAX_BACKOFF_SECONDS``.

    Honour a numeric ``Retry-After`` header when present; otherwise use capped exponential
    backoff (1s, 2s, 4s, ... up to 60s). HTTP-date ``Retry-After`` values are not parsed —
    they fall through to backoff, which is still correct (just possibly sooner).

    The header is **clamped to the ladder's own ceiling, and non-finite values are refused**
    (docs/adr-input-hardening.md decision 2). Unclamped, a server saying ``Retry-After: 999999999``
    parked the run in ``time.sleep`` for ~31 years, and ``inf`` passed a bare ``>= 0`` check and
    raised ``OverflowError`` from inside it. Reusing ``_MAX_BACKOFF_SECONDS`` rather than picking a
    second constant keeps one answer to "how long can one retry ever wait". The cost of clamping a
    genuinely long wait is bounded and visible: at most ``_MAX_RETRIES`` attempts arrive early and
    fail, and each is recorded in the run log — while the daily cap, the one limit measured to need
    hours, is never retried at all.
    """
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            seconds = float(str(retry_after))
        except ValueError:
            seconds = -1.0
        if math.isfinite(seconds) and seconds >= 0.0:
            return min(seconds, _MAX_BACKOFF_SECONDS)
    backoff = _BASE_BACKOFF_SECONDS * (2**attempt)
    return backoff if backoff < _MAX_BACKOFF_SECONDS else _MAX_BACKOFF_SECONDS


class PageBudget:
    """A shared count of transaction pages one account-window may still fetch.

    An object rather than an ``int`` parameter because one account-window can be fetched as
    **several** request spans — a gap in the middle of the cache splits it — and the bound has to
    span them. Passing the constant per call gave each span its own fresh allowance, making the real
    bound ``cap x spans``; counting transactions instead of pages (the first attempt at this) was
    worse, because a high-volume account would exhaust a *page* budget on its first span and then
    fail the next one spuriously. This counts requests, which is what the cap is about.
    """

    __slots__ = ("remaining",)

    def __init__(self, remaining: int = _MAX_PAGES_PER_ACCOUNT) -> None:
        self.remaining = remaining

    def spend(self) -> None:
        """Charge one page, or raise if this account-window has had its allowance."""
        if self.remaining <= 0:
            raise ResponseLimitExceeded(
                f"transactions paginated past {_MAX_PAGES_PER_ACCOUNT} page(s) for one account "
                "and still offered another; refusing to keep following it",
                operation="transactions",
            )
        self.remaining -= 1


def _operation_of_path(path: str) -> str:
    """Which endpoint a path belongs to, for reporting: never invent an explanation.

    Mirrors :func:`gnucash_ofx.run._operation_of`, which derives the same label from an
    ``httpx.HTTPError``'s request. Hard-coding one would tell a user that ``transactions``
    misbehaved when the oversized body came back from ``/balances``.
    """
    if path.endswith("/balances"):
        return "balances"
    if path.endswith("/transactions"):
        return "transactions"
    if path.startswith("/sessions"):
        return "session"
    return "request"


class ResponseLimitExceeded(Exception):
    """An ASPSP response went past a bound this client enforces locally.

    Its own type, not a reused :class:`ValueError`, for the reason
    :class:`gnucash_ofx.run.BankError` is one: the *scope* of the failure is a property of the error
    rather than of where a ``try`` happens to sit. Raised, never swallowed into a short answer,
    because a truncated statement would advance the coverage ledger over transactions that were
    never written (docs/adr-input-hardening.md decision 5, rejected option F).

    Scope is the caller's to decide, and it differs by endpoint: an account's runaway response is
    account-scoped, because nothing was *refused* — every one of those requests returned 200, and a
    sibling account may well be fine, unlike the per-ASPSP `429` that makes a sibling's request
    certain to fail. A session response has no account to blame, so ``fetch_bank`` turns it into a
    :class:`gnucash_ofx.run.BankError`; without that it was a bare ``Exception`` escaping every
    handler, which cost the *other* banks their files and replaced the stdout file list with a
    traceback.

    ``operation`` names the endpoint, so the failure report does not have to guess.
    """

    def __init__(self, message: str, *, operation: str = "request") -> None:
        super().__init__(message)
        self.operation = operation


def api_error_code(response: httpx.Response) -> str | None:
    """Enable Banking's machine-readable error token, or None.

    The code lives in ``error`` (``ASPSP_ERROR``, ``ASPSP_RATE_LIMIT_EXCEEDED``). ``message`` is
    prose for a human and ``code`` merely repeats the HTTP status, so neither is matched on — see
    the error-envelope note in ``docs/enable-banking.md``.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error")
    return code if isinstance(code, str) else None


def api_error_name(response: httpx.Response) -> str | None:
    """The platform exception wrapped in ``detail.error_name``, or None.

    ``detail`` is null on plain ASPSP errors and an object on platform-level ones —
    ``RateLimitException`` on the verified 429, ``HttpException`` on N26's bank-down 400 of
    2026-08-19 (see the error-envelope note in ``docs/enable-banking.md``). It is the only field
    that separates those shapes from any other error carrying the same ``error`` token, which is
    why the run log persists it alongside :func:`api_error_code`.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if not isinstance(detail, dict):
        return None
    name = detail.get("error_name")
    return name if isinstance(name, str) else None


class RequestObserver(Protocol):
    """Notified once per HTTP response, retries included.

    A protocol rather than an import of :mod:`gnucash_ofx.runlog`, so the client stays a transport
    that knows nothing about how a run is recorded. The keywords match ``RunLog.record_request``,
    which is therefore usable as an observer directly.
    """

    def __call__(
        self,
        *,
        method: str,
        path: str,
        status: int,
        attempt: int,
        elapsed: float,
        api_code: str | None = None,
        error_name: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...  # pragma: no cover - structural type only


class EnableBankingClient:
    """Thin client over the Enable Banking REST API."""

    def __init__(
        self,
        application_id: str,
        private_key: bytes,
        *,
        origin: str = API_ORIGIN,
        http: httpx.Client | None = None,
        max_retries: int = _MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        psu_headers: dict[str, str] | None = None,
        observer: RequestObserver | None = None,
    ) -> None:
        self._application_id = application_id
        self._private_key = private_key
        # The audience must match the API host, so it follows an overridden origin.
        self._audience = httpx.URL(origin).host
        self._http = http or httpx.Client(base_url=origin, timeout=30.0)
        self._owns_http = http is None
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self._max_retries = max_retries
        self._sleep = sleep
        # PSU headers (e.g. Psu-Ip-Address, Psu-User-Agent) mark data fetches as "online",
        # which ASPSPs rate-limit far less strictly than background fetches. Sent only on the
        # data-retrieval endpoints (balances, transactions), never on auth/discovery calls.
        self._psu_headers = psu_headers or {}
        self._observer = observer

    def close(self) -> None:
        """Close the underlying HTTP client if we created it (injected clients are left alone)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> EnableBankingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _jwt(self) -> str:
        issued_at = int(time.time())
        return pyjwt.encode(
            {
                "iss": "enablebanking.com",
                "aud": self._audience,
                "iat": issued_at,
                "exp": issued_at + _JWT_TTL_SECONDS,
            },
            self._private_key,
            algorithm="RS256",
            headers={"kid": self._application_id},
        )

    def _headers(self, *, psu: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._jwt()}"}
        if psu:
            headers.update(self._psu_headers)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        psu: bool = False,
    ) -> dict[str, Any]:
        """Send a request, retrying on 429 with backoff until ``max_retries`` is exhausted.

        Except for the one 429 that backoff cannot help. ``ASPSP_RATE_LIMIT_EXCEEDED`` is a
        **daily** cap whose documented recovery is ~6 hours; the ladder tops out at 31 seconds, so
        all five retries are certain to fail — and each is another counted request sent at an ASPSP
        that has just said it has had enough. Generic and platform-level 429s keep the backoff,
        because those are genuinely transient.
        """
        for attempt in range(self._max_retries + 1):
            started = time.monotonic()
            response = self._http.request(
                method, path, params=params, json=json, headers=self._headers(psu=psu)
            )
            # Bounded here, ahead of every parse: `_observe` reads an error body to extract its
            # api_code, the 429 branch reads it again, and `raise_for_status()` below means an error
            # response would never reach a check placed after it. Observed first all the same, with
            # the body left unread - the request was spent either way, and the log is the account of
            # what was spent.
            oversized = len(response.content) > _MAX_RESPONSE_BYTES
            self._observe(
                method,
                path,
                response,
                attempt,
                time.monotonic() - started,
                parse_error_body=not oversized,
            )
            if oversized:
                # The path is deliberately *not* in this message. It reaches BankFailure.payload and
                # the stderr summary users are steered to paste, and it would carry the full account
                # uid - or, from get_session, a live session id - re-opening through a new channel
                # exactly the leak #40 masked out of the run log. `operation` says which endpoint
                # it was, which is all a reader needs.
                raise ResponseLimitExceeded(
                    f"the {_operation_of_path(path)} response was {len(response.content)} bytes, "
                    f"over the {_MAX_RESPONSE_BYTES}-byte cap",
                    operation=_operation_of_path(path),
                )
            if (
                response.status_code == 429
                and api_error_code(response) == ASPSP_RATE_LIMIT_EXCEEDED
            ):
                response.raise_for_status()
            if response.status_code == 429 and attempt < self._max_retries:
                self._sleep(_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
        raise AssertionError("unreachable: retry loop always returns or raises")  # pragma: no cover

    def _observe(
        self,
        method: str,
        path: str,
        response: httpx.Response,
        attempt: int,
        elapsed: float,
        *,
        parse_error_body: bool = True,
    ) -> None:
        """Report one response to the observer, if any. Never raises.

        The error body is parsed only for a failure: on a 200 it is the payload we came for, and
        re-reading it to look for a field that cannot be there is pure waste.

        ``parse_error_body=False`` records the request while reading no body at all, which is how an
        over-cap response still reaches the log: the request was spent, so the log has to show it,
        but its body is precisely what must not be parsed.
        """
        if self._observer is None:
            return
        try:
            self._observer(
                method=method,
                path=path,
                status=response.status_code,
                attempt=attempt,
                elapsed=elapsed,
                api_code=(
                    None
                    if response.is_success or not parse_error_body
                    else api_error_code(response)
                ),
                error_name=(
                    None
                    if response.is_success or not parse_error_body
                    else api_error_name(response)
                ),
                headers=response.headers,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never cost a fetch
            return

    def _get(
        self, path: str, params: dict[str, Any] | None = None, *, psu: bool = False
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params, psu=psu)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body)

    def get_application(self) -> dict[str, Any]:
        return self._get("/application")

    def list_aspsps(self, country: str) -> list[dict[str, Any]]:
        data = self._get("/aspsps", params={"country": country})
        aspsps: list[dict[str, Any]] = data.get("aspsps", [])
        return aspsps

    def start_authorization(
        self,
        *,
        aspsp_name: str,
        country: str,
        redirect_url: str,
        valid_until: str,
        psu_type: str = "personal",
        state: str | None = None,
    ) -> str:
        """Begin PSU authorization; return the bank SCA URL to open in a browser."""
        body: dict[str, Any] = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": country},
            "redirect_url": redirect_url,
            "psu_type": psu_type,
            "state": state if state is not None else secrets.token_urlsafe(16),
        }
        data = self._post("/auth", body)
        return str(data["url"])

    def create_session(self, code: str) -> dict[str, Any]:
        """Exchange the redirect ``code`` for a session (with the list of authorized accounts)."""
        return self._post("/sessions", {"code": code})

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/sessions/{session_id}")

    def get_balances(self, account_uid: str) -> dict[str, Any]:
        # Data-retrieval endpoint: send PSU headers so the ASPSP treats it as an online fetch.
        return self._get(f"/accounts/{account_uid}/balances", psu=True)

    def iter_transactions(
        self,
        account_uid: str,
        *,
        date_from: str,
        date_to: str | None = None,
        budget: PageBudget | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw transaction objects, following ``continuation_key`` pagination.

        Bounded by ``budget``: the loop's exit condition is the server's own ``continuation_key``,
        so an ASPSP that always returns a fresh one held the run here forever, spending a counted
        request per turn. Exhausting the budget raises :class:`ResponseLimitExceeded` rather than
        stopping quietly — a short answer that looked complete would advance the coverage ledger
        over transactions nobody ever wrote.

        ``budget`` is a shared :class:`PageBudget` rather than a per-call number because one
        account-window can be fetched as several request spans, and the caller reaches this function
        once *per span* — so the budget has to outlive the call. Omitted, each call gets its own
        allowance, which suits a single-span caller and the tests.
        """
        budget = budget if budget is not None else PageBudget()
        params: dict[str, Any] = {"date_from": date_from}
        if date_to is not None:
            params["date_to"] = date_to
        continuation_key: str | None = None
        while True:
            # Charged before the request, so the budget bounds requests *sent* rather than pages
            # accepted: at most `remaining` more go out, whatever the server offers after them.
            budget.spend()
            if continuation_key:
                params["continuation_key"] = continuation_key
            # Data-retrieval endpoint: PSU headers mark this as an online fetch (higher limits).
            data = self._get(f"/accounts/{account_uid}/transactions", params=params, psu=True)
            yield from data.get("transactions", [])
            continuation_key = data.get("continuation_key")
            if not continuation_key:
                return
