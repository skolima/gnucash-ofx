# ADR: a coverage ledger, and the three things `fetch` should say before they cost you data

**Status:** Accepted. All eight decisions implemented in
[#16](https://github.com/skolima/gnucash-ofx/issues/16) — see *Where this stands* below. Measurements
in §1–§8 taken 2026-08-09 against the local `state/` and `cache/` directories, the OFX DTD shipped
with GnuCash for Windows, and the code as of [#13](https://github.com/skolima/gnucash-ofx/issues/13).
§9 is the one live measurement: 5 counted requests against Alior and Erste the same day, which
settled the only question this design could not answer locally.
**Date:** 2026-08-09.
**Scope:** a new `coverage.py` (the ledger and its interval arithmetic), `run.py` (window
resolution, three warnings, one write per account), `cli.py` (optional `--from`/`--to`, a warning
block, `status --check`), and one constant imported from `cache.py`. `ofxout.py`,
`sources/enablebanking.py` and `state.py` are untouched; no new API call is made anywhere.
**Issue:** [#6](https://github.com/skolima/gnucash-ofx/issues/6). Depends on decisions 5 and 6 of
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — both shipped in
[#12](https://github.com/skolima/gnucash-ofx/issues/12) — and inherits `LATE_BOOKING_MARGIN` from it
rather than re-picking a number. Its §7 named this file's subject as out of scope and pointed here.

---

## Where this stands

- [x] **1.** A coverage ledger at `state/coverage/<bank>.json`, keyed by a digest of `ACCTID` —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **2.** Only a successful fetch advances coverage, written per account as it lands —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **3.** `--from`/`--to` become optional; the default window is resolved per bank —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **4.** The gap warning, before anything is spent, escalated by whether the gap can still be
      recovered — [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **5.** Consent expiry is warned at 45 days —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **6.** `status --check` exits 1 when a bank needs attention; bare `status` still exits 0 —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **7.** Stale-identity warning keyed on the symptom, evaluated after `GET /sessions` —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)
- [x] **8.** Warnings are a third channel; the exit code does not move —
      [#16](https://github.com/skolima/gnucash-ofx/issues/16)

Mapped onto the issue's own parts: (a) nothing recorded what had been fetched → decisions 1–4; (b)
consent expiry was pull-only → decisions 5–6; (c) state predating the IBAN-based `ACCTID` →
decision 7, keyed on the **symptom**, not the schema version, which §6 shows would be wrong on 5 of
the 6 banks here; where warnings go → decision 8.

All eight shipped in one PR, [#16](https://github.com/skolima/gnucash-ofx/issues/16), closing
[#6](https://github.com/skolima/gnucash-ofx/issues/6). Nothing here was blocked, and nothing
here needed a live request to settle beyond §9's five-request probe of the history horizon. The one
number that was a policy pick rather than a measurement is the consent threshold, and §5 gives the
arithmetic it was picked against.

---

## Context

The three parts of [#6](https://github.com/skolima/gnucash-ofx/issues/6) look like three
features. They are one: **every case where the tool holds the information needed to warn you, and
says nothing, and the silence is what costs you the transactions.**

The urgency is not general. Alior, Alior Kantor and Erste serve roughly 90 days of history and
`400 ASPSP_ERROR` on any window reaching further back (see
[`enable-banking.md`](enable-banking.md)). At those three banks a missed period is not a chore
deferred — it is data that ages out and cannot be fetched at any price, by this tool or by hand.
Millennium and Wise serve much more, so the same mistake there is recoverable whenever it is
noticed. The tool currently treats both the same way, which is to say it treats neither.

What exists today:

- The **cache** knows which account-months it holds — but as a rate-limit shield, and §1 shows it
  actively forgets coverage it once had.
- **`state/<bank>.json`** knows the session, the accounts, and `valid_until`; `days_until_expiry()`
  is already written and already called by `status` and `--dry-run`.
- **`fetch`** knows the window it was asked for, and asks for exactly that, every time, with no
  memory of the last one.

So a mistyped `--from`, a month never run, or a consent that lapsed while nobody was looking all
produce the same output as a correct run: some files, exit 0, no complaint. The files even look
right — they are named for the window that *was* asked for.

---

## Constraints, measured rather than assumed

Everything below is from this machine's `state/` and `cache/` on 2026-08-09: 6 banks, 19 accounts
in current sessions, 157 month chunks in the cache. No amounts, counterparties or account numbers
appear in any of it.

### 1. The cache cannot be the coverage record, because it forgets

The month-chunk layout stores `covered_from`/`covered_to` per account-month, which looks exactly
like a coverage record. It is not one, and the difference is not academic — `save_cached_month`
overwrites a month's chunk with whatever the latest request covered, wider *or narrower*.
Simulated against the real `cache.py` functions in a temp dir:

| Step | Chunk for `2026-07` records |
|---|---|
| `--from 2026-07-01 --to 2026-07-24` | `2026-07-01 .. 2026-07-24` |
| then `--from 2026-07-25 --to 2026-07-31` | `2026-07-25 .. 2026-07-31` |

and `cached_window(2026-07-01, 2026-07-31)` afterwards reports July as **missing**, for days that
were fetched and paid for. This is not a bug in the cache: a month is its unit of *retrieval*, the
second request legitimately re-fetched what it needed, and the chunk honestly describes what the
file holds. But it means the cache's answer to "was this period fetched?" is **no lower bound at
all**, in the direction that matters.

Three further reasons the cache cannot carry this, each independently sufficient:

- **It is keyed on the account UID**, which Enable Banking regenerates on every re-link (§4).
- **It is disposable by design.** Deleting `cache/` is a supported, if expensive, recovery step.
  Coverage deleted with it would silently reset to "nothing known", which under decision 1's
  absent-means-unknown rule means *no warnings* — the failure mode returning by way of a cleanup.
- **A settled month never expires** (decision 6 of the rate-limit ADR), so a cache hit will
  outlive any TTL-based reasoning about whether it is still evidence. It is evidence of a fetch,
  but only of the fetch that wrote it last.

### 2. The gaps are already there, and nothing said a word

Across the 43 account digests present in the cache, **8 have at least one uncovered day inside
their own cached span** — 130 such days in total. On the accounts of one bank the recorded coverage
runs `… 2026-06-29`, then `2026-07-01 …`, then stops at `2026-07-24` and resumes `2026-08-01`.

**That number is an upper bound, not a finding of loss** — precisely because of §1. Some of those
days may have been fetched by an earlier, wider request whose record was overwritten. That is the
point worth taking from it: **the tool cannot currently answer its own question.** Neither the user
nor this ADR can determine, from what is on disk, whether those 130 days were ever fetched. A
ledger is what turns that into a yes or a no.

The reconstructed run history says the same thing from the other side. Clustering chunk timestamps
into sessions gives fetches on 2026-06-30, 2026-08-07, 2026-08-08 and 2026-08-09, with windows
including `2026-06-01..2026-06-25`, `2026-01-01..2026-01-30`, `2026-02-01..2026-02-24`,
`2026-03-01..2026-03-21`, `2026-04-11..2026-04-30` and `2026-05-06..2026-05-31`. Windows that stop
on the 21st, 24th, 25th and 30th of a month, and resume on the 1st or the 11th of the next. Whether
each was deliberate is unknowable and beside the point: **the tool's behaviour is identical either
way**, and a monthly re-type is exactly how a window ends up starting on the 11th.

### 3. Widening the window is free where coverage is complete, and costs only the months that are missing

The obvious objection to defaulting to a wide "since last covered, with overlap" window is that it
re-spends the allowance the rate-limit ADR just finished protecting. Measured, over all 19 live
accounts, as the number of month-requests `cached_window` would still demand today:

| Window | Month-requests over 19 accounts |
|---|---|
| last 30 days | 38 |
| last 90 days | 53 |
| since 2026-01-01 | 108 |

But the distribution is the whole story. For the two banks whose cached coverage is complete
(Millennium and `wise_personal`, 6 accounts), **all three windows cost exactly 2 month-requests per
account** — the moving tail, which any window pays for. Widening from 30 days to eight months costs
them nothing, because a settled month does not expire. The 55 extra requests at the union window
land entirely on accounts with recorded holes: they are the price of the repair, not overhead.

So the overlap margin is close to free by construction, and it is free for exactly the reason
decision 6 of the rate-limit ADR exists. The asymmetry the issue asserts — overlap is cheap, a gap
is permanent — holds numerically, not just rhetorically.

### 4. A re-link is what resets anything keyed on the UID, and it has already happened repeatedly here

Of the 43 account digests in the cache, **24 belong to no current session** — 45 of the 157 chunks,
29% of the cache, stranded under UIDs that no longer exist. They were written between 2026-06-30
and 2026-08-09, i.e. this is not archaeology; it is the ordinary consequence of re-linking, which
this deployment did as recently as today.

Two things follow.

- **The ledger must be keyed on the same value as `ACCTID`**, not on the UID. Consent lasts ~180
  days and the history horizon is ~90; a re-link therefore falls *inside* the period a coverage
  warning is supposed to protect, and a UID-keyed ledger would blank itself at precisely the moment
  its warning mattered.
- **Keying it on `ACCTID` also makes it impossible for the ledger to lie.** `ACCTID` is what
  GnuCash derives `online_id` from. If it changes, GnuCash sees a new account and the ledger sees
  an unknown one — the two failures are the same failure, and the ledger can never claim coverage
  for an account GnuCash considers new.

The stranded chunks also price decision 5's advice to re-link early: the 112 chunks currently
serving live accounts all become unreachable at the next re-link, and the fetch after it re-pays
for every month it wants. Re-linking is not free, which is an argument against a large warning
threshold, not for one.

### 5. The consent clock, against the run cadence and the history horizon

`link` requests the ASPSP's advertised maximum, 180 days for every bank here. Current state: four
of six consents expire on the same day, 88 days out; a fifth at 78; the sixth (re-linked today) at
179. Four expiring together means one warning implies four SCA dances, which is a reason to warn
with enough lead time to spread them.

When does a lapsed consent actually cost data? With `H` the bank's history horizon (~90 days at the
three that age out), `I` the interval between runs, and `R` the user's reaction time between
noticing and completing the browser dance, a consent expiring at `E` is noticed at worst at `E + I`
and repaired at `E + I + R`; the oldest fetchable date is then `E + I + R − H`, while coverage
reaches back at worst to `E − I`. **Data is lost when `2I + R > H`.**

Measured cadence: fetches happened on 4 distinct days in the last ~6 weeks, the longest gap between
them being **38 days**. Taking `I = 38` and `H = 90` gives loss at `R > 14` — a fortnight of
procrastination, which is not a comfortable margin, and the whole point of a warning is to make `R`
irrelevant by moving the repair to *before* `E`.

For the warning to be seen at all it must fire at least one run before expiry, so `N ≥ I`. For it
to be actionable it needs the reaction time too: **`N = 45`** (38 + a week). The cost of a larger
`N` is more frequent re-linking, since a user who acts on the first warning re-links every
`180 − N` days: 2.4 links/bank/year at `N = 30`, 2.7 at `N = 45`, 3.0 at `N = 60` — and each one
strands that bank's cache (§4). 45 buys the lead time for about two-thirds of one extra SCA dance
per bank per year.

`state/fetch-log.jsonl` does not exist on this machine: no `fetch` has run since the run log
shipped in [#10](https://github.com/skolima/gnucash-ofx/issues/10). So the cadence above is
reconstructed from cache timestamps and is the best evidence available; the run log will give a
better `I` in a few months, and `N` is worth re-checking against it then.

### 6. Part (c)'s condition is not the schema version — checking that would be wrong on 5 of 6 banks

| Bank | `version` in state | accounts | with IBAN | with `identification_hash` | with currency | would resolve `ACCTID` to `uid` |
|---|---|---|---|---|---|---|
| alior | absent (v1) | 5 | 5 | 0 | 0 | **0** |
| alior_kantor | absent (v1) | 3 | 3 | 0 | 0 | **0** |
| erste | absent (v1) | 2 | 2 | 0 | 0 | **0** |
| millennium | absent (v1) | 2 | 2 | 0 | 0 | **0** |
| wise_personal | absent (v1) | 4 | 4 | 0 | 0 | **0** |
| wise_business | 2 | 3 | 3 | 3 | 3 | **0** |

**Five of six state files are on the pre-`accounts` layout, and not one of the 19 accounts is in
the condition that orphans anything.** They all carry `account_ibans`, so `ACCTID` resolves to an
IBAN exactly as it does under the current schema. A literal "warn when the state file is old" check
would fire on five banks, tell the user to re-link all of them, cost five SCA dances and five
stranded caches (§4), and fix nothing. The rate-limit ADR's §7 already recorded this — *"they carry
`account_ibans`, so `ACCTID` still resolves"* — and this table is the count behind it.

The harmful condition is narrower and is a property of one account, not of a file: `_known_acctid`
returns the bare `uid` only when the account has **no** stored IBAN **and** no
`identification_hash` from either link time or the fetch-time `accounts_data` array. Two
consequences:

- The check must run **after** `GET /sessions`, because `accounts_data` is where a v1-schema
  account's stable hash comes from — and `GET /sessions` is a call `fetch` already makes, so this
  still costs nothing.
- **`--dry-run` cannot perform this check.** It passes `{}` for the hashes because it may not call
  anything, so it would over-report. The warning is fetch-only, and honestly so.

There is also a real but *different* condition visible in that table: 16 of 19 accounts have no
link-time currency. That is what makes `--dry-run` suppress those banks entirely and what keeps
`/balances` being called on closed windows purely to learn a currency. It is a cost, not a data
loss, and it is already reported where it bites. Folding the two into one warning would put "you
will orphan your accounts" and "your dry run is less useful" behind the same sentence.

### 7. The coverage record already exists once per file, and nothing accumulates it

OFX's own client/server model assumes the client remembers what it has downloaded. From
`ofx160.dtd` as shipped with GnuCash for Windows:

```
2275: <!ELEMENT INCTRAN       - - (DTSTART? , DTEND? , INCLUDE) >
1143: <!ELEMENT BANKTRANLIST  - - (DTSTART , DTEND , STMTTRN*)>
2018: <!ELEMENT ACCTINFORQ    - - (DTACCTUP, SVC2*)>
```

A direct-connect client asks for transactions *since* a date it kept (`INCTRAN`), the server states
the range it actually answered with (`BANKTRANLIST`), and account discovery is itself keyed on a
remembered timestamp (`DTACCTUP`). Every file this tool writes already carries its own
`BANKTRANLIST` `DTSTART`/`DTEND`. What is missing is not the concept — it is that nobody keeps the
running total, because this tool is the producer and GnuCash's import matcher works per file.

That suggests deriving coverage from `output/` instead, and it does not survive contact with two
facts. `write_account_ofx` **returns `None` and writes no file when an account has no transactions
in the window** — so the accounts with the quietest months, where a gap is hardest to spot, would
leave no trace at all — and `output/` is a working directory the user moves files out of after
importing. Filenames cannot distinguish "fetched, nothing there" from "never fetched", which is the
only distinction that matters.

### 8. There is no channel for a warning, and `status` has no way to signal

`FetchReport` has exactly two things in it: `written` and `failures`. `failures` is what makes
`_report` return 1. Progress lines are a callback with no severity. So today a warning can only be
a failure (wrong: the fetch succeeded, and #6 forbids it), or a progress line indistinguishable
from `fetching transactions...` (invisible in a run that prints one line per account).

`status_lines` returns strings and `_cmd_status` returns a literal `0` in every case, including for
a bank it just described as `consent EXPIRED`. A scheduled monthly run has nothing to test.

### 9. Exactly 90 days back is served, at both banks that age out

The one thing here that no local file could answer: `enable-banking.md` says history reaches back
"~90 days" at Alior, Alior Kantor and Erste, and a default window has to land on the right side of
that boundary or it turns every bare `fetch` into a `400`. Probed 2026-08-09 in online mode, one
account per bank, single-day windows so the response is minimal, a control request first so a
refusal is attributable:

| Bank | `date_from` | Result |
|---|---|---|
| alior | today − 30 (2026-07-10) | `200` — control |
| alior | today − 90 (2026-05-11) | **`200` — served** |
| alior | today − 120 (2026-04-11) | `400 ASPSP_ERROR` |
| erste | today − 30 (2026-07-10) | `200` — control |
| erste | today − 90 (2026-05-11) | **`200` — served** |

**90 days back is inside the window, not on the wrong side of it.** The `~` in the docs was doing
real work — it left open whether 90 was the last day served or the first day refused, and it is the
last day served.

The exact edge between 90 and 120 was **not** narrowed, deliberately. Each bisection step is
another counted request at the ASPSP whose allowance this entire design exists to protect, and no
decision here depends on the answer: the default never wants to reach past 90, and anything deeper
is the user typing an explicit `--from` and finding out. Alior Kantor was not probed either — same
institution, same `aspsp` in `config.toml`, and plausibly the same allowance (the open question of
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md)), so a probe there would risk the
budget to re-learn Alior's answer.

The 5 requests are in `state/fetch-log.jsonl` under `command: "probe-history-horizon"` — which is
also the first content that file has ever had, so a future reader of §5's cadence should know these
records are a probe and not a fetch.

---

## Options considered

### A. Do nothing

**Rejected.** §2 is the argument: the tool cannot answer whether 130 recorded-uncovered days were
ever fetched, and at three of six banks the answer expires.

### B. Derive coverage from the cache

**Rejected on §1.** It forgets coverage on re-fetch, it is keyed on an identity that dies every
~180 days, and it is the one directory the user is told they may delete. Two of those three could
be worked around; the first cannot, because the information is gone.

### C. Derive coverage from the filenames in `output/`

**Rejected on §7.** An account with no transactions writes no file, so the silence that most needs
recording is exactly what is not recorded, and the directory is not ours to depend on.

### D. Keep coverage inside `state/<bank>.json`

Tempting: one file per bank already exists, `_write_atomic` already protects it, and `load_session`
already surfaces corruption as a per-bank problem.

**Rejected.** That file is currently written **once per link** and its loss costs a browser SCA
dance — `state.py` says so at `_write_atomic`. Coverage is written **once per account per fetch**,
which turns a rarely-touched file into a frequently-rewritten one, and a `StateError` on it fails
the whole bank. A ledger whose corruption can cost a consent has the risk exactly backwards: the
ledger must be the disposable one.

### E. Store coverage as month chunks, mirroring the cache

Appealing for symmetry, and it would let the cache and the ledger share `months_in`/`month_end`.

**Rejected.** A month map cannot express two disjoint spans inside one month (`07-01..07-05` and
`07-20..07-25`), so it must either widen — claiming coverage it does not have, which is the one
thing a warning system may never do — or keep the last, which is §1's forgetting reproduced in the
component built to fix it. The cache stores months because a month is the unit of **retrieval**;
the ledger stores days because a day is the unit of **loss**. They are different units for good
reason and should not be unified.

### F. Change `status`'s exit code by default

**Rejected**, per the issue's own leaning. `status` is documented in the README as a report, it has
always exited 0, and `gnucash-ofx status && …` is a plausible thing to have written. The change
would be a silent break for the one kind of user who cares. Behind `--check` it also gets a place
to define *what* counts as attention, which will grow.

### G. A per-account default window rather than a per-bank one

Considered because coverage is per account, so the natural default is too. **Rejected for now**:
the window is currently one value per run, and it reaches into the OFX statement dates, the
filename, `require_ordered_window`, the run log's `run` record and `--dry-run`'s prediction.
Per-bank resolution (decision 3) gets nearly all of the benefit — §3 shows a well-covered account
pays nothing to be dragged back to a sibling's start — without a window that varies inside a single
bank's output.

### H. A per-account ledger of day intervals in `state/`, an implied window, and three warnings

**Accepted** — below.

---

## Decision

### 1. A coverage ledger at `state/coverage/<bank>.json`, keyed by a digest of `ACCTID`

One file per bank, in its own subdirectory so that `state/*.json` keeps meaning "session files" for
any script or human that has learned to read it that way. Written with the same
temp-file-plus-`os.replace` dance as `save_session`.

```json
{
  "version": 1,
  "bank": "alior",
  "accounts": {
    "c0ffee1234abcd56": {
      "covered": [["2026-05-01", "2026-06-29"], ["2026-07-01", "2026-08-08"]],
      "last_fetch": "2026-08-08T10:11:02+00:00"
    }
  }
}
```

- **The key is `sha1(ACCTID)[:16]`, not the `ACCTID`.** §4 requires the identity to be `ACCTID`;
  the digest keeps that property while holding no account number, so this file is genuinely
  pasteable into an issue — which matters more here than for `state/<bank>.json`, because a file of
  dates *looks* harmless and will be pasted. Same reasoning, and the same helper shape, as the
  cache's `_uid_digest` and the run log's redaction. Cost: it cannot be read by eye; `--dry-run`
  and `status` render it back to a redacted `ACCTID` from the session.
- **Merged, sorted, inclusive day intervals.** Merged on write, with adjacent intervals coalescing,
  so the list stays one or two entries in normal use and a gap is literally a hole in it.
- **Dates and digests only.** No amounts, no transaction data, no counterparties, no account
  numbers — the run log's rule, for the same reason.
- **Absent, unreadable or wrong-version means *unknown*, never "gap since the epoch."** Every
  existing bank has no ledger; the first run after upgrade must warn about nothing. An unknown
  account is simply not warned about, and its first successful fetch starts its record.
- **The ledger is not seeded from the cache.** It could be — the UID is known at fetch time, so
  `m-<digest>-*.json` chunks could be attributed to an `ACCTID` — but §1 says a chunk records only
  the last window that touched its month, so seeding would import fabricated gaps into the one
  component whose value is that its gaps are real. One blind period is cheaper than a wall of
  warnings nobody can act on.

### 2. Only a successful fetch advances coverage, and it is written per account as it lands

- **Success means the account's transactions were fetched *and* mapped** — the point where
  `fetch_bank` appends to `pending`. Not the point where a file is written: an account with no
  transactions in the window writes no file (§7) and is nonetheless fully covered. Recording
  coverage at the write would be a self-inflicted version of the very gap this ADR is about.
- **A failed account advances nothing**, matching the cache: a failure caches nothing today and
  must record nothing either. Nor does the currency-unknown skip, which produces no data.
- **Cache hits count.** Serving a month from cache is a period we hold; the ledger records what is
  known, not what was paid for. (What was paid for is the run log's job.)
- **Written per account, not once per bank at the end.** Same argument as `save_cached_month`: a
  rate-limited sibling must not discard the coverage of the accounts that already succeeded, and
  that failure mode lands hardest on the bank that can least afford the re-fetch.
- **`--dry-run` never writes it**, structurally — it fetches nothing, so it knows nothing new.
  `--refresh` does advance it: it is a successful fetch.
- **A write failure is a warning, never a failed fetch.** `RunLog`'s rule, with one deliberate
  difference: the run log swallows `OSError` silently because losing a diagnostic costs nothing,
  while losing a coverage write costs a re-fetch on every subsequent run. So it degrades to unknown
  *and says so*.

### 3. `--from` and `--to` become optional, and the default window is resolved per bank

- `--to` defaults to **today**.
- `--from` defaults to **(the earliest `covered` end across that bank's known accounts) −
  `LATE_BOOKING_MARGIN`**, the constant defined in `cache.py` and imported, not re-picked — the
  rate-limit ADR's decision 6 reserved it for exactly this. A bank can book a transaction with a
  booking date days before it appears; §3 measures the re-fetch as free where coverage is complete.
- When **no** account of a bank has a record, `--from` defaults to a **fixed lookback of 89 days**.
  §9 measured 90 as served at both banks that age out, so the ceiling is 90 and not less. The one
  day held back is not a guess at the boundary — it is the gap between the local `date.today()`
  this tool computes from and whatever day the ASPSP thinks it is. West of UTC those differ, in the
  direction that makes the local answer one day too old, and a default that `400`s is not a
  default. One day of horizon is a cheap price for removing the only way this default can fail, and
  it is the same reasoning as `_CONSENT_SAFETY_MARGIN` in `start_link` — the difference being that
  this one now sits one day inside a *measured* boundary rather than an assumed one.
- The same 89-day clamp applies to a computed default that would reach further back. **The clamp is
  reported, not silent**: "coverage ends 2026-04-01; the oldest window this bank will serve starts
  2026-05-12; 40 days cannot be recovered by default — pass `--from` if this bank serves more."
- **Per bank, not per run.** Otherwise Millennium's eight months of history would drag Alior's
  window past its horizon and turn a working fetch into a `400` — the same trap `_request_spans`
  documents. An **explicit** `--from`/`--to` still applies to the whole run and is **never
  clamped**: the user knows their bank.
- **One resolution function, called by `fetch_enablebanking` and `dry_run_enablebanking`**, as
  `require_ordered_window` already is. A dry run that predicts filenames for a different window
  than the fetch will use is worse than no dry run — and since the resolved window now depends on
  local state rather than on the command line, this is the property that keeps `--dry-run` the
  cheap way to see what a bare `fetch` is about to do. The resolved window is printed per bank on
  stderr in both.
- The run log's `run` record carries the window as given; the resolved per-bank window is recorded
  where the bank's requests are, so a log read months later still shows what was actually asked for.

### 4. The gap warning, before anything is spent, escalated by whether the gap can still be recovered

Computed from the ledger and the resolved window alone — both local — and therefore emitted
**before** the first request, and equally available in `--dry-run`.

- **Plain warning** when the window leaves any day uncovered against a known record: the account
  (redacted), the uncovered range, and the `--from` that would close it.
- **Escalated** when an uncovered day is still inside the recoverable horizon but will leave it:
  name the date it becomes unrecoverable. The horizon used here is **90 days for every bank** — the
  measured value (§9), not the 89-day default of decision 3, because that extra day is a margin on
  what we *ask for* and has no business shortening what we tell the user is still recoverable. It
  is phrased as what it is — the PSD2 floor that every ASPSP guarantees — rather than as a claim
  about that bank. A per-bank `history_days` option can raise it later for Millennium and Wise; it is not
  needed for the warning to be true, and adding config surface that must be right for the warning
  to be safe is the wrong dependency.
- **Stated, not escalated**, when the gap is already older than the horizon: it says so, once. A
  warning that repeats forever about something nobody can fix is how a user learns to ignore
  warnings.
- Gaps **inside** the recorded coverage are reported too, not only the one between the last covered
  day and `--from`. This is what §2's holes are, and a high-water mark alone would have forgotten
  them.

### 5. Consent expiry is warned at 45 days, from wherever the state is already loaded

`fetch`, `status` and `fetch --dry-run` all already load `valid_until` and all already call
`days_until_expiry()`. `CONSENT_WARNING_DAYS = 45`, one constant, derived in §5 and documented at
its definition as what it is: a policy pick against a measured run cadence, to be re-checked once
`state/fetch-log.jsonl` has a few months of real intervals in it. The message names the bank, the
days remaining and `gnucash-ofx link <bank>`.

### 6. `status --check` exits 1 when a bank needs attention; bare `status` still exits 0

Behind a flag, per option F. "Needs attention" is: not linked, consent expired, consent within
`CONSENT_WARNING_DAYS`, an unreadable state file, or a recorded coverage gap still inside the
recoverable horizon. `status` gains a coverage line per bank, from the ledger, costing nothing.

### 7. Stale-identity warning keyed on the symptom, evaluated after `GET /sessions`

Warn when an account's `ACCTID` resolves to the bare `uid` — that is, no stored IBAN and no
`identification_hash` from link time or `accounts_data` — naming the bank, the count of affected
accounts, and `gnucash-ofx link <bank>`. **Not** a schema-version check (§6). Fetch-only, because
only a fetch has the fetch-time hashes; `--dry-run` says nothing rather than over-reporting.

**What `ACCTID` resolves to does not change.** Changing it for these accounts would orphan exactly
the history the warning exists to protect. The warning is the whole intervention.

### 8. Warnings are a third channel, and the exit code does not move

`FetchReport` gains `warnings`, a list of a small `FetchWarning(bank_key, kind, message,
account=None)` — a distinct type rather than a reused `BankFailure`, so that "this did not fail"
lives in the type and cannot be lost by a future edit to `_report`. Rendered to **stderr**, grouped
by bank, collapsing identical per-account messages the way `_dry_run_problem_lines` already does.
`fetch` still exits 1 only when a bank actually failed, and 0 with warnings present.

### 9. Untouched

`FITID`, `BANKID`, `ACCTID` resolution, the `NAME`/`MEMO`/`CHECKNUM` composition, stdout as the
file list, per-account failure isolation, the cache's keys and semantics, `ofxout.py` entirely.

### 10. Out of scope, including two things found while measuring

- **Re-keying the *cache* on `ACCTID`.** §4 found 45 of 157 chunks stranded by re-links, 29% of the
  cache, and the fetch after every re-link re-pays for everything. Fixing that is real and is not
  this: the cache is a rate-limit concern, belongs to
  [#9](https://github.com/skolima/gnucash-ofx/issues/9)'s domain, and the ledger deliberately
  does not depend on it.
- **Pruning stranded chunks.** Nothing deletes them; nothing needs to yet.
- **Per-bank `history_days`**, per decision 4.
- **Warning that an account has no link-time currency** (§6) — a cost, not a loss, already surfaced
  by `--dry-run`.

---

## Consequences

**Accepted costs.**

- `state/` gains a file per bank that is rewritten on every fetch. It is the one local file whose
  loss costs only a re-fetch, and that is deliberate (option D).
- The default window makes a bare `fetch` spend the allowance without the user naming a range.
  §3 bounds it — a fully covered account pays the same two month-requests it always did — and
  `--dry-run` shows the resolved window for free before any of it is spent. That interlock is why
  the default is safe, and it is why decision 3 insists both commands resolve it in one function.
- A first run after upgrade warns about nothing, on every bank, by design (decision 1). Coverage
  starts accruing from that run.
- Digesting the ledger key costs human readability of the file; `status` and `--dry-run` are the
  way to read it.
- 45 days of warning means a user who acts immediately re-links about 2.7 times a year per bank
  instead of 2.0, and each re-link strands that bank's cache (§4).
- More stderr. The stdout contract is untouched, but a run with several warnings is noisier, and
  the grouping in decision 8 is what keeps that from becoming unreadable.

**Cost to reverse: low.** Nothing here touches `FITID`, `BANKID` or `ACCTID`, so no account orphans
and no transaction re-imports. Backing it out means deleting `state/coverage/` and restoring
`required=True` on two arguments.

---

## To verify before implementing

- **Is there any bank in the wild in part (c)'s condition?** Not on this machine: 0 of 19 accounts
  (§6). So decision 7 must be tested against a **synthetic** state file — `account_ids` present,
  `account_ibans` absent — plus a session response with no `accounts_data`. Both halves matter;
  testing only the state file would pass a check that the fetch-time hash makes unnecessary.
- **Does a bank ever answer a window with less than it was asked for, and 200?** Coverage records
  "this window was fetched successfully", which is the strongest claim available: a truncated
  history and a quiet month are indistinguishable in the response. Millennium ignoring date filters
  is the known case and errs the other way (it returns more). If a bank is ever found to silently
  serve short, the ledger would record coverage it does not have — the only failure mode in this
  design that is worse than silence, and worth watching for.
- **`N = 45` against a real `I`.** Re-derive from `state/fetch-log.jsonl` once it has a few months
  of `run` records; §5's cadence is reconstructed from cache timestamps because no fetch has run
  since the run log shipped.

Settled by measurement, recorded so they are not re-derived:

- ~~Does Alior (or Erste) accept a window starting exactly 90 days back?~~ **Yes, both** — and
  Alior refuses 120 (§9). The default holds one day back for clock skew, not for the boundary.
- ~~Can the cache serve as the coverage ledger?~~ **No** — it overwrites a month's recorded
  coverage with the latest, narrower window (§1, simulated against the real functions).
- ~~Is a wide default window expensive?~~ **Not where coverage is complete** — 2 month-requests per
  account whether asked for 30 days or 8 months (§3).
- ~~Would a state-schema check find part (c)'s banks?~~ **No** — it would fire on 5 of 6 banks and
  be wrong on all of them (§6).
- ~~Are there gaps in what has actually been fetched?~~ **The cache records 130 uncovered days
  across 8 of 43 account digests, and cannot say whether they are real** (§1, §2). That
  indeterminacy is the finding.

## References

- **Implemented by:** [#16](https://github.com/skolima/gnucash-ofx/issues/16) (all eight decisions).
- [#6](https://github.com/skolima/gnucash-ofx/issues/6) — the issue, and the source of the
  constraints this ADR sharpens rather than invents.
- [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — decision 5 (month chunks),
  decision 6 (`LATE_BOOKING_MARGIN`, shared not re-picked), §7 (this file's subject declared out of
  scope, and the `account_ibans` observation §6 counts).
- [`decisions.md`](decisions.md) — *A settled month does not expire*; *`--dry-run` takes no client*;
  *Everything the link response carries is captured*; *Partial success is reported, not thrown
  away*.
- [`enable-banking.md`](enable-banking.md) — the ~90-day history horizon at Alior, Alior Kantor and
  Erste; UID regeneration on re-link; `accounts_data` as the fetch-time source of
  `identification_hash`; 180-day `maximum_consent_validity`.
- `ofx160.dtd` as shipped with GnuCash for Windows, lines 1143, 2018, 2020 and 2275 — OFX's own
  assumption that a client remembers what it downloaded.
