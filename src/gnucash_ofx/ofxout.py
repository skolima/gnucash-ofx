"""Convert normalized transactions into OFX files for GnuCash import.

Builds an :class:`ofxstatement.statement.Statement` and serializes it with
:class:`ofxstatement.ofx.OfxWriter`. One file is written per account/currency. The statement
always carries a period (``DTSTART``/``DTEND``) and a ``LEDGERBAL`` so the output validates as
proper OFX and so GnuCash can offer balance reconciliation.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from xml.etree.ElementTree import Element, TreeBuilder

from ofxstatement.ofx import OfxWriter
from ofxstatement.statement import BankAccount, Statement, StatementLine

from gnucash_ofx.models import Account, Conversion, Txn
from gnucash_ofx.naming import safe_component

# The OFX spec types BANKID as A-9; longer values fail strict validation.
_MAX_BANKID_LEN = 9

# The combined file's stem (docs/adr-combined-ofx-file.md decision 6). Not reserved: a bank
# literally keyed "combined" would produce a per-account name sharing this prefix, but the two
# shapes never actually collide (a per-account name always has a currency segment a combined one
# never has), so there is nothing here for a guard to prevent - see ofx_filename().
_COMBINED_STEM = "combined"

# libofx reads NAME into `char name[OFX_TRANSACTION_NAME_LENGTH]`, which is `96 + 1`, via
# `std::strncpy(dest, src, sizeof(dest))`. strncpy does not NUL-terminate when the source is at
# least as long as the buffer, so a 97-character NAME leaves the struct member unterminated and
# GnuCash reads past it. 96 is therefore a hard ceiling, not the spec's advisory A-32.
_MAX_NAME_LEN = 96

# MEMO lands in `char memo[OFX_MEMO2_LENGTH]` = `390 + 1` through the same unterminated-strncpy
# path. Real remittance text (A-255) plus an IBAN stays well inside this; the cap exists so the
# buffer overrun is impossible rather than merely improbable.
_MAX_MEMO_LEN = 390

# libofx reads CHECKNUM into `char check_number[OFX_CHECK_NUMBER_LENGTH]`, which is `12 + 1` — the
# smallest of its buffers — through the same non-NUL-terminating `strncpy` as NAME and MEMO.
# Measured with libofx 0.10.5: a 19-character value comes back as 13 characters, and 13 fills the
# buffer with no terminator. `ofxtools` types CHECKNUM as a strict `String(12)` (not the lenient
# `NagString` it uses for ACCTID and NAME), so an over-long value is a hard parse failure for other
# OFX consumers rather than a warning.
_MAX_CHECKNUM_LEN = 12

# A reference is only emitted when it is a plain token: it has to survive the same SGML path as
# everything else, and the Num column is no place for prose.
_CHECKNUM_ALLOWED = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z")

# Components of NAME are joined with this, matching GnuCash's own AqBanking importer.
_NAME_SEPARATOR = "; "

# A filename disambiguator is the ACCTID's last four characters, but only when they are plain
# alphanumerics: safe_component() also permits '-' and '_', and either would blur the boundary
# between the account part of the name and the date range that follows it.
_ACCTID_TAIL = re.compile(r"[A-Za-z0-9]{4}\Z")


def _to_datetime(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


# Characters that stand in for a space rather than vanishing, so a tab or a line separator between
# two words does not run them together; every other control is dropped outright. The C0 whitespace
# set, plus NEL/U+2028/U+2029 — those three are *not* ASCII, so they would otherwise be dropped by
# the `< 128` filter, and they are exactly the separators decision 1 established that ASPSPs really
# do put in strings (`json.dumps` does not escape them). Same argument, so the same treatment.
_WHITESPACE_CONTROLS = frozenset("\t\n\v\f\r\x85  ")

# Letters that NFKD does not decompose into "base + combining mark".
_TRANSLITERATIONS = {
    "Ł": "L",
    "ł": "l",
    "Ø": "O",
    "ø": "o",
    "Æ": "AE",
    "æ": "ae",
    "Œ": "OE",
    "œ": "oe",
    "ß": "ss",
    "Đ": "D",
    "đ": "d",
    "Ð": "D",
    "ð": "d",
    "Þ": "Th",
    "þ": "th",
}


# invariant: folding to ASCII is deliberate - GnuCash for Windows drops non-ASCII on import.
# AGENTS.md#invariants
def to_ascii(text: str) -> str:
    """Fold text to ASCII so GnuCash does not silently drop characters on import.

    GnuCash parses OFX 1.x via libofx, which uses the OpenSP SGML parser. OpenSP's SGML
    declaration marks non-ASCII code points as ``UNUSED``, so it drops them — and escaping them
    as numeric character references (``&#220;``) does not help, because the character itself is
    not permitted in the document character set. ``"Kämpf OÜ"`` imports as ``"Kmpf O"``.

    Folding keeps names readable and, just as important, *stable*: GnuCash's Bayesian matcher
    learns whatever tokens it consistently sees, so a deterministic ASCII form matches reliably.

    **C0 control characters and DEL do not survive** (docs/adr-input-hardening.md decision 4a). The
    ``< 128`` filter passed them, and one ``\\x02`` in a memo aborted the writer's minidom step with
    an uncaught ``ExpatError`` — the whole run, not the account. Stripped rather than rejected:
    zero occurrences were measured across every string in the local cache, so stripping breaks no
    real data, and failing a run over one byte the file could never carry fails proportionality.
    A separator becomes a space instead of vanishing (``_WHITESPACE_CONTROLS``), so
    ``"ACME\\tInvoice"`` stays two tokens for the matcher rather than collapsing into one. That set
    is explicit and not ``str.isspace()``, which also calls U+001C–001F whitespace; it covers
    NEL/U+2028/U+2029 too, since those are separators the ``< 128`` filter would otherwise drop
    silently. A source CRLF therefore folds to *two* spaces — deterministic, and GnuCash tokenizes
    on whitespace runs.
    """
    folded = "".join(_TRANSLITERATIONS.get(char, char) for char in text)
    decomposed = unicodedata.normalize("NFKD", folded)
    kept: list[str] = []
    for char in decomposed:
        code = ord(char)
        if char in _WHITESPACE_CONTROLS:
            kept.append(" ")
            continue
        if code >= 128 or unicodedata.combining(char):
            continue
        if code < 0x20 or code == 0x7F:
            continue
        kept.append(char)
    return "".join(kept)


# The fields that bypass to_ascii entirely and are written to the file raw. Printable ASCII with no
# controls, deliberately **not** a tight alphanumeric whitelist: `entry_reference` is the majority
# FITID source and legitimately carries `- . _ / |` across three of the six banks measured, so
# `[A-Za-z0-9-]` would reject real FITIDs on day one (adr-input-hardening.md rejected option D).
_PRINTABLE_ASCII = re.compile(r"\A[\x20-\x7e]+\Z")
# CURDEF is different: ISO 4217 is a real closed contract and 100% of measured values conform.
_ISO_CURRENCY = re.compile(r"\A[A-Z]{3}\Z")


def validate_raw_fields(
    account_id: str, currency: str, txn_ids: Iterable[str], bank_id: str | None = None
) -> None:
    """Raise ``ValueError`` if a field written raw cannot go into the file as-is.

    ``ACCTID``, ``FITID``, ``CURDEF`` and ``BANKID`` never pass through :func:`to_ascii`, so nothing
    else stands between the incoming bytes and the OFX. ``BANKID`` comes from *config* rather than
    from an ASPSP — but :func:`bank_id_for` only strips whitespace, upper-cases and truncates, so a
    control character in a hand-written ``bankid`` reaches the file exactly as an ASPSP's would, and
    a typo is a likelier source than a hostile server rather than a rarer one.

    A control character in any of them aborts the writer's XML step, and a non-ASCII one is silently
    dropped by GnuCash for Windows — which for an *identifier* is
    worse than for prose: a mangled `FITID` breaks de-duplication on re-import, and a mangled
    `ACCTID` orphans the account.

    **Validated, never repaired** (decision 4b): mutating a `FITID` would make the same transaction
    re-import as new, and mutating an `ACCTID` would orphan every transaction already imported under
    it, so a value the bank sent wrong is the bank's to fix. The caller turns this into one
    account's failure while its siblings still get their files.

    Called at the mapping step rather than from :func:`build_statement`, deliberately: the write
    loop has no per-account guard and coverage has already advanced by the time it runs, so a
    rejection there would abort the run *and* leave the ledger claiming a day whose file was never
    written.
    """
    if not _ISO_CURRENCY.match(currency):
        # Truncated: this is an ASPSP-controlled string of unbounded length and it reaches the
        # stderr summary. `repr` neutralises any control character in it; the slice bounds the size.
        raise ValueError(f"CURDEF is not an ISO 4217 code: {currency[:8]!r}")
    if not _PRINTABLE_ASCII.match(account_id):
        raise ValueError(f"ACCTID is not printable ASCII ({_field_shape(account_id)})")
    if bank_id is not None and not _PRINTABLE_ASCII.match(bank_id):
        raise ValueError(f"BANKID is not printable ASCII ({_field_shape(bank_id)})")
    for txn_id in txn_ids:
        if not _PRINTABLE_ASCII.match(txn_id):
            raise ValueError(f"FITID is not printable ASCII ({_field_shape(txn_id)})")


def _field_shape(value: str) -> str:
    """Describe a rejected identifier without quoting it.

    An ``ACCTID`` *is* the account number, and a ``FITID`` can be derived from one, so neither
    belongs in a message that reaches the stderr summary users are asked to paste
    (``AGENTS.md#data``). The offending code points are named, since those are what a bug report
    needs and none of them is anybody's account number.
    """
    if not value:
        # A measured case, not a theoretical one: libofx hands an empty ACCTID back to its caller
        # as ">", silently corrupting the field GnuCash derives `online_id` from.
        return "empty"
    offenders = sorted({ord(char) for char in value if not _PRINTABLE_ASCII.match(char)})
    shown = ", ".join(f"U+{code:04X}" for code in offenders[:4])
    return f"{len(value)} chars, offending code point(s): {shown}"


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, preferring a whitespace boundary.

    A mid-word cut would feed GnuCash's Bayesian matcher a partial token that matches nothing,
    so prefer the last word that fits. A single component longer than the budget has no boundary
    to cut at and is truncated outright.
    """
    if len(text) <= limit:
        return text
    # Already at a boundary: the character that falls off is itself the separating space.
    if text[limit] == " ":
        return text[:limit].rstrip(" ;")
    head, separator, _tail = text[:limit].rpartition(" ")
    return (head if separator and head else text[:limit]).rstrip(" ;")


def compose_conversion(conversion: Conversion | None) -> str | None:
    """The canonical currency-conversion annotation, or ``None`` when there is no pairing.

    e.g. ``"12000.00 EUR -> 51720.00 PLN @ 4.310000"``, identical on both legs of the pair
    (docs/adr-currency-conversion-pairs.md decision 4) — it states the counter-amount and rate,
    which is exactly what the user needs at hand when GnuCash's "Assign exchange rate" dialog
    asks for a number. ASCII by construction (decimals, ISO currency codes, ``->``): do not
    reach for ``→``, which :func:`to_ascii` would only fold away.
    """
    if conversion is None:
        return None
    return (
        f"{conversion.from_amount:.2f} {conversion.from_currency} -> "
        f"{conversion.to_amount:.2f} {conversion.to_currency} @ {conversion.rate}"
    )


def compose_name(
    payee: str | None, memo: str | None, conversion: Conversion | None = None
) -> str | None:
    """Compose the OFX ``NAME`` — which GnuCash shows as the register Description.

    GnuCash's single-line register shows only the Description, and its OFX importer fills that
    from ``NAME`` (falling back to ``MEMO``). Sending the bare counterparty name therefore hides
    the remittance information, which is usually the part that identifies the transaction, and
    leaves repeat payments to the same counterparty indistinguishable in the register.

    This mirrors ``gnc_ab_description_to_gnc()`` in GnuCash's own AqBanking importer, which
    composes the Description for the same kind of SEPA data: join the components with ``"; "``,
    **remittance first**, dropping any component already contained in what has been accumulated.
    That containment check is what stops ``"Sent money to ACME; ACME"``; like AqBanking's
    ``utf8_strstr()`` it is case-sensitive, so differently-cased repeats are kept.

    A currency-conversion annotation (:func:`compose_conversion`), when present, leads ahead of
    even the remittance — a deliberate exception to remittance-first
    (docs/adr-currency-conversion-pairs.md decision 4): for a conversion the FX detail *is* the
    substantive remittance, and it is needed at the exact moment "Assign exchange rate" asks for
    a number. What it displaces is boilerplate repeated verbatim on every deal, which the 96-cap
    truncation below eats from the tail, leaving the annotation intact.

    Folding to ASCII happens here, before the containment check, so all components are compared
    in the same alphabet. Returns ``None`` when nothing is left, which leaves ``NAME`` unset and
    lets GnuCash fall back to ``MEMO`` as it did before.

    The counterparty IBAN is deliberately absent: it stays in ``MEMO`` (see
    :func:`_memo_with_iban`), which GnuCash tokenizes for matching just as it does the
    description, so spending scarce ``NAME`` budget on it would buy nothing.
    """
    composed = ""
    for component in (compose_conversion(conversion), memo, payee):  # annotation, then remittance
        if not component:
            continue
        folded = to_ascii(component).strip()
        if not folded or folded in composed:
            continue
        composed = f"{composed}{_NAME_SEPARATOR}{folded}" if composed else folded
    return _truncate(composed, _MAX_NAME_LEN) or None


def compose_memo(
    memo: str | None, counterparty_iban: str | None, conversion: Conversion | None = None
) -> str | None:
    """Build the OFX ``MEMO``: the conversion annotation, the remittance, then the IBAN.

    GnuCash's import matcher tokenizes the transaction description and memo to route
    transactions to accounts (own-account transfer detection, recurring-payee classification).
    libofx does not expose the ``BANKACCTTO`` aggregate, so the IBAN must be a token in the
    memo to be usable as a matching signal. The bare IBAN is a single, stable token.

    The remittance stays here in full even though :func:`compose_name` also puts it in ``NAME``.
    ``MEMO`` is the roomy copy — ``NAME`` is capped at 96 characters — and the duplication is
    free: GnuCash's ``tokenize_string()`` adds only tokens it has not already seen, so a repeated
    word does not gain extra weight in the matcher.

    A currency-conversion annotation, when present, leads ahead of the remittance — see
    :func:`compose_name` for why. The IBAN stays last with its reserved budget, unchanged: it is
    reserved out of the budget rather than appended and hoped for, because a naive cut at
    :data:`_MAX_MEMO_LEN` would otherwise drop precisely the token that account routing depends
    on; instead the annotation-plus-remittance text yields the space.
    """
    folded = to_ascii(memo).strip() if memo else ""
    annotation = compose_conversion(conversion)
    if annotation:
        folded = f"{annotation}{_NAME_SEPARATOR}{folded}" if folded else annotation
    iban = to_ascii(counterparty_iban).strip() if counterparty_iban else ""
    if not iban:
        return _truncate(folded, _MAX_MEMO_LEN) or None
    budget = _MAX_MEMO_LEN - len(iban) - 1  # -1 for the separating space
    if budget <= 0:  # pathological IBAN; keep it whole and drop the remittance
        return _truncate(iban, _MAX_MEMO_LEN)
    head = _truncate(folded, budget)
    return f"{head} {iban}" if head else iban


def compose_check_number(reference: str | None) -> str | None:
    """Return the source's machine reference as an OFX ``CHECKNUM``/``REFNUM`` value, or ``None``.

    GnuCash's ``process_bank_transaction()`` passes ``check_number`` to ``gnc_set_num_action()``,
    so this lands in the register's Num column (or the split's Action field, per the book option).
    That keeps a per-transaction id visible — which is what distinguishes otherwise-identical
    repeat payments to one counterparty — without spending ``NAME`` budget on it or feeding the
    matcher a token that can never recur.

    ``REFNUM`` is the semantically correct field for a reference that is not a check number, but it
    cannot be used on its own: GnuCash tests ``data->reference_number_valid`` and then assigns
    ``data->check_number`` in that branch (``gnc-ofx-import.cpp``), so a ``REFNUM``-only
    transaction gets an empty Num. Both are written — the same "correct field plus the one that
    works" split as :func:`compose_memo` and ``BANKACCTTO``.

    An over-long or non-token reference is **dropped, not truncated**. Truncation is right for
    prose (:func:`_truncate`) and wrong for an identifier: a cut id still looks like an id, is no
    longer unique, and would defeat the only reason for emitting it.
    """
    if not reference:
        return None
    # invariant: a reference carrying a control character is dropped, decided on its *raw* form -
    # to_ascii deletes controls, so `R\x027` would fold to the valid-looking `R7`, which is exactly
    # the cut id the length rule below exists to refuse. AGENTS.md#invariants
    # Only controls, not folding in general: `REF-Ä12` -> `REF-A12` is a deliberate, information-
    # preserving transliteration and must keep working.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in reference):
        return None
    token = to_ascii(reference).strip()
    # invariant: an over-long CHECKNUM is dropped, not truncated - a cut id is no longer unique.
    # AGENTS.md#invariants
    if len(token) > _MAX_CHECKNUM_LEN or not _CHECKNUM_ALLOWED.match(token):
        return None
    return token


# Enable Banking's documented cash_account_type vocabulary is six values — CACC, CARD, CASH,
# LOAN, OTHR, SVGS — not the full ISO 20022 list (docs/adr-accttype-mapping.md §2). CACC and SVGS
# are the only ones OFX's bank message set can honestly carry; nothing maps to MONEYMRKT or
# CREDITLINE because no Enable Banking value honestly is either.
_ACCTTYPE_FOR_CASH_ACCOUNT_TYPE = {"CACC": "CHECKING", "SVGS": "SAVINGS"}

# Documented types the bank message set cannot carry. CARD in particular is not a field change:
# a card statement belongs in CREDITCARDMSGSRSV1 (whose CCACCTFROM has no ACCTTYPE element), and
# ofxstatement's OfxWriter has no such path — that is a future writer subclass, not a mapping.
_UNCARRYABLE_CASH_ACCOUNT_TYPES = {"CARD", "CASH", "LOAN"}


class UncarryableAccountType(ValueError):
    """The bank's account type names an account the bank message set cannot honestly carry.

    Raised by :func:`accttype_for` and converted by ``run.py`` into an account-scoped failure —
    the account's statement is withheld while its siblings still ship
    (docs/adr-accttype-mapping.md decision 2). No guessed mapping is offered: a wrong ``ACCTTYPE``
    is the silent, permanent version of exactly this error.
    """

    def __init__(self, cash_account_type: str) -> None:
        self.cash_account_type = cash_account_type
        if cash_account_type in _UNCARRYABLE_CASH_ACCOUNT_TYPES:
            detail = "OFX's bank message set cannot honestly carry it" + (
                " - a card statement needs the CREDITCARDMSGSRSV1 writer, which does not "
                "exist yet; unlink the account or wait for that path"
                if cash_account_type == "CARD"
                else "; unlink the account rather than mislabel it"
            )
        else:
            detail = (
                "not in Enable Banking's documented vocabulary (CACC, CARD, CASH, LOAN, OTHR, "
                "SVGS) - refusing rather than guessing; if the value is real, it needs a mapping "
                "in accttype_for()"
            )
        super().__init__(f"account type {cash_account_type}: {detail}")


def accttype_for(cash_account_type: str | None) -> str:
    """Map the bank's ``cash_account_type`` onto OFX ``ACCTTYPE``, or refuse.

    The single place the mapping decision lives (docs/adr-accttype-mapping.md decision 1): the
    fetch loop and ``--dry-run`` both call this, so the dry-run prediction and the real run can
    never disagree about which accounts are writable. Total over Enable Banking's documented
    vocabulary plus absence: ``CACC`` → ``CHECKING``, ``SVGS`` → ``SAVINGS``, and every documented
    type the bank message set cannot carry — or any undocumented value, which means the
    six-value-vocabulary premise is broken — raises :exc:`UncarryableAccountType`.
    """
    if cash_account_type is None or cash_account_type == "OTHR":
        # The explicit fallback, chosen here rather than inherited from ofxstatement's default:
        # absence means a session linked before the v2 state schema captured the field (only a
        # re-link can fill it), and OTHR is the bank explicitly answering "not otherwise
        # specified" — the same information content as absence, never a refusal
        # (docs/adr-accttype-mapping.md decision 1, option H).
        return "CHECKING"
    mapped = _ACCTTYPE_FOR_CASH_ACCOUNT_TYPE.get(cash_account_type)
    if mapped is None:
        raise UncarryableAccountType(cash_account_type)
    return mapped


def bank_id_for(bank_key: str, override: str | None = None) -> str:
    """Return an OFX-legal ``BANKID``, preferring an explicit configured value.

    ``override`` is the bank's ``bankid`` option — normally its BIC. BICs are 8 or 11 characters
    but OFX limits ``BANKID`` to 9, so an 11-character BIC is reduced to its 8-character
    primary-office form (dropping the branch suffix), which identifies the same institution.

    Without an override the config key is truncated. Either way the result is deterministic and
    stable across runs, which is what matters: GnuCash derives an account's ``online_id`` from
    ``BANKID``+``ACCTID``, so changing this value orphans already-imported accounts. Uniqueness is
    carried by ``ACCTID`` (the IBAN), so two banks sharing a ``BANKID`` — e.g. two connections to
    the same institution — still yield distinct accounts.
    """
    if override:
        bic = "".join(override.split()).upper()
        if len(bic) == 11:  # institution + country + location + branch -> drop the branch
            bic = bic[:8]
        if bic:
            return bic[:_MAX_BANKID_LEN]
    return bank_key[:_MAX_BANKID_LEN]


def _bankid_from_iban(iban: str) -> str:
    """Derive a non-empty ``BANKID`` for the ``BANKACCTTO`` aggregate.

    OFX requires ``BANKID`` in a bank-account aggregate, so it cannot be blank. For Polish
    IBANs the 4-digit national bank code (positions 5-8) is meaningful for auditing; otherwise
    fall back to the ISO country code. GnuCash ignores ``BANKACCTTO`` entirely, so this only
    needs to be present, stable, and valid OFX.
    """
    iban = iban.strip()
    if iban[:2].upper() == "PL" and len(iban) >= 8 and iban[4:8].isdigit():
        return iban[4:8]
    return iban[:2].upper() or "NA"


def build_statement(
    account: Account,
    txns: Iterable[Txn],
    period_start: date,
    period_end: date,
) -> Statement:
    """Build an ofxstatement ``Statement`` from normalized transactions.

    Raises :exc:`UncarryableAccountType` for an account whose type the bank message set cannot
    carry — callers are expected to have refused such an account already (``run.py`` does, before
    spending any request on it), so a raise here is a missed check upstream, not a user error.
    """
    statement = Statement(
        bank_id=bank_id_for(account.bank_key, account.bank_id),
        account_id=account.account_id,
        currency=account.currency,
        account_type=accttype_for(account.cash_account_type),
    )
    total = Decimal("0")
    for txn in txns:
        line = StatementLine(
            id=txn.id,
            date=_to_datetime(txn.date),
            memo=compose_memo(txn.memo, txn.counterparty_iban, txn.conversion),
            amount=txn.amount,
        )
        # invariant: NAME repeats MEMO's remittance on purpose - GnuCash's tokenizer already
        # dedupes, so the duplication is free. AGENTS.md#invariants
        # NAME carries remittance + counterparty name, not the bare payee: it is what GnuCash
        # shows as the register Description. MEMO above keeps the roomier copy.
        line.payee = compose_name(txn.payee, txn.memo, txn.conversion)
        line.trntype = "CREDIT" if txn.amount >= 0 else "DEBIT"
        # CHECKNUM is the field GnuCash actually reads into the Num column; REFNUM is the
        # semantically correct one but cannot stand alone. See compose_check_number().
        line.check_no = line.refnum = compose_check_number(txn.reference)
        # invariant: the counterparty number reaches BANKACCTTO raw, so it is gated like the other
        # raw-passthrough fields - but omitted, never failed. AGENTS.md#invariants
        if txn.counterparty_iban and _PRINTABLE_ASCII.match(txn.counterparty_iban):
            # invariant: libofx does not parse BANKACCTTO - the memo copy is what routes accounts.
            # AGENTS.md#invariants
            # Also emit the standard BANKACCTTO aggregate for manual auditing / other tools.
            # GnuCash's libofx ignores it, which is why the IBAN is repeated in the memo above.
            line.bank_account_to = BankAccount(
                _bankid_from_iban(txn.counterparty_iban), txn.counterparty_iban
            )
        statement.lines.append(line)
        total += txn.amount

    statement.start_date = _to_datetime(period_start)
    statement.end_date = _to_datetime(period_end)

    if account.end_balance is not None:
        statement.end_balance = account.end_balance
        # Back-compute the opening balance so LEDGERBAL stays consistent with the lines.
        statement.start_balance = (
            account.start_balance
            if account.start_balance is not None
            else account.end_balance - total
        )
    else:
        statement.start_balance = (
            account.start_balance if account.start_balance is not None else Decimal("0")
        )
        statement.end_balance = statement.start_balance + total

    return statement


def statement_to_ofx(statement: Statement) -> str:
    """Serialize a statement to an OFX document string."""
    return OfxWriter(statement).toxml(pretty=True)


class _SuppressedTreeBuilder(TreeBuilder):
    """A ``TreeBuilder`` that swallows one tag's start/end pair, passing everything else through.

    ``OfxWriter.buildBankTransactionList`` opens and closes ``BANKMSGSRSV1`` around a single
    statement's body in one method, with no seam to split them
    (docs/adr-combined-ofx-file.md §1). Assigning an instance of this to ``self.tb`` for the
    duration of that call is what lets :class:`_CombinedOfxWriter` open the wrapper once for N
    statements instead of once per statement.
    """

    def __init__(self, real: TreeBuilder, suppressed_tag: str) -> None:
        super().__init__()
        self._real = real
        self._tag = suppressed_tag

    # Both return None for the suppressed tag rather than an Element, breaking the base class's
    # signature on purpose: OfxWriter never reads tb.start()/tb.end()'s return value (buildText()
    # and friends call them for effect only), so the mismatch is real but harmless in practice.
    def start(self, tag: str, attrs: dict[str, str]) -> Element[str]:
        if tag == self._tag:
            return None  # type: ignore[return-value]
        return self._real.start(tag, attrs)

    def end(self, tag: str) -> Element[str]:
        if tag == self._tag:
            return None  # type: ignore[return-value]
        return self._real.end(tag)

    def data(self, text: str) -> None:
        self._real.data(text)


class _CombinedOfxWriter(OfxWriter):
    """Serializes several statements into one document: one ``<BANKMSGSRSV1>``, N
    ``<STMTTRNRS>`` (docs/adr-combined-ofx-file.md decision 3, route A).

    Overrides ``buildTransactionList`` rather than ``buildBankTransactionList``, because the
    latter welds the message-set wrapper to a single statement's body with nothing to override
    in between. ``self.statement`` is re-read by ``buildBankTransactionList`` on every call
    rather than captured once, which is what makes driving it round a loop cheap — and also why
    this depends on both staying true of whatever ``ofxstatement`` version is installed
    (``pyproject.toml`` pins an upper bound accordingly).

    invariant: this project only ever emits bank-message-set (``BANKMSGSRSV1``) statements —
    ``accttype_for()`` maps every account onto ``CHECKING`` or ``SAVINGS``, both of which live in
    that one message set, and refuses any type that would need another (``ofxstatement``'s
    ``OfxWriter`` has no ``CREDITCARDMSGSRSV1`` path to emit one anyway) — so one message-set
    block is always correct here. A second message-set type (a future card-statement writer)
    would need a second block, grouped by type; there is deliberately no such grouping machinery
    yet, because the refusal keeps there being nothing to group. AGENTS.md#invariants
    """

    def __init__(self, statements: Sequence[Statement]) -> None:
        super().__init__(statements[0])
        self._statements = statements

    def buildTransactionList(self) -> None:
        real_tb = self.tb
        real_tb.start("BANKMSGSRSV1", {})
        self.tb = _SuppressedTreeBuilder(real_tb, "BANKMSGSRSV1")
        try:
            for statement in self._statements:
                self.statement = statement
                self.buildBankTransactionList()
        finally:
            self.tb = real_tb
        real_tb.end("BANKMSGSRSV1")


def combine_statements(statements: Sequence[Statement]) -> str:
    """Serialize several statements into one OFX document.

    invariant: packaging several statements into one file must never merge them into one
    statement. Each stays exactly what ``build_statement()`` already made it — one account in one
    currency, with its own ``CURDEF``, ``BANKACCTFROM``, ``BANKTRANLIST`` and ``LEDGERBAL`` inside
    its own ``STMTTRNRS`` — and this function only changes how many statements share a document,
    never what is inside one. AGENTS.md#invariants

    Every statement given here is written; dropping an empty one is the caller's job
    (:func:`write_combined_ofx` does it, matching :func:`write_account_ofx`'s behaviour for a
    single empty account). Calling this with no statements is a caller error, not an empty file.
    """
    if not statements:
        raise ValueError("combine_statements() requires at least one statement")
    return _CombinedOfxWriter(statements).toxml(pretty=True)


def combined_filename(period_start: date, period_end: date) -> str:
    """Return the combined output filename, e.g. ``combined_2026_08_01-2026_08_09.ofx``.

    Derived from the window alone (docs/adr-combined-ofx-file.md decision 6): a combined file
    spans banks and currencies, so neither of the two components that make a per-account name
    informative (``<bank_key>_<currency>``) applies here. Fixed-width dates keep lexicographic
    order chronological, matching :func:`ofx_filename`.
    """
    return f"{_COMBINED_STEM}_{period_start:%Y_%m_%d}-{period_end:%Y_%m_%d}.ofx"


def _acctid_digest(account_id: str) -> str:
    """An opaque but stable disambiguator for an ``ACCTID`` with no usable tail."""
    return "eb-" + hashlib.sha1(account_id.encode("utf-8")).hexdigest()[:8]


def account_disambiguators(account_ids: Sequence[str]) -> list[str]:
    """Return one filename disambiguator per ``ACCTID``, positionally aligned with the input.

    Derived from the account identifier rather than from the account's position in the fetch, so
    a given account keeps the same filename between runs even when a sibling account has no
    transactions that period. ``ACCTID`` stability is already an invariant, so this inherits it.

    Normally the last four characters of the identifier — short, and recognisable as the tail of
    an IBAN. Where that is not a plain alphanumeric run, or where two accounts in the group share
    a tail, every member falls back to a digest so the group stays collision-free as a whole.
    """
    matches = [_ACCTID_TAIL.search(account_id) for account_id in account_ids]
    tails = [match.group() for match in matches if match is not None]
    if len(tails) == len(account_ids) and len(set(tails)) == len(tails):
        return tails
    return [_acctid_digest(account_id) for account_id in account_ids]


def ofx_filename(
    account: Account,
    period_start: date,
    period_end: date,
    *,
    disambiguator: str | None = None,
) -> str:
    """Return the output filename, e.g. ``alior_PLN_2026_05_01-2026_05_31.ofx``.

    The account prefix comes first so every file for one account sorts together — importing into
    GnuCash is per-account, so that is the unit of work. The dates are fixed-width, which makes
    lexicographic order chronological within a group, and they spell out the period actually
    covered: re-fetching a different window writes a new file instead of silently overwriting one
    whose name only recorded the end month.

    When a bank has multiple accounts in the same currency, pass ``disambiguator`` (from
    :func:`account_disambiguators`) to produce ``millennium_PLN_5387_2026_05_01-2026_05_31.ofx``.
    GnuCash matches statements via ``BANKID``+``ACCTID`` inside the OFX, not the filename, so
    renaming is invisible to import.
    """
    bank_key = safe_component(account.bank_key, "bank_key")
    currency = safe_component(account.currency, "currency")
    stem = f"{bank_key}_{currency}"
    if disambiguator is not None:
        stem = f"{stem}_{safe_component(disambiguator, 'disambiguator')}"
    # '_' inside each date and '-' between them keeps the range separator unambiguous even though
    # safe_component() permits '-' in a bank key.
    return f"{stem}_{period_start:%Y_%m_%d}-{period_end:%Y_%m_%d}.ofx"


def split_into_batches(txns: Iterable[Txn], batch_size: int | None) -> list[list[Txn]]:
    """Split one account's transactions into review-sized batches, oldest first.

    docs/adr-ofx-batch-splitting.md decisions 1 and 3. ``batch_size`` of ``None`` means no
    batching: the whole list comes back as a single batch, which is today's behaviour and what a
    ``fetch`` without ``--batch-size`` must keep producing.

    **A booking date is never split across two batches** (decision 3). Transactions are grouped by
    date and whole days are accumulated; a day that would take the batch past ``batch_size`` starts
    the next one instead. The boundary is therefore always *pulled back* to the day break rather
    than extended forward through it — the direction decision 3 left open, chosen here so that
    "at most ``batch_size``" holds for every batch except the one case decision 1 names.

    **That one exception: a single booking date holding more than ``batch_size`` transactions
    becomes one oversized batch.** There is no date boundary inside it to break on, and splitting
    it would separate a day's related entries (a payment and its fee, the two sides of a transfer)
    across two review sittings — the coherence day-grouping exists to protect. ``batch_size`` is a
    soft cap for exactly this case and no other. This is also what guarantees every batch of one
    account gets a distinct date range, which is why ``ofx_filename`` needs no batch marker
    (decision 4) — **do not "fix" an oversized batch by cutting the day; that silently makes two
    files collide on one name.**

    Sorting is done here rather than assumed: nothing upstream orders a source's transactions, so
    an unsorted list would otherwise produce batches whose date ranges interleave. **Only the
    batching path sorts.** With ``batch_size`` of ``None`` the input order is preserved exactly,
    because "no batching" is a byte-identical guarantee, not merely an equivalent-file one — an
    unbatched fetch must write the same bytes it wrote before this function existed, and upstream
    hands transactions in cached-then-fetched order, not date order.
    """
    if batch_size is None:
        unbatched = list(txns)
        return [unbatched] if unbatched else []
    ordered = sorted(txns, key=lambda txn: txn.date)
    if not ordered:
        return []

    batches: list[list[Txn]] = []
    current: list[Txn] = []
    for _, group in groupby(ordered, key=lambda txn: txn.date):
        day = list(group)
        if current and len(current) + len(day) > batch_size:
            batches.append(current)
            current = []
        current.extend(day)
    if current:
        batches.append(current)
    return batches


def batch_statements(
    account: Account,
    txns: Iterable[Txn],
    period_start: date,
    period_end: date,
    batch_size: int | None,
) -> list[tuple[Account, list[Txn], date, date]]:
    """One ``(account, txns, period_start, period_end)`` per batch, oldest first.

    The single entry point both write paths use, so batching cannot drift between the per-file
    path and ``--combine``'s (docs/adr-ofx-batch-splitting.md decision 8). The tuple shape is
    already what :func:`write_combined_ofx` consumes.

    **Without ``batch_size`` this returns exactly one item carrying the requested window**, so an
    unbatched run keeps producing byte-identical output to today's — there is no second code path
    to keep in step.

    **With it, each batch is dated by its own first and last transaction**, not by the requested
    window: decision 4 names a batch file by the range it actually covers, which is what makes the
    name exact and re-derivable (#3) and what makes it indistinguishable from a hand-ranged
    ``fetch --from … --to …``. This applies even when only one batch results — the window a user
    asked for is not the window a batch covers.

    ``end_balance`` is cleared on every batch but the last (decision 5). The live balance may only
    be attached where the *requested* window ends today or later, and only the final batch of an
    account can reach today; every earlier one is a closed sub-window and takes
    :func:`build_statement`'s computed running total. ``account.end_balance`` is already ``None``
    for a window that closed before today, so this narrows rather than contradicts that rule.

    The final batch's ``DTASOF`` is its last *booked* transaction's date, which may fall before
    today — and the live balance is still exact as of that date, not an anachronism: a ledger
    balance only moves when a transaction books, and any entry booked after that date would be in
    this batch and extend it. The requested window reaching today is what closes that argument;
    it is why the live balance may not be attached to a closed window's last batch, whose balance
    could have moved after the window shut.
    """
    batches = split_into_batches(txns, batch_size)
    if not batches:
        return []
    if batch_size is None:
        return [(account, batches[0], period_start, period_end)]
    last = len(batches) - 1
    return [
        (
            account if index == last else replace(account, end_balance=None),
            batch,
            batch[0].date,
            batch[-1].date,
        )
        for index, batch in enumerate(batches)
    ]


def write_account_ofx(
    account: Account,
    txns: Iterable[Txn],
    period_start: date,
    period_end: date,
    output_dir: Path,
    *,
    disambiguator: str | None = None,
) -> Path | None:
    """Write one OFX file for ``account``; return its path, or ``None`` if there are no txns."""
    txns = list(txns)
    if not txns:
        return None

    statement = build_statement(account, txns, period_start, period_end)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ofx_filename(account, period_start, period_end, disambiguator=disambiguator)
    # invariant: newline="" is required - default text mode doubles OfxWriter's CRLF on Windows.
    # AGENTS.md#invariants
    # OfxWriter already emits CRLF line endings. Write with newline="" so the platform does not
    # translate the "\n" a second time — on Windows that produced "\r\r\n" on every line, which
    # is malformed OFX (some parsers reject it outright).
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(statement_to_ofx(statement))
    return path


def write_combined_ofx(
    accounts_and_txns: Sequence[tuple[Account, Sequence[Txn], date, date]],
    filename_period_start: date,
    filename_period_end: date,
    output_dir: Path,
) -> Path | None:
    """Write one OFX file carrying every account's statement; ``None`` if none has a transaction.

    docs/adr-combined-ofx-file.md decision 2: an account with no transactions is omitted, exactly
    as :func:`write_account_ofx` writes nothing for one — and when every account is empty, nothing
    is written here either, rather than a file with a ``BANKMSGSRSV1`` and no ``STMTTRNRS`` in it.

    Each item carries **its own** ``(period_start, period_end)`` — a bank's resolved window, not
    the run's — because that is what ``BANKTRANLIST``'s ``DTSTART``/``DTEND`` and ``LEDGERBAL``'s
    ``DTASOF`` are dated from (:func:`build_statement`). Per-bank windows routinely differ
    (:func:`~gnucash_ofx.run.resolve_window` resolves per bank, not per run), so stamping every
    statement with one shared window would misreport the range a bank was actually asked for.
    ``filename_period_start``/``filename_period_end`` are separate on purpose: decision 6 derives
    the *file's* name from the run's window as a whole, which is the union across banks, not any
    one statement's.
    """
    statements = [
        build_statement(account, txns, period_start, period_end)
        for account, txns, period_start, period_end in accounts_and_txns
        if txns
    ]
    if not statements:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / combined_filename(filename_period_start, filename_period_end)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(combine_statements(statements))
    return path
