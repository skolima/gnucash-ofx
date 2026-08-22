# ADR: `fetch --batch-size` splits one account's OFX output by transaction count, never by calendar

**Status:** **Accepted and merged ([#34](https://github.com/skolima/gnucash-ofx/issues/34)).
Implemented — every decision shipped in
[#46](https://github.com/skolima/gnucash-ofx/issues/46), closing
[#14](https://github.com/skolima/gnucash-ofx/issues/14); see *Where this stands*.**
§10 and §11 were added **after** acceptance and **change the recommended `--batch-size` value from
40 to 120** — §10 on the owner's reported review tolerance, §11 on the answer to open question Q5.
Neither changes a decision; both change what the docs will suggest, before that suggestion reached
the README. §8's `N = 100` bullet is withdrawn, and §1's "only one institution overlaps" claim is
corrected by §11. **§12 (2026-08-17) records the GnuCash 5.16 throwaway-book import**, answering
the two remaining GnuCash-side open questions: the matching prompt is de-duplicated, and the
reconcile pre-fill ignores `LEDGERBAL` entirely — decision 5 stands as defence in depth.
Originally recorded as: *Proposed. Nothing implemented — `src/` is untouched.* Measurements in §1–§3 were taken
2026-08-10 against this machine's real `output/` and `cache/`, using the same local-simulation
method [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) established.
§4–§7 are read directly from the current source (`ofxout.py`, `run.py`, `conversions.py`,
`adr-combined-ofx-file.md`), dated the same day. **§2's corroboration, §8 and §9 were measured
2026-08-12** against this user's own archive of historical bank exports — 24 account series across
12 institutions, 2012–2026 — read strictly read-only and outside the repository; nothing from it is
copied here beyond ratios. **No ASPSP probe was spent or is needed**: every open question below is
either a local re-simulation, a free local comparison against `cache/`, or a zero-request GnuCash
import — the same kind of gate `adr-combined-ofx-file.md` already used, not a counted request.
**Date:** 2026-08-10, extended 2026-08-12.
**Scope:** `ofxout.py` (the per-account write step gains a batching entry point that calls
`ofx_filename()` N times instead of once — **`ofx_filename()` itself is unchanged**, decision 4),
`run.py` (the
`for account, txns in pending: write_account_ofx(...)` loop in `fetch_bank`, and the equivalent
prediction path in `dry_run_enablebanking`), `cli.py` (a new `--batch-size` option on `fetch`, and
`--dry-run`'s reporting when it is set, plus `--no-combine`/`--no-batch-size` and the effective-
packaging line on stderr — decision 9), `config.py` (a new top-level `[fetch]` section, read the way
`[output]`/`[state]`/`[cache]` already are) and `config.example.toml`. **Explicitly not in scope:**
`cache.py`, `coverage.py`,
`state.py`, `sources/enablebanking.py`, `FITID`/`BANKID`/`ACCTID` and their resolution, the state
schema, and the cache key. `ofxout.py`'s `_CombinedOfxWriter`/`combine_statements`/
`write_combined_ofx` machinery is **used unchanged** by decision 8 — batching feeds it more
statements, it composes them the way it already does.
**Issue:** [#14](https://github.com/skolima/gnucash-ofx/issues/14). Depends on no other ADR and
inherits no shared constant. Interacts with
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) (decision 8, below) and relies on
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) already having shipped
— the issue's own sequencing note flagged #6 as a prerequisite "worth landing before or alongside
this"; §5 shows it already landed and this design does not touch it at all.

---

## Where this stands

- [x] **1.** Count-based batching, chronological by booking date, oldest first, up to `N` per
      batch, last batch gets the remainder, no rebalancing —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **2.** `--batch-size N` is a single flag, global to the run; no per-bank/account override —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **3.** A booking date is never split across batches — `N` is a soft cap, and a single day
      exceeding it becomes one oversized batch; the nudge direction, left open here, is settled by
      the implementation as **pull back to the day break** (the only reading consistent with
      decision 1's "at most `N`" plus this decision's single named exception; pinned by
      `test_days_are_never_split_and_boundary_is_pulled_back`) —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **4.** Filenames are unchanged: the date range alone names every batch, and `ofx_filename()`
      needs no new component — [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **5.** `LEDGERBAL`: only the final batch of a window reaching today carries the live balance;
      every earlier batch gets today's existing zero-based running total, independently per batch —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46); measured moot for the GnuCash 5.16
      reconcile pre-fill, §12, and kept as defence in depth
- [x] **6.** `--dry-run` predicts no paths at all while `--batch-size` is set, and says why —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46), suppressed in
      `dry_run_enablebanking` itself so report and stderr narration cannot disagree
- [x] **7.** Coverage ledger untouched — not a decision to ship; recorded so nothing here is
      mistaken for touching `coverage.py`. Verified untouched in
      [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **8.** `--batch-size` and `--combine` compose — a batched account contributes its N statements
      to the combined file, routing already answered by the 2026-08-10 imports (§7) —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46), the batched-combined shape verified
      against real libofx 0.10.5 and imported for real (§12)
- [x] **9.** Both packaging flags are settable in `config.toml` under a new `[fetch]` section,
      global to the run, with `--no-combine`/`--no-batch-size` as the CLI off-switches —
      [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **10.** Untouched — not a decision to ship; recorded so nothing here is mistaken for touching
      identity fields, the cache, or the state schema. Verified untouched in
      [#46](https://github.com/skolima/gnucash-ofx/issues/46)
- [x] **11.** Out of scope — nothing to ship; nothing in
      [#46](https://github.com/skolima/gnucash-ofx/issues/46) reached into it

One contract the review round added rides on no numbered decision: **`split_into_batches` sorts
only when `batch_size` is set** — with no batch size the cached-then-fetched input order is
returned untouched, because "no `--batch-size`" is a byte-identical guarantee (pinned by
`test_unbatched_input_order_is_preserved_not_sorted`; condensed into `AGENTS.md` and
[`decisions.md`](decisions.md)).

---

## Context

`write_account_ofx` (`ofxout.py`) writes exactly one file per account per currency per fetched
window, no matter how many transactions land inside it. Every file still ends at GnuCash's own
import assistant, where the user reviews a list of proposed matches and either accepts or corrects
each one — and the assistant's Bayesian matcher is trained by exactly what happens in that dialog.
A large review list gets rubber-stamped or abandoned partway, which trains the matcher on bad data;
a run of several smaller, sequential imports each teaches the matcher something before the next one
lands. The tension the issue names is real: forcing a monthly split is its own tax on a multi-month
catch-up, and the coverage ledger (`adr-coverage-ledger-and-warnings.md`) already makes a wide
catch-up window something `fetch` produces on its own when `--from` is omitted, not something a
user has to construct by hand.

What exists today: a fetched window's transactions go into one `Statement`, one file, regardless of
count. Nothing controls how many transactions land in it.

---

## Constraints, measured rather than assumed

### 1. Real review batches are already large enough to matter

Measured 2026-08-10 across every real `.ofx` file this tool has produced to date (105 files — a
structural, artifact-level count, the same kind `adr-combined-ofx-file.md` §4 reports for `output/`
file counts). A non-trivial share of them are already large enough that a single-sitting review is
the rubber-stamp risk the issue describes, and at least one real file was produced by an explicit
wide `--from` spanning several months — a real multi-month catch-up, not a hypothetical one — rather
than the default window. **Exact per-file transaction counts and the size distribution are withheld
here**: `AGENTS.md#data` excludes transaction counts and volumes from a public document even with
amounts removed, because a transaction count is itself a financial-scale signal. The qualitative
finding — unbatched catch-up windows already exist in this project's own output, and already
produce reviewably large files — is what this decision rests on; its magnitude is not needed to
justify batching as an *option*, only as motivation for offering it.

### 2. Same-day boundary splitting is the majority case at plausible batch sizes — decisive

Simulated count-based chronological splitting over 28 real cached accounts (an account count, not a
transaction count — the same kind of sample size `adr-coverage-ledger-and-warnings.md` reports
throughout) at three candidate batch sizes spanning a plausible small/medium/large range. At every
size tested, a majority-to-near-majority of the resulting boundaries land **inside** a run of
same-day transactions: roughly two-thirds at the smallest size tested, a little over two-fifths at
the middle size, about half at the largest. Same-day runs holding more than one transaction are
common in this data, not rare, and some extend well beyond a pair — nudging "to the run's edge" is
a meaningfully different operation from nudging by one transaction.

**This settles the issue's open question about nudging.** At every batch size worth shipping,
splitting a same-day run across two files is the majority case, not an edge case that can be left
undecided. Decision 3 below is "yes, nudge"; the *direction* of the nudge is not settled by this
measurement (see *Open questions*).

**Reproduced at ~400× the scale, on an independent corpus — the small sample was not lying.**
Measured 2026-08-12 against this user's own archive of historical bank exports (read-only, outside
the repo): **24 account series across 12 institutions, 2012–2026**, in ten CSV dialects plus QIF and
OFX, deduplicated against single-file ground truth (validated on 606 fully-covered account-months,
1.0% residual over-count, worst case immaterial to every figure below). Two numbers from §2's small
cache sample reappear almost exactly:

| Property | 28 cached accounts (2026-08-10) | 24 series, 2012–2026 (2026-08-12) |
|---|---|---|
| Booking dates carrying more than one transaction | ~57% | **60.6%** |
| Longest same-day run observed | the ceiling seen there | **the same ceiling** |

Also measured on the large corpus: **84.5% of all transactions sit on a day shared with at least one
other**. So the clustering §2 relies on is a
**stable, structural property of this data across fourteen years and twelve institutions**, not an
artifact of a thin cache. The *Open questions* entry that previously asked whether it generalizes is
answered and has moved to the settled list.

**One derived constraint follows directly, and it sets a floor under `N`.** Because decision 3 never
splits a booking date, a day holding more than `N` transactions becomes a single batch larger than
`N` — the cap yields to day coherence. Measured against every account's longest observed run, **some
accounts exceed the smaller candidate sizes and none exceed the middle of the range**, so an `N`
below that range produces oversized batches for some accounts however the nudge resolves, and an `N`
above it never does.

That is the floor: below it, `--batch-size` silently stops meaning what it says for the busiest
accounts, which are exactly the ones it exists to serve. The run lengths themselves, and the per-size
counts of affected accounts, are withheld under `AGENTS.md#data`: a same-day run length is a
transaction count, and a sweep of how many accounts exceed each candidate `N` reconstructs the
distribution of those counts.

### 3. Date-range-only filenames would look ambiguous in most real cases — decisive

Across every simulated boundary at all three sizes, no two consecutive batches of one account ever
produced an identical `(first, last)` date-range pair — but a substantial share of consecutive pairs
(tracking the same-day-boundary rate above almost exactly) had **touching** ranges: batch *N*'s last
transaction date equals batch *N+1*'s first. No exact collision was observed, but the sample was
small — 5 to 11 eligible accounts per size, an account count safe to report the same way
`adr-coverage-ledger-and-warnings.md` §6's per-bank account table is — so "zero collisions observed"
is weak evidence of "never happens." A majority of consecutive pairs would produce two filenames
whose date ranges *touch* on their shared day.

**Read carefully, this measurement settles the issue's open question in the opposite direction to
what it first appears — and an earlier draft of this ADR got it backwards.** The simulation was run
on **un-nudged** splitting. Every touching pair it found is, by construction, a boundary that landed
inside a same-day run — which is precisely what decision 3 moves. The draft that cited this section
as proof that an explicit index was always needed was leaning on evidence its own sibling decision
removes.

**With decision 3 in force, a collision is not merely rare — it is impossible.** Decision 3 never
splits a booking date, so every batch ends on a strictly earlier date than the next one begins.
Consecutive ranges are therefore disjoint, ranges increase monotonically across an account's batches,
and **no two batches of one account can ever carry the same date range.** That is a structural
guarantee from the batching rule itself, not a property of this sample that a different bank could
violate — which is what lets decision 4 leave `ofx_filename()` alone entirely.

This also **contradicts the issue's own proposed shape**, which assumed `ofx_filename()` "needs a
third optional component alongside the existing currency disambiguator" and asked whether the batch
marker should be an index or implied by the date range. Measured and reasoned through: it needs
neither, because the date range is already unique. The issue's open question dissolves rather than
resolving one way or the other.

### 4. Where batching hooks into the existing write path

`ofx_filename()` (`ofxout.py`):

```python
def ofx_filename(
    account: Account,
    period_start: date,
    period_end: date,
    *,
    disambiguator: str | None = None,
) -> str:
    bank_key = safe_component(account.bank_key, "bank_key")
    currency = safe_component(account.currency, "currency")
    stem = f"{bank_key}_{currency}"
    if disambiguator is not None:
        stem = f"{stem}_{safe_component(disambiguator, 'disambiguator')}"
    return f"{stem}_{period_start:%Y_%m_%d}-{period_end:%Y_%m_%d}.ofx"
```

Component order today: `<bank_key>_<currency>[_<disambiguator>]_<start>-<end>.ofx`. **Decision 4
leaves this function exactly as it stands** — the issue expected a batch marker to become a fourth
component coexisting with `disambiguator`, but §3 shows the date range is already unique per batch,
so the signature and the component order are untouched. `disambiguator` continues to do its existing
job of separating two same-currency accounts at the same bank, and batching separates one account's
own files by range alone.

`write_account_ofx` calls `ofx_filename()` exactly once and writes exactly one file (or none, when
`txns` is empty). `run.py`'s `fetch_bank` calls it from one place, once per pending account:

```python
for account, txns in pending:
    path = write_account_ofx(
        account, txns, date_from, date_to, output_dir,
        disambiguator=disambiguators.get(account.account_id),
    )
```

Batching replaces this with a loop that, for a batched account, groups `txns` into consecutive
chronological chunks and calls a batch-aware write once per chunk, with each chunk's own
`period_start`/`period_end` derived from its actual first/last transaction date — never an evenly
divided calendar slice, matching the exact-and-re-derivable filename convention
[#3](https://github.com/skolima/gnucash-ofx/issues/3) already established for the single-file
case.

### 5. Coverage already advances before any file is written — #6 is not a dependency

`fetch_bank`'s loop advances the coverage ledger per account (`run.py`, immediately after
`pending.append((account, txns))`) from `date_from`/`date_to` — the whole requested window — before
`pending` is ever split into files at all, and before `pair_conversions()` runs. Coverage records
"this window was fetched," not "this many files were written for it." Batching happens strictly
downstream, in the write loop shown in §4, so **no change to `coverage.py` is needed**: however many
files one account's window ends up split into, the ledger sees exactly the same thing it does today.

### 6. Batching cannot split a paired currency-conversion leg, by construction

`pair_conversions()` (`conversions.py`) pairs legs sharing a `conversion_key` **across accounts**
within one bank's fetch, and does not change how many `Txn` objects exist — it only attaches a
`Conversion` annotation to the two it matches. The two legs of one conversion already live in two
different accounts (different currency, per the existing one-statement-per-account-per-currency
invariant), so they were never candidates for landing in the same batch to begin with. Batching one
account's `txns` list, after `pair_conversions()` has already run in `fetch_bank`, cannot separate a
pair that was never in the same list.

### 7. The `--combine` imports already answer the routing question, and invert its failure mode

The shape `--batch-size` + `--combine` would produce for the first time is one `BANKID`+`ACCTID`
pair appearing in more than one `<STMTTRNRS>` inside a single document. No import has literally
exercised that file, but the two real GnuCash 5.16 imports recorded in
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) *Open questions* (2026-08-10, Build
2026-06-27) decompose it exactly, and each half was directly observed:

| Observed 2026-08-10 | What it fixes for a repeated pair |
|---|---|
| Round 1: **three account-matching passes, one per statement** — the assistant did not treat the file as one account | Matching is keyed on **each statement's own** `BANKID`+`ACCTID`, so a second statement naming pair *P* is resolved against *P*, not against whichever statement came first |
| Round 1: **one combined transaction-import window**, all statements' transactions listed together, **each line carrying its own account in a per-line account column** | The target account is resolved **per transaction from its parent statement**, into one flat list. Two statements naming *P* contribute lines that both resolve to *P*'s account |
| Round 2: the same three pairs, now existing in the book, **matched silently, zero popups**, account column pre-populated | A pair already known to the book binds to the existing account without prompting — which is the state the *second* occurrence of *P* is in once the first has been matched or created |

**The failure mode the #8 gate was built to catch cannot apply here.** That gate's fail condition,
in its own words, was "transactions from more than one statement landing in a single register."
For a batched account that is not the failure — **it is the required outcome.** #8 risked two
*different* accounts collapsing into one register; batching asks two statements naming the *same*
account to land in that one account's register. There is no wrong account available to route to.

Two residues were unobserved when this section was written — **§12 has since answered both**
(2026-08-17, GnuCash 5.16). Neither was a routing risk; kept as written for what they claimed:

- **Whether a first-ever import of a batched account prompts once or twice for the same pair.**
  Round 1's per-statement pass ran against three *distinct* pairs, so whether the assistant
  de-duplicates identical pairs within one file is unknown. Worst case is a duplicate matching
  prompt for one account — cosmetic, visible, and not a misroute.
- **Two `<LEDGERBAL>` values for one account in one document** (decision 5: earlier batches carry a
  computed running total, the final one may carry the live balance). libofx §3 measured independent
  per-statement `LEDGERBAL`s across *different* accounts; which value GnuCash's reconcile dialog
  associates with the account when one document carries two for the same account is new. This
  already happens across separate files today — two runs, two files, two `LEDGERBAL`s for one
  account — but not simultaneously within one document.

Both were settled exactly that cheaply — `ofxdump` plus one throwaway-book import, zero ASPSP
requests (§12) — and neither misplaced a transaction.

### 8. `N` has a defensible range, and it is narrower than the plausible one

Same 2026-08-12 corpus as §2 (24 account series, 12 institutions, 2012–2026). Candidate `N` from 10
to 100 was swept over every fully-covered account-month and over rolling 90-day windows — the latter
being the real worst case, since 90 days is the hard history horizon this tool can never fetch past.

**The per-`N` numbers themselves are withheld under `AGENTS.md#data`, and the reason is worth
stating because it is not obvious.** The share of windows that split at a given `N` *is* the survival
function of transactions-per-window — P(count > `N`) — so a table of split rates against named `N`
values is a transaction-volume distribution wearing percentage signs, not a set of ratios. Published
against a stated denominator it reconstructs the absolute counts §1 withholds. Only the **shape** of
the sweep is reported here, which is all the decision needs:

- The split rate falls **monotonically** across the sweep, steeply at the small end and slowly at the
  large one.
- The short-remainder rate — how often the trailing batch holds no more than a few transactions —
  falls steeply from the smallest sizes, reaches a **minimum in the middle of the range**, and is
  flat-to-slightly-worse above it.
- At the largest sizes tested, so few windows split at all that the flag is effectively **inert** for
  almost every account.

**What this rules out.**

- **`N ≤ 15` is out**, on two independent grounds: it sits below §2's measured same-day-run ceiling,
  so for some accounts a single busy day exceeds the requested size and `--batch-size` stops meaning
  what it says for exactly the accounts it exists to serve; and it is where the short-remainder rate
  is at its worst — the "pointlessly short trailing file" outcome the issue's no-rebalancing rule
  accepts only as a rare tail, not as a routine one.
- ~~**`N = 100` is out as a default** — it is deep into the inert end of the sweep, so it would
  change nothing for almost every account and the flag would appear broken.~~ **Withdrawn
  2026-08-12, see §10.** The argument mistook inertness for failure. `--batch-size` is opt-in and
  its job is to cut an *unreviewable* window down to a reviewable one; doing nothing to a window
  that is already reviewable is the correct outcome, not a broken flag. This bullet only held
  against a default-on flag, which decision 2 never proposed.

**What this supports: a floor, and not much else.** Across 25–50 no account is forced into an
oversized batch by clustering and short trailing batches become uncommon rather than routine, with
`N = 40` the minimum of the short-remainder curve on both denominators.

**§10 supersedes this section's choice of a recommended value, and the reason is worth stating: the
sweep optimises the wrong quantity.** Split rate and short-remainder waste are properties of *files*.
The question a user asks — "how much can I review in one sitting?" — is a property of *people*, and
nothing in this sweep measures it. What §8 legitimately establishes is the **floor** (below it,
clustering forces oversized batches and short remainders become routine). The value to recommend
comes from §10.

**Either way this is a recommendation for documentation, not a default.** `--batch-size` stays off
unless asked for (decision 2); what is being settled is only what the README and `--help` should
suggest when a user asks "what number should I put here", which would otherwise be a guess.

### 10. The sweep optimises files; the review unit is a person's sitting — which is why the recommendation sits far above the sweep's optimum

Added 2026-08-12, from the one kind of evidence open question Q4 asks for and no local measurement
can supply: **the repository owner's own review experience.** Reported directly — a month's worth of
one account is routinely imported and reviewed in one sitting without difficulty, with a busy month
at that account sitting near the upper limit of what is comfortable.

That single fact reframes the whole sizing question, and it disagrees with §8:

- **A normal month should not split at all.** The sweep optimises how files divide, not how a person
  reviews, so its optimum would cut ordinary runs up rather than only catch-ups — work the user has
  never needed and did not ask for. The issue's complaint was never "a month is too much"; it was
  that a *multi-month catch-up* is.
- **What the flag is actually for is the catch-up.** At a value in this region a 90-day backfill —
  the widest window this tool can ever fetch, given the history horizon — splits, and an ordinary run
  does not. How many files a given backfill produces depends on that account's own volume and is not
  stated here. Splitting the catch-up is precisely the outcome the issue described wanting and
  rejected doing by hand: "telling the user to now go run `fetch` six times, once per month, and
  import each."
- **Inertness on ordinary runs is the design working.** Hence the withdrawal of §8's `N = 100`
  bullet above.

**Recommendation: `N = 120`** — a round value well clear of §8's floor, set high enough to be inert
in ordinary use and to act only on a catch-up. It is deliberately **not** fitted to a measured
monthly count: `AGENTS.md#data` withholds those, and a value derived to fit one publishes one by
implication. Pick your own if your tolerance differs.

**What this is and is not.** It is n=1, self-reported, from the person who will use the flag most —
which is exactly the evidence Q4 is waiting on and exactly the evidence that cannot be manufactured
locally. It is not a measurement of matcher training, which stays open. A different user with a
different tolerance should pick a different number, which is why this is a suggestion in the docs and
not a default in the code.

### 11. The API splits fees into their own entries; the export corpus does not

Open question Q5, measured 2026-08-12 by joining the export archive against this project's own
`cache/` — both already on disk, zero requests. Three findings, and the first corrects this ADR.

**The overlap is limited by time, not by institution — §1's framing was wrong.** This document
asserted that only one of the archive's institutions is among the configured banks. Measured, **at
least three are.** What actually restricts the comparison is that `cache/` begins long after most
export series end, leaving a handful of comparable account-months at one bank-style institution and
a few days at one fintech-style one.

**Where the two can be compared month-for-month at a bank-style institution, they agree exactly** —
entry for entry on booking date and signed amount, with no entry on either side the other lacks. The
one apparent shortfall is a chunk whose coverage starts mid-month, a fetch-window boundary rather
than a granularity difference.

**At the fintech-style institution there is one systematic difference, and it makes the API side
finer.** Per-transaction fees arrive as their **own entries** keyed to their parent, where the export
carries the same fee as a column on the parent row. Every fee child is booked on its parent's date.
So for such accounts this tool produces **more** entries per window than the export corpus predicted,
and **longer same-day runs** — the longest run in the whole cache belongs to that account and is
substantially fee children. Card authorisation vs settlement, pending vs booked, conversion legs and
internal transfers showed **no** count difference at all; the cache is entirely booked.

**Effect on the recommendation: it pushes the same way §10 does, which is up.** A finer API side
means windows reach a given size sooner than the export sweep suggested, so a recommendation derived
from that sweep would if anything be too small. §2's floor survives and is mildly reinforced: the
API-side same-day ceiling is at least as high as the export-side one, for the structural reason that
fee children share their parent's booking date, and still sits below the floor.

**What it does not settle.** The account that motivates batching most was never compared — its export
series ends a quarter before the cache's data begins. One fresh export of that account for the cached
months would turn the strongest claim here from an adjacent-period argument into a month-matched
result. That is a keyboard gate, not a probe, and it costs the API nothing.

### 12. GnuCash ignores `LEDGERBAL` for the reconcile pre-fill, and de-duplicates the matching prompt

Added 2026-08-17, from the throwaway-book import [`docs/testing.md`](testing.md) prescribes —
**GnuCash 5.16 (Build ID 5.16+(2026-06-27), Windows)**, both fixture files, each carrying one
`BANKID`+`ACCTID` pair across three `<STMTTRNRS>` with three distinct `<LEDGERBAL>`s. This answers
§7's two residues and closes the last two open questions below.

**A first-ever batched import prompts once, not three times.** Round 1, into a book where the
account did not exist: one matching prompt and one account offered for creation, though the pair
appears in three statements. GnuCash de-duplicates identical pairs within a file's matching phase,
so the cosmetic worst case named in §7 does not occur. Round 2, same pair now existing: zero
prompts, all six new transactions routed silently, the Bayesian matcher already suggesting
categories.

**The reconcile dialog's pre-filled ending balance is computed from the register — `LEDGERBAL` is
not consulted at all.** After round 1 it pre-filled **-333.00** (the register total), not the final
statement's 5000.00 and not an earlier batch's value; after round 2, **-1110.00**, likewise. That is
the third row of `testing.md`'s outcome table — the harmless one, and *more* defensive than the
"last statement wins" reading decision 5 was designed around: in this version no batch's
`LEDGERBAL`, early or final, can mislead reconciliation, because none reaches the dialog.

**Decision 5 stands unchanged.** Its zero-based running totals were chosen so that *if* a caller
honours an earlier batch's `LEDGERBAL`, the value it sees is at least arithmetically consistent
rather than a stale live balance. GnuCash 5.16 turning out to honour none of them makes that
precaution unobservable here, not wrong — libofx has other callers, and the pre-fill source is
version-specific behaviour nothing pins. Register totals confirmed while there: six transactions,
-333.00 after round 1; twelve, -1110.00 after round 2 — every batch landed whole, in date order, in
the one account.

As everywhere in this document, a pass is a statement about one version; re-verification is owed
against whatever GnuCash is current when this next matters, and the procedure stays in
[`docs/testing.md`](testing.md).

### 9. Count-based batching absorbs a 30× volume spread that `--bank` cannot

The same corpus measured one thing that **contradicts an argument an earlier draft of this ADR
made**, and the correction is worth recording rather than quietly editing. That draft justified
declining a per-bank `batch_size` override partly on the grounds that `--bank` already scopes a run,
so a user wanting different sizes per bank could simply run `fetch` twice.

**Measured, that workaround does not do what it claims on this user's data.** Seven of the 24 series
sit behind a *single* institution — one already among this project's configured bank keys — and
within that one bank key the per-account activity spans roughly **30×** between the quietest and the
busiest account. The busiest account in the entire corpus and several of the quietest **share one
bank key**, so `--bank` cannot separate them at all: any per-bank size would apply to both ends of
that spread simultaneously.

**The conclusion survives on a better argument, which is why decision 2 is unchanged.** `N` is a
**cap on review size, not a target for it**. A count-based split self-adjusts to whatever volume an
account actually has: at `N = 40` a quiet account writes one file exactly as it does today, and a
heavy one writes several — no per-account configuration required, because the *transaction count is
already the input*. This is precisely the property the issue chose count-based splitting *for*
("so a review batch stays a predictable size regardless of how bursty a given account's activity
is"), and it is what makes a single global `N` correct rather than merely convenient. Per-account
sizing would only be needed to give different accounts deliberately *different review sizes*, which
nothing in the issue or this data asks for.

---

## Options considered

### A. Do nothing — leave splitting to the user running `fetch` several times

**Rejected.** This is precisely the tax the issue names: `--from`/`--to` are now normally omitted
(the coverage ledger resolves them), so there is no user-held date range left to chop up by hand
without reimplementing the ledger's own resolution logic per sub-period.

### B. Calendar-based (monthly) splitting

**Rejected — the issue's own non-goal, restated with the reasoning.** A monthly split produces
batches whose size tracks how bursty a given period was, not a size chosen for review — exactly
backwards from what makes a batch predictable. It is also already available today: running `fetch`
monthly *is* calendar-based splitting. The problem this ADR solves is specifically the case where
that is too coarse (a bursty account) or too fine to bother with by hand (a multi-month catch-up in
one run).

### C. Rebalance batches to even sizes (e.g. ceil-divide the total)

**Rejected**, matching the issue's explicit instruction and the precedent already set by the
existing month-chunk cache, which accepts a short trailing chunk rather than rebalancing across
chunk boundaries. Rebalancing would also break the filename promise in decision 4/§4: a rebalanced
boundary is chosen to make counts even, not because it is where the data actually ends, so the
date range in the name would no longer be the exact range #3 requires.

### D. Date-range-only filenames, with no batch marker anywhere in the scheme

**Accepted — this is decision 4.** Because decision 3 never splits a booking date, an account's batch
ranges are disjoint and strictly increasing, so the date range names every batch uniquely with no
tiebreaker and no change to `ofx_filename()` (§3). A batch file is indistinguishable from a
hand-ranged `fetch --from … --to …`, which is the intent.

### D′. An explicit `_partN` on every batch file, always present

The first draft of this ADR. Every batched file carries a marker regardless of whether anything
collides, so a file list announces that batching was used without the reader knowing the run's
parameters.

**Rejected.** Its stated justification was §3's touching ranges, which decision 3 eliminates, so it
was paying a permanent cost for a problem that does not survive the design. Every batched file would
be marked as *not* an ordinary statement when in substance it is exactly one. It also inverts the
project's existing filename logic, where the disambiguator is emitted **only on collision**; a marker
present unconditionally is the odd one out in a scheme built on tiebreakers. What it buys — being
able to tell from a filename that a set is a batched run, and hence to notice a missing file — is
real but small, and is bought at the price of every filename in the common case.

### E′. Split a booking date that exceeds `N`, and add `_partN` to name the pieces

The second draft. It keeps `N` a hard cap by cutting an oversized day by count, which produces
several batches sharing one date range and therefore needs a collision-breaking marker appended to
the filename.

**Rejected, and this one is a judgement rather than a measurement — the two designs are
indistinguishable on correctness.** It is internally inconsistent: decision 3 nudges boundaries off
same-day runs specifically to keep a day's transactions together, and this option abandons that rule
exactly when the day is busiest, which is when related entries are most likely to be separated. It
trades the principle for the number. It also reintroduces a filename component — and with it the
positional-index argument, an index-scoping rule, a zero-padding rule and a collision-lookahead — all
to serve a case §2's floor under `N` already keeps out of reach. Keeping the day whole costs one
oversized review list for a pathological day, bounded by that day's activity; splitting it costs a
permanent complication of the naming scheme and a rule that contradicts its own sibling.

### E. No same-day nudging — accept a boundary landing mid-run

**Rejected on §2.** At every batch size tested, this is the majority case, not a rare corner —
shipping it unaddressed means most real batched runs would split a same-day cluster across two
files, which undermines the same review-quality goal batching exists to serve.

### F. A per-bank or per-account `batch_size` override, added now

**Deferred, not rejected outright — but the reason changed under measurement, and the original one
was wrong.** An earlier draft argued that `--bank` already scopes a run, so per-bank sizes could be
had by running `fetch` twice. **§9 measures that claim as false on this user's data:** the busiest
and several of the quietest accounts share one bank key, spanning roughly 30× in activity, so no
per-bank value can serve both.

The option is still declined, on the argument §9 replaces it with: `N` is a **cap** on review size,
not a target, and a count-based split already takes the transaction count as its input — so one
global `N` produces one file for a quiet account and several for a heavy one *without* being told
which is which. Per-account sizing would only be needed to give different accounts deliberately
different review sizes, which nothing asks for. Adding the config surface now would also be unused
surface for a feature with zero users, the same argument `adr-aspsp-rate-limit-domain.md` decision 1
makes for not adding an unused `rate_limit_domain` override. Revisit if real use produces a case
where one account genuinely wants a different review size from its sibling.

### G. Make `--batch-size` and `--combine` mutually exclusive until a same-`ACCTID` import is run

The cautious option, and the one an earlier draft of this ADR took: refuse the combination at the
CLI until someone imports a file carrying one `BANKID`+`ACCTID` pair in two `<STMTTRNRS>` blocks.

**Rejected on §7.** The 2026-08-10 GnuCash 5.16 imports already observed both halves of that shape —
per-statement account matching (round 1) and silent binding of an already-known pair (round 2) — and
observed that all statements' transactions arrive in **one flat import window with a per-line account
column**, so a transaction's destination is resolved from its own statement rather than from the
file. More decisively, #8's fail condition ("transactions from more than one statement landing in a
single register") is what batching *wants*: there is no wrong account for a repeated pair to route
to. Blocking the combination would be paying a real usability cost — the two flags solve opposite
halves of the same import-ergonomics problem and compose naturally — to re-answer a question the
recorded evidence already decomposes. The residues §7 names are cosmetic (a possible duplicate
matching prompt) or narrow (two `LEDGERBAL`s in one document), and both are named as open questions
rather than used to gate the whole combination.

### H. Count-based batching, oldest-first, never splitting a booking date, named by date range alone,
final-batch-only live balance, opt-in and global, composing with `--combine`

**Accepted** — below.

---

## Decision

### 1. Count-based batching: chronological by booking date, oldest first, up to `N` per batch, remainder in the last

The mechanics the issue already specifies, restated as this document's binding decision. `txns`
(already deduplicated by stable id, and already annotated by `pair_conversions()` — §6) is split
into consecutive groups of at most `N`, ordered oldest first by booking date; the final group is
whatever is left over, however small. No rebalancing (option C).

**`N` is a cap with exactly one exception, and decision 3 states it:** a single booking date is never
divided, so a day holding more than `N` transactions forms one batch larger than `N`. Every other
batch honours the cap.

### 2. `--batch-size N` is one flag, global to the run

Applies uniformly to every account of every bank fetched in that invocation. No per-bank or
per-account override in this design (option F): `N` is a **cap on review size, not a target**, and
because a count-based split takes the transaction count as its input it already self-adjusts — one
global `N` leaves a quiet account writing the single file it writes today while splitting a heavy
one, with no per-account configuration. §9 measures a ~30× activity spread *inside a single bank
key*, which is both why per-bank sizing would not have helped and why per-account sizing is
unnecessary.

**No default value.** Omitting `--batch-size` is off, exactly as today. The number the docs should
*suggest* when a user asks what to put here is **120** (§10) — high enough to stay inert on an
ordinary run and split only a multi-month catch-up. §8's floor still binds from below: a value at the
small end of the sweep lets the measured same-day-run ceiling force oversized batches. A suggestion
in `--help` and the README, never a value the tool picks on its own, and a user whose review
tolerance differs should pick differently.

### 3. A booking date is never split across batches — nudge direction left open

Resolved by §2: yes, nudge. A boundary that would otherwise fall inside a run of same-day
transactions moves to the edge of that run instead — never a boundary strictly inside one date's
transactions. **This decision does not fix which edge.** Extending the earlier batch forward to
swallow the run, versus pulling the boundary back so the whole run rolls into the next batch, both
satisfy "never split a same-day run," and no local data favors one over the other — see *Open
questions*.

**A booking date is never split, so `N` is a soft cap.** When a single day holds more than `N`
transactions there is no boundary between dates to nudge to, and the day becomes one batch larger
than `N`. This is a deliberate limit, not an oversight: **day coherence is the principle nudging
exists to serve, and splitting a day when it grows large would abandon that principle exactly where
it matters most.** A design that nudges boundaries off same-day runs to keep a day's transactions
together, and then cuts a day in half as soon as it is busy, is not internally consistent — it would
be trading the rule for the number.

The cost is real and named: for a pathological day, one review list exceeds the size the user asked
for. It is bounded by a single day's activity, and a day's transactions are the one grouping a
reviewer least wants split — related entries (a payment and its fee, the two sides of a transfer)
sit together, and seeing them in one sitting is what makes the review correct. §2 puts a floor under
`N` that keeps this case out of reach entirely for every account measured.

An earlier draft of this decision split such a day by count and introduced a `_partN` filename marker
to name the pieces. That is rejected — see option E′ — and its removal is what lets decision 4 leave
the filename scheme untouched.

### 4. Filenames do not change at all — the date range already names every batch uniquely

```
<bank_key>_<currency>[_<disambiguator>]_<period_start>-<period_end>.ofx
```

That is today's scheme, unaltered. **`ofx_filename()` gains no parameter and no component**; the only
change is that the write step calls it once per batch instead of once per account, with each batch's
own first/last transaction date.

**This is a guarantee, not a probability.** Because decision 3 never splits a booking date, every
batch ends on a strictly earlier date than the next one begins. An account's batch ranges are
disjoint and strictly increasing, so two batches of one account can never produce the same filename
(§3). There is no collision to break, and therefore nothing to break it with.

**The consequence is intended, not tolerated: a batch file is named exactly as if the user had run
`fetch --from … --to …` by hand for that sub-window.** A batch is not a special kind of artifact that
needs announcing in its filename — it is an ordinary statement over an exact, re-derivable range, and
it should look like one. Handed to another OFX tool, or found in `output/` a year later, it is
self-describing on the same terms as every other file this project writes. Lexicographic order stays
chronological because the date components are already fixed-width, so a batched account's files sort
into reading order for free.

**What this costs, stated plainly:** nothing in a filename says a run was batched, so a set of batch
files is not recognisable as a set and a missing one cannot be spotted from the listing. That is the
price of the equivalence above, and it is accepted rather than mitigated — see *Consequences*.

**It also retires the positional-index question entirely.** [`decisions.md`](decisions.md) rejects a
positional index for the disambiguator ("the same account could change its number between runs"),
and two earlier drafts of this decision had to argue why a batch index would not repeat that mistake.
No such argument is needed now: there is no index. The existing rule — identity components, then the
period, then a tiebreaker only if the period is not unique — is left exactly as it stands, with
batching simply never reaching the third clause.

### 5. `LEDGERBAL`: only the final batch of a window reaching today carries the live balance

Direct generalization of the existing single-file rule (`AGENTS.md`: `LEDGERBAL` carries the bank's
balance only when the window ends today or later) — no new balance logic, as the issue specifies.
`Account` is a frozen dataclass, so the mechanism is `dataclasses.replace(account, end_balance=None)`
for every batch but the last of an account whose overall fetch window reaches today; the last batch
keeps the account's real `end_balance`. Every earlier batch takes the existing `end_balance is None`
path in `build_statement()` — a zero-based running total, computed **independently per batch, not
chained from the previous batch's total**. This is an accepted, named cost (see *Consequences*), not
an oversight: chaining balances across batches is new balance logic, and the issue is explicit that
this generalization should not add any.

### 6. `--dry-run` predicts no paths while `--batch-size` is set

Dry-run never sees transaction data (`AGENTS.md`: "`--dry-run` spends nothing, structurally"), and
batch boundaries — including whether a same-day nudge would even apply — depend entirely on
transaction counts and dates, which is exactly what dry-run cannot know. Rather than predict a
filename that decision 4 makes exact only after real data exists, `--dry-run --batch-size N`
predicts **no paths at all**, for the whole run, and states why on stderr (e.g. "`--batch-size` is
set; batch filenames depend on transaction counts and cannot be predicted"). This mirrors the
existing precedent of suppressing a whole bank's prediction rather than guess (`AGENTS.md`: "one
account with no link-time currency suppresses its whole bank's prediction... never 'improve' this by
predicting the accounts that are known") — applied here to the whole run, because `--batch-size` is
global (decision 2), so every account's prediction is equally unknowable once it is set.

### 7. Coverage ledger: untouched

§5. Batching is strictly a change to how `pending` becomes files, downstream of where
`with_span()`/coverage persistence already runs in `fetch_bank`'s loop. Nothing in `coverage.py`
changes, and no test there needs to.

### 8. `--batch-size` and `--combine` compose

§7. A batched account contributes its N statements to the run's combined file exactly as an
unbatched account contributes one; `combine_statements` needs no change, because "more statements"
is the only thing batching hands it. The two flags pull in opposite directions on *file count* and
in the same direction on *import ergonomics* — `--combine` removes the per-account assistant runs,
`--batch-size` keeps each review list short — so refusing the combination (option G) would block the
configuration a large catch-up most wants: one file to open, several right-sized statements inside
it.

Ordering within the combined file follows `pending`, i.e. one account's batches stay adjacent and in
chronological order. Round 1 observed GnuCash flattening every statement into a single import window
keyed per line, so ordering is presentational, not semantic — but adjacent-and-chronological is what
a human scanning that window expects, and it costs nothing to preserve.

**What this decision does not claim.** The routing conclusion is a decomposition of two directly
observed imports (§7), not a third import of a literal same-`ACCTID`-twice file. The two residues
§7 names stay in *Open questions* — the duplicate-prompt cosmetic and the two-`LEDGERBAL` question —
and the second is worth answering before a batched `--combine` run is relied on for reconciliation.
Neither can misplace a transaction, which is why they are questions rather than a gate.

### 9. Both packaging flags are configurable, in a new `[fetch]` section, global to the run

`--combine` and `--batch-size` are **preferences, not per-run decisions**: a user who wants combined
output wants it every run, and having to retype it is not just friction — *forgetting* it silently
changes the output shape, which is the worse failure. Both become settable in `config.toml`:

```toml
[fetch]
# Write every account's statement into one OFX file instead of one file per account.
combine = true
# Split each account's statements into batches of at most this many transactions.
batch_size = 25
```

**A new `[fetch]` section, not `[output]`.** The competing read is that both flags are pure output
packaging — neither changes a single API request (§5, and `adr-combined-ofx-file.md` decision 7) —
which would put them beside `[output] dir`. `[fetch]` wins on room to grow: it is the section a later
fetch-shaped default would also belong in, whereas `[output]` would then hold two unrelated kinds of
thing. **`--refresh` must never join it** — it spends rate-limit allowance and has to stay a
deliberate per-run act, not something a config file can turn on permanently.

**Global to the run; no per-bank override**, for the same reasons as decision 2. `combine` cannot be
per-bank even in principle — the combined file spans banks by construction, and per-bank combining
was already rejected as option F of `adr-combined-ofx-file.md`. `batch_size` could be, but §9's
measurement removes the motivation: the spread that would justify per-bank sizing sits *within* a
single bank key, where a per-bank value cannot reach it, and a global cap already self-adjusts to
each account's own volume. There is also a mechanical reason to keep both out of
`[banks.*]`: `load_config` stringifies every per-bank option (`config.py`, `options = {k: str(v) …}`),
so a per-bank int or bool would need parsing and error-reporting that a top-level section gets from
`tomllib` for free.

**Precedence is CLI explicit > `config.toml` > built-in default**, and the built-in defaults do not
move: no combining, no batching. Turning a configured setting off for one run needs real off-switches,
since `store_true` cannot express "off":

- `--combine` becomes `argparse.BooleanOptionalAction`, giving `--combine` / `--no-combine`.
- `--batch-size N` gains `--no-batch-size`. Not `--batch-size 0`: a magic integer has to be
  documented as meaning "off" rather than rejected as invalid input, and `N <= 0` should stay a
  plain validation error.

**The cost this introduces, named because it is the real one:** behaviour set in `config.toml` is
invisible in `--help` and invisible in the command the user typed, so a surprising output shape is
harder to trace than it is today. **`fetch` and `--dry-run` must therefore state the effective
packaging on stderr** — which flag values are in force and whether they came from config — the same
stderr-is-for-humans split `AGENTS.md` already requires, and the same reason decision 6 makes
`--dry-run` explain itself rather than stay silent.

**Scope note.** The `combine` half of this decision changes the configuration surface of a flag that
already shipped ([#31](https://github.com/skolima/gnucash-ofx/issues/31)), not one this ADR
introduces. It is decided here because the mechanism — the section, the precedence rule, the
off-switch convention, the stderr disclosure — is one piece of work serving both flags, and shipping
`[fetch] batch_size` without `[fetch] combine` would leave an obviously asymmetric config file. If
this ADR is accepted but the `combine` half is judged to belong to its own change, it splits cleanly:
the section and precedence rule stay, and only the `--combine`/`--no-combine` wiring moves.

### 10. Untouched

`FITID`, `BANKID`, `ACCTID` and their resolution; the cache and its key; the state schema; the
coverage ledger's schema and semantics; the `NAME`/`MEMO`/`CHECKNUM` composition;
`sources/enablebanking.py`; per-account and per-bank failure isolation; the stdout-is-the-file-list
contract (a batched account simply contributes more lines to the same list). Each batch keeps the
exact same account identity as an unbatched fetch — only `period_start`/`period_end` and the derived
filename differ between batches of one account, which is not a new shape for GnuCash's importer to
see: re-running `fetch` on a weekly cadence already produces several files for one account over time,
each with its own `BANKID`+`ACCTID`+date-range triple. Batching produces that same familiar shape
within one run instead of across several — for the default (non-`--combine`) packaging path, this is
not new risk.

### 11. Out of scope

- **Per-bank/per-account `batch_size`** (option F) — the `--bank`-scoped workaround exists today;
  revisit if it proves insufficient.
- **`fetch` suggesting or defaulting to batching.** The issue's non-goal is explicit that this is not
  a default-behavior change, and this design adds nothing that recommends a batch size — see *Open
  questions* for why the suggestion question itself stays open rather than closed either way.
- **Rebalancing the last batch** (option C) — rejected, not merely deferred.
- **Cross-batch balance chaining** (decision 5) — would be new balance logic, which the issue rules
  out.
- **A `--combine`-specific batch marker.** Inside a combined file the batches are `STMTTRNRS` blocks,
  not filenames, and each already carries its own `DTSTART`/`DTEND`. Nothing is added.
- **Announcing batching in the filename.** Decision 4 deliberately makes a batch file
  indistinguishable from a hand-ranged fetch, so there is no marker, count or manifest saying "this
  set came from one batched run" (option D′). Detecting a missing file from the names alone is
  therefore not possible, and is not attempted.
- **Making `N` a hard cap.** A booking date larger than `N` stays whole (decision 3, option E′).

---

## Consequences

**Accepted costs.**

- **A batched account's earlier files each show a reconcile-dialog balance starting from zero, not
  continuous with the previous batch's total** (decision 5). This is the existing single-file
  closed-window wart — `adr-aspsp-rate-limit-domain.md` decision 4 already calls that "a choice
  between two false values, not a fix" — now repeated once per earlier batch instead of appearing
  once. A user reconciling batch 2 sees no arithmetic relationship to batch 1 in the file itself.
- **`--dry-run` loses its predictive value entirely whenever `--batch-size` is set** (decision 6).
  This is a real, named gap in dry-run's usefulness for exactly the runs where batching matters most
  — a large catch-up. The alternative (a partial or hedged prediction) was rejected as worse: a dry
  run that guesses is worse than one that refuses, per the existing `--dry-run` invariant.
- **One more axis a bug report has to describe** — file packaging can now vary by batch size as well
  as by `--combine`, echoing the same cost `adr-combined-ofx-file.md` accepted for its own flag. Off
  by default keeps it cheap.
- **A batch file is indistinguishable from a hand-ranged fetch, by design** (decision 4). The
  filename carries no evidence that batching was used, so a set of batch files cannot be recognised
  as a set, and a missing one cannot be spotted from the listing. This is the deliberate price of
  the equivalence decision 4 is built on — a batch really *is* an ordinary statement over an exact
  range — but it is a genuine loss against option D′, and it is the reason `--dry-run`'s refusal to
  predict (decision 6) has to be explicit rather than silent: the filenames will not tell the user
  afterwards what they were not told beforehand.
- **`N` is a soft cap: a booking date larger than `N` produces one oversized batch** (decision 3,
  option E′). For a pathological day the user gets a review list bigger than the one they asked for,
  and no flag value prevents it — only a larger `N` keeps it out of reach. Accepted because the
  alternative abandons day coherence exactly where it matters most, and because the overrun is
  bounded by a single day's activity. §2 measures where the floor under `N` sits.
- **Packaging behaviour becomes invisible in the command line** (decision 9). Once `config.toml` can
  set `combine`/`batch_size`, the command a user types no longer describes what the run will produce,
  and a bug report has to include the config file as well as the command. This is the price of the
  flags being preferences rather than per-run choices, and it is paid down — not removed — by the
  effective-packaging line decision 9 puts on stderr.
- **A batched `--combine` run puts several statements for one account in one document**, which is a
  document shape no import has literally exercised (§7). The routing argument is a decomposition of
  two observed imports rather than a third observation, and the reconcile behaviour with two
  `LEDGERBAL`s for one account is genuinely open. Accepted because neither residue can misplace a
  transaction — the #8 failure mode is structurally unavailable here — but it is why decision 8
  carries a "does not claim" paragraph rather than a clean tick.

**Cost to reverse: low.** Nothing here touches `FITID`, `BANKID`, `ACCTID`, the cache, or the state
schema, so no account orphans and no transaction re-imports. Reversing the code is deleting a flag,
the batching helper, its tests, and a `config.toml` key — an unknown key in `[fetch]` should be
reported rather than ignored, so a reverted install tells a user their setting no longer exists
instead of silently changing what they get. A user who imported batched files and then reverts to an
unbatched fetch of the same window gets files GnuCash's `FITID` dedup already handles correctly —
the same reversal argument `adr-combined-ofx-file.md` makes for its own flag.

---

## Open questions

### Which edge does the same-day nudge move the boundary to?

**Not settled here, and not settleable by local measurement.** Extending the earlier batch forward
(never fewer than `N` per batch, sometimes more) and pulling the boundary back (never more than `N`
per batch except the run itself, sometimes fewer) both satisfy "never split a same-day run," and no
data in this sample argues for one over the other. **What would settle it:** this is an
implementation-time design pick, not evidence — the implementing PR should choose one, pin it with a
test using a synthetic same-day cluster, and state the choice in code where a future edit would
otherwise silently flip it. The case where a single booking date exceeds `N` is **not part of this
question** — decision 3 settles it independently of direction: the day stays whole and becomes one
oversized batch either way.

### Does batch size actually change how well GnuCash's matcher learns?

**The question the whole feature rests on, and the one no measurement here touches.** §8 establishes
what `N` does to *files* — how often a window splits, how often a remainder is uselessly short — and
§2 what it does to *boundaries*. None of it says a 40-transaction review list trains GnuCash's
Bayesian matcher better than a 400-transaction one. That premise comes from the issue's own
reasoning about human review behaviour, and it is assumed, not shown.

**What would settle it:** not local data and not a probe — a person importing two comparable windows
into two throwaway books, one batched and one not, and comparing how many rows GnuCash pre-matches
on the *following* import. Zero ASPSP requests; a keyboard gate, like the `--combine` verification.
Worth naming plainly: if this premise is wrong, `--batch-size` is still harmless and still useful for
splitting an unwieldy list, but its headline justification would be the ergonomics, not the matcher.

### ~~Do exported CSV volumes match what this tool actually fetches?~~ Answered — §11

**Mostly yes, with one systematic exception that pushes the recommendation up rather than down.**
See §11: exact agreement month-for-month at a bank-style institution; per-transaction fees split
into their own entries by the API and merged into a column by the export at a fintech-style one.
The residue — a month-matched comparison for the highest-volume account, which needs one fresh
export from the bank's own web banking — is a keyboard gate costing the API nothing, and is
recorded in §11 rather than left here as though it blocked anything.

The original framing, kept because it was wrong in an instructive way:

§8's sizing corpus is **historical bank exports, not Enable Banking responses**, and only one of its
twelve institutions is among this project's configured banks. Pending-versus-booked handling, fee
entries split or merged, and card authorisations could all shift the per-window counts that the `N`
recommendation is calibrated against.

**What would settle it, and it is free:** compare one account's transaction count for one month in
its export CSV against the same account-month already sitting in `cache/`. Both sides exist locally
already, so this is **not a probe** — it spends no allowance and needs no consent. Worth doing before
the 25–50 recommendation reaches the README, since a systematic granularity difference would move
the band.

### ~~Which `LEDGERBAL` does GnuCash use when one document carries two for the same account?~~ Answered — §12

**Neither — GnuCash 5.16 computes the reconcile pre-fill from the register and consults no
`LEDGERBAL` at all** (-333.00 and -1110.00 pre-filled, matching the register totals exactly; see
§12). The harmless outcome, and one that leaves decision 5 as defence in depth for other libofx
callers and other versions rather than behaviour observable in this one.

The original framing, kept because its "what would settle it" is now the re-verification procedure:

§7's second residue, and the one worth answering before a batched `--combine` run is trusted for
reconciliation. Decision 5 gives earlier batches a computed running total and the final batch the
live balance; combined into one file, that is two `<LEDGERBAL>` values for one `BANKID`+`ACCTID`.
Across separate files this already happens and GnuCash sees it as two imports; within one document
it is new. **What would settle it:** a synthetic two-`STMTTRNRS` file sharing one `BANKID`+`ACCTID`
with different date ranges and different `LEDGERBAL`s, `ofxdump`-checked, then imported into a
throwaway book with the reconcile dialog opened afterward — the same procedure and the same
zero-ASPSP cost as the original `--combine` verification. A wrong answer here misstates a balance in
a dialog; it does not misplace a transaction.

### ~~Does a first-ever batched import prompt once or twice for the same account?~~ Answered — §12

**Once.** One matching prompt and one account offered for creation across three statements naming
the pair; the follow-up import into the same book prompted zero times (§12). GnuCash 5.16
de-duplicates identical `BANKID`+`ACCTID` pairs within a file's matching phase, so even the
cosmetic worst case does not occur.

The original framing:

§7's first residue. Round 1's per-statement matching passes ran against three distinct pairs, so
whether GnuCash de-duplicates identical `BANKID`+`ACCTID` pairs within one file's matching phase is
unobserved. **What would settle it:** the same import as above, into a book where the account does
*not* already exist. Purely cosmetic either way — worst case is one redundant prompt — so this rides
along with the `LEDGERBAL` check rather than justifying its own session.

### Settled already, recorded so they are not re-derived

Following [`adr-combined-ofx-file.md`](adr-combined-ofx-file.md)'s convention. Each of these was
raised while reviewing this design and is answered by something already on `main`:

- ~~Can two batches of one account collide on `FITID`, or defeat GnuCash's dedup?~~ **No, by
  construction.** Batching *partitions* one account's already-deduplicated `txns` list, so every
  transaction appears in exactly one batch and no `FITID` repeats across batches of a run. OFX
  scopes `FITID` uniqueness to the `ACCTID` in any case
  ([`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) §5), and `AGENTS.md`'s stable-`FITID`
  invariant is what makes a re-import of an overlapping window dedup correctly — unchanged here,
  since batching never synthesizes an id.
- ~~How are one account's batches ordered inside a `--combine` file, and does it matter?~~
  **Adjacent and chronological, and it is presentational only** — decision 8. Round 1 observed
  GnuCash flattening every statement into one import window keyed per line, so ordering does not
  affect routing.
- ~~Does the batch marker reopen the positional-index mistake [`decisions.md`](decisions.md)
  rejected?~~ **The question no longer arises: there is no marker.** Decision 4 leaves
  `ofx_filename()` untouched, so the existing rule stands unmodified and batching never reaches its
  tiebreaker clause. Two earlier drafts needed an argument here; the third needs none.
- ~~Is an explicit marker needed on every batch file, as §3's touching date ranges suggested?~~
  **No — §3 was measured on un-nudged splitting and the first draft read it backwards.** Every
  touching pair it found is a boundary decision 3 moves. Rejected as option D′.
- ~~Does `ofx_filename()` need the third component the issue asked for?~~ **No** — §3: with a
  booking date never split (decision 3), an account's batch ranges are disjoint and strictly
  increasing, so the date range is already unique. The issue's open question about index-versus-range
  dissolves rather than resolving either way.
- ~~Does batching need anything from `coverage.py`, or change what a window records?~~ **No** — §5:
  coverage advances from the requested window before `pending` is ever split into files.
- ~~Can batching split a paired currency conversion across two files?~~ **No** — §6: the two legs
  live in different accounts by the one-statement-per-account-per-currency invariant, so they were
  never in the same list to split.
- ~~Does §2's same-day clustering rate generalize beyond this machine's thin cache?~~ **Yes** — §2:
  re-measured 2026-08-12 over 24 account series across 12 institutions spanning 2012–2026, and both
  the multi-transaction-day share and the longest-run ceiling reproduced almost exactly. This was
  an open question in the first draft; it is now the basis of §8's floor under `N`.
- ~~Would a per-bank `batch_size` give per-account sizing in practice?~~ **No** — §9: the widest
  activity spread sits *inside* one bank key, so `--bank` cannot separate the accounts it would need
  to. The earlier draft asserted the opposite; the measurement corrected it, and decision 2 now
  rests on `N` being a self-adjusting cap instead.

### Should `fetch` ever suggest batching itself?

The issue's open question #4, and it stays open here as a judgment call, not a measurement. §1
supports "batching would help in real observed cases" but does not mandate any particular UX for
suggesting it. **Partly answered 2026-08-12 — §10.** The owner's own review tolerance is now on record (a month is
comfortable; a busy month is near the limit), which is what set the recommended value. That is the
*ergonomic* half of the premise and it is enough to size the flag. **The matcher half stays open:**
nothing shows that a shorter review list trains GnuCash's Bayesian matcher better, only that a long
one is unpleasant. If the matcher claim turns out to be false the flag is still worth having — it
still cuts an unreviewable window down to a reviewable one — but the ADR's headline justification
would become ergonomics rather than matcher training, and the issue's argument 2 would need
retracting.

**What would settle the rest:** not local evidence — real usage of
`--batch-size` once it ships, to see whether an unbatched run's
review length is a good enough proxy to warn on. If it is ever adopted, the existing `FetchWarning`
channel (`adr-coverage-ledger-and-warnings.md` decision 8) is the natural mechanism — a warning,
never a `BankFailure`, never moving the exit code — rather than a new reporting path.

---

On acceptance, decisions 1–10 condense into [`decisions.md`](decisions.md), and decision 4 extends
the existing *Output filenames* entry there **without changing it** — the point of decision 4 is that
the rule already covers batching. What condenses is the reason: **"a batch is an ordinary statement
over an exact range, so it is named like one; never split a booking date, and the range stays
unique."** This file stays for the rejected options — D′ and E′ especially, which were this ADR's own
first and second drafts — the measurements, and the open questions that need a keyboard, a spike or
real usage rather than more local data.

## References

- [#14](https://github.com/skolima/gnucash-ofx/issues/14) — the issue this decides.
- [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) — already shipped;
  §5 shows its coverage-advance step is untouched by this design, and decision 8 there is where a
  future batching-suggestion warning would live.
- [`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) — §3/§5's `BANKID`+`ACCTID` measurement and
  the two 2026-08-10 GnuCash 5.16 import verifications behind §7 and decision 8; the reversal
  argument decision 8's *Consequences* reuses.
- [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — decision 1's "err narrow,
  no escape-hatch config until it has a user" reasoning, reused for decision 2's flag scope; decision
  4's framing of the existing `LEDGERBAL` wart as "a choice between two false values."
- `AGENTS.md` — the `LEDGERBAL` invariant this generalizes (decision 5), the filenames-follow-the-
  connection invariant this generalizes (decision 4), the `--dry-run` invariant decision 6 applies,
  and the `AGENTS.md#data` rule behind §1's withheld transaction counts.
