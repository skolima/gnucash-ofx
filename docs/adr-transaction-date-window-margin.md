# ADR: requests open a transaction-date margin early; the cache keeps booking-date months, and stops discarding or shrinking what it holds

**Status:** Accepted; decisions 1–5 shipped in
[#47](https://github.com/skolima/gnucash-ofx/issues/47), merged 2026-08-13, and decision 6's
repair refresh was run the same day. The ASPSP-side measurement is the live probe
`probe-txn-date-window`
(2026-08-13, Alior, 6 requests — see [`probes.md`](probes.md)); every local measurement was taken
2026-08-13 against this machine's `state/`, `cache/` and `state/fetch-log.jsonl`, zero API
requests. The shrink decision 3 stops has since also been observed **live**, not only by
simulation: the N26 onboarding's `--refresh` run
([#48](https://github.com/skolima/gnucash-ofx/issues/48), 2026-08-13) watched a narrower re-fetch
overwrite a month chunk's recorded coverage down to a single day.
**Date:** 2026-08-13.
**Scope:** `src/gnucash_ofx/run.py` (the request planning in `_account_raw_data`/`_request_spans`,
the month-filing save loop at ~862–876, and the coverage claims it writes),
`src/gnucash_ofx/cache.py` (one new named constant beside `LATE_BOOKING_MARGIN`;
`save_cached_month` gains merge semantics), the warnings plumbing for one new warning.
Explicitly **not** changed: `coverage.py`'s schema and `with_span`'s meaning (decision 4 is that
they stay), the cache key `(uid, YYYY-MM)`, the chunk schema fields, `sources/enablebanking.py`,
`ofxout.py`, and every identity field.
**Issue:** [#35](https://github.com/skolima/gnucash-ofx/issues/35). Inherits the
`LATE_BOOKING_MARGIN` precedent (a named, labelled policy value) from
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) decision 6, and the ledger
semantics plus the warnings channel from
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md).

---

## Where this stands

- [x] **1.** Every planned request span opens `TRANSACTION_DATE_MARGIN` (7 days, policy) earlier
      than its un-widened start — [#47](https://github.com/skolima/gnucash-ofx/issues/47).
      Shipped refinement: the 89-day floor bounds **every** window's wire, explicit `--from`
      included — decision 1's "exactly as it does for a resolved window" is the operative
      reading, which is what reconciles this with decision 6's repair command — and the wire is
      capped at `claim_from`, so a floor above the whole window cannot invert the request
- [x] **2.** Months stay bucketed by booking date, and a returned entry is never discarded for
      booking outside the requested span — [#47](https://github.com/skolima/gnucash-ofx/issues/47)
- [x] **3.** `save_cached_month` merges: authoritative inside the incoming claim, never lossy
      outside it — [#47](https://github.com/skolima/gnucash-ofx/issues/47). Shipped refinements:
      a merge that retains days only the old fetch answered keeps the old `fetched_at` (a
      crosser-only save must not make a stale moving-tail claim read as fresh), and a poisoned
      chunk — an entry already on disk raising `_BAD_DATA_ERRORS` from the id-fallback hash —
      degrades the merge to the incoming set for that one chunk instead of aborting the run
- [x] **4.** Coverage claims record the resolved window — never the widened request, never the
      response's extent — [#47](https://github.com/skolima/gnucash-ofx/issues/47)
- [x] **5.** An observed booking−transaction lag exceeding the margin raises a warning —
      [#47](https://github.com/skolima/gnucash-ofx/issues/47)
- **6.** Repair of the existing damage is one ordinary owner-run refresh of the Alior connection
  **after** 1–3 ship — operational; deliberately no code to tick a box for. Run 2026-08-13 after
  #47 merged: exit 0, no failures, no lag warnings
- **7.** Identity fields, the cache key, the chunk schema, the ledger schema and the ≤90-day
  request chunking untouched — not a decision to ship; recorded so nothing here is mistaken for
  having changed them

---

## Context

The tool asks an ASPSP for a `date_from`/`date_to` window and files what comes back into
calendar-month chunks by **booking date** (`_txn_in_window`, `run.py:703`, same precedence as the
mapper). The probe settled what the window means on the other side: **Alior filters the requested
window by `transaction_date` — the purchase date — not by `booking_date`** (`probe-txn-date-window`,
2026-08-13; details in [`probes.md`](probes.md) and the quirk entry in
[`enable-banking.md`](enable-banking.md#transaction-data-quirks)). A card purchase made on the
5th and booked on the 7th is therefore invisible to any request opening on the 7th, even though it
is booked squarely inside the window — and it is only reachable by asking for days *before* the
window.

Three individually-correct mechanisms then seal the hole shut:

- `CachedMonth.covers()` (`cache.py:114`) answers from the *claimed* span, so the deficient chunk
  reads as complete and the month is never re-requested.
- `chunk_ttl` (`cache.py:131`) settles a month `LATE_BOOKING_MARGIN` after it ends, after which the
  deficient chunk **never expires**.
- `with_span` (`run.py:1436`) advances the coverage ledger by the **requested** window
  unconditionally, so `status --check` shows no gap.

**What this costs, measured on this machine (2026-08-13):**

1. **It fired on the default path this very morning.** The daily resume fetch (window resolved to
   2026-07-30..2026-08-13 by `LATE_BOOKING_MARGIN` stepping back from the ledger) rewrote the live
   Alior card account's July chunk down to **a small remnant of the entries booked inside its
   claimed span 07-30..31** — everything else booked in those two days carries a
   `transaction_date` of 07-28/29, just before `date_from`, and vanished. The probe's controls
   independently confirmed every vanished entry still exists at the ASPSP: both control windows
   returned them all, in the same session, minutes apart. `covers()` says July is complete; from
   2026-08-14 the chunk settles and never expires; the ledger shows one unbroken span
   2026-05-12..2026-08-13.
2. **`save_cached_month` overwrites a fuller month chunk with a narrower one, unconditionally** —
   and did so twice since the issue was filed (08-12, window 07-27..08-12; then 08-13, window
   07-30..08-13). Full-July data for the live uid now survives only in a stranded chunk under a
   pre-re-link uid. The live Alior session has no 2026-05 or 2026-06 chunks at all — only stranded
   uids do.
3. **The lag is real, one-sided, and small so far.** Over every datable entry in the cache
   (deduped; all alior-family — the only banks populating the field): booking − transaction lag
   runs 0 to 3 days, median 0, **never negative**; a small minority cross a calendar month. The
   sample is six weeks of populated data with no holiday settlement cycle in it, so the observed
   max of 3 days does not bound the tail.
4. **The mechanism is bank-invisible almost everywhere.** `transaction_date` is populated only by
   `alior`/`alior_kantor`, and only in responses fetched since ~July 2026 — the same months fetched
   on 2026-06-30 have it null throughout. Revolut populates `value_date` but always with
   zero lag on every entry; Erste, Millennium and Wise populate neither extra date. At those banks the filter
   basis is **unobservable, not disproven** — and any ASPSP that starts populating the field
   inherits the exposure, because the tool's slicing rule is bank-independent.
5. **A second edge, ours alone: the tool discards data it was given.** For the 08-10 July-only
   windows, a handful of entries transacted at July's very end but booked 08-01/02 were *returned*
   by the ASPSP and dropped on the way to the cache: `months_in(span_from, span_to)` yields only
   the requested months, and the save loop's `_txn_in_window` filter (`run.py:862–876`) gives an
   entry booking outside them nowhere to go. Later overlapping fetches happened to heal them all;
   the defect is independent of what the ASPSP filters on.
6. **The loss becomes permanent at exactly the exposed banks.** Alior, Alior Kantor and Erste serve
   ~90 days of history. A day the cache certifies as complete-but-deficient is never re-requested,
   and once it drifts past the horizon it is unrecoverable at any price.

The problem was anticipated in one direction and not the other: `LATE_BOOKING_MARGIN` already steps
`date_from` back 14 days to catch bookings that *appear* late; nothing steps the request back to
catch bookings whose *purchase* was earlier. A wide backfill is unaffected — the data looks right
whenever anyone goes looking with a wide window — which is why this survived unnoticed.

---

## Decisions

### 1. Every planned request span opens `TRANSACTION_DATE_MARGIN` earlier than its un-widened start

A new named constant, **7 days**, defined once beside `LATE_BOOKING_MARGIN` and labelled the same
way: **a policy value, not a measured one.** The arithmetic it was picked against: the measured
maximum lag is 3 days (fact 3), the sample holds no holiday settlement cycle, and 7 is
max-observed + 4 days of slack — one full calendar week, covering any weekend-plus-holiday
settlement run the sample could not contain. Decision 5 is what keeps this honest: an observed lag
beyond the margin announces itself instead of waiting to be re-discovered the way #35 was.

The widening applies at **request planning only**, and its unit is the planned span.
`_request_spans` groups the missing months into contiguous span groups — a window whose middle the
cache already served plans several, one per gap — and **each planned span's opening `date_from`
steps back by the margin on the wire request**. Interior boundaries need no margin of their own:
where the ~90-day cap splits one contiguous group into consecutive requests, an entry straddling
the cut is returned by one side or the other under *either* filter basis — its `transaction_date`
falls inside the earlier request's window or its `booking_date` inside the later one — and
decision 2 files it into its booking month wherever it arrives. The un-widened span is carried
through to the save loop untouched; it is what claims keep deriving from (decision 4).

The margin applies at every bank, not just the alior family: it is free where the basis is
booking date or the filter is ignored (Millennium), and it is the only protection available where
the basis is unobservable (fact 4). On the default path the widened bound still respects the
existing 89-day clamp — a margin must never turn a working fetch into a `400` at the horizon banks.
An explicit `--from` keeps its contract (used as given, never narrowed); the margin widens the
request below it exactly as it does for a resolved window.

**Request cost, in counted requests:** the typical resumed fetch (a two-to-three-week window) stays
one span — zero extra requests. A cold fetch at the 89-day clamp cannot widen (the clamp absorbs
the margin) — zero extra requests, and the first margin-width of that window is only as complete as
the horizon allows (open question 3). Only a wide-but-not-clamped window (over ~83 days at a bank
serving deep history) can tip a span past the 90-day request limit and cost **at most one extra
request per account per fetch**, in the rarest window shape.

**Direction is one-sided, and that is measured, not assumed.** The lag is never negative in any
datable entry measured, and structurally a booking follows the purchase. An entry booked on or before
`date_to` therefore has `transaction_date` on or before `date_to` and is returned; no forward
margin on `date_to` exists (see rejected option F).

**What this rules out:** changing what the window *means*. The tool's semantics — DTSTART/DTEND,
month bucketing, the coverage ledger — stay booking-date; the margin compensates for a measured
ASPSP behaviour at the request boundary and nowhere else. Since the probe, this is not papering
over a guess: the filter basis is confirmed, controls and all.

### 2. Months stay bucketed by booking date, and a returned entry is never discarded

The `(uid, YYYY-MM)`-by-booking-date invariant holds. What changes is the filing: **every entry the
ASPSP returned is filed into its booking month's chunk, including months outside the requested
span** — the save loop stops narrowing to `months_in(span_from, span_to)` and instead files what it
actually received. This fixes fact 5 independently of anything the ASPSP filters on: data paid for
with a counted request must never be thrown away by our own slicing.

An entry filed into a month outside the request **never widens a coverage claim** (decision 4). It
lands beyond the claim: merged into that month's existing chunk if one exists, or into a new chunk
whose claim is empty. The representation of an empty claim is the implementing PR's choice; the
invariant it must satisfy is that it can never make `covers()` answer true. A chunk may hold entries
beyond its claimed span — they are correct data, served when a window asks for them, and replaced
the next time a fetch actually claims their days (decision 3).

**What this rules out:** bucketing by the field the ASPSP filters on (rejected option B), and any
"widen the claim to the booking dates received" variant — a crosser proves its own existence, not
that its day was served in full.

### 3. `save_cached_month` merges: authoritative inside the incoming claim, never lossy outside it

The rule, exactly: **the incoming fetch is authoritative for the days it claims — inside the
incoming claimed span, the incoming set replaces what the chunk held, so a bank-side withdrawal or
amendment still lands. Outside the incoming claim, the chunk keeps what it had.** The stored claim
widens to the union when the two spans touch or overlap; when they are disjoint the wider claim
wins and the gap is never claimed — the one thing a coverage record may never do is claim days it
lacks. No schema change: same fields, new write semantics.

**The merge is keyed, and the key is the identity `FITID` already rests on**: `_transaction_id`
(`sources/enablebanking.py:385`) — the bank's `transaction_id`, else `entry_reference`, else the
deterministic hash of stable fields. **The incoming copy wins on any key collision, inside or
outside the claim.** The key is what makes the merge idempotent at Millennium, which returns full
history on every fetch: an unkeyed preserve-outside-the-claim would duplicate that history on
every save. Two residuals, accepted and named: an amendment that moves an entry's booking date
across the claim boundary leaves the stale copy in the now-unclaimed month until some fetch claims
those days — same `FITID` by construction when the id is the bank's, so GnuCash's import
de-duplication bounds it; and an entry identified only by the fallback hash embeds its booking
date in its key, so a bank-side amendment of that date reads as a new entry — exactly as it
already does under today's overwrite.

**Why the overwrite cannot stay.** It is measured as lossy on the default path, twice in two days
(fact 2), and decision 1 does not make it safe: the margin fixes what a request *returns*, not what
a narrower *claim* destroys on save — this morning's window 07-30..08-13 would still have replaced
a full-July chunk with a two-day one, margin or no margin. `decisions.md` ("Coverage is a ledger of
days") documents the overwrite as the reason the *ledger* exists rather than as a virtue of the
cache; what changes here is that the cache stops re-paying, in counted requests and — past the
~90-day horizon — in unrecoverable data, for information it already held.

**What this rules out:** blind union (keeps ghosts of withdrawn entries forever on the days a fetch
actually re-answered), and keeping overwrite with a "fetch wider" convention nobody can enforce.

### 4. Coverage claims record the resolved window — never the widened request, never the response

`with_span` keeps recording the resolved window, and a chunk's claim keeps deriving from the
**planned, un-widened missing span** intersected with the month — the spans `_request_spans`
actually decided to fetch, already clamped to the resolved window and already skipping the months
the cache served. Two nearby bases are wrong and ruled out by name. Not the resolved window at
large: when a cache hit splits the window, the fetch plans only the gaps, and claiming
resolved-window days that a served month absorbed would certify days this fetch never asked for.
And not the widened request: claiming the margin days would repeat the original bug one level
down — a request opening at `from − margin` is only booking-complete from `from`, by exactly the
argument that motivates the margin. The implementation therefore carries each un-widened span
through to the save loop separately from the wire request's bounds.

This is the decision that keeps `covers()` sound again: a chunk claiming `[F..T]` was produced by a
request opening at `F − margin`, so every entry booked in `[F..T]` with lag ≤ margin was returned —
and lag beyond the margin is decision 5's job to surface.

**What this rules out:** "record what came back" (rejected option C). The requested-window basis is
honest once the margin holds, and it is the only basis that is *observable at every bank*.

### 5. An observed lag exceeding the margin raises a warning

Whenever any fetched entry's booking − transaction lag exceeds `TRANSACTION_DATE_MARGIN`, the run
emits a warning through the existing warnings channel
([`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md)): a warning, never a
failure — the files are correct; it is the *margin* that has been measured too small, and some
other window may have a hole. It costs zero requests, and it is the named revisit trigger for the
margin's value. It also self-arms at any bank that starts populating `transaction_date` in the
future (fact 4).

**What the tripwire can and cannot see — the detection is censored, and that is stated rather than
papered over.** The warning observes only entries that were *returned*: an entry whose lag exceeds
the margin and whose `transaction_date` precedes its request's widened opening is exactly the one
that request cannot return, so it cannot warn at the fetch that lost it. What keeps the tripwire
useful is overlap the system already has: on a resumed account the next window's `date_from` steps
back `LATE_BOOKING_MARGIN` (14 days) behind the ledger and the wire opens `TRANSACTION_DATE_MARGIN`
below that, so an entry booked near one fetch's edge is re-asked with room, and lags up to roughly
the two margins' sum (~21 days) surface as warnings on the ordinary cadence. Beyond that sum — and
at the opening edge of a cold backfill, where the horizon forecloses re-asking — a censored outlier
is invisible, bounded to one margin-width of days per cold backfill.

**What this rules out:** treating 7 days as settled, and treating silence as proof. It is a policy
pick with a tripwire whose reach is ~21 days on resumed accounts; re-picking the margin on the
warning's evidence is the intended lifecycle, and open question 1 words what silence does and does
not establish.

### 6. Repair of the existing damage is one ordinary command, after the fix lands

One `fetch --bank alior --from <repair-day − 89 days> --refresh`, **after** decisions 1–3 ship.
`--bank alior`, because `fetch` defaults to every configured bank and the repair should spend only
the Alior connection's allowance. `--from` at the deepest day the ~90-day history horizon still
serves on the repair day — deeper opens a `400` (an explicit `--from` is used as given, decision 1),
and shallower abandons recoverable days: the 2026-05/06 months missing for the live uid are
recoverable only to the extent the horizon still serves them, which shrinks daily — the repair
should not wait on anything beyond the fix itself. Refetching before the fix would re-send a
transaction-date-filtered window and enshrine the same hole under a fresh `fetched_at`. **The fix
PR ships no repair code**: the damaged chunks are one machine's artifacts, and the ordinary refresh
path already does the job once the requests it sends are complete.

### 7. Untouched

`FITID`, `BANKID`, `ACCTID`, the cache key `(uid, YYYY-MM)`, the chunk schema fields, the ledger
schema and `resolve_window`'s user-facing contract (the resolved window stays the one reported; the
widened request bounds appear where every request's bounds do, in `state/fetch-log.jsonl`),
`LATE_BOOKING_MARGIN`'s value, the ≤90-day request chunking, and `sources/enablebanking.py` — the
client passes dates through and keeps doing so.

---

## Rejected options

### A. Do nothing, document the quirk

**Rejected on fact 1.** This fires on the *default* path — the daily resume fetch is precisely the
narrow-window shape that triggers it — and the system then certifies the result complete three
independent ways (`covers()`, settled-month TTL, the ledger). At the ~90-day-horizon banks the loss
graduates from "annoying" to "unrecoverable at any price" as each deficient day drifts past the
horizon. A quirk note cannot un-seal that.

### B. Bucket chunks by whatever field the ASPSP filters on

**Rejected on fact 4, which is a measurement about time, not just about banks.** The field is
absent at four of six banks — and was absent *at Alior itself* before ~July 2026: the same months
fetched on 2026-06-30 carry `transaction_date: null` throughout. The bucket key would therefore
churn retroactively at a single bank — the same entry filed under different months depending on
when it was fetched — which is cache-membership instability in the identity-adjacent layer.
It also breaks what booking-date bucketing carries: `DTSTART`/`DTEND` name booking-date ranges, and
the coverage ledger's days are booking days. Two record-keeping systems on two different date
bases, one of them unobservable at most banks, is strictly worse than one basis plus a margin.

### C. Record what came back, rather than what was asked for

The most honest-sounding option, and **rejected on an observability argument plus a measured
counterexample.** A response cannot testify to its own coverage: a day with no transactions returns
nothing, so response-derived coverage can never claim a quiet day without falling back to trusting
the request — at which point it *is* the requested-window basis, plus schema surgery on the chunk
format and the ledger's meaning. And the measured counterexample: Millennium ignores server-side
date filters entirely ([`enable-banking.md`](enable-banking.md#transaction-data-quirks)), so "what
came back" there is the full history on every fetch, and the response's extent would claim
everything, always. Decision 4 keeps the requested basis and decision 1 makes it honest again.

### D. Keep the overwrite in `save_cached_month`, rely on the margin

**Rejected on fact 2.** The overwrite destroyed a fuller July chunk twice in the two days since the
issue was filed, on the default path, and the margin does not defend against it: it completes what
a request returns, while the overwrite's damage is a narrower *claim* replacing a wider chunk
wholesale. The surviving full-July data sits in a stranded pre-re-link uid chunk — the demonstration
that the loss is real, not theoretical.

### E. Probe the other ASPSPs' filter basis before fixing anything

**Rejected on fact 4.** Where `transaction_date` is null, the discriminating observation does not
exist — a probe there returns no answer at any request price. The margin is the correct posture for
the unobservable case: near-zero cost, applied bank-independently, with decision 5 as the tripwire
if the mechanism ever becomes visible elsewhere.

### F. A symmetric forward margin on `date_to`

**Rejected on measurement.** The lag is never negative (every datable entry, fact 3) and cannot be: a booking
follows its purchase. Every entry booked on or before `date_to` is therefore inside the ASPSP's
transaction-date filter already. A forward margin would spend requests fetching days the resolve
logic will request tomorrow anyway, to defend against a direction the data rules out. (The forward
*crossers* — transacted in-window, booked after `date_to` — are decision 2's job, and they cost
nothing: the ASPSP already returned them.)

---

## Cost to reverse

- **Decision 1 (the margin):** trivial in code — one constant and one subtraction — and expensive
  in silence. Removing it re-opens a hole that the system then certifies closed three ways, and at
  the horizon banks every day fetched narrow while it is off ages into permanent loss. Any reversal
  must ship with a `--refresh` of the affected tail, for the same reason decision 6 orders repair
  after the fix.
- **Decision 3 (merge-on-save):** low. No schema change, so chunks written under merge read fine
  under a reverted overwrite; the cost of reverting is the measured narrow-over-wide loss
  returning.
- **Decision 4 (requested-window claims):** reversing *toward* response-derived claims is the
  invasive redesign option C describes — chunk format and ledger meaning both move. That direction
  should reopen this ADR, not patch past it.
- **Identity fields:** nothing here touches them, so no account orphans and nothing re-imports
  under any reversal.

---

## Open questions

1. **The true lag tail.** The measured max is 3 days over six holiday-free weeks; the margin is 7
   by policy. **What settles it:** decision 5's warning, over months of ordinary fetches — zero
   requests — within the tripwire's reach: on the resumed cadence it detects lags up to roughly
   `LATE_BOOKING_MARGIN + TRANSACTION_DATE_MARGIN` (~21 days); beyond that, and at cold-backfill
   openings, it is censored (decision 5). A lag above 7 re-picks the margin against new
   arithmetic; a year of silence is evidence the tail stays under ~21 days on resumed accounts —
   not proof about the censored region, which stays an accepted, bounded exposure.
2. **The filter basis at the other ASPSPs.** Unobservable until they populate `transaction_date`
   (fact 4), and the margin already covers them either way. **What settles it:** the field
   appearing in their responses — at which point decision 5 is already armed there. No probe is
   worth counted requests for a question the fix does not depend on.
3. **Is the ~90-day history horizon itself transaction-date-based?** If so, an entry booked just
   inside `today − 89` but transacted before the horizon is unreachable by *any* request — a
   permanent, bounded (≤ margin days) blind spot at the very edge of a cold backfill at
   Alior/Alior Kantor/Erste. **What settles it:** a small probe at the horizon edge (a
   `probe-designer` plan, ~2 counted requests: a window opening at the horizon, checked for
   entries booked just inside with `transaction_date` just outside) — or waiting, since the
   exposure is one margin-width once per cold backfill and shrinks to zero on every resumed fetch.
4. **Background mode.** Every measurement and the probe ran online (`psu_mode: "online"`
   throughout); the ~4/day background allowance has never been exercised against this design.
   Nothing here depends on it — recorded so nobody reads the probe as bounding background
   behaviour. **What settles it:** nothing needs to; the first background run's fetch-log records
   answer it for free if it ever matters.

---

Decisions 1–5 are condensed into [`decisions.md`](decisions.md) as imperative lines with their
reversal costs (post-#47); this file stays for the probe, the rejected options and the
measurements behind them.
