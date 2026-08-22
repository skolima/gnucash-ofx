# ADR: currency conversions booked as two single-currency legs

**Status:** Decisions 1–4 and 6 implemented — see *Where this stands* below
([#27](https://github.com/skolima/gnucash-ofx/issues/27)). Decision 5 stays deferred; option D
closed. Verified end-to-end against GnuCash on 2026-08-08 (§8), and again through the shipped
pipeline on 2026-08-10 (five-scenario manual import check: clean pair, Wise trio with fee row,
unpaired leg, `NAME`-truncation, re-import de-dup — PR #27).
**Date:** 2026-08-08.
**Scope:** `models.py`, `sources/enablebanking.py`, `ofxout.py`, and one new pairing pass in
`run.py`. Condenses into a section of [`decisions.md`](decisions.md) on acceptance; kept as its
own file because it also carries the rejected options and the per-decision reasoning, which
`decisions.md` does not.

---

## Where this stands

- [x] **1.** Pair by an explicit, per-source deal key — the Wise/Kantor table lives in
      `sources/enablebanking.py`'s `_conversion_key()`
      ([#27](https://github.com/skolima/gnucash-ofx/issues/27)); the table grew its Revolut
      `EXCHANGE` row in [#53](https://github.com/skolima/gnucash-ofx/issues/53)
      ([adr-revolut-exchange-pairing.md](adr-revolut-exchange-pairing.md))
- [x] **2.** Validate before acting, never guess — `conversions.py`'s `_validate()`, all six checks,
      all-or-nothing ([#27](https://github.com/skolima/gnucash-ofx/issues/27))
- [x] **3.** Derive the rate from the two booked amounts, never prose — `conversions._derive()`,
      quantized to 6dp ([#27](https://github.com/skolima/gnucash-ofx/issues/27))
- [x] **4.** Surface it in `NAME`/`MEMO`, identical on both legs, leading even the remittance —
      `ofxout.compose_conversion()` ([#27](https://github.com/skolima/gnucash-ofx/issues/27))
- [ ] **5.** Deferred: emit `<ORIGCURRENCY><CURRATE><CURSYM>` alongside the annotation — not
      shipped; §8 measured it buys the primary consumer (GnuCash) nothing beyond what 1–4 already
      give it
- [x] **6.** `FITID` untouched by pairing — pairing changes description text only
      ([#27](https://github.com/skolima/gnucash-ofx/issues/27))

---

## Context

A currency conversion is one event to the user and **two transactions to us**, in two different
currencies, and therefore — because an OFX statement is single-currency (`CURDEF`), see *One OFX
file per account, per currency* in [`decisions.md`](decisions.md) — in **two different files**.
Neither file states the rate in a form anything can act on.

Two sources produce this, in two different shapes.

### Wise — `wise_personal`, 2026-03-15

| File | `TRNAMT` | `FITID` | `NAME` |
|---|---|---|---|
| `wise_personal_EUR_2026_02_14-2026_03_15.ofx` | `-1.65` | `50001.FEE-BALANCE-9000000001` | `Wise Charges for: BALANCE-9000000001` |
| `wise_personal_EUR_2026_02_14-2026_03_15.ofx` | `-345.00` | `50001.BALANCE-9000000001` | `BALANCE-9000000001 Converted 346.65 EUR to 300.00 GBP (fee: 1.65 EUR)` |
| `wise_personal_GBP_2026_02_14-2026_03_15.ofx` | `+300.00` | `50002.BALANCE-9000000001` | `BALANCE-9000000001 Converted 346.65 EUR to 300.00 GBP` |

Plus, in the same GBP file, a fourth line — `-300.00 GBP` to HMRC — which is an ordinary outgoing
payment that happens to consume the proceeds. It needs nothing from this ADR and is mentioned only
because it is what makes the row look like "a conversion and a transfer in one".

Wise gives a shared deal key (`BALANCE-9000000001`), a machine-readable
`bank_transaction_code.code == "CONVERSION"`, and prose naming both currencies.

### Alior Kantor — `alior_kantor`, deal 104, 2026-05-18

| File | `TRNAMT` | `NAME` |
|---|---|---|
| `alior_kantor_EUR_…ofx` | `-12000.00` | `Rozliczenie transakcji Kantor Walutowy 104 Rozliczenie transakcji wymiany walut` |
| `alior_kantor_PLN_…ofx` | `+51720.00` | `Rozliczenie transakcji Kantor Walutowy 104 Rozliczenie transakcji wymiany walut` |

Kantor gives a shared deal key (`Kantor Walutowy 104`), **no** `bank_transaction_code` at all, no
fee leg (the margin is inside the rate) — and **prose that names neither the counter-amount nor the
counter-currency**. For Kantor the rate is not merely unstructured; it is *absent from either file
taken alone*. This is strictly worse than Wise and is the case that will dominate by volume.

### What the aggregator does not give us

Enable Banking's transaction schema **has an `exchange_rate` field**. It is `null` in **every cached
transaction record**, across every bank and every conversion leg. There is no second endpoint to
ask: per-transaction detail is ruled out in *Not done, and why* in [`decisions.md`](decisions.md).

So the rate exists in our data only as the **ratio of the two booked amounts**.

---

## Constraints, measured rather than assumed

### 1. OFX has the field. libofx throws it away.

`ofx160.dtd` line 1262 admits `(CURRENCY | ORIGCURRENCY)?` as the last child of `STMTTRN`, and line
3458 types it `- - (CURRATE , CURSYM)`. Six hand-built variants through the GnuCash-bundled
`ofxdump` 0.10.5 (`C:\Program Files (x86)\gnucash\bin\ofxdump.exe`, per [`testing.md`](testing.md)):

| Variant | exit | libofx delivers | stderr |
|---|---|---|---|
| no currency aggregate | 0 | — | clean |
| `<ORIGCURRENCY><CURRATE>1.15000<CURSYM>EUR` | 0 | `Amounts are in foreign currency: Yes` | `WRITEME: CURRATE (1.15000) is not supported by the TRANSACTION container`<br>`WRITEME: CURSYM (EUR) is not supported by the TRANSACTION container` |
| `<CURRENCY>`, same children | 0 | `Amounts are in foreign currency: No` | same two `WRITEME`s |
| `<ORIGCURRENCY><CURSYM>…<CURRATE>…` (wrong child order) | **1** | flag only; `CURRATE ()` **empty** | OpenSP `otherError` |
| `<ORIG_CURRENCY>…` (what `ofxstatement` writes) | **1** | **nothing** | OpenSP `otherError`, `incoming_data should be empty! … data was lost: CURRENCY>` |
| unterminated `<CURRATE>`/`<CURSYM>` | 0 | flag only | same two `WRITEME`s |

The rate and the symbol are **discarded**. The source says why: in
[`ofx_container_transaction.cpp`](https://github.com/libofx/libofx/blob/master/lib/ofx_container_transaction.cpp)
the base `OfxTransactionContainer` maps `CURRENCY`/`ORIGCURRENCY` to a single boolean
(`ASSIGN(data.amounts_are_foreign_currency, …)`), while `CURRATE` → `data.currency_ratio` and
`CURSYM` → `data.currency` are implemented **only in `OfxInvestmentTransactionContainer`**. On a
bank transaction they fall through to the generic container's `WRITEME`.

### 2. GnuCash would not use it if libofx delivered it.

In `gnc-ofx-import.cpp`, `currency_ratio` is read only inside `process_investment_transaction()` /
`ofx_get_investment_amount()`. `amounts_are_foreign_currency` appears **nowhere in the file**.
`process_bank_transaction()` takes the statement currency and does
`xaccSplitSetBaseValue(split, gnc_amount, xaccTransGetCurrency(transaction))` — no ratio applied.

### 3. The OFX importer has no channel for a rate, independent of 1 and 2.

`import-main-matcher.h` offers two entry points:

```c
void gnc_gen_trans_list_add_trans (GNCImportMainMatcher *gui, Transaction *trans);
void gnc_gen_trans_list_add_trans_with_split_data (GNCImportMainMatcher *gui,
                                                   Transaction *trans,
                                                   GNCImportLastSplitInfo *lsplit);
```

`GNCImportLastSplitInfo` (`import-backend.h`) carries exactly what is needed —
`gnc_numeric price; gnc_numeric amount; Account *account; …`. The **CSV** importer uses the
`_with_split_data` form. The **OFX** importer calls `gnc_gen_trans_list_add_trans` — no `lsplit`,
no price. So even a fixed libofx would need a GnuCash change too.

Without a price, `gnc_import_process_trans_item` in `import-backend.cpp` reaches the branch
commented *"Bad! user asked to create a balancing split in an account with different
currency/commodity than the transaction but didn't provide an exchange rate"* and emits
`PWARN("Missing exchange rate …, will assume rate of 1")`.

**Three independent upstream blockers.** Any option resting on OFX carrying the rate into GnuCash
is dead, and `#1`-style "wait for upstream" is not a plan here: it would need fixes in two
projects.

### 4. The manual path exists and is per-transaction.

`import-main-matcher.cpp` has `gnc_gen_trans_set_price_to_selection_cb` — context menu **"Assign
e_xchange rate"**, which opens `gnc_xfer_dialog()` and calls `gnc_import_TransInfo_set_price()`.
That is the workflow the user is left with, and it needs one number typed per conversion. Whatever
we do must put that number in front of them, in the register, at that moment.

### 5. The prose rate is the *wrong* rate.

Wise's own words are `Converted 346.65 EUR to 300.00 GBP (fee: 1.65 EUR)`. But the booked legs are
`-345.00 EUR` and `+300.00 GBP`, with the `1.65` as a separate row.

| Source of rate | EUR per GBP | Balances against the booked splits? |
|---|---|---|
| prose (`346.65 / 300.00`) | 1.155500 | **No** — implies 346.65 EUR against a 345.00 EUR split |
| booked amounts (`345.00 / 300.00`) | 1.150000 | Yes, exactly |

0.48% apart, and only one of them produces a balanced transaction. **The rate must be derived from
the two booked amounts; parsing the prose is a defect, not a shortcut.**

Kantor corroborates from the other direction: `51720.00 / 12000.00 = 4.310000`,
`21450.00 / 5000.00 = 4.290000`, `8500.00 / 2000.00 = 4.250000` — all exact to four decimals,
i.e. deriving from booked amounts recovers the dealer's quoted rate **exactly**.

### 6. The pairing signal is already reliable.

Prototype over the cache, grouping Wise on `bank_transaction_code == CONVERSION` +
`reference_number ~ BALANCE-<digits>`, and Kantor on `Kantor Walutowy (\d+)` in the remittance:

| Deal | legs | currencies | booked | derived rate |
|---|---|---|---|---|
| `kantor 101` | 2 | EUR, PLN | `-2000.00 EUR` / `+8500.00 PLN` | 4.250000 |
| `kantor 102` | 2 | EUR, PLN | `-9000.00 EUR` / `+38430.00 PLN` | 4.270000 |
| `kantor 103` | 2 | EUR, PLN | `-5000.00 EUR` / `+21450.00 PLN` | 4.290000 |
| `kantor 104` | 2 | EUR, PLN | `-12000.00 EUR` / `+51720.00 PLN` | 4.310000 |
| `wise BALANCE-9000000001` | 2 + 1 fee | EUR, GBP | `-345.00 EUR` / `+300.00 GBP` | 1.150000 |

Every group: exactly two non-fee legs, two distinct currencies, opposite signs, one booking date.
No false groups, no ambiguity.

### 7. `run.py` already has the right shape.

`fetch_and_write_all` collects **every account of a bank into `pending` before writing any file**
(`run.py:622`, written at `run.py:630`) — so a cross-account pass slots in between with no
restructuring. Both legs are always fetched in the same run, over the same window, and share a
booking date, so **a window boundary can never split a pair**; only a per-account fetch failure can.

### 8. The manual path works, fails loudly, and only has to be walked once per deal.

Run against GnuCash with a throwaway book and a hand-built conversion pair (see *To verify* below)
— the decisive test, because everything in this ADR rests on the assumption that a cross-currency
destination account is reachable at all.

| Step | Result |
|---|---|
| Import the EUR leg, set destination to the PLN account | Row goes **red**, *"need price to transfer"* |
| Right-click → **Assign exchange rate**, enter the derived rate | Row goes **green**, type reverts to a normal `New` |
| Then import the PLN leg | **Matched automatically and cleanly** — no duplicate, no second rate entry |

Three consequences, all favourable:

- **The matcher does offer a different-commodity account.** Option F is viable; the ADR is not
  built on sand.
- **The 1:1 fallback of §3 does not fire silently here.** The missing rate is a visible, blocking,
  red-background error. So the annotation is a *convenience* that saves the user looking up the
  other leg — not a safety net against quiet corruption.
- **The cost is one rate entry per deal, not two.** Assigning the rate creates the full two-split
  transaction, so the second file's leg matches the split that already exists. This is materially
  cheaper than assumed when the options were weighed, and it is what closes option D.

The third file — the same PLN credit twice, once bare and once carrying
`<ORIGCURRENCY><CURRATE>0.232019<CURSYM>EUR` — behaved **identically**, both requiring the same
`need price to transfer` fix. GnuCash confirms at application level what `ofxdump` showed at parser
level (§1, §2): the aggregate changes nothing. See decision 5 for what that costs it.

---

## Prior art

Searched for anything that already solves this. **Nothing does — for GnuCash, via OFX.** That is
consistent with §1–§3: the format cannot carry it and the importer cannot receive it, so no amount
of cleverness on the producing side would have worked.

What the search did establish:

- **Splitting per currency is the officially recommended approach, not a workaround.** Asked in
  2014 how to fix the 1:1 rate on an imported multi-currency transfer, Derek Atkins' answer on
  [gnucash-user](https://lists.gnucash.org/pipermail/gnucash-user/2014-June/054958.html) was that
  the importer does not support multi-currency transactions and *"you need to split your QIF file
  into individual currencies and import them separately."* That is exactly the architecture this
  project already has. The rate gap is **inherent to the endorsed design**, not a symptom of a
  wrong turn — which is the strongest argument that it has to be closed on our side.
- **`README.OFX` does not mention currency at all** — no support statement, no limitation. The
  silence dates from ~2003 and matches the code: it was never in scope.
- **QIF is no better** (no multi-currency support), so no alternative output format rescues this.
- **The CSV multi-currency path is documented as broken by GnuCash itself** — see option D.
- **`beancount-wise`** ([rkok](https://github.com/rkok/beancount-wise)), an importer for a ledger
  system that *does* support multi-currency natively with `@` prices, still needs an
  `Equity:Wise:ConversionDifference` account because *"Wise almost often takes/leaves 'dust' when
  converting between currencies."* Useful warning: expect residuals and decide a policy rather than
  discovering one.
- **Reconcile-by-reference is the mainstream technique.** `beancount-import`
  ([jbms](https://github.com/jbms/beancount-import)) matches pending transactions against each
  other on metadata during reconciliation, and general Wise-export guidance is to *"reconcile by
  reference, compute the effective exchange rate, and choose a consistent gross vs net policy."*
  Two things follow. First, pairing belongs **in the tool**, with the file format as a delivery
  channel — which is what this ADR does. Second, the **gross-vs-net policy is a named trap**, and
  §5 is exactly it: Wise's prose is gross of fee (346.65), its booked legs are net (345.00).
  Decision 3 picks **net**, because net is what has to reconcile against the splits.

The nearest neighbours are all ledger-side reconcilers (`beancount-import`) or format converters
with no FX story (`ofxstatement` and its plugins — whose own `orig_currency` support is broken, see
option B). No tool was found that pairs conversion legs *at export time*.

---

## Options considered

### A. Do nothing

Status quo: Wise readable-but-wrong (§5), Kantor unrecoverable from a single file. **Rejected** —
Kantor is about to become the high-volume case and currently emits no usable rate at all.

### B. Emit `<ORIGCURRENCY><CURRATE><CURSYM>` and rely on GnuCash

**Rejected outright** on §1–§3. Also note `ofxstatement`'s `OfxWriter.buildBankTransaction` writes
`<ORIG_CURRENCY>` (wrong tag) with `CURSYM` before `CURRATE` (wrong order): using its own
`StatementLine.orig_currency` field produces a file that fails to parse, exit 1 (§1, row 5). Its
`Currency`/`orig_currency` support is unusable as shipped.

### C. Model conversions as OFX investment transactions

libofx *does* honour `CURRATE`/`CURSYM` in the investment container, and GnuCash *does* apply
`currency_ratio` there. **Rejected**: it requires `INVSTMTRS` instead of `STMTRS`, i.e. changing the
statement type — and therefore the account semantics — for every transaction in the file, and would
have GnuCash treat a currency as a security and create commodity accounts. Wildly disproportionate,
and it would break the ordinary 95% to serve the 5%.

### D. Sidecar GnuCash-CSV for the pairs; omit those legs from the OFX

GnuCash's CSV importer is the one first-party path that *natively* expresses this:
`gnc-imp-props-tx.hpp` has `ACCOUNT`, `AMOUNT`, `VALUE`, `PRICE`, `TACCOUNT`, `TAMOUNT` and a
`UNIQUE_ID`, and it feeds `gnc_gen_trans_list_add_trans_with_split_data`. One row per conversion
would import as a balanced two-split cross-currency transaction at the exact rate, zero typing.

**Rejected for v1**, on four counts, the first of which is decisive:

1. **GnuCash documents this path as not working.** The
   [CSV Import/Export wiki page](https://wiki.gnucash.org/wiki/CSV_Import/Export): *"There is an
   issue importing multi-currency transfers. In this case the Price column may be added to the CSV
   record and it affects the amount going to the receiving account but the resulting value is
   strange."* The one first-party route to an automatic cross-currency import is upstream-flagged
   as producing wrong numbers. That is worse than manual entry, because it is wrong *quietly*.
2. Omitting legs from the OFX breaks `LEDGERBAL` reconciliation, which every other decision here
   protects.
3. It splits the user's workflow across two importers with two different account-mapping UIs.
4. CSV dedup via `UNIQUE_ID` is *believed* to map to `online_id` but is **unverified**, and a
   weaker dedup path than `FITID` is not a trade to make blind.

**Closed, not merely deferred.** §8 measured the OFX path end to end: it costs **one rate entry per
deal**, the second leg matches itself, and a missing rate is a loud red error rather than a silent
1:1. The escape hatch existed for the case where manual entry proved intolerable at Kantor volume;
at one entry per deal it does not. Reopening this would need a new reason — not just curiosity
about whether point 1 still reproduces.

### E. Generic heuristic pairing (same bank, same date, opposite signs, two currencies)

**Rejected.** It would pair a genuine same-day EUR payment out with an unrelated PLN receipt.
[`decisions.md`](decisions.md) already settled this shape of question — *"a rule keyed on shape
cannot tell any of these apart; a table of prefixes can, and it is the artifact to read when a new
source arrives."* Both sources hand us an explicit shared deal key; there is no reason to guess.

### F. Detect the pair; derive the rate from booked amounts; surface it as canonical text

The only channel GnuCash actually reads is `NAME`/`MEMO` (§1–§3), and the only action available is
typing a number into the exchange-rate dialog (§4). **Accepted** — see below.

---

## Decision

1. **Pair by an explicit, per-source deal key.** A source-specific rule extracts a conversion-deal
   key from a transaction; legs sharing a key within **one bank's fetch** form a candidate pair.
   The initial table:

   | Source | Signal | Key |
   |---|---|---|
   | Wise | `bank_transaction_code.code == "CONVERSION"` | `reference_number` matching `BALANCE-<digits>`, with the `FEE-` variant marked as the fee leg |
   | Alior Kantor | remittance matching `Kantor Walutowy (\d+)` | the deal number |

   This table is the artifact to read when a new source arrives, mirroring the machine-reference
   prefix table already in `enablebanking.py`.

2. **Validate before acting; never guess.** A candidate is a pair only if it has exactly two
   non-fee legs, two distinct currencies, opposite signs, and one booking date. Anything else is
   left completely untouched — no annotation, no partial output, no warning that reads as an error.

3. **Derive the rate from the two booked amounts** (§5). Prose is never parsed for a rate.
   Enable Banking's `exchange_rate` is used only as a cross-check if it ever populates, never as
   the value, because the booked amounts are what must reconcile inside GnuCash.

4. **Surface it in `NAME`/`MEMO` in a canonical, machine-generated form** carrying the
   counter-amount, counter-currency and rate, on **both** legs. Exact composition is left to
   implementation, but it must: state the counter-amount (the number the user types into "Assign
   exchange rate"); survive the 96-character `NAME` cap — for which the source's own duplicated
   boilerplate (`Rozliczenie transakcji wymiany walut`) is the budget to reclaim, not the
   counterparty name; and be identical on both legs so the two files read the same.

5. **Deferred: emit a spec-correct `<ORIGCURRENCY><CURRATE><CURSYM>`.** The appeal was the
   **"correct field plus the one that works"** split already used twice — `MEMO` + `BANKACCTTO`,
   and `CHECKNUM` + `REFNUM`. The disanalogy is cost: in both precedents the correct field was
   *free*, because `ofxstatement` already wrote it. Here it is not — the writer emits
   `<ORIG_CURRENCY>` with reversed children (option B), so this needs a writer override, and §8
   confirms it buys the primary consumer exactly nothing. Not rejected — the file would be more
   honest with it, and `ofxtools` reads it — but it is a separate change with its own justification,
   not a rider on 1–4. Report the `<ORIG_CURRENCY>` tag bug upstream regardless.

6. **`FITID` is untouched**, as in the machine-reference decision. Pairing changes description text
   only; it must never influence transaction identity.

---

## Consequences

**Accepted costs.**

- The conversion still needs **one manual rate entry per deal** in GnuCash (§8) — on whichever leg
  is imported first; the other then matches itself. This ADR makes that entry quick by putting the
  number in the register; it does not automate it. Automation needs an upstream fix.
- A leg whose partner is missing — only reachable via a **per-account fetch failure** (§7) — keeps
  today's text. So a description can differ between a failed run and its retry. `FITID` dedup means
  this only ever affects a first import, and the Bayesian imap tolerates it; but it is a genuine,
  if narrow, dent in the `NAME`/`MEMO` stability that [`decisions.md`](decisions.md) otherwise
  treats as absolute.
- The deal key (`BALANCE-<id>`, `Kantor Walutowy 104`) is near-unique and therefore teaches the
  matcher nothing. It is already present in both sources' text today; this ADR does not add it, and
  should not be read as endorsing it — whether it belongs in `NAME` at all is the *Machine
  references go to `CHECKNUM`* question, and should be settled separately for `BALANCE-<id>`.
- One more cross-account pass in `run.py`, and `Txn` gains fields it did not have.

**Explicitly out of scope.** Cross-*bank* pairing (conversions are internal to one institution);
the fee leg, which is already prose-linked to its parent and needs nothing; and the ordinary
outbound payment that follows a conversion.

**Cost to reverse:** low. Nothing here touches `BANKID`, `ACCTID` or `FITID`, so no imported
account is orphaned and no transaction re-imports. Backing it out changes description text only.

---

## To verify before implementing

The §8 fixtures are **not committed** — `/output/` and `*.ofx` are gitignored, and they are trivial
to recreate. Two single-transaction OFX files sharing one deal (`-12000.00 EUR` and `+51720.00 PLN`,
same date, `BANKID` `FXTEST`, distinct `ACCTID`s), plus a third file carrying the same PLN credit
twice — once bare, once with `<ORIGCURRENCY><CURRATE>0.232019<CURSYM>EUR` — to isolate decision 5.
Check them through `ofxdump` first (see [`testing.md`](testing.md)), then **import into a throwaway
book, never the real one.**

Settled by measurement, recorded so the answers are not re-derived:

- ~~Does the matcher offer a different-commodity destination account?~~ **Yes** — §8.
- ~~What does the register show before a rate is assigned?~~ **A red, blocking "need price to
  transfer"**, not a silent 1:1 — §8.
- ~~Does `<ORIGCURRENCY>` change GnuCash's behaviour?~~ **No** — §8.
- ~~Does the pairing rule produce false pairs?~~ **No.** Over the full cache, deduped per account by
  stable id exactly as `run.py` does: every complete pair validated, and the single incomplete group
  was correctly refused. That refusal is Kantor deal `102`, which appeared under two Enable Banking
  UIDs across a re-link and so presented four legs across four accounts — decision 2's validation
  declined to guess, which is the designed failure.
- ~~Do Wise conversions leave a residual, as `beancount-wise` reports for its own?~~ **Not in this
  data** — `346.65 − 1.65 = 345.00` exactly, on every conversion in the cache.
- ~~Whether the second leg's `FITID` is recorded as `online_id`~~ **Yes.** The §8 throwaway book
  (`gnucash-exchange.gnucash`, SQLite storage) was inspected directly, 2026-08-10: the Kantor `220`
  pair became one transaction with two splits, and both splits carry an `online_id` slot equal to
  their own leg's `FITID` (`T1-EUR-220`, `T1-PLN-220`) — including the matched PLN split, not just
  the one that created the transaction. Re-importing an overlapping window dedupes normally; not a
  blocker.

Genuinely open:

- **Whether any source books the two legs on different dates.** Nothing in the cache does, and
  decision 2 would refuse to pair them if one did — safe, but it would silently give up on real
  conversions. A deal struck across a month-end is the case to watch.
- **Whether 6dp is the right rate precision for decision 3.** It is display-only — the user can
  always type the exact counter-amount into GnuCash's transfer dialog instead of trusting the
  rounded rate — so nothing observed so far has needed it to be finer.

---

Condensed on acceptance ([#27](https://github.com/skolima/gnucash-ofx/issues/27)): decisions 1–4 and
6 into [`decisions.md`](decisions.md#currency-conversion-legs-are-paired-the-rate-comes-from-the-two-booked-amounts-never-prose),
and the conversion exception to remittance-first into `AGENTS.md`'s `NAME`-composition bullet.
Decision 5 stays here, undecided, until it ships or is formally rejected — this file remains the
place to read the rejected options and the per-decision reasoning either way.

## References

- [`decisions.md`](decisions.md) — *One statement per account, per currency*; *Machine references go
  to `CHECKNUM` + `REFNUM`*; *Counterparty account number in both `MEMO` and `BANKACCTTO`*; and,
  since [#27](https://github.com/skolima/gnucash-ofx/issues/27), *Currency-conversion legs are
  paired; the rate comes from the two booked amounts, never prose* — decisions 1–4 and 6 condensed.
- [`testing.md`](testing.md) — the `ofxdump` harness used for §1.
- libofx [`ofx_container_transaction.cpp`](https://github.com/libofx/libofx/blob/master/lib/ofx_container_transaction.cpp)
- GnuCash [`gnc-ofx-import.cpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/ofx/gnc-ofx-import.cpp),
  [`import-backend.cpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/import-backend.cpp),
  [`import-backend.h`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/import-backend.h),
  [`import-main-matcher.cpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/import-main-matcher.cpp),
  [`gnc-imp-props-tx.hpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/csv-imp/gnc-imp-props-tx.hpp)
- `ofx160.dtd` as shipped with GnuCash for Windows, lines 1262 and 3458.
- GnuCash manual, [Importing Transactions from Files](https://www.gnucash.org/docs/v5//C/gnucash-manual/trans-import.html)
  — the CSV importer's Price / Transfer Amount columns behind option D; and the
  [CSV Import/Export wiki page](https://wiki.gnucash.org/wiki/CSV_Import/Export), which documents
  that path as producing "strange" values for multi-currency transfers.
- [gnucash-user, June 2014](https://lists.gnucash.org/pipermail/gnucash-user/2014-June/054958.html)
  — split per currency and import separately, from a GnuCash maintainer.
- [`beancount-wise`](https://github.com/rkok/beancount-wise) — conversion "dust" and why a residual
  policy is needed; [`beancount-import`](https://github.com/jbms/beancount-import) — pairing by
  metadata during reconciliation, the mainstream shape of this problem.
