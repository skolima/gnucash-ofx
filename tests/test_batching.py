"""`split_into_batches` and the filename guarantee decision 4 rests on.

docs/adr-ofx-batch-splitting.md. The randomised test in this module is the load-bearing one: the
ADR drops the `_partN` filename marker entirely on the strength of a *deduction* — a booking date
is never split (decision 3), therefore an account's batch date ranges are disjoint and strictly
increasing, therefore two batches can never produce the same filename (decision 4). Nothing else
in the suite would notice if that deduction stopped holding, and the failure it guards is silent:
two batches computing one filename means the second overwrites the first, losing a whole batch
with no error anywhere.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from gnucash_ofx.models import Account, Txn
from gnucash_ofx.ofxout import batch_statements, ofx_filename, split_into_batches

ACCOUNT = Account(bank_key="alior", account_id="PL123", currency="PLN")
# An account whose window reached today, so the live balance is attached (run.py only sets
# end_balance when ledger_balance_wanted).
OPEN_ACCOUNT = Account(
    bank_key="alior", account_id="PL123", currency="PLN", end_balance=Decimal("100.00")
)
REQUESTED = (date(2026, 5, 1), date(2026, 5, 31))


def _txn(day: date, seq: int) -> Txn:
    return Txn(id=f"tx-{day:%Y%m%d}-{seq}", date=day, amount=Decimal("-1.00"), currency="PLN")


def _on(day: date, count: int) -> list[Txn]:
    return [_txn(day, seq) for seq in range(count)]


# --------------------------------------------------------------------------- basic mechanics


def test_no_batch_size_returns_one_batch() -> None:
    """Omitting --batch-size must reproduce today's one-file-per-account behaviour exactly."""
    txns = _on(date(2026, 5, 1), 3) + _on(date(2026, 5, 2), 4)
    assert split_into_batches(txns, None) == [txns]


def test_empty_input_produces_no_batches() -> None:
    # write_account_ofx already writes nothing for an empty account; batching must not invent a
    # file for one either.
    assert split_into_batches([], 10) == []
    assert split_into_batches([], None) == []


def test_batches_are_chronological_even_when_input_is_not() -> None:
    """Nothing upstream sorts a source's transactions, so the batching path has to."""
    late, early = _on(date(2026, 5, 9), 1), _on(date(2026, 5, 2), 1)
    early_batch, late_batch = split_into_batches(late + early, 1)
    assert [txn.date for txn in early_batch + late_batch] == [date(2026, 5, 2), date(2026, 5, 9)]


def test_unbatched_input_order_is_preserved_not_sorted() -> None:
    """No --batch-size is a byte-identical guarantee, and upstream order is not date order.

    run.py hands transactions in cached-then-fetched insertion order, and the pre-batching write
    path emitted them exactly so. Sorting here would silently reorder every ordinary fetch's OFX
    lines the first time a source delivers out-of-order data - equivalent for GnuCash, but no
    longer the same bytes, and "with no flag set, output is byte-identical" is the contract that
    let this feature ship as packaging-only.
    """
    late, early = _on(date(2026, 5, 9), 1), _on(date(2026, 5, 2), 1)
    assert split_into_batches(late + early, None) == [late + early]


def test_days_are_never_split_and_boundary_is_pulled_back() -> None:
    """decision 3: a day that would overshoot starts the next batch rather than being cut."""
    txns = _on(date(2026, 5, 1), 3) + _on(date(2026, 5, 2), 3)
    # A naive count-based split at 4 would put one of 05-02's three into the first batch.
    assert [len(batch) for batch in split_into_batches(txns, 4)] == [3, 3]


def test_single_day_larger_than_batch_size_becomes_one_oversized_batch() -> None:
    """decision 3's named exception: N is a soft cap, and this is the only case that exceeds it."""
    txns = _on(date(2026, 5, 1), 2) + _on(date(2026, 5, 2), 9) + _on(date(2026, 5, 3), 2)
    assert [len(batch) for batch in split_into_batches(txns, 5)] == [2, 9, 2]


def test_trailing_batch_is_short_rather_than_rebalanced() -> None:
    """Option C: no rebalancing — the last batch is whatever is left, however small."""
    txns = [_txn(date(2026, 5, 1) + timedelta(days=offset), 0) for offset in range(7)]
    assert [len(batch) for batch in split_into_batches(txns, 3)] == [3, 3, 1]


# --------------------------------------------------------------------------- the guarantee


def _random_txns(rng: random.Random) -> list[Txn]:
    """A month of activity with heavy same-day clustering, the shape §2 found real data to have.

    Day sizes are test parameters chosen to exercise the oversized-day branch across every cap
    below, not measured run lengths.
    """
    txns: list[Txn] = []
    for offset in range(rng.randint(1, 30)):
        day = date(2026, 5, 1) + timedelta(days=offset)
        if rng.random() < 0.25:
            continue  # a quiet day
        # The largest must exceed the biggest cap below, or the oversized-day branch never fires
        # at the top of the sweep and those cases assert nothing. Deliberately far above any run
        # length this project has measured — these are cap-exercising parameters, not data.
        txns.extend(_on(day, rng.choice([1, 1, 2, 3, 5, 9, 14, 30, 60])))
    rng.shuffle(txns)  # input order is not guaranteed to be chronological
    return txns


# --------------------------------------------------------------------------- batch_statements


def test_unbatched_keeps_the_requested_window() -> None:
    """No --batch-size must reproduce today's call exactly: one file, named by the window asked for.

    The dates matter as much as the count. Today's per-account file is named from `date_from`/
    `date_to`, not from what the transactions cover, and an unbatched run has to keep doing that.
    """
    txns = _on(date(2026, 5, 4), 2)
    assert batch_statements(OPEN_ACCOUNT, txns, *REQUESTED, None) == [
        (OPEN_ACCOUNT, txns, date(2026, 5, 1), date(2026, 5, 31))
    ]


def test_batches_are_dated_by_what_they_actually_cover() -> None:
    """decision 4: the name is the batch's own first/last transaction, not the requested window."""
    txns = _on(date(2026, 5, 4), 2) + _on(date(2026, 5, 9), 2)
    assert [
        (start, end) for _, _, start, end in batch_statements(ACCOUNT, txns, *REQUESTED, 2)
    ] == [
        (date(2026, 5, 4), date(2026, 5, 4)),
        (date(2026, 5, 9), date(2026, 5, 9)),
    ]


def test_a_lone_batch_is_still_dated_by_its_transactions() -> None:
    """Batching on with one batch resulting is still a batch — the window asked for is not it."""
    txns = _on(date(2026, 5, 4), 2)
    ((_, _, start, end),) = batch_statements(ACCOUNT, txns, *REQUESTED, 40)
    assert (start, end) == (date(2026, 5, 4), date(2026, 5, 4))


def test_only_the_final_batch_carries_the_live_balance() -> None:
    """decision 5: earlier batches are closed sub-windows and take the computed running total."""
    txns = _on(date(2026, 5, 4), 2) + _on(date(2026, 5, 9), 2) + _on(date(2026, 5, 20), 2)
    balances = [
        account.end_balance
        for account, _, _, _ in batch_statements(OPEN_ACCOUNT, txns, *REQUESTED, 2)
    ]
    assert balances == [None, None, Decimal("100.00")]


def test_a_closed_window_gives_no_batch_a_live_balance() -> None:
    """A closed window's `end_balance` is already None; batching narrows that rule, never widens."""
    txns = _on(date(2026, 5, 4), 2) + _on(date(2026, 5, 9), 2)
    assert all(
        account.end_balance is None
        for account, _, _, _ in batch_statements(ACCOUNT, txns, *REQUESTED, 2)
    )


def test_an_account_with_no_transactions_yields_no_batches() -> None:
    # write_account_ofx already returns None for one; the batched path must not write a file either.
    assert batch_statements(OPEN_ACCOUNT, [], *REQUESTED, 10) == []
    assert batch_statements(OPEN_ACCOUNT, [], *REQUESTED, None) == []


SEEDS = range(200)


@pytest.mark.parametrize("batch_size", [1, 2, 5, 10, 25, 40])
def test_batch_date_ranges_never_collide(batch_size: int) -> None:
    """The deduction decision 4 rests on, checked rather than trusted.

    Asserts all four properties together, because it is their *conjunction* that makes the
    filename safe: every transaction is kept, no booking date spans two batches, ranges strictly
    increase, and therefore every filename is distinct. Seeds are looped in-test rather than
    parametrised so a few hundred cases stay a handful of tests.
    """
    for seed in SEEDS:
        why = f"seed={seed} batch_size={batch_size}"
        txns = _random_txns(random.Random(seed))
        batches = split_into_batches(txns, batch_size)

        assert [txn.id for batch in batches for txn in batch] == [
            txn.id for txn in sorted(txns, key=lambda t: t.date)
        ], f"batching must partition the transactions, losing and duplicating none ({why})"

        seen_days: set[date] = set()
        previous_end: date | None = None
        names: list[str] = []
        for batch in batches:
            assert batch, f"an empty batch would write an empty file ({why})"
            days = {txn.date for txn in batch}
            assert not (days & seen_days), f"a booking date spans two batches ({why})"
            seen_days |= days

            start, end = batch[0].date, batch[-1].date
            if previous_end is not None:
                assert start > previous_end, f"ranges must strictly increase ({why})"
            previous_end = end
            names.append(ofx_filename(ACCOUNT, start, end))

        assert len(names) == len(set(names)), f"two batches share one filename ({why})"


@pytest.mark.parametrize("batch_size", [1, 2, 5, 10, 25, 40])
def test_only_an_oversized_day_may_exceed_the_cap(batch_size: int) -> None:
    """decision 1: `N` holds for every batch except one made of a single over-`N` day."""
    for seed in SEEDS:
        for batch in split_into_batches(_random_txns(random.Random(seed)), batch_size):
            if len(batch) <= batch_size:
                continue
            assert len({txn.date for txn in batch}) == 1, (
                "the only batch allowed to exceed the cap is one whole booking date "
                f"(seed={seed} batch_size={batch_size})"
            )
