"""Normalized data model shared by all sources.

Every fetcher converts a bank's proprietary transaction format into a list of :class:`Txn`
grouped under an :class:`Account`. The OFX writer (:mod:`gnucash_ofx.ofxout`) only ever sees
these types, so adding a new source means writing a fetcher, not touching OFX generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

# ISO 4217's own reserved code for "no currency" — seen from Enable Banking on a real account
# (Alior, 2026-08-10) where it means the ASPSP has none to report, not that the account holds a
# currency literally called XXX. Shared by every place a currency crosses in from ASPSP JSON (or
# from state persisted from it), so it is treated as absent rather than trusted as a known-but-
# wrong currency. See docs/adr-xxx-currency-placeholder.md.
_NO_CURRENCY_CODE = "XXX"

# What reading one account's data may raise before it counts as that account's failure rather than
# the run's (docs/adr-input-hardening.md decision 3). `ValueError` alone was too narrow to deliver
# the containment its own comment promised: `Decimal("garbage")` raises `decimal.InvalidOperation`,
# an `ArithmeticError`, and a wrong-typed field — `"transaction_amount": "100"` instead of an object
# — raises `AttributeError` from the `.get` chain, so either one escaped every handler and aborted a
# whole multi-bank run. `TypeError` covers the remaining wrong-shape cases. Deliberately not bare
# `Exception`: a programming error should still crash rather than be filed as a bank's bad data.
# Defined here rather than in run.py because the cache's merge keys entries the same way the mapper
# reads them and needs the identical containment — one named set, not two drifting copies.
_BAD_DATA_ERRORS = (ValueError, ArithmeticError, TypeError, AttributeError)


def currency_or_none(value: Any) -> str | None:
    """A currency string, or ``None`` if absent or ISO 4217's ``XXX`` placeholder."""
    text = str(value) if value else None
    return None if text == _NO_CURRENCY_CODE else text


@dataclass(frozen=True, slots=True)
class Conversion:
    """Both sides of one currency conversion. Identical on both legs of the pair.

    ``rate`` is ``to_amount / from_amount``, quantized to 6 dp, derived from the two booked
    amounts rather than parsed from either source's prose — see
    ``docs/adr-currency-conversion-pairs.md`` decision 3.
    """

    from_amount: Decimal  # magnitude of the debited leg
    from_currency: str
    to_amount: Decimal  # magnitude of the credited leg
    to_currency: str
    rate: Decimal


@dataclass(frozen=True, slots=True)
class Txn:
    """A single normalized transaction.

    ``id`` becomes the OFX ``FITID`` and must be **stable** across runs (derived from the
    bank's own transaction/entry reference) so GnuCash de-duplicates reliably on re-import.
    ``amount`` is signed: credits positive, debits negative.
    """

    id: str
    date: date
    amount: Decimal
    currency: str
    payee: str | None = None
    memo: str | None = None
    # Counterparty account IBAN (creditor for debits, debtor for credits) when the bank
    # provides it. Appended to the OFX MEMO (the token GnuCash's matcher uses to route
    # transactions to accounts) and also emitted as the standard <BANKACCTTO> aggregate.
    counterparty_iban: str | None = None
    # The source's own machine reference for this transaction, when it hands one over as a
    # distinct value rather than as remittance text (Wise's transfer id). It is *not* remittance
    # text: unique per transaction, so it can never earn a Bayesian match, which is why it goes to
    # OFX CHECKNUM/REFNUM (the register's Num column) instead of into NAME/MEMO.
    reference: str | None = None
    # A source-specific deal key set by the mapper on a currency-conversion leg (never on its fee
    # row). Legs sharing a key within one bank's fetch are candidates for pairing by
    # gnucash_ofx.conversions.pair_conversions(). None for every ordinary transaction.
    conversion_key: str | None = None
    # Set by pair_conversions() once a candidate pair validates; the same Conversion on both legs.
    # None until then, and permanently None for a candidate that failed validation.
    conversion: Conversion | None = None


@dataclass(frozen=True, slots=True)
class Account:
    """A source account, scoped to a single currency.

    One OFX file is produced per ``Account``; multi-currency accounts (Wise, Alior FX) yield
    one ``Account`` per currency. ``end_balance`` is the real closing balance when the source
    reports it (written as OFX ``LEDGERBAL``); when absent it is derived from the transactions.
    """

    bank_key: str
    account_id: str
    currency: str
    end_balance: Decimal | None = None
    start_balance: Decimal | None = None
    # Explicit OFX BANKID (the bank's BIC), from the bank's ``bankid`` config option.
    # None falls back to a truncation of ``bank_key``.
    bank_id: str | None = None
    # The bank's cash_account_type exactly as stored at link time; None when the session predates
    # the v2 state schema (only a re-link fills it). Kept raw on purpose — the state file speaks
    # the bank's vocabulary, and ofxout.accttype_for() is the single place the OFX ACCTTYPE
    # mapping decision lives (docs/adr-accttype-mapping.md decision 4).
    cash_account_type: str | None = None
