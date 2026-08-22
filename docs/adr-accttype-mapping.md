# ADR: `ACCTTYPE` comes from the bank's `cash_account_type`; absence falls back to `CHECKING` explicitly; a type the bank message set cannot carry refuses the account, not the run

**Status:** Accepted ([#42](https://github.com/skolima/gnucash-ofx/issues/42)); decisions 1–5
implemented in [#43](https://github.com/skolima/gnucash-ofx/issues/43) (2026-08-13) — see *Where
this stands* below. Measurements taken 2026-08-13 against the local
`state/`, `output/`, `cache/` and `state/fetch-log.jsonl`, Enable Banking's API reference, and
libofx / GnuCash / ofxstatement source, all fetched or read the same day.
**Date:** 2026-08-13.
**Scope:** `src/gnucash_ofx/models.py` (one new optional field on the frozen `Account`),
`src/gnucash_ofx/ofxout.py` (one mapping function, `build_statement`, and the invariant comment at
`_CombinedOfxWriter`), `src/gnucash_ofx/run.py` (threading at the two `Account(...)` construction
sites and one `status` line), plus the matching wording in `AGENTS.md` and
[`decisions.md`](decisions.md). Explicitly **not** touched: `state.py` and
`sources/enablebanking.py` (they already persist and parse the value — `state.py:67,116,185`,
`enablebanking.py:297`), `cache.py`, `FITID`/`BANKID`/`ACCTID`, and the card-account
`OfxWriter` subclass, which stays where
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) decision 2 left it.
**Issue:** [#22](https://github.com/skolima/gnucash-ofx/issues/22). Follows up the open question
"Are there card accounts, mislabelled as `CHECKING`?" in
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md), whose decision 2 (group statements by
message set, never assume one) is what this ADR's refusal branch preserves.

---

## Where this stands

- [x] **1.** The total mapping: `CACC` → `CHECKING`, `SVGS` → `SAVINGS`, absence and `OTHR` fall
      back to `CHECKING` explicitly, `CARD`/`CASH`/`LOAN` and any undocumented string refuse —
      shipped in [#43](https://github.com/skolima/gnucash-ofx/issues/43) with the ship gate met:
      verified 2026-08-13 against the GnuCash-bundled `ofxdump` (libofx 0.10.5) — a synthetic
      `SVGS` statement built through the real `build_statement()` gave exit 0 and zero
      `LibOFX ERROR` lines, the type surfaced verbatim as `Account type: SAVINGS`; the
      `None`-fallback control was byte-identical to today's output except the single `ACCTTYPE`
      line; and a combined CHECKING+SAVINGS file kept exactly one `BANKMSGSRSV1` with both types
      correctly paired to their accounts
- [x] **2.** A refusal is account-scoped: siblings still ship, stderr + exit 1
      ([#43](https://github.com/skolima/gnucash-ofx/issues/43))
- [x] **3.** Fallback use is visible in `status`, never at fetch time
      ([#43](https://github.com/skolima/gnucash-ofx/issues/43))
- [x] **4.** The raw stored value threads state → `Account` → `build_statement`; one mapping
      function serves fetch and `--dry-run`, and `--dry-run` predicts the refusal too
      ([#43](https://github.com/skolima/gnucash-ofx/issues/43) — shipped stronger than written;
      see the implementation note at the end of decision 4)
- [x] **5.** The one-message-set invariant is reworded from "`CHECKING`" to bank-message-set
      terms, in `ofxout.py` and wherever `AGENTS.md`/`decisions.md` restate it
      ([#43](https://github.com/skolima/gnucash-ofx/issues/43))
- [ ] **6.** Identity fields and the card writer path untouched — not a decision to ship;
      recorded so nothing here is mistaken for having changed them

---

## Context

Every OFX file this project writes carries `ACCTTYPE=CHECKING`, and nothing chooses it. Measured
2026-08-13 over the real `output/`: **76 files, 642 statement instances, every `ACCTTYPE` is
`CHECKING`** — the vocabulary is exactly {`CHECKING`}, all of it `ofxstatement`'s
`Statement.__init__` default (`account_type="CHECKING"`, confirmed on the installed 0.9.3), never
a decision. `build_statement` (`ofxout.py:284`) never touches account type; the cache holds no OFX
at all (0 of 226 files). The bank's own answer *is* captured — `cash_account_type` is parsed at
`enablebanking.py:297` and persisted by `state.py` — and then read by nothing that writes a file.

The value is correct today, which is the only reason this is not a data-integrity bug already.
The defect is that nothing *makes* it true, and nothing would notice when it stops being true: a
savings or card account linked tomorrow imports as a checking account, silently, in every file.

The issue's 2026-08-09 numbers are stale, in a way that changes no conclusion but must not be
re-cited. Measured 2026-08-13 over the real `state/`: **24 linked accounts** across 7 bank state
files (alior 5, alior_kantor 3, erste 2, millennium 2, revolut 5, wise_business 3,
wise_personal 4), not 19; `cash_account_type` present on **16**, absent on **8**, not 3/16. The
delta is that alior, alior_kantor and revolut were re-linked after 2026-08-09 under the v2 state
schema (state-file `valid_until`/mtime evidence; link operations never appear in
`fetch-log.jsonl`, whose `command` vocabulary is only `fetch` and `probe-*`).

---

## Constraints, measured rather than assumed

All measurements 2026-08-13 unless stated. Structural counts only; no account numbers, payees or
amounts appear anywhere below.

### 1. The observed input vocabulary is {`CACC`} — and the 8 absences are a schema artefact, not a bank's answer

Distinct `cash_account_type` values across all 24 parsed account records **and** the 16 raw
`POST /sessions` account bodies kept in v2 `session_raw`: **`CACC` only, ×16**. No `SVGS`, no
`CARD`, nothing outside {`CACC`}.

The 8 absent are exactly the accounts of the three v1-schema state files (erste, millennium,
wise_personal). That layout predates the field, so absence means "never persisted", **not** "bank
omitted it" — and no routine fetch can ever populate it: `POST /sessions` is the only endpoint
that returns the full account resource, and `GET /accounts/{uid}` is 404 in Restricted Mode. Only
a re-link of those three banks would observe their real types. Any design here must treat absence
as the common case for a long time yet; the issue is explicit about this and it is restated here
as a constraint, not relitigated.

Consequence worth stating up front: `CACC` → `CHECKING` plus a `CHECKING` fallback is a **no-op
for all 24 current accounts** — 16 mapped explicitly, 8 via fallback. Nothing already imported
changes value.

### 2. Enable Banking's enum is six values, and `TRAN` is not one of them

Enable Banking's API reference (fetched 2026-08-13) documents `cash_account_type` as a six-value
enum, **not** the full ISO 20022 `ExternalCashAccountType1Code` list: **`CACC`** (posting
account), **`CARD`** (card payments only), **`CASH`**, **`LOAN`**, **`OTHR`** (not otherwise
specified), **`SVGS`** (savings). The issue's proposed `TRAN` → `CHECKING` mapping was based on
the full ISO list; **`TRAN` cannot occur here** and this ADR corrects the issue on that point.
The mapping must be total over these six plus absence — plus a defensive branch for a value
outside the documented enum, since the value is ASPSP-supplied and the enum is Enable Banking's
documentation claim, not a validated contract.

### 3. What OFX's bank message set can honestly carry

`BANKACCTFROM`/`ACCTTYPE` can honestly carry `CHECKING` and `SAVINGS`, of what this enum offers.
OFX's `ACCTTYPE` enum also has `MONEYMRKT` and `CREDITLINE`; nothing maps to them because no
Enable Banking value is honestly either — `CARD` in particular is not a credit line assertion,
and guessing is the failure mode this ADR exists to remove.

`CARD` is not a field change at all: `ofxstatement` 0.9.3's `OfxWriter` has no
`CREDITCARDMSGSRSV1` path, `CCACCTFROM` carries no `ACCTTYPE` element, and emitting a card inside
the bank message set is wrong OFX — the issue and
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) both already establish this. The card path
is a future `OfxWriter` subclass, out of scope here; decision 2 of that ADR already requires
grouping statements by message set for the day it exists.

### 4. Changing `ACCTTYPE` cannot orphan an imported account — settled from source

The issue asked for this to be confirmed rather than assumed. Confirmed 2026-08-13 from source:

- libofx `lib/ofx_container_account.cpp`, `gen_account_id()`: for bank accounts, `account_id` is
  `BANKID + " " + BRANCHID + " " + ACCTID` concatenated. **`ACCTTYPE` does not participate**; it
  lands in `OfxAccountData.account_type` only. (Credit-card accounts use `ACCTID + " " + ACCTKEY`
  — a different shape entirely, relevant only to the out-of-scope card path.)
- GnuCash `gnucash/import-export/ofx/gnc-ofx-import.cpp`: existing accounts are matched by
  passing `data.account_id` to `gnc_import_select_account()`; `data.account_type` is used
  **only** as `default_type` when creating a *new* account — and `OFX_CHECKING` and `OFX_SAVINGS`
  both map to `ACCT_TYPE_BANK` there (`OFX_CREDITCARD` to `ACCT_TYPE_CREDIT`).

So flipping an already-imported account from `CHECKING` to `SAVINGS` cannot disturb the match,
and in GnuCash the created type would have been the same `ACCT_TYPE_BANK` anyway. This is what
makes the whole ADR's reversal cost low.

### 5. `product` is not an alternative signal

Absent on all 24 parsed records and all 16 raw session bodies. Dead as a route to the answer.

### 6. Where the value has to travel

`models.Account` (frozen dataclass, `models.py:79`) has no account-type field. `run.py` builds
`Account(...)` at two sites — `run.py:1265` (fetch) and `run.py:1601` (dry-run) — from stored
state, without the value. The only `cash_account_type` references in `src/` are persistence and
parsing (§ Scope). So the implementation must thread it **state → `Account` →
`build_statement`**; there is no shorter path, and the dry-run site is why the mapping cannot
live only inside the fetch loop.

---

## Options considered

### A. Do nothing

**Rejected.** The value is correct today by coincidence (§1), and the failure is silent and lands
in every file the day it stops being true. The issue is right to file it as a bug.

### B. Derive the type from `product`

**Rejected on §5.** Absent everywhere it could be observed — all 24 records, all 16 raw bodies.

### C. Map `TRAN` → `CHECKING`

**Rejected on §2.** `TRAN` is not in Enable Banking's enum; the issue's mention came from the
full ISO list. Mapping a value that cannot arrive is dead code that misleads the next reader
about what the input vocabulary is.

### D. Refuse on absence

**Rejected.** Absence is 8 of 24 accounts today, is permanent until the three v1 banks re-link,
and no fetch can fix it (§1) — this would fail three banks on every run for months, for a
condition that is a schema artefact, not evidence of a mislabelled account.

### E. Abort the run on an uncarryable type

**Rejected.** "One bank's failure must not cost another bank its files" — partial success is
reported, not thrown away ([`decisions.md`](decisions.md), the `BankFailure` machinery, exit 1
if anything failed). One savings-bank oddity aborting Millennium's files would violate the
invariant for no benefit; the account-scoped refusal in decision 2 carries all the signal.

### F. Emit `CARD` as `CREDITLINE` (or any guess-mapping) in the bank message set

**Rejected on §3.** `CCACCTFROM` has no `ACCTTYPE`, `ofxstatement` 0.9.3 has no card message-set
path, and a card statement inside `BANKMSGSRSV1` is wrong OFX whether or not libofx recovers from
it. The honest card path is the writer subclass that
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) decision 2 left room for.

### G. A per-fetch stderr note whenever the fallback fires

**Rejected.** It would fire on three banks on every fetch until they re-link — months — about a
condition no fetch can change. [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md)
decision 4 already names the failure mode: a warning that repeats forever about something nobody
can fix is how a user learns to ignore warnings. Decision 3 puts the honesty in the pull channel
instead.

### H. `OTHR` refuses like `CARD`/`CASH`/`LOAN`

**Rejected**, and worth recording because it is the one genuinely close call. `OTHR` is *inside*
the documented enum: the bank explicitly answering "not otherwise specified". That is the same
information content as absence — a shrug, not a claim — and it is the escape value an ASPSP uses
for an ordinary account its core system does not classify. Refusing on it would fail accounts
that are overwhelmingly ordinary current accounts, on the strength of a non-answer. The refusal
branch exists for a **known** uncarryable type; `OTHR` is by definition not a known anything.
The asymmetry that decides the *undocumented*-string case (decision 1) does not apply here,
because `OTHR` cannot be a leak of a richer vocabulary — it is the documented absence of one.

### I. The mapping of decision 1, refusing per account

**Accepted** — below.

---

## Decision

### 1. One total mapping, in one function, with an explicit fallback

A single pure function in `ofxout.py`, total over Enable Banking's documented enum plus absence
plus the defensive branch:

| `cash_account_type` | `ACCTTYPE` | Why |
|---|---|---|
| `CACC` | `CHECKING` | Posting account; the observed universe today (§1) |
| `SVGS` | `SAVINGS` | The one other value the bank message set honestly carries (§3) |
| absent (`None`) | `CHECKING` — **explicit fallback** | §1: absence is a schema artefact, common until re-link; the fallback is written in this function with a comment, never inherited from the library default |
| `OTHR` | `CHECKING` — same fallback | Documented "not otherwise specified" — the same information content as absence (option H) |
| `CARD`, `CASH`, `LOAN` | **refuse** | Known types the bank message set cannot honestly carry (§3); the card path needs a writer subclass, not a field |
| anything else | **refuse** | Defensive: a value outside the documented enum means the enum premise (§2) is broken — most plausibly a full-ISO leak — and a loud refusal is what says so. A refusal costs one account's statement for one run and is fixed by adding a mapping and re-running, largely from cache; a silent `CHECKING` default for an unrecognised type is the original bug, permanent and invisible |

Nothing maps to `MONEYMRKT` or `CREDITLINE`: no Enable Banking value is honestly either (§3), and
a mapping table with entries nothing can reach teaches the next reader the wrong vocabulary.

This rules out: any second mapping site, any per-bank override, any inference from `product` or
account name, and any silent handling of an unrecognised value.

**Ship gate.** Verification for the output change is `ofxdump` (libofx 0.10.5, the GnuCash-bundled
one), not pytest or `ofxtools` — [`testing.md`](testing.md), the `ofx-conformance` agent. Before
decision 1's box is ticked, a `SAVINGS`-carrying statement must go through `ofxdump`: exit 0,
zero errors, the type surfaced as `OFX_SAVINGS`. Until a real bank sends `SVGS` (open question
below), that statement is a synthetic fixture, and the tests for the refusal branch are too.

### 2. A refusal is account-scoped; siblings still ship

A refused account becomes a `BankFailure` with `scope="account"`, through the existing machinery
(`run.py:117`): the message names the bank key, the redacted account, the offending type, and
says the bank message set cannot carry it — no guessed mapping offered. It goes to stderr with
the failure summary, the run exits 1, and **every sibling account and every other bank still
writes its files**. This is the invariant "partial success is reported, not thrown away" applied,
not bent: the account that cannot be honestly written is the only thing withheld.

This rules out: a run-level abort (option E), a silent skip (a withheld file with exit 0 is a gap
the coverage ledger would then have to explain), and writing the file with a guessed type.

### 3. Fallback use is visible in `status`, never at fetch time

One line per affected bank in `status` (`run.py:1888`), computed from state already loaded, zero
requests: *N of M accounts carry no stored account type; `ACCTTYPE` falls back to `CHECKING`;
re-linking captures the bank's answer.* Nothing is printed at fetch time (option G), and nothing
pretends the 8 fallback accounts were verified — the issue's twin traps, a check that fires on
every account and a silence that lulls, are both avoided by putting the fact in the pull channel.
`OTHR` accounts, should any appear, count in the same line's spirit but are a bank's explicit
answer, not an unverified one; they are not flagged.

This rules out: a `FetchWarning` per fetch, and documenting the fallback nowhere but a comment.

### 4. The raw value threads state → `Account` → `build_statement`; `--dry-run` sees the same function

`models.Account` gains one optional field carrying the **stored bank value as-is**
(`cash_account_type`-shaped, `None` when absent) — a new defaulted field on the frozen dataclass,
so every existing constructor call stays valid. `run.py` threads it from stored state at both
construction sites (`run.py:1265` and `run.py:1601`); `build_statement` calls decision 1's
function to set `Statement.account_type`. The refusal is a typed error raised by that function
and converted by `run.py`'s existing per-account handling into decision 2's `BankFailure`.

`--dry-run` **does** apply the refusal: it predicts no path for an account the real run would
refuse, reported the way dry-run already reports per-account problems. The input is already local
in state, so the prediction stays zero-cost — and a dry-run that predicts a file the fetch then
refuses to write is a wrong prediction, which is the one thing `--dry-run` may not be
([`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) decision 3's
argument, applied here).

This rules out: mapping at parse time or persist time (the state file keeps the bank's
vocabulary, not ours), a second copy of the mapping for dry-run, and an `Account` that carries
the already-mapped OFX word (which would smuggle the mapping decision away from the one function).

*Implementation note ([#43](https://github.com/skolima/gnucash-ofx/issues/43), review commit
`2e98866`): shipped stronger than written.* In `_planned_files` the refusal check **precedes** the
currency check, matching the fetch, which refuses before `/balances` could teach it a currency.
So an account that is refused *and* currencyless reports the refusal and does **not** suppress its
bank's prediction — unlike an ordinary unknown-currency account, it can never join a
disambiguation group mid-run, so the suppression rule's reason never reaches it.

### 5. The one-message-set invariant is reworded, not weakened

The invariant comment at `_CombinedOfxWriter` (`ofxout.py:392-397`) currently reads "this project
only ever emits `BANKMSGSRSV1`/`CHECKING` statements". After this ADR the true statement is:
**this project only ever emits bank-message-set (`BANKMSGSRSV1`) statements — `CHECKING` or
`SAVINGS`, both of which live in that one message set — and the refusal in decision 1 is what
keeps it true**, because every type that would need a second message set refuses instead of
shipping. One message-set block therefore stays correct. The comment, and the restatements in
`AGENTS.md` and [`decisions.md`](decisions.md), change wording in the same PR as the code; an
invariant comment that names `CHECKING` while `SAVINGS` files exist is a comment the next reader
correctly stops trusting.

### 6. Untouched

`FITID`, `BANKID`, `ACCTID` (§4 is *why* they can stay untouched), the state schema and parser
(they already carry the value), the cache, the `NAME`/`MEMO`/`CHECKNUM` composition, stdout as
the file list, and the card-account writer subclass with its message-set grouping
([`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) decision 2's territory).

---

## Consequences

**Accepted costs.**

- **The refusal and `SAVINGS` branches ship on synthetic fixtures only.** No local account sends
  `SVGS`, `CARD`, `CASH`, `LOAN` or `OTHR` today (§1), and only a re-link can change what is
  observable. The `ofxdump` gate in decision 1 is what keeps the `SAVINGS` path honest anyway.
- **A `CARD`/`CASH`/`LOAN` account, if one is ever linked, gets no file at all** until the writer
  subclass exists — an account-scoped failure on every fetch that includes it. That is by design
  (the alternative is wrong OFX), but it is a standing exit-1 for such a user, and the message
  must say what to do (unlink the account or wait for the card path) rather than just what is
  wrong.
- One more field threaded through a frozen dataclass and two construction sites; one more thing
  `status` reports.
- **No file changes value today.** The whole change is a no-op for all 24 current accounts —
  16 mapped explicitly, 8 via the fallback (§1). This is a property, not a cost, but it is also
  why shipping it produces no observable diff to check against real data.

**Cost to reverse: low.** §4 is the measurement: `ACCTTYPE` is not part of libofx's
`account_id`, so no re-mapping — including backing this out entirely — can orphan an imported
account or re-import a transaction; and GnuCash makes `SAVINGS` and `CHECKING` the same
`ACCT_TYPE_BANK` on creation anyway. Reversal is deleting one field and one function and
restoring the comment.

---

## Open questions

- **What do erste, millennium and wise_personal actually send for `cash_account_type` — and does
  any account anywhere here send `SVGS`, `CARD` or `OTHR`?** Settled only by re-linking those
  banks: a user consent action costing **zero counted API requests** (`POST /sessions` is not an
  ASPSP data call in the daily-cap sense, and the v1→v2 state rewrite happens then anyway — the
  standing advice from [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) §7 is
  to re-link at the next consent expiry rather than specially). Until then the refusal branch has
  zero local test data and the `status` line of decision 3 covers exactly these 8 accounts.
- **Does `OTHR` ever occur in the wild here?** Same instrument, same answer: the next re-links
  are the only observation point. If one arrives and turns out to be something other than an
  ordinary account, option H is the section to reopen — with that observation in hand.

No probe rows: nothing here spent an ASPSP request. Everything live-looking above was settled
from local files and public source.

Settled by measurement, recorded so they are not re-derived:

- ~~Does changing `ACCTTYPE` on an already-imported account disturb GnuCash's match?~~ **No** —
  §4, from libofx and GnuCash source: `gen_account_id()` excludes it, and the importer reads it
  only as `default_type` for new accounts, where `SAVINGS` and `CHECKING` are both
  `ACCT_TYPE_BANK`.
- ~~Is `product` an alternative signal?~~ **No** — absent on all 24 records and all 16 raw
  session bodies (§5).
- ~~Is any file already written mislabelled?~~ **No** — input vocabulary is {`CACC`} ×16 plus 8
  schema-artefact absences; output vocabulary is {`CHECKING`} across 76 files / 642 statements
  (§1, Context). The fix is a no-op for everything on disk.
- ~~Can `TRAN` arrive?~~ **No** — not in Enable Banking's six-value enum (§2); the issue's
  mention came from the full ISO list.
- ~~Do the 8 absent values mean the banks omitted the field?~~ **No** — all 8 are v1-schema
  state files that predate the field; only a re-link can observe the banks' answers (§1).

---

Decisions 1–5 are condensed into [`decisions.md`](decisions.md) with the reversal cost attached;
this file stays for the rejected options — option H in particular — and the §4 source trail that
makes the reversal cost claim checkable.

## References

- [#22](https://github.com/skolima/gnucash-ofx/issues/22) — the issue, whose constraints §1–§3
  restate rather than relitigate.
- [`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) — decision 2 (group by message set,
  never assume one), §2's "every file is `BANKMSGSRSV1`/`CHECKING`" measurement this supersedes
  in wording, and the open question this ADR answers for today.
- [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) — decision 3 (a
  dry-run that predicts differently from the fetch is worse than none) and decision 4 (the
  warning-fatigue argument behind option G's rejection).
- [`decisions.md`](decisions.md) — *Partial success is reported, not thrown away*; the stdout
  contract; the invariant wording decision 5 touches.
- [`testing.md`](testing.md) — verification is `ofxdump`, not pytest; the `ofx-conformance`
  agent.
- Enable Banking API reference (fetched 2026-08-13) — the six-value `cash_account_type` enum.
- libofx `lib/ofx_container_account.cpp` (`gen_account_id()`), GnuCash
  [`gnc-ofx-import.cpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/ofx/gnc-ofx-import.cpp)
  (`gnc_import_select_account`, `default_type`) — both read 2026-08-13, the §4 confirmation.
- `ofxstatement` 0.9.3 — `Statement.__init__`'s `account_type="CHECKING"` default; `OfxWriter`'s
  message-set inventory.
