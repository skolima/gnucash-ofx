# PRD: gnucash-ofx

Status: retroactive — written after v1 shipped, to give ongoing and future work a fixed reference
point. Where this describes something already built, treat it as "what v1 committed to and why,"
not a proposal.

## Problem

GnuCash has no built-in Open Banking support. The alternative to this tool is: log into each bank
monthly, download a statement, hand-correct it, and import it — repeated per bank, per currency,
forever. It doesn't scale past one or two accounts and it's easy to silently skip a month.

## Who this is for

A single user: someone who runs GnuCash, holds accounts at PSD2-covered banks (or Wise), and is
comfortable running a CLI tool and a one-time RSA/API setup. Not a hosted product, not multi-tenant,
not aimed at anyone who isn't already both a GnuCash user and willing to self-host a credential.

## Goals

- Replace manual statement download/re-entry with `fetch`, run monthly, producing OFX GnuCash can
  import directly.
- Make de-duplication and re-import safe: stable `FITID`, stable `BANKID`/`ACCTID`, so re-running a
  fetch (or importing the same month twice) never creates duplicate transactions.
- Get enough payee/memo signal into each transaction that GnuCash's Bayesian matcher can route it
  without the user hand-categorizing every row.
- Survive partial failure: one bad bank, one expired consent, one corrupted state file must not
  block the banks that are fine.
- Never leave the user's machine, never require a server, never store secrets anywhere but
  env/`.env`.

## Non-goals

- **No categorization.** GnuCash's own matcher does this from import history; this tool's job ends
  at clean OFX. Adding rules here would duplicate and fight that matcher.
- **No multi-user / hosted mode.** Restricted Mode Enable Banking access is explicitly
  single-owner; there's no auth model, no multi-tenant state, and none is planned.
- **No GUI.** CLI only, meant to be run by hand monthly or from cron/Task Scheduler.
- **No write path.** This tool only reads bank data and writes files; it never touches the bank
  side (no payments, no transfers) or writes into a running GnuCash instance directly.

## v1 scope (shipped)

**Sources:** Alior Bank, Bank Millennium, Erste (Santander Bank Polska), Wise (personal +
business) — all via Enable Banking's PSD2 API in Restricted Mode.

**Commands** (`gnucash-ofx <cmd>`):

| Command | Purpose |
|---|---|
| `aspsps --country CC` | List exact bank names Enable Banking exposes for a country, for `config.toml`. |
| `link <bank>` | One-time (~180-day) browser consent flow; also the only point account number/currency/name are captured, into `state/<bank>.json`. |
| `status [--check]` | Consent days-remaining and coverage per bank; `--check` exits 1 if any needs attention. |
| `fetch [--from Y-M-D] [--to Y-M-D] [--bank all\|<bank>] [--refresh] [--dry-run] [--combine]` | Fetch transactions, write one `.ofx` per account per currency to `output/` (or one combined file with `--combine`). Dates default to resuming from recorded coverage. |

**Output contract:**

- One statement per account per currency (a statement is single-currency; Wise/Alior FX accounts
  split accordingly). By default one file per statement; `--combine` puts every statement of a run
  into one file instead and changes nothing else about them — see *Decided and built* below.
- Signed amounts: credits positive, debits negative.
- `FITID` stable across re-fetches — the basis for GnuCash's de-dup on re-import.
- `BANKID` (from config `bankid`/BIC, else truncated bank key) + `ACCTID` (IBAN, normalized) stable
  across re-links — GnuCash derives `online_id` from the pair, so changing either orphans
  already-imported accounts.
- `NAME` = remittance text first, then counterparty (mirrors GnuCash's own AqBanking builder);
  `MEMO` = remittance + counterparty IBAN. A source's opaque machine reference (Wise transfer/card/
  cashback ids) is split into `CHECKNUM`/`REFNUM` instead of leading the description, and dropped
  entirely if it can't fit rather than left in readable text.
- Payee/memo text folded to ASCII — a deliberate, measured workaround for GnuCash-for-Windows
  silently dropping non-ASCII on import (confirmed OFX 2.x doesn't fix it either); scope stays
  Windows-only, not widened.

**Reliability contract:**

- `fetch --bank all` fails per-bank, not globally: a `BankFailure`/`FetchReport` per bank, files
  from the working banks still get written, and only truly global problems (missing credentials,
  bad config, an unknown `--bank`) abort the whole run.
- Exit 1 if any bank failed even though files were written (so cron notices); exit 0 only if every
  requested bank succeeded; exit 2 is argparse's own.
- Re-running a failed `fetch` retries only what failed — successes are served from a ~6h cache and
  spend no further rate-limit allowance.
- stdout carries only the written file paths (one per line, bare); everything else — progress,
  warnings, the failure summary, the count-and-directory header — goes to stderr, so
  `fetch > files.txt` stays parseable, and is empty when nothing was written.
- `fetch --dry-run` answers "what would this run write?" from `config.toml` + `state/<bank>.json`
  alone — no API call, no cache read or write, no run-log entry. The predicted paths are the real
  ones (same filename code, same session-wide disambiguation group), so adding a bank or checking a
  date range costs nothing. Exits 0 whenever the config resolves; a lapsed consent is reported, not
  a failure ([#7](https://github.com/skolima/gnucash-ofx/issues/7)).

**Rate limits:** fetches run in "online" mode (PSU headers) to get the higher end-user allowance
rather than the ~4/day background cap; a 6h fetch cache absorbs retries/re-imports without
spending it.

## Constraints that shaped the design

- PSD2 guarantees ~90 days of history without a fresh consent, enforced inconsistently — some banks
  (Alior, Alior Kantor, Erste) hard-reject anything older; a full-year backfill only works on banks
  that serve more.
- libofx (which GnuCash embeds) has fixed, unterminated-`strncpy` buffers for `NAME` (96),
  `MEMO` (390) and `CHECKNUM` (12) — over-length values are a memory-safety hazard downstream, not
  just a cosmetic overflow, which is why truncation vs. drop is chosen per field deliberately rather
  than uniformly.
- Enable Banking never re-returns full account details after `link` — they must be captured then or
  not at all, which is why `state/<bank>.json` exists and why old-schema state is degraded-not-broken
  rather than migrated.

## Success criteria

- A month's transactions for every configured bank import into GnuCash with zero manual retyping
  and zero duplicate rows on a second import of the same range.
- A single bank outage (expired consent, API error) during `fetch --bank all` never costs the other
  banks their files for that run.
- No real account number, balance, or transaction volume ever appears in the repo, an issue, or a
  log line.

## Known gaps / stretch (not v1)

- **mBank IKE/IKZE** — likely outside PSD2 scope; mechanism TBD.
- **Interactive Brokers** — not a PSD2 bank; would go through Flex Web Service + `ibflex2` as a
  separate source under `sources/`.
- **Non-ASCII on Linux** ([#1](https://github.com/skolima/gnucash-ofx/issues/1)) — folding is
  currently unconditional and Windows-only in cause, but applied on every platform; a Linux user
  importing generated OFX loses diacritics they didn't need to.
- Pre-v0.1-linked banks fall back to an opaque `ACCTID`, which changes on every re-link — those
  banks need a one-time re-`link` the user must remember to do; there's no automatic detection or
  prompt today. **Decided: fix this — see Planned next.**

## Decided and built

Resolved from the open questions above. The coverage/warning group shipped for
[#6](https://github.com/skolima/gnucash-ofx/issues/6); see
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) for the measurements
and the rejected options, and [`decisions.md`](decisions.md) for what it settled.

- **`fetch` surfaces what will silently cost you data.** Shipped. A per-account coverage ledger
  in `state/coverage/`, which makes `--from`/`--to` optional (each bank resumes from what it has
  fetched, with a deliberate overlap) and makes a gap reportable before it ages out; a consent
  warning at 45 days; and a warning when an account's `ACCTID` still resolves to an opaque `uid`.
  All three are stderr warnings that never change the exit code. The identity check is on the
  **symptom**, not the state-file schema — 5 of the 6 banks here are on the pre-`accounts` layout
  and none is in the harmful condition, so a schema check would cost five browser SCA dances and
  fix nothing.
- **ASCII folding stays unconditional — no platform check.** Considered and rejected: gating the
  fold on `sys.platform` would reintroduce exactly the silent-data-loss failure mode for anyone who
  runs this tool from Windows against a shared/portable GnuCash file, or moves platforms later. The
  fold is cheap and the cost of folding unnecessarily (losing diacritics on a Linux-only setup,
  [#1](https://github.com/skolima/gnucash-ofx/issues/1)) is far smaller than the cost of
  reintroducing silent corruption on Windows. Revisit only if GnuCash-for-Windows itself fixes the
  underlying libofx/OpenSP transcoding — not by adding a branch here.
- **`fetch --combine` writes one file for the whole run instead of one per account.** Shipped for
  [#8](https://github.com/skolima/gnucash-ofx/issues/8); see
  [`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) for the measurements, the gate a real
  GnuCash import had to pass before this could ship at all, and why the answer was worth having:
  8–10 import-assistant dialogs a month collapsing to 1, not the "ten-plus" the issue assumed. Off
  by default — per-file output is the only path with real mileage on it — and verified against
  GnuCash 5.16 specifically, not every version.
