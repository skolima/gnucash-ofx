# Contributing

Bug reports and patches are welcome, especially from a bank this tool has not been tried against.

## Before you post anything

**Never include real financial data** — account numbers, payee names, employer or client names,
invoice numbers, balances, or transaction counts — in code, tests, fixtures, docs, commit messages,
PR descriptions or issues. Invent replacements; see
[SECURITY.md](SECURITY.md#sanitizing-before-you-post) for how, and
`tests/fixtures/enablebanking_transactions.json` for the shape of a sanitized sample. Account
numbers in tests must pass the IBAN checksum, so they are generated with the recipe in
[AGENTS.md](AGENTS.md#data) rather than typed at random.

Editing a GitHub issue does not remove what it contained — the previous revision stays visible in
its edit history. Sanitize first.

## Setup

```sh
uv sync                       # install, including the dev group
uv run gnucash-ofx --help
```

## Before opening a PR

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```

All three run in CI on every PR.

## Conventions

- **Test-driven.** For a fetcher or mapper: write a failing test from a sanitized JSON fixture that
  asserts the normalized `Txn` and the resulting OFX (parsed with `ofxtools.OFXTree`), implement to
  green, then refactor. **No live API calls in tests.**
- **Changing `ofxout.py`?** `pytest` only checks the OFX is well-formed, not what GnuCash's libofx
  parser does with it. See [docs/testing.md](docs/testing.md) for how to verify against real
  libofx (`ofxdump`) before opening the PR.
- Python 3.12+, fully type-annotated, `mypy --strict` clean.
- Keep runtime dependencies minimal; prefer the standard library.
- Some odd-looking things are load-bearing — counterparty account numbers duplicated into `MEMO`,
  ASCII folding, `newline=""`. [AGENTS.md](AGENTS.md) lists them as invariants and
  [docs/decisions.md](docs/decisions.md) explains what reversing each one costs. Read those before
  "cleaning up" one of them.

## Adding a bank

Most banks need no code: add a `[banks.<key>]` section to `config.toml` with the exact `aspsp` and
`country` from `gnucash-ofx aspsps --country <CC>`. What tends to need work is a bank's quirks —
where it puts the counterparty account number, whether it honours date filters, how far back its
history goes. Those belong in [docs/enable-banking.md](docs/enable-banking.md), with a test fixture
covering the shape of its response.
