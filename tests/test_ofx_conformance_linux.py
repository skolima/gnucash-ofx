"""Run generated OFX through real libofx (``ofxdump``) — Linux only.

Proves what `ofxtools` cannot: that libofx's own parser accepts the file, that its NAME/MEMO
buffers (96/390) and CHECKNUM buffer (12) behave the way `ofxout.py`'s constants assume, and that
line endings are `\\r\\n`, never `\\r\\r\\n`. Skipped wherever `ofxdump` is not on PATH — no
version is pinned, matching `docs/testing.md`.

**Does not prove anything about ASCII folding.** `to_ascii()` runs at compose time, strictly
before any fixture here is written, so every string reaching `ofxdump` in this module is already
pure ASCII. Linux libofx keeping non-ASCII characters is true and irrelevant: there is nothing
non-ASCII left in these files for it to keep. The folding-aware check is gated on the Windows
build, stays manual, and lives in the `ofx-conformance` agent
(`.claude/agents/ofx-conformance.md`, #17) — see `docs/testing.md`. A green run of this module is
not evidence about that regression.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gnucash_ofx.models import Account, Txn
from gnucash_ofx.ofxout import batch_statements, write_account_ofx, write_combined_ofx

pytestmark = pytest.mark.skipif(
    shutil.which("ofxdump") is None,
    reason="ofxdump not installed; see docs/testing.md",
)

PERIOD_START = date(2026, 5, 1)
PERIOD_END = date(2026, 5, 31)

_PAYEE_LABEL = "Name of payee or transaction description:"
_MEMO_LABEL = "Extra transaction information (memo):"


def _run_ofxdump(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ofxdump", str(path)], capture_output=True, text=True, check=False)


def _field(stdout: str, label: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"{label!r} not found in ofxdump output:\n{stdout}")


def test_batched_account_combined_into_one_file_parses_and_keeps_its_statements(
    tmp_path: Path,
) -> None:
    """One account's several batches in one combined file — the shape `--batch-size --combine` adds.

    docs/adr-ofx-batch-splitting.md §7 and decision 8. `adr-combined-ofx-file.md` §3 measured
    libofx accepting one `<BANKMSGSRSV1>` with several `<STMTTRNRS>`, but every statement in that
    measurement belonged to a *different* account. This is the first file carrying **one
    `BANKID`+`ACCTID` pair in more than one `<STMTTRNRS>`**, which is what batching plus combining
    produces, and what the ADR reasoned about from two observed imports rather than measured.

    Verified manually against the Windows libofx 0.10.5 that GnuCash itself loads (`testing.md`):
    exit 0, zero `LibOFX ERROR` lines, three statements recovered, all reporting the same account.
    This module runs on whatever `ofxdump` CI provides, so treat a pass here as parse acceptance —
    what GnuCash's *importer* does with the shape is the keyboard gate the ADR leaves open.
    """
    account = Account(
        bank_key="alior",
        account_id="PL10000000000000000000AAAA",
        currency="PLN",
        bank_id="AAAAPLPW",
        end_balance=Decimal("500.00"),
    )
    txns = [
        Txn(
            id=f"tx-{day.isoformat()}-{seq}",
            date=day,
            amount=Decimal("-10.00"),
            currency="PLN",
            payee="ACME Sp. z o.o.",
            memo="Test entry",
        )
        for day in (date(2026, 5, 4), date(2026, 5, 9), date(2026, 5, 20))
        for seq in range(2)
    ]
    items = batch_statements(account, txns, PERIOD_START, PERIOD_END, 2)
    assert len(items) == 3, "fixture must actually batch, or this proves nothing"

    path = write_combined_ofx(items, PERIOD_START, PERIOD_END, tmp_path)
    assert path is not None
    assert path.read_text(encoding="utf-8").count("<STMTTRNRS>") == 3

    result = _run_ofxdump(path)
    assert result.returncode == 0, result.stderr
    # INFO/WARNING lines are routine here (OfxDummyContainer wrappers, DTPOSTED carrying no time
    # part); an ERROR is what a repeated message set produced in adr-combined-ofx-file.md §3, and
    # is what must not appear.
    assert "LibOFX ERROR" not in result.stderr

    assert result.stdout.count("ofx_proc_statement():") == 3, (
        "libofx must hand its caller one statement per batch, not a merged one"
    )
    account_ids = {
        line.split("Account ID:", 1)[1].strip()
        for line in result.stdout.splitlines()
        if "Account ID:" in line
    }
    assert account_ids == {"AAAAPLPW  PL10000000000000000000AAAA"}, (
        "every batch must identify the same account; a differing one would misroute on import"
    )

    # decision 5, end to end through real libofx rather than the unit test: only the final batch
    # of a window reaching today carries the live balance, and each earlier one carries its own
    # zero-based running total (two debits of 10.00 each).
    balances = [
        line.split("Ledger balance:", 1)[1].strip()
        for line in result.stdout.splitlines()
        if "Ledger balance:" in line
    ]
    assert balances == ["-20.00", "-20.00", "500.00"]


def test_normal_transaction_matches_expected_payee_and_memo(tmp_path: Path) -> None:
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-001",
        date=date(2026, 5, 4),
        amount=Decimal("-42.50"),
        currency="PLN",
        payee="Biedronka",
        memo="Grocery store",
    )
    path = write_account_ofx(account, [txn], PERIOD_START, PERIOD_END, tmp_path)
    assert path is not None

    # Same category of libofx-facing structural concern as the checks below: newline="" at write
    # time must produce CRLF, never the doubled CRCRLF that a default-text-mode write produces on
    # Windows. AGENTS.md#invariants; see test_ofxout.test_written_file_has_clean_crlf_line_endings
    # for the unit-level version of this check.
    raw = path.read_bytes()
    assert b"\r\r\n" not in raw
    assert b"\r\n" in raw

    result = _run_ofxdump(path)
    assert result.returncode == 0, result.stderr
    assert _field(result.stdout, _PAYEE_LABEL) == "Grocery store; Biedronka"
    assert _field(result.stdout, _MEMO_LABEL) == "Grocery store"


def test_name_truncates_at_a_word_boundary_in_real_libofx(tmp_path: Path) -> None:
    # Mirrors tests/test_ofxout.py::test_compose_name_truncates_at_a_word_boundary, but through
    # the real libofx buffer rather than the pure-Python compose_name() unit test.
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-long-name",
        date=date(2026, 5, 10),
        amount=Decimal("-10.00"),
        currency="PLN",
        memo=f"{'A' * 50} {'B' * 50}",
    )
    path = write_account_ofx(account, [txn], PERIOD_START, PERIOD_END, tmp_path)
    assert path is not None

    result = _run_ofxdump(path)
    assert result.returncode == 0, result.stderr
    assert _field(result.stdout, _PAYEE_LABEL) == "A" * 50


def test_memo_caps_at_the_libofx_buffer_in_real_libofx(tmp_path: Path) -> None:
    # Mirrors tests/test_ofxout.py::test_compose_memo_caps_at_the_libofx_buffer.
    account = Account(bank_key="alior", account_id="PL123", currency="PLN")
    txn = Txn(
        id="tx-long-memo",
        date=date(2026, 5, 11),
        amount=Decimal("-10.00"),
        currency="PLN",
        memo="A" * 500,
    )
    path = write_account_ofx(account, [txn], PERIOD_START, PERIOD_END, tmp_path)
    assert path is not None

    result = _run_ofxdump(path)
    assert result.returncode == 0, result.stderr
    assert _field(result.stdout, _MEMO_LABEL) == "A" * 390


def test_over_long_checknum_is_dropped_not_rejected(tmp_path: Path) -> None:
    # Mirrors tests/test_ofxout.py::test_compose_check_number_drops_an_over_long_reference. The
    # dropped reference means no <CHECKNUM> tag reaches the file at all, so the file still parses
    # cleanly — a naive first version of this test might expect ofxdump to fail here; it must not.
    account = Account(bank_key="wise_personal", account_id="PL123", currency="EUR")
    txn = Txn(
        id="tx-long-ref",
        date=date(2026, 5, 12),
        amount=Decimal("-10.00"),
        currency="EUR",
        memo="Invoice",
        reference="TRANSFER-9000000001",
    )
    path = write_account_ofx(account, [txn], PERIOD_START, PERIOD_END, tmp_path)
    assert path is not None
    assert "<CHECKNUM>" not in path.read_text(encoding="utf-8")

    result = _run_ofxdump(path)
    assert result.returncode == 0, result.stderr
