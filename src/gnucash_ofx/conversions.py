"""Pair currency-conversion legs and derive the rate from the two booked amounts.

Pure and source-agnostic: groups on ``Txn.conversion_key``, which a source's mapper sets on a
conversion leg (never its fee row) - see ``gnucash_ofx.sources.enablebanking._conversion_key``.
See ``docs/adr-currency-conversion-pairs.md`` decisions 1-4 for why pairing lives here rather
than in a source module or ``ofxout.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal

from gnucash_ofx.models import Account, Conversion, Txn

# Rate rounding is display-only: the user can type the counter-amount into GnuCash's transfer
# dialog instead and let it derive the rate exactly. See adr-currency-conversion-pairs.md §5.
_RATE_QUANTUM = Decimal("0.000001")

_Leg = tuple[Account, Txn]


def _validate(legs: list[_Leg]) -> tuple[_Leg, _Leg] | None:
    """Return ``(debit leg, credit leg)`` if ``legs`` is a valid conversion pair, else ``None``.

    All six checks must hold (docs/adr-currency-conversion-pairs.md decision 2) or the group is
    left completely untouched: exactly two legs, two distinct accounts, two distinct currencies,
    one booking date, opposite signs, and both amounts non-zero. Anything else is a source we do
    not understand yet - or a false grouping, such as a re-linked account presenting a stale key
    across more legs than expected - and guessing is worse than doing nothing.
    """
    if len(legs) != 2:
        return None
    (account_a, txn_a), (account_b, txn_b) = legs
    if account_a == account_b:
        return None
    if txn_a.currency == txn_b.currency:
        return None
    if txn_a.date != txn_b.date:
        return None
    if txn_a.amount == 0 or txn_b.amount == 0:
        return None
    if (txn_a.amount < 0) == (txn_b.amount < 0):  # same sign -> not opposite
        return None
    return (legs[0], legs[1]) if txn_a.amount < 0 else (legs[1], legs[0])


def _derive(debit: Txn, credit: Txn) -> Conversion:
    from_amount = -debit.amount
    to_amount = credit.amount
    rate = (to_amount / from_amount).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
    return Conversion(
        from_amount=from_amount,
        from_currency=debit.currency,
        to_amount=to_amount,
        to_currency=credit.currency,
        rate=rate,
    )


def pair_conversions(
    accounts: Sequence[tuple[Account, list[Txn]]],
) -> list[tuple[Account, list[Txn]]]:
    """Pair legs sharing a ``conversion_key`` across every account passed in, and attach the rate.

    ``accounts`` is normally one bank's whole fetch - every account collected before any file is
    written (``run.py``'s ``pending``) - so both legs of a same-bank deal are always considered
    together. A ``Txn`` with no ``conversion_key`` passes through unchanged; one whose key's group
    fails validation keeps ``conversion=None``, unchanged from today. On success the *same*
    ``Conversion`` is attached to both legs (decision 4: identical text on both files).

    Never mutates the input: a new list is returned, and a changed ``Txn`` is a new object via
    ``dataclasses.replace`` (``Txn`` is frozen).
    """
    groups: dict[str, list[_Leg]] = {}
    for account, txns in accounts:
        for txn in txns:
            if txn.conversion_key is not None:
                groups.setdefault(txn.conversion_key, []).append((account, txn))

    conversions: dict[str, Conversion] = {}
    for key, legs in groups.items():
        pair = _validate(legs)
        if pair is None:
            continue
        (_, debit), (_, credit) = pair
        conversions[key] = _derive(debit, credit)

    result: list[tuple[Account, list[Txn]]] = []
    for account, txns in accounts:
        new_txns: list[Txn] = []
        for txn in txns:
            conversion = conversions.get(txn.conversion_key) if txn.conversion_key else None
            new_txns.append(replace(txn, conversion=conversion) if conversion is not None else txn)
        result.append((account, new_txns))
    return result
