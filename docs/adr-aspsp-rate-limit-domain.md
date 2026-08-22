# ADR: the ASPSP is the rate-limit domain, and the cache stores months rather than requests

**Status:** **Accepted. Six of eight decisions implemented; decisions 1 and 3 are parked pending
evidence, not pending work.** Everything that does not rest on the shared-allowance premise has
shipped. What remains needs a real `429` to occur in ordinary use — see *Parked: what closes this*
at the end, which is written to be picked up cold.
**Date:** 2026-08-09. Measurements in §1–§7 taken the same day against the local cache, the OFX
DTD, the live Enable Banking platform endpoints, and GnuCash/libofx source.
**Scope:** `config.py` (one derived key), `run.py` (when `/balances` is called, and the request
planner), `sources/enablebanking.py` (retry policy for one error code), `cache.py` (rewritten
around a different storage unit), `runlog.py` (new). `ofxout.py` is untouched. The implemented
parts have condensed into [`decisions.md`](decisions.md); this file stays because it holds the
rejected options, the measurements, and the two open questions.
**Issue:** [#9](https://github.com/skolima/gnucash-ofx/issues/9). Depends on nothing;
[#6](https://github.com/skolima/gnucash-ofx/issues/6) depends on decision 5 of this, and shares
`LATE_BOOKING_MARGIN` with decision 6.

---

## Where this stands

- [ ] **1.** Rate-limit domain is `(aspsp, country, psu_type)` — exists as
      `BankConfig.rate_limit_domain`, recorded in the run log as a **label only**; parked until
      decision 3 lands, together with it
- [x] **2.** `ASPSP_RATE_LIMIT_EXCEEDED` is never retried —
      [#12](https://github.com/skolima/gnucash-ofx/issues/12)
- [ ] **3.** The exhaustion mark gates network calls, not banks — **Parked.** Blocked on the
      shared-allowance premise; see *Parked: what closes this*
- [x] **4.** `/balances` only when the window is still open —
      [#11](https://github.com/skolima/gnucash-ofx/issues/11)
- [x] **5.** The cache stores calendar months, persisted as they land —
      [#12](https://github.com/skolima/gnucash-ofx/issues/12)
- [x] **6.** Age-based TTL; a settled month does not expire —
      [#12](https://github.com/skolima/gnucash-ofx/issues/12)
- [ ] **7.** Coverage ledger out of scope here — shipped separately as
      [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md),
      [#16](https://github.com/skolima/gnucash-ofx/issues/16), closing
      [#6](https://github.com/skolima/gnucash-ofx/issues/6)
- [ ] **8.** Identity fields untouched — not a decision to ship; recorded so nothing here is
      mistaken for having changed it

Plus one thing this ADR did not originally contain: the run log
([#10](https://github.com/skolima/gnucash-ofx/issues/10)), which exists because §7 found that the
tool was discarding the evidence needed to settle its own premise.

**Nothing further is blocked.** Decisions 1 and 3 are the only part that needs the premise, and
they are separable: if it turns out false, decision 3 degrades to a per-connection mark, which
still stops a connection re-probing an allowance it has already been refused.

---

## Context

`429 ASPSP_RATE_LIMIT_EXCEEDED` hits the two Alior connections far more than the other banks, and
the cause is structural. `alior` and `alior_kantor` are two connections to **one ASPSP** — both
`aspsp = "Alior Bank"` in [`config.toml`](../config.toml) — over disjoint sets of accounts. The
tool has no notion of a rate-limit domain at all: it has bank keys, and it treats two connections
to the same institution as unrelated banks with unrelated allowances.

Every account costs one `/balances` call plus one `/transactions` call per 90-day chunk. So for
`N` accounts across the two connections over a `W`-day window, `fetch --bank all` sends
`N × (1 + ceil(W/90))` requests at that single ASPSP, back to back, with nothing pacing them and
nothing accounting for the shared budget. It is driven by **account count against one
institution**, not by consent count — which is why merging the two connections would not help
even if the bank permitted it, and why the banks with fewer accounts per ASPSP never trip it.

Three things then compound it: half those requests fetch a balance that cannot be correct for the
window being written; the failure path spends *more* counted requests on an allowance already
known to be gone; and the cache is keyed so tightly that the near-misses of ordinary use miss it
entirely.

---

## Constraints, measured rather than assumed

### 1. `/balances` is a live balance, always, and we write it as a historical one

Over every balance record in the local cache:

| Measure | Result |
|---|---|
| `reference_date` vs the date of the fetch | **identical in every record that carries one** (difference 0 days, no exceptions) |
| `reference_date` vs the requested `date_to` | **after it in 73 of 90**; equal in the other 17 (the fetches whose window ended today) |
| `balance_type` on the records that carry a `reference_date` | `ITAV` — *interim available* — in **all** of them; the `ITBD` and `CLBD` records carry no `reference_date` at all |
| Cached fetches whose `date_to` was already in the past when made | **121 of 154** |

So the ASPSP answers "what is this account's balance *now*". There is no historical-balance
endpoint and no parameter that asks for one — `GET /accounts/{uid}` is 404 in Restricted Mode, and
`/balances` takes no date. **A true closing balance for a past window is not obtainable from this
API at all**, from any call at any price.

What we do with it makes that worse rather than neutral. `ofxstatement`'s `OfxWriter` writes

```python
tb.start("LEDGERBAL", {})
self.buildAmount("BALAMT", self.statement.end_balance, False)
self.buildDateTime("DTASOF", self.statement.end_date, False)
```

— `DTASOF` from the statement's **end date**, i.e. the requested `date_to`. And `ofxout.py:279-286`
back-computes the opening balance from it (`start_balance = end_balance - total`). A fetch for a
window that closed weeks ago therefore asserts *today's live balance as the ledger balance on that
past date*, and derives a wrong opening balance from it. In the common case — 121 of 154 fetches —
both numbers in the file are wrong.

GnuCash consumes exactly this. In `gnc-ofx-import.cpp`:

```c
if (account && statement->ledger_balance_valid)
{
    gnc_numeric value = double_to_gnc_numeric (statement->ledger_balance, …);
    RecnWindow* rec_window = recnWindowWithBalance (…, account, value,
                                                    statement->ledger_balance_date);
```

`LEDGERBAL` **pre-fills the reconcile dialog**, with our `BALAMT` and our `DTASOF`. So the value is
not inert documentation: it is handed to the user as the figure to reconcile a historical statement
against, and it is the wrong one. This is a correctness defect in its own right, independent of any
request saving.

**But the tag cannot simply be dropped.** `ofx160.dtd` line 921:

```
<!ELEMENT STMTRS - - (CURDEF , BANKACCTFROM , BANKTRANLIST? , LEDGERBAL , AVAILBAL? , MKTGINFO?)>
```

`BANKTRANLIST` is optional; `LEDGERBAL` is not. Omitting it produces a file that is not OFX.

### 2. The retry ladder spends the exhausted budget it is waiting on

`_request` (`enablebanking.py:448-457`) retries any 429 up to `_MAX_RETRIES = 5` times on a
1→2→4→8→16s ladder: **six counted requests and ~31s of sleeping for one logical call.** Against a
cap whose documented recovery is ~6 hours ([`enable-banking.md`](enable-banking.md)), all five
retries are guaranteed to fail — they cannot outlast the cap by three orders of magnitude — and
each one is a request sent at an ASPSP that has just said it has had enough.

The issue's description of what follows is not what the code does, and the correction narrows the
problem usefully. `fetch_bank` **does** stop after the first 429 (`run.py:566-568`, `break`), so the
remaining accounts of that bank are not each retried — [`decisions.md`](decisions.md) already
records that a 429 abandons the whole bank. What nothing stops is the **sibling connection**:
`alior_kantor` then runs from scratch against the same exhausted ASPSP, and pays the same 31s and
the same six requests before reaching the same conclusion. Two connections, ~62s of sleeping and
~12 useless counted requests, to learn something the first 429 already said.

Whether a 429 itself counts against the allowance is not documented and not something we can
observe from outside. It is assumed to, because the alternative — retrying is free — is the
assumption that costs data if wrong.

### 3. The cache key misses what real use actually asks for

`_key` (`cache.py:39-41`) hashes the exact `(uid, date_from, date_to)` triple, so cache hits require
the *identical* window. Over the local cache:

| Measure | Result |
|---|---|
| Distinct `(uid, window)` entries | 154 |
| Entries whose window is **fully covered** by another entry for the same account | **83** |
| Distinct window starts / ends across all of them | 12 / 11 |

More than half of everything on disk was already answerable from something else on disk. And the
windows are not scattered — eleven distinct end dates — they simply never match *exactly*. That is
the shape of ordinary use: `--to 2026-05-31` then `--to 2026-06-01`, or any "`--to` today" habit,
which misses the following day by construction.

The slicing needed to serve a sub-window already exists: `_txn_in_window` (`run.py:422-435`),
written because Millennium ignores server-side date filters.

### 4. Partial progress is discarded exactly where it is most expensive

`save_cached_fetch` runs *after* the whole chunk loop (`run.py:479-486`). A 429 on the last chunk
throws away every chunk already paid for, on precisely the accounts that are rate-limited, and the
next run re-spends all of it. The write is also a plain `path.write_text` — unlike `save_session`,
which goes through temp-file + `os.replace`. A torn cache write degrades safely today
(`load_cached_fetch` swallows the parse error and returns `None`), but it degrades into a re-fetch,
which is the thing being economised.

### 5. Balances and transactions do not belong in the same cache entry

Follows from §1 and is worth stating separately, because it is a modelling error rather than a
tuning one. A transaction list is scoped to the requested window. A balance is scoped to **the
account and the moment of the call** — `reference_date` is the fetch date in every record — and has
no relationship to the window at all. Storing them under one window-keyed entry means two fetches
of different windows for the same account each pay for a balance that would have been identical.

### 6. No measured basis for a small late-booking margin — and one case against immutable months

Both decision 5 in the issue and the overlap rule in #6 rest on "banks book transactions a few days
late". Testing that against the cache — every pair of fetches of the same account where the later
one re-covers a window the earlier one already covered, keyed on `entry_reference`, 24 such pairs:

**Exactly two transactions ever appeared in a later fetch of an already-covered window.** Both were
booked in early June and materialised between fetches on 2026-08-07 and 2026-08-08 — about **two
months** after their booking date. The account carries no `bank_transaction_code` on any
transaction (so, a Polish bank), its history starts ~90 days back, and its session has since been
re-linked. That matches Alior/Alior Kantor and coincides with the integration fix
[`enable-banking.md`](enable-banking.md) records Enable Banking shipping in August 2026, *applied
retroactively to historical transactions*.

Two conclusions, and the second is the more useful:

- The few-days late-booking lag is **not observable in this data**. A margin covering it is a
  policy value, chosen for safety, not a measured one — and it should be documented as such rather
  than presented as derived.
- The one real case of a settled month changing was an **aggregator-side backfill arriving two
  months later**. No TTL length would have caught it at a useful time: a 30-day TTL would have
  re-fetched that month in July and still missed it. This is the decisive argument in §5 of the
  Decision below.

### 7. The shared allowance leaves a mark in the cache — and the cache cannot confirm it

The premise of this whole ADR is that `alior` and `alior_kantor` draw on one budget. Reconstructing
past runs from `fetched_at` gets close to showing it, and then stops, in a way that is itself the
argument for the first item under *To verify*.

The backfill of 2026-08-08 14:08 walked nine rolling 30-day windows across every bank.
`alior` and `erste` are absent from the first six, which the documented ~90-day history horizon
explains exactly: 90 days before 2026-08-08 is 2026-05-10, and both appear from the
`2026-05-31..2026-06-29` window onward, not before. **`alior_kantor` is absent from all nine** —
including the three that its sibling at the same institution completed, in the same run, minutes
apart. Its consent ran to 2026-11-05, and it had itself succeeded on a comparable window at 10:11
that morning. Neither the horizon nor consent accounts for it.

The timing points the same way. In that run every bank-to-bank transition costs 1–3 seconds — bar
one:

| Transition | Elapsed |
|---|---|
| Each bank to the next, across the run | 1–3s |
| The slot where `alior_kantor` should have run, in each of the three windows it could have served | **37–39s** |

31s is precisely the client's 1→2→4→8→16 retry ladder (§2), and the remainder is six requests'
latency.

**It still is not proof, and the reason is the finding.** A gap of comparable length also appears in
two windows in a position where a `400` history-horizon error is expected, and 400s are not
retried — so the gap is not a clean 429 signature. The cache records successes only, which makes a
429, a 400, and a bank that was never requested indistinguishable after the fact. **The tool
discards exactly the evidence needed to settle its own most load-bearing assumption**, and no
amount of re-reading the cache will fix that. Recording the failure path — status, the `error` code,
response headers, retry count and elapsed — costs no ASPSP requests and makes the next natural cap
self-documenting. That is a prerequisite for the verification below, not a nice-to-have.

---

## Options considered

### A. Do nothing

**Rejected.** The `LEDGERBAL` finding in §1 makes this a correctness bug, not only an efficiency
one, and it is the *common* case rather than a corner.

### B. Merge `alior` and `alior_kantor` into one connection

**Closed, and the issue is right about why.** The request count is driven by account count, which
is unchanged by merging; and the bank does not offer the two account sets under one consent
anyway. Recorded so it is not re-proposed.

### C. Pace requests — a token bucket or a fixed inter-request delay per ASPSP

**Rejected.** The observed limit is a **daily cap** with a ~6h recovery, not a rate. Pacing a burst
does not create allowance; it only makes a run that was going to fail take longer to fail. If a
per-*second* limit ever exists at some ASPSP it will present as a 429 that genuinely does recover
under backoff — which is exactly what the retained generic ladder (decision 2) still handles.

### D. Key the rate-limit domain on the `aspsp` name alone

**Rejected.** `wise_personal` and `wise_business` are two different logins at one ASPSP. PSD2
allowances are per PSU per ASPSP, so grouping on `aspsp` alone would fail `wise_business` on the
strength of a cap that only `wise_personal` hit. A circuit-breaker that predicts failure must not
over-group — see decision 1.

### E. Key the domain on `bankid`

**Rejected.** It happens to group Alior with Alior Kantor correctly (both `ALBPPLPW`) and leaves
Wise ungrouped (no `bankid` on purpose). But `bankid` is a **GnuCash identity field** whose whole
point is that it never moves; overloading it as a rate-limit key couples two unrelated concerns, so
a future rate-limit fix would sit one careless edit away from orphaning imported accounts. It also
contradicts Enable Banking's own position that BICs are not reliable ASPSP identifiers
([`decisions.md`](decisions.md#bic), and `bankid` is deliberately unset for Wise).

### F. Persist the "exhausted" mark across runs

**Rejected.** Tempting — a retry ten minutes later would then cost nothing. But a persisted mark can
only ever be *wrong in the expensive direction*: it refuses to fetch when the allowance may have
recovered, and the user cannot see why. A re-probe costs exactly one request and returns the truth.
The cache is what makes the retry cheap; the mark does not need to.

### G. Align the *request* chunk boundaries to calendar dates, as the issue proposes

**Rejected as stated, and the reason is the point of decision 4.** It conflates two different
chunkings. Request size is a **bank constraint** (many ASPSPs reject windows over ~90 days).
Storage granularity is a **reuse concern**. Making requests month-sized to get month-sized cache
units would turn a cold 90-day fetch from one request per account into three or four — at exactly
the bank that caps, whose history only reaches ~90 days back and which therefore always fetches
near its maximum. Decoupling them costs nothing, because the client-side slicing needed to split a
response into months is `_txn_in_window`, which already exists (§3).

### H. Make the ASPSP the rate-limit domain; split request chunking from storage chunking; stop paying for an impossible balance

**Accepted** — below.

---

## Decision

### 1. The rate-limit domain is `(aspsp, country, psu_type)`, derived from config

> **Parked.** Exists as `BankConfig.rate_limit_domain` and is recorded in the run log, but is a
> **label only** — it groups nothing until decision 3 lands.

A small derived key on `BankConfig`; no new config surface. It groups `alior` with `alior_kantor`
(same ASPSP, same country, both defaulting to `psu_type = "personal"`) and keeps `wise_personal`
apart from `wise_business`, which is the distinction option D gets wrong.

**Err narrow.** The circuit-breaker below turns a *measured* failure into a *predicted* one, and a
prediction is only as good as the grouping. Under-grouping costs wasted requests, which the rest of
this ADR is reducing anyway; over-grouping costs a bank its files for a run on the strength of a
guess. If a deployment ever holds two logins of the same `psu_type` at one ASPSP, the escape hatch
is an explicit `rate_limit_domain` config key — deliberately **not** added now, because unused
config is a maintenance cost and this one has no user today.

### 2. `ASPSP_RATE_LIMIT_EXCEEDED` is not retried; every other 429 keeps today's backoff

> **Implemented** in [#12](https://github.com/skolima/gnucash-ofx/issues/12).

Matched on the `error` field of the response body, never on `message` — the error-envelope note in
[`enable-banking.md`](enable-banking.md) is explicit that `message` is prose. This is retry policy,
so it lives in the client (`_request`), which already owns `max_retries` and the backoff ladder.

Generic and platform-level 429s keep the existing 1→60s ladder: those are genuinely transient, and
§2's argument (five retries cannot outlast a six-hour cap) does not apply to them.

### 3. The exhaustion mark gates **network calls**, not banks

> **Parked** on the shared-allowance premise. See *Parked: what closes this*.

A per-run ledger of exhausted domains, created in `fetch_enablebanking` and passed into `fetch_bank`
— an explicit object, not module state, so it is testable and so two runs in one process cannot
contaminate each other.

**Where the check sits is load-bearing.** It goes immediately before each outbound call, *not* at
the top of the bank loop. Under decision 4 much of a sibling connection's work may be servable from
cache with no network at all, and a bank-level short-circuit would throw away files we could have
written for free. What is refused is a *request*, not a bank.

An account that cannot proceed is recorded as a `BankFailure` with `scope="bank"`, no sleeps, and a
message that says the allowance is shared and to retry in ~6h. The message names the **sibling bank
key** — user-chosen, and the thing they can act on — not the ASPSP string; explanations naming
institutions is what [`decisions.md`](decisions.md) rules out.

**On the failure-isolation invariant.** AGENTS.md says one bank's failure must not cost another bank
its files, and this deliberately makes one connection's 429 end its sibling's run. The invariant is
preserved in substance: the invariant protects banks that *could* have succeeded, and a sibling
connection to an exhausted ASPSP cannot. The only thing that changes is whether it fails after 31s
of sleeping and six more counted requests, or immediately. That defence rests entirely on the
grouping being right, which is why decision 1 errs narrow and why §"To verify" treats the shared
allowance as an assumption to confirm rather than a fact.

### 4. `/balances` is called only when the window's end is not in the past

> **Implemented** in [#11](https://github.com/skolima/gnucash-ofx/issues/11), with one correction
> found in the doing: the call is still made on a closed window when the account's currency is
> unknown, because that is the only way to learn it for a session on the pre-currency state
> schema — and its result must then *not* become the `LEDGERBAL`.

Reconciliation stays where it is meaningful; the call disappears where §1 shows it cannot be. This
removes about half the requests for every historical fetch — the common case, 121 of 154.

For a historical window, `LEDGERBAL` becomes the zero-based running total the existing
`end_balance is None` path in `ofxout.py:287-291` already produces. **This is a choice between two
false values, not a fix**, and it is recorded as such: §1 establishes that a true closing balance
for a past window does not exist anywhere in this API, and the DTD forbids omitting the tag. What
tips it is that today's value is a *plausible real balance at the wrong date*, which is what makes
it dangerous in the reconcile dialog, while a period movement from zero is wrong in a way a user
reconciling an account with any opening balance will notice immediately. The honest fix — a stored
closing balance carried forward per account — belongs with #6's coverage ledger, which is the only
thing that could know it.

Balances are also cached **per account**, not per window, with the existing 6h TTL. §5 above: a
balance has no window.

### 5. The cache stores fixed calendar-month chunks, and each is persisted as it lands

> **Implemented** in [#12](https://github.com/skolima/gnucash-ofx/issues/12).

The unit of storage becomes `(uid, YYYY-MM)` — a fact about the world — rather than `(uid, window)`,
a memo of something we once asked. Requests stay as large as the bank allows (≤90 days), aligned to
month boundaries, and the response is sliced into months before writing. Consequences:

- **Subsumption falls out.** Serving a window is "do I hold every month it touches, fresh enough?"
  — no interval algebra, and the 83-of-154 redundancy in §3 disappears.
- **Partial progress survives.** Each month is written as it arrives, so a 429 on the last chunk
  keeps everything already paid for.
- **Boundaries are identical between runs**, which is what makes chunks shareable — the property
  the issue was reaching for, obtained without option G's request-count regression.
- Writes go through temp-file + `os.replace`, as `save_session` already does. The frequency of
  writing rises; the tolerance for a torn one does not.
- Entries carry a `version`; anything unrecognised is ignored, exactly as an unreadable file is.
- The **current month is stored with its actual coverage end**, not assumed complete.

**The existing cache is converted, not discarded.** Old entries carry `uid`, `date_from`, `date_to`,
`fetched_at` and the raw transactions — enough to slice into months, keeping those a legacy entry
covers in full and dropping partial months at its edges. One-shot, no network. Discarding instead
would make the upgrade itself cost a full re-fetch at the bank that can least afford one, which is
a strange way to ship a rate-limit fix.

### 6. TTL is a function of the chunk's age, and a settled month does not expire

> **Implemented** in [#12](https://github.com/skolima/gnucash-ofx/issues/12).

- The **current month, and any month within the late-booking margin of today**: the existing 6h TTL,
  which is tied to the ASPSP's recovery guidance and is right for a moving target.
- Every **older month**: served regardless of age.

§6 is what decides the second half. The only observed change to a settled month was an
aggregator-side backfill that arrived two months late; no TTL that anyone would pick catches that,
so paying to re-fetch every settled month everywhere buys nothing real. `--refresh` already exists,
is documented as spending allowance, and is the correct instrument for "I have reason to believe
the bank changed something".

The **late-booking margin is one named constant, defined once** (in `cache.py`) and imported by
#6's overlap rule rather than re-picked there. It is explicitly a **policy value, not a measured
one** — §6 found no data to derive it from — and the comment at its definition must say so, so that
nobody later reads it as an empirical finding.

### 7. Out of scope

Fetching only the missing tail (item 6 in the issue) needs a coverage ledger, which is #6. Nothing
here records coverage: the cache stays a rate-limit shield, and a cache hit must never be mistaken
for evidence that a period was fetched. The Alior state files on the pre-`accounts` schema are #6c
and, as the issue notes, are not the orphaning case — they carry `account_ibans`, so `ACCTID` still
resolves. Re-link them at the next expiry rather than spending consent on it specially.

### 8. Untouched

`FITID`, `BANKID`, `ACCTID`, the `NAME`/`MEMO`/`CHECKNUM` composition, the stdout-is-the-file-list
contract, per-account failure isolation for everything that is not a shared-allowance 429, and
`ofxout.py` in its entirety.

---

## Consequences

**Accepted costs.**

- **A historical fetch's `LEDGERBAL` stays wrong**, in a new way. It is wrong today and would remain
  wrong under any option available; decision 4 stops paying a request for the wrong answer and picks
  the falsehood that misleads the reconcile dialog least. It is a real regression for anyone who was
  reading that number as approximately-the-current-balance. Recorded in
  [`decisions.md`](decisions.md) and in the README's rate-limit section.
- **Within one domain, the connection that runs first spends the allowance.** If a run is partially
  capped, the same sibling loses every time, since bank order is config order. Not addressed here,
  and it is not permanent: with decision 5 the successful connection's months are cached, so the
  retry costs almost nothing and reaches the starved sibling. #6's coverage ledger is what would
  make prolonged starvation *visible* rather than silent.
- **A wrongly-grouped domain costs a bank its files for that run** (recovered on the next run, from
  cache for the parts already paid for). This is the price of predicting a failure instead of
  measuring it, and decision 1 is the thing keeping it small.
- **A settled month is never re-fetched without `--refresh`.** An aggregator backfill into a month
  we already hold will be missed until someone asks for it explicitly. §6 argues no TTL would have
  caught the one observed case either, but this makes it a *design property* rather than an
  accident. Stated in the README so a user meets it before it surprises them.
- More state in `run.py`, and a `cache.py` rewritten rather than extended.
- **The cache format changed.** Entries written by older versions are converted in place on the
  next run rather than discarded, so the upgrade itself costs no requests. The conversion is
  best-effort — where several old entries touched one month the widest coverage wins rather than
  being merged — and whatever it leaves uncovered is simply re-fetched.

**Cost to reverse: low.** Nothing here touches transaction identity or account identity, so no
account orphans and no transaction re-imports. Backing out decision 4 restores a balance call.
Backing out decision 5 costs the cache — deleting `cache/` is already a supported, if expensive,
operation.

---

## Parked: what closes this

Written to be picked up cold, months from now, by someone who has forgotten all of it.

### The one open question

**Do `alior` and `alior_kantor` draw on a single allowance?** Everything in decisions 1 and 3 rests
on yes. The evidence is strong and entirely circumstantial (§7): in one backfill `alior_kantor`
produced nothing across all nine windows including the three its sibling completed minutes apart,
with consent valid and a successful fetch of its own that morning, and the slot where it should
have run cost 37–39s against 1–3s for every other bank-to-bank transition. None of that is proof,
because the cache recorded successes only.

**It cannot be answered by thinking harder.** There is no quota counter anywhere (measured — see
below), so the allowance cannot be inspected, only spent. A refusal has to be observed.

### Why waiting is the right move

The probe costs one request but needs a cap to exist, and inducing one deliberately spends a day of
real bank access. Since decisions 2, 4, 5 and 6 have already cut the request count substantially,
caps should now be rarer — which is good for the tool and slow for the experiment. So: **do not
chase it. Let it happen, and be ready.** The instrumentation that makes a natural cap conclusive is
already in place and costs nothing to keep running.

### What will be on disk when you come back

`state/fetch-log.jsonl`, appended to by every `fetch` since
[#10](https://github.com/skolima/gnucash-ofx/issues/10). One `run` record per invocation and one
`request` record per HTTP response, carrying `bank`, `domain`, `path` (account UID redacted),
`status`, `api_code`, `attempt` and `elapsed_s`. No amounts, no counterparties, no account numbers
— it is safe to paste into an issue.

This prints every daily-cap refusal with the requests either side of it, which is the whole of
step 3 below. Verified against a synthetic log of the expected shape.

```python
import json
import pathlib

log = pathlib.Path("state/fetch-log.jsonl")
rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]
runs = [r for r in rows if r["event"] == "run"]
print("runs:", len(runs), "| background runs:", sum(r["psu_mode"] != "online" for r in runs))
caps = [r for r in rows if r.get("api_code") == "ASPSP_RATE_LIMIT_EXCEEDED"]
print("daily-cap refusals:", len(caps))
for cap in caps:
    i = rows.index(cap)
    print(f"\n--- {cap['at']}  {cap['bank']}  domain={cap['domain']}")
    for r in rows[max(0, i - 3) : i + 8]:
        if r["event"] != "request":
            continue
        mark = ">>" if r is cap else "  "
        print(f"{mark} {r['at'][11:19]} {r['bank']:<14} {r['status']} a{r['attempt']} {r['path']}")
```

### The reading, in order

1. **Confirm the run was online.** `psu_mode` on the `run` record must be `online`. A `background`
   run has only ~4 requests a day, and then the connection with the most accounts trips first —
   which looks exactly like a shared allowance and is not. This is the trap that nearly derailed
   the whole investigation; check it first, every time.
2. **Find the first refusal.** The earliest `request` record with `status: 429` and
   `api_code: ASPSP_RATE_LIMIT_EXCEEDED`. Note its `bank` and `domain`.
3. **Look at what the sibling did next.** Filter to the other `bank` sharing that `domain`.
   - **Its very first request is a 429**, with no successful request of its own earlier in the same
     run → **the allowance is shared. Implement decisions 1 and 3 as written.**
   - **It succeeds** → the premise is false. Degrade decision 3 to a per-connection mark and drop
     the sibling short-circuit; decision 1's key becomes documentation of an intent rather than a
     grouping. Say so here and stop.
   - **Nothing from it at all** → the run ended first. Not evidence either way; wait for another.
4. **Rule out ordering.** Config order decides who spends the allowance first, so one observation
   is consistent with "the second connection is always starved" as well as with a shared budget.
   Confirm in the reverse order — a run with `--bank alior_kantor` first, on a different day —
   before treating it as settled.
5. **Optional corroboration.** If both connections recover at the same time (~6h) rather than
   independently, that is one budget refilling.

If you would rather force it than wait, the deliberate version is: on a day when no fetch is
needed, exhaust one connection with `--refresh` over a narrow window, then immediately

```sh
uv run gnucash-ofx fetch --bank alior_kantor --refresh --from <yesterday> --to <today>
```

and read the log as above. That costs one ASPSP's daily allowance and is the owner's call to make,
not something to do casually.

### Attempted 2026-08-10

The deliberate version above was tried, with the owner's explicit authorization — on `alior`
alone. `alior_kantor` was never touched today, so this attempt establishes headroom for one
connection under repeated re-fetching; it does not reach the sibling-sharing question the ADR
actually turns on.

**What happened first, and why it matters.** Both connections were re-linked from scratch (fresh
browser SCA consent, renewed to 180 days) before probing, to move them off the pre-`accounts` state
schema (§7). That turned out to matter on its own: the new `state/alior.json` carries `version: 2`,
a full `accounts` array, and a non-empty `currency` — `"XXX"`, ISO 4217's "no currency" placeholder
— on every one of the five accounts, taken verbatim from Alior's own `POST /sessions` response, not
a tool-side default. The coverage ledger (`state/coverage/alior.json`,
`state/coverage/alior_kantor.json`) survived the re-link untouched, still showing continuous
coverage through 2026-08-09, confirming it really is keyed on account identity (IBAN-derived) and
not the session UID, exactly as
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) says. Account UIDs
did regenerate (e.g. `65a2e954-…` → `2c88e622-…` for the same IBAN, `PL91***2192`), matching the
UID-regeneration-on-re-link note in [`enable-banking.md`](enable-banking.md).

**The probe.** `fetch --bank alior --refresh --from 2026-07-01 --to 2026-07-31` — a full past
calendar month already inside existing coverage, so a re-fetch could only widen-or-equal any cached
chunk, never shrink it — run 8 times in succession. Read from `state/fetch-log.jsonl` filtered to
2026-08-10:

| Measure | Result |
|---|---|
| Requests, across all 8 rounds | **56** — 7 per round (one `GET /sessions/{id}`, six `GET /accounts/{uid}/transactions`; one account's list paginates into two requests) |
| Status | **200 on all 56** |
| `api_code` | **`null` on all 56** — no `ASPSP_RATE_LIMIT_EXCEEDED`, no 429 of any kind |
| `/balances` calls | **0** — `currency` is non-empty (if bogus) for every account, so decision 4's `currency_known` check correctly skipped it every round |
| Wall-clock window | 12:21:36–12:23:31 UTC (under two minutes) |
| Cache files after | 5 added (`m-*-2026-07.json`, one per account), 0 removed |
| State files after | 14 before, 14 after — content changed on the two re-linked banks, nothing added or deleted |

The owner stopped after 8 rounds rather than keep spending unboundedly — the checkpoint the probe
design already built in (*Why waiting is the right move*, above). **This is inconclusive: not a
refutation and not a confirmation.** `alior` alone showed no headroom limit in under two minutes of
repeated re-fetching, and `alior_kantor` was never engaged in the same window, so nothing here bears
on whether the two share an allowance.

**A confound not previously considered.** Re-linking issues a brand-new session and a fresh consent
grant for both connections. If Alior's ~6h-recovery daily cap is tied to the session/consent grant
rather than being a pure calendar-day counter independent of it, re-linking may have reset whatever
budget the original circumstantial evidence (§7) was bumping into — so today's large headroom does
not necessarily mean the cap is bigger than assumed; it may mean re-linking handed both connections
a fresh budget. This sits alongside the psu_mode/background-cap confound already closed under
*Settled by measurement*, and is **not** closed. **For the next attempt: do not re-link before
probing** — it re-arms this confound. Either let a refusal happen through ordinary use as originally
planned, or, if forcing it deliberately again, do so without re-linking either connection first.

**Aside, flagged separately, not addressed here.** The bogus `currency: "XXX"` introduced by the
re-link broke local processing on 2 of the 5 `alior` accounts, on every one of the 8 rounds:
`map_transaction` rejects a transaction whose currency (`EUR`/`PLN`) does not match the account's
(`XXX`), so no OFX was written for those two accounts on any round. This does not affect the
request-count evidence above — the API call itself still happened and returned 200 before the local
rejection — but it is a real bug, flagged separately, not addressed here.

### Attempted 2026-08-10 (attempt 2)

The aside above is now closed — [`adr-xxx-currency-placeholder.md`](adr-xxx-currency-placeholder.md)
shipped and was itself verified live (`probe-xxx-currency-fix` in [`probes.md`](probes.md)) — so a
second attempt was run the same day, with the owner's explicit authorization to spend real
allowance again, correcting attempt 1's confound rather than repeating it: **neither connection was
re-linked.** `state/alior.json` and `state/alior_kantor.json` were read exactly as they stood from
the morning's re-link, `"XXX"` still on disk and all.

**The window.** Both attempt 1 and this attempt use `--refresh` against a window that can only
widen-or-equal cached coverage, never lose data. The owner asked that the discriminating
`alior_kantor` calls (Phase 1 and Phase 3, below) also use a full settled calendar month —
`2026-07-01..2026-07-31`, the same window already used for `alior` in attempt 1 — rather than a
one-day slice landing in the *current* month, so that no phase of the probe forces a write into
the 6h-TTL current-month cache bucket for no reason.

**The design, run via the internal API (no CLI flag sets a custom RunLog `command`), tagged
`probe-alior-shared-allowance`:**

1. **Phase 1 — control.** `alior_kantor --refresh` over the July window. Must come back clean or
   the probe aborts before touching `alior` — a later refusal is worthless if `alior_kantor` had
   its own unrelated problem.
2. **Phase 2 — hammer `alior`.** The same window, `--refresh`, repeated for up to 8 rounds,
   stopping the instant a round reports `ASPSP_RATE_LIMIT_EXCEEDED`.
3. **Phase 3 — immediately re-check `alior_kantor`,** same window, regardless of how Phase 2
   ended. This is the discriminating step: a 429 on `alior_kantor`'s first call, with no prior
   success in that run, confirms a shared allowance; a clean success refutes it.

**What happened.** Read from `state/fetch-log.jsonl`, filtered to
`command: "probe-alior-shared-allowance"`:

| Phase | Bank | Requests | Status | `api_code` |
|---|---|---|---|---|
| 1 (control) | `alior_kantor` | 7 | 200 × 7 | `null` × 7 |
| 2, rounds 1–8 | `alior` | 12 × 8 = 96 | 200 × 96 | `null` × 96 |
| 3 (check) | `alior_kantor` | 7 | 200 × 7 | `null` × 7 |
| **Total** | | **110** | **200 on all 110** | **no refusal at all** |

Every `run` record shows `psu_mode: "online"`. Zero `BankFailure`s across all 10 phases (8 rounds
plus the two `alior_kantor` checks). The per-round shape changed from attempt 1's 7 requests to
12: `state/alior.json`'s `currency` field is still the literal string `"XXX"` on disk (the
normalizer fixes it in memory on load, per `adr-xxx-currency-placeholder.md`, but nothing rewrites
the file), so `currency_known` stays `False` for every account and `/balances` is called live on
every round rather than skipped — confirmed by the request count, not assumed.

**This is inconclusive again — and this time cleanly so.** No refusal surfaced within the fixed
8-round ceiling, with the re-link confound closed and the currency confound closed. Combined with
attempt 1's 56 requests and `probe-xxx-currency-fix`'s 6, this ASPSP domain took **at least 172
online requests over the course of 2026-08-10** without a single `429`. That still is not proof of
no shared cap — the round ceiling was a chosen bound, not a derived one, and nobody has a
documented figure for Alior's real daily allowance to compare against — but it does mean today's
combined ordinary-plus-probe traffic did not find it. The standing recommendation is unchanged:
**do not chase it further today.** The next natural refusal, whenever it happens through ordinary
use, is still the cheapest way to read the three-way branch above for real.

### Assumptions still riding along

Neither blocks anything; both are cheap to notice in the same log.

- **That `wise_personal` and `wise_business` have separate allowances** — what decision 1's
  `psu_type` component assumes. Failure mode is benign: under-grouping only wastes requests.
- **That `GET /sessions/{id}` does not count against the ASPSP allowance.** Assumed platform-side
  like `GET /aspsps`. Deliberately *dissolved rather than tested*: decision 3's check goes in front
  of it anyway, so being wrong costs nothing.

### Settled by measurement, recorded so they are not re-derived

- ~~Is the `LEDGERBAL` written for a historical window actually wrong?~~ **Yes** — §1, and worse
  than assumed: `DTASOF` dates it to the window end and GnuCash pre-fills the reconcile dialog from
  it.
- ~~Can a correct historical closing balance be obtained from the API?~~ **No** — §1. There is no
  historical-balance call and `/balances` takes no date.
- ~~Can `LEDGERBAL` simply be omitted?~~ **No** — required by `ofx160.dtd` line 921.
- ~~Does the exact-triple cache key cost real hits?~~ **Yes** — 83 of 154 entries are already
  covered by another entry for the same account, across only 11 distinct window ends.
- ~~Does the client really retry a 429 for every remaining account?~~ **No** — `fetch_bank` breaks
  after the first. The waste is ~31s and six counted requests *per connection*, doubled by the
  sibling.
- ~~Is there a measurable late-booking lag to size the margin from?~~ **No** — §6. The only
  observed late arrivals were an aggregator backfill two months after the fact. So
  `LATE_BOOKING_MARGIN` is a policy value and says so where it is defined.
- ~~Is there a quota counter to read?~~ **No.** Measured 2026-08-09 over `GET /application` and
  `GET /aspsps`: no `X-RateLimit-*`, no `RateLimit-*`, no `Retry-After` on success — nothing but
  Google Front End plumbing and an `x-request-id`. The cheap three-request probe a counter would
  have allowed does not exist, which is why the plan above waits for a refusal instead.
- ~~Was the run in online mode at all?~~ **Closed as a confound**, and it was a real competing
  explanation, not exclusive with the premise. PSU headers need an IP; one lookup service supplied
  it, and a failure dropped the *entire run* to the ~4/day background cap, after which the
  connection with the most accounts trips first. Several services are now tried, a run that cannot
  determine an address stops rather than quietly taking the smaller allowance, and `psu_mode` is
  recorded per run. It can no longer happen unnoticed — but step 1 above still checks it.
- ~~Does a 429 response itself count against the allowance?~~ **Not observable**, and it does not
  matter: decision 2 is correct either way, because 31s of retries cannot outlast a ~6h cap.
- ~~Does any ASPSP here apply a per-second limit distinct from the daily cap?~~ **Not yet seen, and
  it now answers itself.** The `attempt` field distinguishes a 429 that recovered under backoff from
  one that never does; if the former ever appears, option C is worth revisiting.

### Learned during implementation, and worth keeping

- **Decision 4 nearly cost five of six connections their files.** `/balances` has a second job —
  discovering the account's currency — and an account with no currency is skipped entirely. Every
  connection on the pre-currency state schema depended on it. The call still happens when the
  currency is unknown, and a test caught the follow-on bug that its result was then still becoming
  the `LEDGERBAL`.
- **Option G's rejection held up in practice.** Keeping request size (a bank constraint) apart from
  storage granularity (a reuse concern) is what let monthly chunks cost no extra requests. The
  invariant that made per-chunk saving simple was *spans never split a month*, which was not
  obvious when the decision was written.
- **`AppConfig`'s directory defaults are relative**, so a test that builds one without naming them
  addresses the real `cache/`. Harmless while the cache was append-only; not harmless once
  decision 5 added a migration step that rewrites and deletes. A `conftest.py` guard now fails any
  test that touches the real directories.

## References

- **Implemented by:** [#10](https://github.com/skolima/gnucash-ofx/issues/10) (the run log, and the
  PSU-IP hardening that closed the background-mode confound),
  [#11](https://github.com/skolima/gnucash-ofx/issues/11) (decision 4),
  [#12](https://github.com/skolima/gnucash-ofx/issues/12) (decisions 2, 5, 6).
- [#9](https://github.com/skolima/gnucash-ofx/issues/9) — the issue this decides;
  [#6](https://github.com/skolima/gnucash-ofx/issues/6) — the coverage ledger that consumes §5 and
  shares the late-booking margin.
- [`enable-banking.md`](enable-banking.md) — the error envelope (match on `error`, not `message`),
  the ~4/day background cap and ~6h recovery, PSU headers, and the retroactive August 2026 Alior
  integration fix behind §6.
- [`decisions.md`](decisions.md) — *Online mode + 6-hour fetch cache*; *Partial success is reported,
  not thrown away* (the existing 429-abandons-the-bank rule this refines); *`BANKID` identity* and
  *BIC* (option E); *Errors carry an explanation only when we have one* (why the message names a
  bank key, not an institution).
- GnuCash
  [`gnc-ofx-import.cpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/ofx/gnc-ofx-import.cpp)
  — `recnWindowWithBalance()`, the one consumer of `LEDGERBAL`.
- `ofx160.dtd` as shipped with GnuCash for Windows, line 921 — `LEDGERBAL` is not optional.
- `ofxstatement`'s `OfxWriter.buildStatement` — `DTASOF` comes from the statement end date.
