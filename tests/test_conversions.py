"""TDD for currency-conversion leg pairing and rate derivation.

See docs/adr-currency-conversion-pairs.md decisions 1-4. Txns are built directly, as
test_ofxout.py does, rather than routed through a source's mapper.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from gnucash_ofx.conversions import pair_conversions
from gnucash_ofx.models import Account, Conversion, Txn

_DATE = date(2026, 5, 18)


def _account(currency: str, account_id: str) -> Account:
    return Account(bank_key="alior_kantor", account_id=account_id, currency=currency)


def _txn(
    id: str,
    amount: str,
    currency: str,
    *,
    conversion_key: str | None = "kantor:220",
    txn_date: date = _DATE,
) -> Txn:
    return Txn(
        id=id,
        date=txn_date,
        amount=Decimal(amount),
        currency=currency,
        conversion_key=conversion_key,
    )


def test_clean_pair_gets_identical_conversion_with_derived_rate() -> None:
    eur, pln = _account("EUR", "acct-eur"), _account("PLN", "acct-pln")
    debit, credit = _txn("d1", "-12000.00", "EUR"), _txn("c1", "51720.00", "PLN")

    paired = pair_conversions([(eur, [debit]), (pln, [credit])])

    expected = Conversion(
        from_amount=Decimal("12000.00"),
        from_currency="EUR",
        to_amount=Decimal("51720.00"),
        to_currency="PLN",
        rate=Decimal("4.310000"),
    )
    paired_debit, paired_credit = paired[0][1][0], paired[1][1][0]
    assert paired_debit.conversion == expected
    assert paired_credit.conversion == expected
    # The same object on both legs, not merely an equal one - decision 4 needs identical text.
    assert paired_debit.conversion is paired_credit.conversion


def test_legs_supplied_in_either_order_still_pick_debit_as_from() -> None:
    eur, pln = _account("EUR", "acct-eur"), _account("PLN", "acct-pln")
    credit, debit = _txn("c1", "51720.00", "PLN"), _txn("d1", "-12000.00", "EUR")

    paired = pair_conversions([(pln, [credit]), (eur, [debit])])  # credit's account passed first

    conversion = paired[0][1][0].conversion
    assert conversion is not None
    assert conversion.from_currency == "EUR"
    assert conversion.to_currency == "PLN"


def test_three_legs_on_one_key_are_left_untouched() -> None:
    a, b, c = _account("EUR", "a"), _account("PLN", "b"), _account("PLN", "c")
    paired = pair_conversions(
        [
            (a, [_txn("d1", "-100.00", "EUR")]),
            (b, [_txn("c1", "430.00", "PLN")]),
            (c, [_txn("c2", "430.00", "PLN")]),
        ]
    )
    assert all(txn.conversion is None for _, txns in paired for txn in txns)


def test_one_leg_only_is_left_untouched() -> None:
    eur = _account("EUR", "acct-eur")
    paired = pair_conversions([(eur, [_txn("d1", "-100.00", "EUR")])])
    assert paired[0][1][0].conversion is None


def test_same_currency_is_left_untouched() -> None:
    a, b = _account("EUR", "a"), _account("EUR", "b")
    paired = pair_conversions(
        [(a, [_txn("d1", "-100.00", "EUR")]), (b, [_txn("c1", "100.00", "EUR")])]
    )
    assert all(txn.conversion is None for _, txns in paired for txn in txns)


def test_same_sign_is_left_untouched() -> None:
    a, b = _account("EUR", "a"), _account("PLN", "b")
    paired = pair_conversions(
        [(a, [_txn("d1", "-100.00", "EUR")]), (b, [_txn("c1", "-430.00", "PLN")])]
    )
    assert all(txn.conversion is None for _, txns in paired for txn in txns)


def test_different_dates_are_left_untouched() -> None:
    a, b = _account("EUR", "a"), _account("PLN", "b")
    paired = pair_conversions(
        [
            (a, [_txn("d1", "-100.00", "EUR", txn_date=date(2026, 5, 18))]),
            (b, [_txn("c1", "430.00", "PLN", txn_date=date(2026, 5, 19))]),
        ]
    )
    assert all(txn.conversion is None for _, txns in paired for txn in txns)


def test_same_account_is_left_untouched() -> None:
    # Not reachable via the real sources today (an Account is scoped to one currency), but the
    # check stands on its own rather than leaning on that as an accident of current sources.
    a = _account("EUR", "a")
    paired = pair_conversions(
        [(a, [_txn("d1", "-100.00", "EUR")]), (a, [_txn("c1", "430.00", "PLN")])]
    )
    assert all(txn.conversion is None for _, txns in paired for txn in txns)


def test_zero_amount_is_left_untouched_without_a_zerodivisionerror() -> None:
    a, b = _account("EUR", "a"), _account("PLN", "b")
    paired = pair_conversions(
        [(a, [_txn("d1", "0.00", "EUR")]), (b, [_txn("c1", "430.00", "PLN")])]
    )
    assert all(txn.conversion is None for _, txns in paired for txn in txns)


def test_conversion_key_none_passes_through_unchanged() -> None:
    a = _account("PLN", "a")
    txn = Txn(id="t1", date=_DATE, amount=Decimal("10.00"), currency="PLN")
    paired = pair_conversions([(a, [txn])])
    assert paired[0][1][0] is txn


def test_rate_is_quantized_to_six_decimal_places() -> None:
    a, b = _account("EUR", "a"), _account("PLN", "b")
    paired = pair_conversions([(a, [_txn("d1", "-1.00", "EUR")]), (b, [_txn("c1", "3.00", "PLN")])])
    conversion = next(txn.conversion for _, txns in paired for txn in txns if txn.conversion)
    assert conversion.rate == Decimal("3.000000")
    assert -conversion.rate.as_tuple().exponent == 6


def test_input_list_and_txns_are_not_mutated() -> None:
    a, b = _account("EUR", "a"), _account("PLN", "b")
    debit, credit = _txn("d1", "-12000.00", "EUR"), _txn("c1", "51720.00", "PLN")
    original = [(a, [debit]), (b, [credit])]

    pair_conversions(original)

    assert original[0][1][0] is debit
    assert original[1][1][0] is credit
    assert debit.conversion is None
    assert credit.conversion is None
