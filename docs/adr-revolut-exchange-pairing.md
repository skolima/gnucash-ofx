# ADR: Revolut `EXCHANGE` legs pair on `entry_reference` now, on the working assumption that both legs book on one date — `_validate`'s date check stays strict

**Status:** Decision 1 implemented in
[#53](https://github.com/skolima/gnucash-ofx/issues/53) — see *Where this stands* below; accepted
in [#52](https://github.com/skolima/gnucash-ofx/issues/52). Behind it: a
local-evidence pass run **2026-08-19** on the owner's machine over the real `cache/`, `state/` and
`state/fetch-log.jsonl`, executing the project's own functions (`_conversion_key` with the proposed
row attached, `conversions.pair_conversions`, `_validate`) with no network call; plus the
2026-08-12 first-fetch record and the 2026-08-13 GnuCash import gate inherited from
[`adr-revolut-onboarding.md`](adr-revolut-onboarding.md).
**Date:** 2026-08-19.
**Scope:** `src/gnucash_ofx/sources/enablebanking.py` — one row in `_conversion_key`'s deal-key
table — and one offline test over `tests/fixtures/revolut_transactions.json` (the fixture already
carries the byte-identical cross-pocket pair; onboarding ADR decision 8 property 2 was authored for
exactly this, so the fixture itself does not change). **Explicitly not in scope:**
`conversions.py` — `_validate` keeps all six checks, including one booking date (decision 2 below);
`_transaction_id`'s fallback order; `models.py`, `ofxout.py`, `run.py`; `FITID`, `BANKID`,
`ACCTID`; the cache key; the state schema; every other source's key rows.
**Issue:** [#50](https://github.com/skolima/gnucash-ofx/issues/50). Inherits the pairing
machinery and its invariants from
[`adr-currency-conversion-pairs.md`](adr-currency-conversion-pairs.md) decisions 1–4 and 6
(per-source key table, all-or-nothing validation, rate from booked amounts, identical annotation on
both legs, `FITID` untouched by pairing), and the Revolut record from
[`adr-revolut-onboarding.md`](adr-revolut-onboarding.md) decisions 8–10 and open questions 4–5.

---

## Where this stands

- [x] **1.** `bank_transaction_code.code == "EXCHANGE"` → deal key `revolut:<entry_reference>`,
      the third row in `_conversion_key`'s table, plus the offline fixture test
      ([#53](https://github.com/skolima/gnucash-ofx/issues/53))
- **2.** The one-booking-date working assumption — nothing to tick: it ships as the *absence* of a
      `_validate` change. Its tripwire is the named revisit trigger (see the decision); if that
      trigger ever fires, the finding goes back to #50 before any code moves
- **3.** Untouched — not a decision to ship, nothing to tick: `_transaction_id`'s
      `transaction_id` → `entry_reference` → hash order, `_validate`'s six checks, and every
      identity field (`FITID`/`BANKID`/`ACCTID`), per onboarding ADR decision 10

---

## Context

### What exists today, and what it costs

The pairing machinery is generic and measurably **inert** on Revolut data: Revolut's code is
`EXCHANGE`, not Wise's `CONVERSION`, and no `BALANCE-<digits>` remittance element exists, so
neither existing branch of `_conversion_key` (`sources/enablebanking.py:89-100`) fires and every
Revolut conversion leg carries `conversion_key=None`.

The cost is measured, not hypothetical (onboarding ADR decision 9 and the 2026-08-13 gate):
`compose_name` produces the word-for-word identical `Exchanged to <CUR>` on every leg of every
conversion into one currency, no rate is stated anywhere (`exchange_rate` is a present-but-null key
on 100% of rows), and the GnuCash gate observed a pair's legs importing as two separate
transactions — one against a manually assigned transfer account, one against Imbalance. The user
gets no counter-amount, no counter-currency and no rate in the register, on the source where the
rate is derivable *only* from the pair.

Issue #50's original gate was "do not commit to the join key until `entry_reference` has been seen
pairing across booking dates". This ADR replaces that gate with a working assumption (decision 2),
because the 2026-08-19 pass showed the gate has an unbounded horizon and the failure mode of
proceeding is refusal, not mis-pairing. The gate's replacement is argued under rejected option A.

### Measurement record, local-evidence pass 2026-08-19

Over all cached Revolut data plus `state/` and `state/fetch-log.jsonl`, real functions executed,
no network call. Scale, stated once and used as the ceiling throughout: **a single-digit number of
deals, all booked on one day, in one active month of one connection.**

1. **Corpus shape — and a structural fact decision-2 narratives elsewhere did not predict.**
   Revolut cache chunks exist under two uid generations. Old (pre-relink) uids: 4 months per pocket
   (2026-05..08), of which 2026-05/06/07 are claimed-but-empty on all 5 pockets. Current uids:
   exactly 1 month per pocket — 2026-08, claimed 2026-08-01 → 2026-08-18 — because **the
   post-relink re-fetch opened its window at 2026-08-01 and did not re-buy 2026-05..07**; the
   settled empty months live only under dead uids, so the current session's cache coverage starts
   2026-08-01. All cached Revolut activity sits inside 2026-08. Fetch-log: 67 Revolut requests,
   all 200, across 4 days.
2. **The key clusters perfectly.** Clustering on `entry_reference` — current session, old session,
   and their union alike: every `EXCHANGE` cluster exactly size 2; zero size-1 `EXCHANGE` legs;
   zero clusters of size ≥ 3 under any code; every `TOPUP`/`TRANSFER` row a singleton; zero
   duplicate references within a pocket. `entry_reference` present on 100% of rows;
   `transaction_id` null on 100%.
3. **The existing validation passes end to end.** Every size-2 `EXCHANGE` cluster passes all six
   `_validate` checks (two legs, distinct accounts, distinct currencies, one booking date, non-zero
   amounts, opposite signs); zero failures on any check. `pair_conversions` with the proposed key
   attached a `Conversion` to every `EXCHANGE` leg and to no other row.
4. **Cross-date evidence: still censored — an absence of a test, not a pass.** The whole corpus
   spans exactly 2 distinct booking dates; every `EXCHANGE` leg books on the single earlier date;
   the later date carries only non-`EXCHANGE` rows. Zero clusters span dates because zero clusters
   *could*. `booking_date == value_date` on 100% of rows. Multiple deals share the one date, so
   rejected option B's measured false-pair hazard stands unchanged.
5. **No fee or multi-leg contamination.** Zero non-`EXCHANGE` rows share a reference with an
   `EXCHANGE` row; zero fee-shaped codes anywhere in the corpus.
6. **Vocabulary.** Exactly `EXCHANGE` / `TOPUP` / `TRANSFER`; `sub_code` null throughout. The
   vocabulary remains **unclosed** (one connection, roughly one active month) — onboarding ADR open
   question 5 stands.
7. **Re-link stability — the strongest new result.** Month 2026-08 was fetched under **both** uid
   generations (old uids 2026-08-14, current uids 2026-08-18). Per pocket, the `entry_reference`
   sets are **equal on 5 of 5 pockets, zero asymmetric values**. For one re-link cycle,
   `entry_reference` — which is both the proposed deal key and the `FITID` — was byte-stable across
   sessions. The key survives the event that regenerates every uid.

---

## Decisions

### 1. Add the third row to `_conversion_key`'s deal-key table: `code == "EXCHANGE"` → `revolut:<entry_reference>`

**Commits to** one row, in the same shape as the existing two (pairing ADR decision 1's table is
the artifact to read when a new source arrives — this is that table growing a row):

| Source | Signal | Key |
|---|---|---|
| Revolut | `bank_transaction_code.code == "EXCHANGE"` | `revolut:<entry_reference>` |

Namespaced like the existing `wise:`/`kantor:` rows, so no cross-source collision is possible.
**Extraction is read-only** — the same field is the `FITID` source (`_transaction_id` falls through
to `entry_reference` because `transaction_id` is null on 100% of Revolut rows), so the key must
never be consumed, mutated or stripped; `_conversion_key` already only reads, and this row keeps it
that way. The onboarding ADR's caution stands: any code assuming "one `entry_reference`, one
transaction" is wrong on this data by construction — the two legs of every deal share the value.

Tested offline against `tests/fixtures/revolut_transactions.json`, which carries the byte-identical
cross-pocket pair for exactly this purpose (onboarding ADR decision 8 property 2). No fixture
change, no network, no counted request.

**Rules out:** keying on remittance text or any shape rule (rejected options A–C settle the
alternatives); touching `_transaction_id`; any test asserting the `bank_transaction_code`
vocabulary is closed — a new code appearing on conversion legs is a measurement to bring back to
#50, not a bug (onboarding ADR decision 8 property 3, open question 5).

### 2. Adopt the working assumption that Revolut never books an exchange's two legs on different booking dates — without measuring it, and without loosening `_validate`

**Commits to** proceeding now, with `_validate`'s one-booking-date check exactly as it is. The
assumption is adopted **unmeasured** — the 2026-08-19 pass is censored on this question (point 4:
the corpus contains one booking date for all `EXCHANGE` legs, so zero clusters could span dates) —
and it is safe to adopt unmeasured because of which way the machinery fails: a cross-date deal
would present a two-leg cluster that `_validate` **refuses** on the date check, producing an
unannotated pair. That is exactly today's behaviour for every Revolut deal. The failure mode is
refusal, never a false pair; being wrong costs nothing that is not already being paid, on every
deal, today.

**Rules out** loosening the date check — now or on any future guess. The check is shared by every
source (Wise, Kantor, Revolut alike), and loosening it speculatively is the thing #50's own text
forbids: "if different-date pairs are real, the decision is about that shared validation check, not
about the key, and it must not be loosened on a guess."

**The revisit trigger, by name:** if a later local re-clustering pass over the cached responses
ever finds an `EXCHANGE` cluster refused by the date check, **or a size-1 `EXCHANGE` leg**, that
finding goes back to #50 before any loosening is proposed. The same pass is the instrument that
answered onboarding open question 4 this far, it costs zero requests, and it covers the fee /
multi-leg case too: a third row sharing a reference makes the cluster size 3, which `_validate`
refuses just as safely.

### 3. Untouched

`_transaction_id`'s `transaction_id` → `entry_reference` → hash order (measured and deliberately
kept, onboarding ADR decision 10); `_validate`'s six checks; `FITID`, `BANKID`, `ACCTID` and every
other identity field; the cache key; the state schema; the other sources' key rows. Pairing changes
description text only (pairing ADR decision 6). Recorded so that if one of these moves in the
implementing PR it reads as scope drift.

---

## Rejected options

### A. Keep the issue's original gate: wait until `entry_reference` is seen pairing across booking dates

**Rejected on the 2026-08-19 measurement of what the wait would buy and what it costs.** The gate
has an unbounded horizon: the whole corpus spans 2 booking dates with every deal on one of them,
so the observation the gate waits for may never occur naturally — and no probe can force it (a
counted request cannot manufacture a midnight-straddling deal; see open question 1). Meanwhile the
cost of waiting accrues per deal: every conversion stays unannotated, rate-less and
register-ambiguous, which is the measured cost the pairing exists to remove. The asymmetry decides
it: if the assumption is wrong, `_validate` refuses and the affected deal is unannotated — the
status quo — while every same-date deal (100% of those observed) is annotated correctly. Waiting
buys protection against a failure mode that is indistinguishable from not shipping at all. The
re-link stability result (measurement point 7: 5 of 5 pockets, reference sets equal across uid
generations) removes the other reason to hesitate — the key does not churn across the event that
regenerates everything else.

### B. Join on remittance text

**Rejected by a measured false pair, not by argument** — onboarding ADR rejected option L, and the
2026-08-19 pass confirms the hazard is still live: the remittance names only the credit side's ISO
code, two same-day conversions into one currency are textually identical, and clustering on the
text produced a false cluster spanning two distinct conversions in the measured set. Multiple deals
share the corpus's single booking date, so adding the date still does not rescue it.

### C. Join on `booking_date`/`value_date` or `transaction_id`

**Rejected by measurement, twice** (2026-08-12 and re-confirmed 2026-08-19): each date field yields
one cluster containing everything (`booking_date == value_date` on 100%, date-only), and
`transaction_id` is null on 100% of rows — there is nothing to join on.

### D. Loosen `_validate`'s date check pre-emptively (e.g. allow adjacent dates) so cross-date deals would pair

**Rejected as the exact move the issue forbids.** It trades a refusal-shaped failure for a
mis-pairing-shaped one across **every** source sharing `_validate`, to serve a case never observed
anywhere (the pairing ADR's own open list has watched for it since 2026-08-08 and nothing in any
bank's cache books a pair across dates). If the case ever appears, decision 2's trigger routes it
back to #50 with a measurement in hand — which is the only defensible basis for touching a shared
check.

---

## Cost to reverse

**Low — and bounded by construction.** One table row plus one fixture test; deleting the row makes
`_conversion_key` return `None` for Revolut again. Reversal un-annotates *future* Revolut files'
conversion legs (`NAME`/`MEMO` text only); files already imported keep their text, and `FITID`
de-duplication means a changed description only ever affects a first import — the same accepted
dent in `NAME`/`MEMO` stability the pairing ADR records for a failed-run retry. **No identity field
moves in either direction:** `FITID`, `BANKID` and `ACCTID` are untouched (decision 3, onboarding
ADR decision 10), so no imported account orphans and no transaction re-imports. Keep it that way.

---

## Open questions

### 1. Does Revolut ever book an exchange's two legs on different booking dates?

Unmeasured and **not probeable**: no live probe can settle it, because a counted request cannot
manufacture a midnight-straddling deal — the only path is a naturally occurring one appearing in a
later fetch window. **What would settle it:** the local re-clustering pass over the cached
responses (zero requests) finding a date-refused `EXCHANGE` cluster or a size-1 `EXCHANGE` leg —
which is decision 2's revisit trigger, so the answer routes back to #50 by construction. No probe
is proposed and [`probes.md`](probes.md) gains no row.

### 2. Should unpaired `EXCHANGE`-coded legs be surfaced at fetch time?

Considered for this ADR and left open rather than silently dropped. Today the signal has nothing to
say — zero unpaired `EXCHANGE` legs exist (measurement point 2) — and both available shapes have a
cost: a generic "key set but unpaired" warning would also fire on Kantor's known re-link stale-key
refusal, turning a designed silence (pairing ADR decision 2: refusal is deliberately quiet) into
recurring noise; a Revolut-specific shape needs the raw transaction code threaded into a layer that
deliberately does not see it. **What would settle it:** the first observed unpaired `EXCHANGE`
leg — the same event as decision 2's trigger — which would supply a measured case to design the
message against. If it is ever built, the house constraints are fixed now: it is a `FetchWarning`,
never a failure, it never moves the exit code, and its message carries **counts only**.

### 3. Is the `bank_transaction_code` vocabulary closed, and does a fee or multi-leg row ever share a reference?

Both inherited (onboarding ADR open questions 5 and 4 respectively), both still open, both settled
by ordinary use plus the same zero-request re-clustering pass. Neither blocks decision 1: a new
code on conversion legs simply does not fire the rule (a measurement, not a bug), and a shared fee
row makes a cluster of 3, which `_validate` refuses without annotating anything.

---

On acceptance, decision 1 condenses into
[`decisions.md`](decisions.md#currency-conversion-legs-are-paired-the-rate-comes-from-the-two-booked-amounts-never-prose)
as one new row in the deal-key table's prose, with decision 2's working assumption and its revisit
trigger attached as one line; this file stays for the rejected options — above all option A, the
replaced gate — and the 2026-08-19 measurement record.
