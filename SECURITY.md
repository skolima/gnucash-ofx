# Security policy

This tool holds credentials that reach bank accounts and writes real transaction data to disk, so
it is worth being precise about what it touches and how to report a problem.

## What the tool handles

| Thing | Where it lives | Notes |
|---|---|---|
| Enable Banking application id | `.env` (`EB_APP_ID`) | Read from the environment only; never logged. |
| RSA private key | a `.pem` path from `.env` (`EB_PRIVATE_KEY`) | Signs the JWT sent to Enable Banking. Anyone holding it can act as the application. |
| Session ids and consent expiry | `state/` | Grants access to the linked accounts until the consent expires. |
| Account numbers, names and the raw link response | `state/` | Captured at `link` because the API will not return it again. Account numbers (in several representations), account labels, BICs and currencies. The stored response also has `postal_address`, `psu_status` and `legal_age` fields — null at every bank checked so far, but an ASPSP that fills them puts personal details in this file. Treat it like `cache/`, not like a config file. |
| Fetched transactions and balances | `cache/` (~6h), `output/*.ofx` | Real financial data: account numbers, counterparties, amounts. |

`.env`, `*.pem`, `*.key`, `config.toml`, `state/`, `cache/`, `output/` and `*.ofx` are all
gitignored. Nothing is uploaded anywhere; the only network calls are to Enable Banking's API and,
unless `EB_PSU_IP` is set, one public-IP lookup.

## Reporting a vulnerability

Report privately through **GitHub's private vulnerability reporting** (the *Report a vulnerability*
button under this repository's Security tab). Please do not open a public issue for anything that
could expose credentials or account data.

Include what you would need yourself: affected version or commit, what an attacker gains, and the
smallest reproduction you can manage — **with real account numbers, payee names and amounts
replaced**. See "Sanitizing" below.

This is a personal project maintained in spare time. Expect an acknowledgement within about a week,
and a fix on a best-effort basis. There is no bounty.

### In scope

Anything that leaks credentials or account data, or that lets an untrusted input (an API response,
a config value, a bank key) escape its intended handling — writing outside the configured output
directory, for instance.

### Out of scope

Vulnerabilities in Enable Banking's platform or in a bank's own systems — report those to them.
Dependency advisories with no exploitable path here are welcome as ordinary issues.

## Sanitizing before you post

Real data pasted into an issue is public forever, and editing does not remove it: GitHub keeps a
visible edit history on issue and comment bodies. So sanitize before posting, not after.

- **Account numbers** — replace with a synthetic IBAN. If the value has to survive the tool's
  checksum validation, generate one with the recipe in [AGENTS.md](AGENTS.md#data); random digits
  will not pass.
- **Payees, employers, clients, invoice numbers** — invent replacements.
- **Balances, transaction counts, file counts** — round them away or drop them. A count is a
  financial-scale signal on its own.
- **OFX and JSON samples** — reduce to the one transaction that shows the problem, then replace
  every field in it.

`tests/fixtures/enablebanking_transactions.json` shows the intended shape of a sanitized sample.

Progress output is already redacted (`PL12***3456`), but that only covers what the tool prints —
anything you copy out of `output/`, `state/` or `cache/` is untouched and needs the pass above.
