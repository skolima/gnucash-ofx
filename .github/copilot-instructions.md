# Copilot review instructions

This repo pulls bank transactions via Open Banking (Enable Banking, which also covers Wise — there
is no separate Wise API integration) and emits **OFX files** for GnuCash import. When reviewing
PRs, prioritize:

## Correctness invariants
- **Stable `FITID`** per transaction, derived from the bank's transaction/entry reference. Flag
  anything that could make a transaction's id change between runs — it breaks GnuCash de-dup.
- **Stable `BANKID` and `ACCTID`.** GnuCash derives an account's `online_id` from these two, so a
  change orphans already-imported accounts. Flag anything that makes either depend on a value that
  varies between runs — in particular Enable Banking's account `uid`, which is regenerated on
  every re-link.
- **Signed amounts**: credits positive, debits negative; verify CRDT/DBIT handling.
- **One OFX file per account, per currency.** Multi-currency accounts (Wise, Alior FX) must not
  be merged into a single statement.
- Dates and currency codes must round-trip; OFX must parse with `ofxtools`.

## Deliberate oddities — do NOT flag these as bugs
These look wrong in isolation and are load-bearing; see [`docs/decisions.md`](../docs/decisions.md).
- Counterparty account numbers are written to **both** `MEMO` and `BANKACCTTO`. libofx does not
  parse `BANKACCTTO`, so the memo copy is what actually drives GnuCash's account matching.
- `NAME`/`MEMO` are **folded to ASCII**. GnuCash's OFX parser silently deletes non-ASCII.
- OFX is written with `newline=""` so CRLF is not translated twice on Windows.

## Security
- Secrets (Enable Banking app id + RSA private key) come from env/`.env` only. Flag any logging,
  printing, or committing of secrets, private keys, or raw account data — including full IBANs in
  progress/log output, which are redacted deliberately.
- No live API calls in tests — they must use recorded/sanitized fixtures.

## Data — this repo is public
Flag anything that looks like real financial data anywhere in the diff, **including test data,
comments, and the PR description itself**:
- account numbers, payee names, employer or client names, invoice numbers, balances, transaction
  counts or volumes;
- quoted output from `output/`, `state/` or `cache/`.

Substitutes must be invented, not merely edited: see
[`tests/fixtures/enablebanking_transactions.json`](../tests/fixtures/enablebanking_transactions.json)
and the synthetic-IBAN recipe in [`AGENTS.md`](../AGENTS.md#data) — account numbers in tests have to
pass the IBAN checksum, so they are generated rather than typed at random.

## Style
- Python 3.12+, fully type-annotated, `mypy --strict` clean, `ruff` clean.
