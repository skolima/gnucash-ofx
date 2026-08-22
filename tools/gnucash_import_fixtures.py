"""Build the synthetic OFX files for the manual GnuCash import gates.

``*.ofx`` is gitignored, and a scratch directory does not survive a machine change — so the
durable artefact is this generator, not the files. ``adr-combined-ofx-file.md`` learned that the
hard way: its four fixtures were built in a session scratchpad and the ADR had to record that
"§1's two routes reproduce all four in minutes, which is the durable way to get them back".

Run it, import what it writes into a **throwaway** GnuCash book, and follow the procedure in
``docs/testing.md``:

    uv run python tools/gnucash_import_fixtures.py /tmp/gnucash-batch-test

**Synthetic throughout** — the canonical fixture IBAN from ``tests/fixtures``, a placeholder BIC,
and ``ACME Sp. z o.o.`` per ``AGENTS.md#data``. Nothing reads ``state/``, ``cache/`` or
``output/``, so the output is safe to attach to an issue.

Built through the **real** shipping path (:func:`batch_statements` +
:func:`write_combined_ofx`), not composed by hand, so what gets imported is what ``fetch``
actually writes. ``adr-combined-ofx-file.md`` flagged re-running its procedure against the real
writer as owed work once that writer existed; this is it.

Each batch is given a **different total**, so its ``LEDGERBAL`` is distinguishable in GnuCash's
reconcile dialog. That is what makes the open ``LEDGERBAL`` question answerable at all: with equal
balances there would be no way to tell which statement the dialog had taken its figure from.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from gnucash_ofx.models import Account, Txn
from gnucash_ofx.ofxout import batch_statements, write_combined_ofx

BANK_ID = "AAAAPLPW"
ACCT_ID = "PL03999999990000000000000005"
BATCH_SIZE = 2

# (day, amounts) — one batch per day at BATCH_SIZE, each with a distinct sum so the resulting
# LEDGERBAL identifies which statement a dialog is reading from.
ROUND_ONE: list[tuple[date, list[Decimal]]] = [
    (date(2026, 5, 4), [Decimal("-10.00"), Decimal("-20.00")]),  # LEDGERBAL -30.00
    (date(2026, 5, 11), [Decimal("-100.00"), Decimal("-200.00")]),  # LEDGERBAL -300.00
    (date(2026, 5, 18), [Decimal("-1.00"), Decimal("-2.00")]),  # final: the live balance
]
ROUND_TWO: list[tuple[date, list[Decimal]]] = [
    (date(2026, 6, 1), [Decimal("-5.00"), Decimal("-15.00")]),  # LEDGERBAL -20.00
    (date(2026, 6, 8), [Decimal("-500.00"), Decimal("-250.00")]),  # LEDGERBAL -750.00
    (date(2026, 6, 15), [Decimal("-3.00"), Decimal("-4.00")]),  # final: the live balance
]


def build(
    days: list[tuple[date, list[Decimal]]],
    live_balance: Decimal,
    tag: str,
    out_dir: Path,
) -> Path:
    """Write one combined OFX carrying several batches of a single account."""
    account = Account(
        bank_key="alior",
        account_id=ACCT_ID,
        currency="PLN",
        bank_id=BANK_ID,
        end_balance=live_balance,
    )
    txns = [
        Txn(
            id=f"{tag}-{day.isoformat()}-{seq}",
            date=day,
            amount=amount,
            currency="PLN",
            payee="ACME Sp. z o.o.",
            # Names the batch a row came from, so the register check is unambiguous.
            memo=f"{tag} batch {index} entry {seq + 1}",
        )
        for index, (day, amounts) in enumerate(days, start=1)
        for seq, amount in enumerate(amounts)
    ]
    window_start, window_end = days[0][0], days[-1][0]
    items = batch_statements(account, txns, window_start, window_end, BATCH_SIZE)

    # Assert the fixture is actually the shape under test. A silently unbatched file would make
    # the whole manual session prove nothing, and that is expensive to discover at the keyboard.
    assert len(items) == len(days), f"expected {len(days)} batches, got {len(items)}"
    balances = [account.end_balance for account, _, _, _ in items]
    assert balances[-1] == live_balance, "the final batch must carry the live balance"
    assert all(balance is None for balance in balances[:-1]), (
        "every earlier batch must carry no live balance (decision 5)"
    )

    written = write_combined_ofx(items, window_start, window_end, out_dir)
    assert written is not None
    target = written.with_name(f"{tag}.ofx")
    written.replace(target)
    return target


def _tagged(text: str, tag: str) -> list[str]:
    return [
        line.split(tag, 1)[1].split("<", 1)[0].strip() for line in text.splitlines() if tag in line
    ]


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    for days, live, tag in (
        (ROUND_ONE, Decimal("5000.00"), "batch_new_account"),
        (ROUND_TWO, Decimal("7000.00"), "batch_existing_account"),
    ):
        path = build(days, live, tag, out_dir)
        text = path.read_text(encoding="utf-8")
        starts, ends = _tagged(text, "<DTSTART>"), _tagged(text, "<DTEND>")
        print(path)
        print(f"    statements : {text.count('<STMTTRNRS>')}")
        print(f"    ranges     : {[f'{s}..{e}' for s, e in zip(starts, ends, strict=True)]}")
        print(f"    LEDGERBAL  : {_tagged(text, '<BALAMT>')}")
    print("\nImport into a THROWAWAY book. Procedure: docs/testing.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
