# ADR: one file, one message set, N statements — behind a flag that must not ship until GnuCash's importer is verified

**Status:** **Accepted and implemented — [#31](https://github.com/skolima/gnucash-ofx/issues/31).**
All three steps of the spike were settled by measurement before implementation began. Step 3 —
whether GnuCash's import assistant routes each `<STMTTRNRS>` to *its own* account — was verified
2026-08-10 by two real manual imports into the same throwaway book on **GnuCash 5.16** (Build
2026-06-27): round 1 (`merged_one_msgset.ofx`, three new accounts) ran three account-matching passes
followed by one combined transaction-import window with a per-line account column; round 2
(`merged_preexisting_accounts.ofx`, same three accounts, new transactions) matched all three
silently with zero popups and the account column pre-populated. Both rounds confirmed correct
per-account placement in the registers afterward. See *Open questions* for the full observation.
**Decisions 1–8 are implemented** ([#31](https://github.com/skolima/gnucash-ofx/issues/31); see
*Where this stands*). **Decision 1's gate is cleared for GnuCash 5.16 only** — a pass is a statement
about one version (decision 1's own caveat), so re-verification is owed before the flag is relied on
against whatever GnuCash version is current at that time.
Measurements in §1–§7 were taken 2026-08-09 against this machine's real `state/`, `cache/` and
`output/`, the installed `ofxstatement` 0.9.3, the test suite as of
[#17](https://github.com/skolima/gnucash-ofx/issues/17), and the GnuCash-bundled Windows
`ofxdump.exe` (libofx 0.10.5) described in [`testing.md`](testing.md). Every one was executed, not
inferred.
**Date:** 2026-08-09. **Gate verified:** 2026-08-10, GnuCash 5.16. **Shipped:** 2026-08-10,
[#31](https://github.com/skolima/gnucash-ofx/issues/31).
**Scope:** `ofxout.py` (a new composition function and the `OfxWriter` subclass behind it —
`build_statement`, `statement_to_ofx`, `ofx_filename` and `write_account_ofx` all stay as they are),
`run.py` (the per-bank write loop hands back `pending` instead of writing, and one write happens per
run), `cli.py` (one flag, threaded into `fetch` and `--dry-run`), `pyproject.toml` (an upper bound
on `ofxstatement`), plus the wording of the invariant in `AGENTS.md`, `decisions.md` and `prd.md`.
**Explicitly not in scope:** `FITID`, `BANKID`, `ACCTID`, `cache.py`, `state.py`, `coverage`, the
`NAME`/`MEMO`/`CHECKNUM` composition, `sources/enablebanking.py`, and the stdout **documentation**
defect found while measuring (§6 — a real bug, a separate change).
**Issue:** [#8](https://github.com/skolima/gnucash-ofx/issues/8). Depends on no other ADR.
Reuses the `ofxdump` harness that [`adr-currency-conversion-pairs.md`](adr-currency-conversion-pairs.md)
§1 established, and inherits its rule that a parse success is not an import success.

---

## Where this stands

| Part of the issue | Question | This ADR |
|---|---|---|
| 1 | Can we even write it? | **Yes, cheaply.** Decision 3: subclass `OfxWriter`, ~25 lines, one method overridden. §1 measured two routes to identical output |
| 2 | Does libofx parse it? | **Yes, exactly one shape of it.** Decision 2: one `BANKMSGSRSV1`, N `STMTTRNRS`. §3 measured the alternative failing DTD validation |
| 3 | Does GnuCash import it correctly? | **Yes, on GnuCash 5.16, on both branches.** Verified 2026-08-10 by two real manual imports (offered-for-creation, then pre-matched) — each statement's transactions landed in its own account both times, confirmed in the registers; see *Open questions* for the full observation |
| if it works | Opt-in, never the default | Decision 1 |
| if it works | stdout stays the file list | Decision 5 — and it must preserve the shape stdout *actually* has, which is not the documented one (§6) |
| if it works | Restate the invariant in code | Decision 4, with the exact rewording and where it lands |
| if it does not | Write the finding down | Decision 1's fail branch: the finding goes to [`testing.md`](testing.md) and the spike closes |

One thing the issue asserts that the measurements **correct rather than confirm**: the benefit is
real but smaller than "ten-plus import dances a month". §4 measures 8–10, with a hard ceiling of 19,
and shows the multi-currency multiplier the issue implies does not exist in this data at all.

**Per-decision status**, the convention [#21](https://github.com/skolima/gnucash-ofx/issues/21)
asks every multi-decision ADR to carry (this one predates the retrofit; added now rather than left
inconsistent):

- [x] **1.** Opt-in, gated on a real GnuCash import — gate cleared 2026-08-10 on GnuCash 5.16
      ([#30](https://github.com/skolima/gnucash-ofx/issues/30)); flag shipped
      ([#31](https://github.com/skolima/gnucash-ofx/issues/31))
- [x] **2.** One `<BANKMSGSRSV1>`, N `<STMTTRNRS>` —
      [#31](https://github.com/skolima/gnucash-ofx/issues/31)
- [x] **3.** Route A: subclass `OfxWriter`, override `buildTransactionList`, proxy `self.tb` —
      [#31](https://github.com/skolima/gnucash-ofx/issues/31)
- [x] **4.** Invariant reworded in `AGENTS.md`, `decisions.md` and `prd.md` —
      [#31](https://github.com/skolima/gnucash-ofx/issues/31)
- [x] **5.** stdout keeps its real shape, one path where there were N —
      [#31](https://github.com/skolima/gnucash-ofx/issues/31)
- [x] **6.** Combined filename derived from the window alone —
      [#31](https://github.com/skolima/gnucash-ofx/issues/31)
- **7.** Untouched — nothing to ship, nothing to tick
- **8.** Out of scope — nothing to ship, nothing to tick

---

## Context

The tool's whole premise is that you stop repeating yourself. It then ends by handing over a
directory of files to be imported one at a time, through an assistant with several pages, once per
account, every month. That work sits entirely *downstream* of everything the last three ADRs
optimised: rate-limit domains, month chunks and coverage ledgers all reduce what is *fetched*, and
none of them touch what is *imported*.

What exists today, measured on this machine 2026-08-09:

- `write_account_ofx` is Account + transactions → `Statement` → string → **one file**, called once
  per pending account at `run.py:1268`.
- `output/` holds 40 files across 11 distinct fetch windows. Files per window:
  1, 1, 1, 1, 2, 3, 5, 6, 7, 12, 1. The 12 is a ~90-day catch-up; the three month-shaped windows
  produced 5, 6 and 7.
- 6 configured banks, 6 live sessions, 19 linked accounts.

**What the current behaviour costs is measured in dialogs, not requests.** This is the one design in
the project whose cost is not counted against a bank's daily cap: composing several statements into
one file costs **zero API requests**, changes nothing about what is fetched, and is invisible to
every rate limit. The cost it removes is 8–10 runs of a multi-page import assistant per month (§4),
and the cost it risks is the one the issue names: a GnuCash that parses all the statements and
routes them all to one account, producing a silently merged register that looks correct and is
discovered months later. That asymmetry — a bounded, recurring annoyance against an unbounded,
silent corruption — is why this ADR is gated rather than accepted.

---

## Constraints, measured rather than assumed

All measurements 2026-08-09. Structural counts, dates and error codes only; no amounts,
counterparties or account numbers appear anywhere below or in any artefact referenced.

### 1. The writer seam is one line, and both routes through it produce identical output

`ofxout.py` composes nothing itself. Serialisation is a single expression:

```python
def statement_to_ofx(statement: Statement) -> str:
    return OfxWriter(statement).toxml(pretty=True)
```

The envelope/body separation lives upstream, in `ofxstatement/ofx.py` (0.9.3 installed;
`pyproject.toml` pins only `>=0.9.1`, with **no upper bound**). Read against that source:

- `buildDocument()` emits `<OFX>`, the signon block, and then calls `buildTransactionList()`. It is
  correct to run exactly once per document.
- `buildBankTransactionList()` emits `<BANKMSGSRSV1>` **and** `<STMTTRNRS>` **and** the statement
  body, in one method. The message-set wrapper is welded to the per-statement aggregate — which is
  precisely the thing a multi-statement document needs to separate.
- `self.statement` is a plain mutable attribute, **read at build time, not captured in
  `__init__`**. This is the property that makes the whole thing cheap.

Two routes were written and run to completion, and their emitted tag sequences were compared token
by token:

| Route | Shape | Size | Emitted tag sequence |
|---|---|---|---|
| **A — subclass** | Override `buildTransactionList`; drive `self.statement` round a loop | ~25 lines, one method | **228 tokens** |
| **B — string surgery** | Concatenate the bodies out of N independent `toxml()` outputs | ~10 lines | **228 tokens, byte-for-byte identical to A** |

Two findings from having actually run them rather than reasoned about them:

- **`xml.etree.ElementTree.TreeBuilder` is a C type whose `.start`/`.end` are read-only**
  (`AttributeError: … attribute 'start' is read-only`), so route A cannot suppress the repeated
  `<BANKMSGSRSV1>` by patching the builder in place. A **proxy object assigned to `self.tb`** does
  work, because `OfxWriter` reads `self.tb` on every call rather than binding it once.
- **The subclass driven with a single statement is tag-identical to today's `statement_to_ofx`
  output.** So the flag degrades exactly to current behaviour at N=1, and there is no separate
  code path to keep in step.

The coupling this creates is worth naming rather than burying: route A depends on
`buildBankTransactionList` existing under that name and on `self.statement` being re-readable
between calls, against a dependency with no pinned upper bound. Decision 3 pays for that with a
version bound and a structural test.

### 2. Nothing account-specific sits at document level

Verified by reading the emitted documents:

| Element | Where it sits today |
|---|---|
| `<SONRS>` (signon) | Exactly **once** per document |
| `<CURDEF>` | **Per statement** |
| `<BANKACCTFROM>` with `<BANKID>`/`<ACCTID>`/`<ACCTTYPE>` | **Per statement** |
| `<BANKTRANLIST>` with its `DTSTART`/`DTEND` | **Per statement** |
| `<LEDGERBAL>` | **Per statement** |

So a combined document needs no new fields and loses nothing: every value that identifies an
account, names its currency, or dates its coverage is already inside the per-statement aggregate.
This is what makes the change *packaging only*, in the literal sense.

Two related facts about what `ofxstatement` 0.9.3 can emit at all:

- Its `OfxWriter` has **no `CREDITCARDMSGSRSV1` path**. There is one message set in the writer.
- `Statement.__init__` defaults `account_type="CHECKING"` and `build_statement` (`ofxout.py:247`)
  never overrides it. **Every file this project has ever written is `BANKMSGSRSV1` / `CHECKING`.**

So a combined file needs **one** message-set block, not two. That is a consequence of what we emit
today, not a claim about the world — see the `ACCTTYPE` open question.

### 3. The decisive measurement: libofx accepts one message set and rejects several

Four documents built by hand from synthetic data, run through the Windows `ofxdump.exe` (libofx
0.10.5) per [`testing.md`](testing.md):

| Variant | exit | `LibOFX ERROR` lines | statements recovered |
|---|---|---|---|
| Single statement — today's output, as a control | 0 | 0 | 1 |
| **One `BANKMSGSRSV1`, three `STMTTRNRS`** | **0** | **0** | **3** |
| Three repeated `BANKMSGSRSV1` blocks | **1** | **2** | 3 (recovered anyway) |
| One `BANKMSGSRSV1` (2 statements) + one `CREDITCARDMSGSRSV1` | 0 | 0 | 3 |

The repeated-message-set variant fails DTD validation with, verbatim:

```
document type does not allow element "BANKMSGSRSV1" here
```

It still recovers all three statements — libofx wraps the stray block in an `OfxDummyContainer` —
but it **exits non-zero and prints errors that GnuCash's UI would swallow**. A file that is invalid
and works anyway is the worst available outcome: it is one libofx release away from silently losing
a statement, with nothing in the UI to say so.

From the accepted variant, libofx reported:

- **three distinct `BANKID`+`ACCTID` pairs**, including two accounts sharing a `BANKID` and
  differing only by `ACCTID` — the case that most resembles a real bank with several accounts, and
  the one most likely to collapse if a matcher keys on the wrong field;
- **the correct currency attached to each statement independently** for a mixed PLN/EUR document.

That is the strongest statement available from this side of the boundary: **libofx delivers N
correctly-identified, correctly-denominated statements to its caller.** It says nothing about what
its caller does with them, which is §8 and the open question.

### 4. N is smaller than the issue assumes, and the multi-currency multiplier does not exist

The issue's motivating figure is "ten-plus times a month". Measured two ways.

**From `output/`** (11 windows, 40 files): 1, 1, 1, 1, 2, 3, 5, 6, 7, 12, 1 files per window. The
12-file window is a ~90-day catch-up; the month-shaped windows produced 5, 6 and 7.

**From the cache**, counting accounts in a current session with at least one transaction in each
calendar month:

| Month | Accounts with ≥1 transaction |
|---|---|
| Jan | 3 |
| Feb | 2 |
| Mar | 2 |
| Apr | 2 |
| May | 9 |
| Jun | 10 |
| Jul | 8 |
| Aug | 8 |

So **realised monthly N is 8–10**, with a ceiling of 19 (every linked account) and a floor of 0.
The early-year figures are low because coverage there is thin, not because the accounts were quiet.

And the multiplier the issue implies is not there. Of the **32 cached account digests whose currency
is determinable, zero hold more than one currency.** Multi-currency at Wise and Alior Kantor is
expressed as **separate accounts**, not as one account yielding several statements: `Account`
carries a single `currency`, and the transaction mapping raises on a mismatch rather than splitting.
**One account is exactly one statement, always.**

This is recorded because the issue's own framing would otherwise survive into the changelog. The
benefit is 8–10 assistant runs collapsing to 1, which is worth having; it is not the
account-count-times-currency-count figure the hypothesis reached for, and nothing in this design
should be justified by that larger number.

### 5. There is no merge collision to resolve

Combining statements into one file creates one new risk that per-file output does not have: two
statements carrying the same identifier, where a consumer might key on the identifier alone.

`_transaction_id` prefers the bank's `transaction_id`, else `entry_reference`, else a deterministic
hash. In real cached data both bank-supplied sources are in use — roughly one third and two thirds
respectively — and **the hash branch has never been exercised by any cached transaction.**

| Scope | Result |
|---|---|
| The 13 current-session accounts holding cached transactions | **No `FITID` appears in more than one account** |
| All 43 cached account digests | 9 groups share `FITID`s — and **every such group contains a digest with no current session** |

The 9 groups are stale cache written under an Enable Banking `uid` that was regenerated by a
re-link, i.e. the same account twice under two identities, not two accounts colliding. There is no
live collision. OFX scopes `FITID` uniqueness to the `ACCTID` in any case, so even a genuine
cross-account repeat would be well-formed.

The one genuinely shared value is **`<TRNUID>`, hardcoded `"0"` by `OfxWriter` for every aggregate**,
which in a combined file means N identical `TRNUID`s. libofx raised zero errors on it and does not
surface the field at all. Whether GnuCash notices is an open question, listed below with what would
settle it.

### 6. The stdout contract is documented wrong, and the flag must preserve the real shape

Measured by capturing `_report()` and `_report_dry_run()` output to a file:

```
Wrote 3 OFX file(s) to output:
  output/alior_PLN_2026_07_01-2026_07_31.ofx
  output/erste_EUR_2026_07_01-2026_07_31.ofx
  output/millennium_PLN_2026_07_01-2026_07_31.ofx
```

A prose header line, then one path per line **indented by exactly two spaces**. The two empty cases
print a single prose line and no paths. `--dry-run` mirrors it (`Would write up to N …`).

`docs/prd.md`, `README.md` and `AGENTS.md` all describe stdout as bare paths, one per line. So
`fetch > files.txt` today produces a file whose first line is prose and whose paths need
`sed 's/^  //'`. **The contract the docs promise has never been the contract the code implements.**

Two things follow, and they must not be confused with each other:

- The flag must preserve the shape stdout **actually** has, with one path where there were N. That
  is decision 5, and it is in scope.
- **Fixing the documentation — or the code — is a separate change.** It is a real defect, named here
  so it is not lost, and deliberately not folded into this decision: a packaging flag that also
  quietly redefines the stdout contract is two changes wearing one hat, and the second one deserves
  its own argument about which side is wrong.

### 7. The test suite has no opinion about document structure, so the blast radius is additive

| Measure | Count |
|---|---|
| Test functions in `tests/` | 412 |
| Touching file packaging at all | **94** (cli 23, run 40, ofxout 21, coverage-warnings 8, cache 1, coverage 1) |
| Asserting document structure (`STMTTRNRS`, `BANKMSGSRSV1`, a golden OFX fixture) | **0** |

Neither tag appears anywhere in `tests/`, and there is no golden file. Because the flag is opt-in
and off by default, **all 94 keep passing unchanged.** The work is roughly four new sibling tests —
a multi-statement structural test in `test_ofxout.py`, a file-count test in `test_run.py`, a stdout
test in `test_cli.py`, and a `--dry-run` prediction test — editing none of the existing ones.

The absence of any structural assertion is itself a finding: nothing today would catch an
`ofxstatement` upgrade changing the emitted tag order. The new structural test is the first one, and
it is what makes decision 3's coupling to an upstream private-ish method safe to take on.

### 8. `ofxdump` cannot answer step 3, and no local artefact can

`ofxdump` is a thin consumer of libofx's callback API: it demonstrates what libofx **delivers to its
caller**. GnuCash's `gnc-ofx-import.cpp` is a different caller. How it iterates the statement list,
how many passes of the import assistant it runs, and how it resolves each statement's
`BANKID`+`ACCTID` to a book account are properties of that file and of the assistant's UI flow, not
of the parser.

So step 3 is not a harder version of step 2; it is a different question with a different instrument,
and the instrument is a human importing a file into a scratch book. This is the entire premise of
the issue, and it is the one thing this ADR does not settle.

It is also, usefully, **not an Enable Banking probe.** It costs zero API requests, spends no bank's
daily allowance, and needs no consent. The gate on it is availability of a person, not budget —
which is why *Open questions* below states it as an executable procedure rather than as something to
wait for.

---

## Options considered

### A. Do nothing — keep one file per account

**Rejected as the *default*, kept as the default.** That is not a contradiction: §4 shows the cost
is 8–10 assistant runs a month, which is worth removing, while §8 shows the risk of removing it
badly is a silently merged register. So this option loses the argument about what should be
*possible* and wins the argument about what should be *automatic*. It survives as decision 1's
"per-file output stays the documented path".

### B. Repeat `<BANKMSGSRSV1>`, one per statement

The obvious composition: run the existing single-statement writer N times and concatenate the
message-set blocks.

**Rejected on §3.** The document fails DTD validation — `document type does not allow element
"BANKMSGSRSV1" here` — `ofxdump` exits 1 and prints two `LibOFX ERROR` lines. libofx recovers the
data anyway by wrapping the stray blocks in an `OfxDummyContainer`, which is exactly what makes this
dangerous: it would appear to work in GnuCash, whose UI does not show libofx's stderr, while resting
on error recovery rather than on the format. This is the measurement that kills the naive option,
and it is why decision 2 is stated as a structural constraint rather than as an implementation
detail.

### C. Ship it as the default

**Rejected on the issue's own constraint, and independently on §8.** Per-file output has years of
real mileage; the combined form has none, and the failure mode is not a broken import but a
plausible-looking one. Even after the gate in decision 1 passes on this machine, one verified import
on one GnuCash version is not the evidence needed to move a default that every user's book depends
on. Revisit after real mileage, as its own change.

### D. Route B — build the combined document by string surgery on N `toxml()` outputs

Genuinely attractive: ~10 lines against ~25, no subclass, no dependence on `ofxstatement`'s internal
method names, and §1 measured its output as **byte-for-byte identical** to route A's.

**Rejected on maintenance grounds, and the rejection is honest about being a judgement rather than a
measurement — the two are indistinguishable on correctness.** Three reasons, in order of weight:

1. It couples to the *serialised text* instead of to a method name. A change in `ofxstatement`'s
   pretty-printing, indentation or tag order breaks a string-slicing routine in ways that produce a
   subtly wrong document; the same change breaks a subclass in ways that produce an exception or a
   failing structural test.
2. The failure mode of "find the opening tag and cut" on unexpected input is a malformed OFX file,
   which §3 establishes is the category of outcome to avoid at all costs here.
3. Route A degrades to today's exact output at N=1 (§1), so there is one code path for one statement
   and many. Route B would have a distinguishable one-statement special case or would slice a
   single document pointlessly.

Route B stays written down as the escape hatch if `ofxstatement` ever removes
`buildBankTransactionList`: it is ten lines and it is known to work.

### E. Compose the OFX document ourselves and drop `ofxstatement`'s writer

**Rejected.** §1 measured the cost of *not* doing this at ~25 lines and one overridden method. Owning
the whole document would mean owning `<SONRS>`, the DTD-ordered children of `STMTRS`, the date and
amount formatting, and the escaping — all of it currently correct and all of it exercised by the
existing suite. The issue explicitly flagged "composing the document ourselves" as a real cost to
weigh against the benefit; measured, that cost is not one we have to pay.

### F. One combined file **per bank** rather than per run

Considered because it keeps the write inside `fetch_bank`, where it is today, and needs no
restructuring at all.

**Rejected on §4's numbers.** With 6 banks and 8–10 monthly accounts, per-bank combining takes the
monthly import from 8–10 dialogs to 6. Per-run combining takes it to 1. The restructuring it avoids
is small — `fetch_bank` returning `pending` instead of writing, with one write in
`fetch_enablebanking` — and the same shape already exists for a different reason
([`adr-currency-conversion-pairs.md`](adr-currency-conversion-pairs.md) §7 relies on every account
being collected before any file is written). Paying a small restructuring to get most of the benefit
rather than a third of it is the trade.

### G. One `BANKMSGSRSV1` carrying N `STMTTRNRS`, behind a flag, gated on a real import

**Accepted — the gate has since passed** — below.

---

## Decision

### 1. The flag is opt-in, and it does not ship until a real GnuCash import is verified

`fetch --combine` (name to be settled in implementation; the semantics are what this decision
fixes). Off by default. **Per-file output remains the documented path**, and remains what a bare
`fetch` produces.

**The gate is hard.** The implementation may be written and reviewed, but it must not appear in a
release — nor have its flag documented in `README.md` — until the procedure in *Open questions* has
been run and passed against a throwaway book. A parse success does not clear the bar; §8 is why.

**The gate passed, twice.** 2026-08-10, GnuCash 5.16 (Build 2026-06-27): a manual import of
`merged_one_msgset.ofx` into a throwaway book routed each of the three statements to its own account
(new accounts, offered for creation), and a second import of `merged_preexisting_accounts.ofx` — same
three accounts, new transactions — matched them silently and again routed correctly. Both confirmed
in the registers afterward — see *Open questions* for the full observation. This decision's condition
is met **for GnuCash 5.16**; decisions 2–8 are no longer conditional.

**The fail branch is a real outcome, not a fallback.** If GnuCash routes several statements to one
account, or runs one assistant pass that binds the whole file to one account, the spike closes: the
finding — "GnuCash binds one file to one account" — is written into [`testing.md`](testing.md) with
the version tested and the date, the flag is not implemented, and this ADR stays as the record of
why. That outcome is worth as much as a success, because it is what stops the question being
reopened.

### 2. The document carries exactly one `<BANKMSGSRSV1>`, containing N `<STMTTRNRS>`

The structural constraint, stated so it can outlive the code that implements it. Repeating the
message set is **invalid OFX** (§3) and must never be emitted, even though libofx recovers from it.

- One `<SONRS>` per document, as today (§2).
- Every account-identifying, currency-naming and date-bearing element stays where it already is,
  inside the statement (§2).
- **One message set is sufficient only because every statement this project emits is
  `BANKMSGSRSV1`/`CHECKING`** (§2). If `ACCTTYPE` ever becomes correct for a card account (see
  *Open questions*), the file needs a **second** message-set block, one per type — a shape §3
  measured as clean, exit 0, zero errors. The composition must therefore group statements by message
  set rather than assume one, even while the grouping has exactly one group today.
- A statement with no transactions is **omitted**, preserving `write_account_ofx`'s current
  behaviour of writing nothing for an empty account. If every statement is empty, **no file is
  written** and stdout says so, exactly as today.
- The combined file is composed **once per run, from whatever `pending` accumulated across all
  banks** (option F). `fetch_bank` returns its pending accounts instead of writing them;
  `fetch_enablebanking` writes once.

### 3. Route A: subclass `OfxWriter`, override `buildTransactionList`, proxy `self.tb`

~25 lines in `ofxout.py`, alongside `statement_to_ofx` rather than replacing it. The subclass drives
`self.statement` round a loop and suppresses the repeated message-set wrapper by assigning a proxy to
`self.tb` — a `TreeBuilder`'s `.start`/`.end` are read-only C attributes and cannot be patched in
place (§1).

Two things this decision commits to beyond the code:

- **`pyproject.toml` gains an upper bound on `ofxstatement`.** It currently pins `>=0.9.1` with no
  ceiling, and this subclass depends on `buildBankTransactionList` existing and on `self.statement`
  being re-read on each call. An unbounded dependency plus a private-ish coupling is how a
  future `uv sync` silently changes the shape of every file we write.
- **The first structural test in the suite** (§7): assert the emitted tag sequence for N statements,
  and assert that the subclass at N=1 is identical to `statement_to_ofx`. Nothing today would catch
  an upstream change to tag order.

This rules out route B (option D) and rules out owning the document (option E). Route B stays
recorded as the ten-line escape hatch if the upstream method disappears.

### 4. The invariant is reworded, not broken — and the restatement lands at the composition seam

The `AGENTS.md` "Invariants (do not break)" entry currently reads, verbatim:

> **One OFX file per account, per currency.** An OFX statement is single-currency, so
> multi-currency accounts (Wise, Alior FX) produce one file each.

The flag changes a written invariant, and pretending otherwise is how invariants rot. Its **stated
reason is untouched** — a statement is still single-currency, and §4 measures that one account is
exactly one statement, always. It is the **file** granularity that becomes variable. Reworded:

> **One statement per account, per currency.** An OFX statement is single-currency (`CURDEF`), so
> multi-currency positions (Wise, Alior FX) are separate accounts and produce one statement each.
> Packaging is separate: by default one statement per file; `--combine` puts every statement of a
> run into one file and changes nothing else about them.

The same rewording lands in [`decisions.md`](decisions.md) (*One OFX file per account, per
currency*, whose title changes to *One statement per account, per currency*) and in `prd.md`'s
output contract.

**In code, the restatement goes on the new composition function in `ofxout.py`** — the one place
where several statements meet — and says what the file layout no longer shows: that each statement
is one account in one currency, that the loop must never merge two accounts' lines into one
statement, and that `CURDEF` is per statement and not per document. Putting it anywhere else (a
module docstring, `run.py`) puts it where the mistake would not be made.

### 5. stdout keeps its real shape, with one path where there were N

The stdout-is-the-file-list contract is preserved as **implemented**, not as documented (§6): the
prose header line, then paths indented by two spaces. With the flag, that is one path instead of N,
so `fetch > files.txt` behaves exactly as it does today — including the pre-existing need to strip
the header and the indent.

`--dry-run` mirrors it, as it already does.

**The documentation defect is named and excluded.** `prd.md`, `README.md` and `AGENTS.md` describe a
stdout format that has never existed. Fixing it — in the docs or in the code, which is the question —
is a separate change with its own argument. This decision commits only to not making it worse.

### 6. The combined filename is derived from the window alone

`combined_<from>-<to>.ofx`, using the same fixed-width date spelling as `ofx_filename` so
lexicographic order stays chronological.

**This is a policy pick, not a measurement.** The arithmetic it was picked against: a combined file
spans banks and currencies, so neither of the two components that make a per-account name
informative (`<bank_key>_<currency>`) can appear; what remains that is both true and stable is the
window, which is also what makes re-fetching a different range write a new file instead of
overwriting one. One collision to keep in mind: a bank literally keyed `combined` would produce a
per-account name with the same prefix. Worth a guard in `safe_component`'s caller rather than a
redesign.

### 7. Untouched

`FITID`, `BANKID`, `ACCTID` and their resolution; the `NAME`/`MEMO`/`CHECKNUM` composition;
`LEDGERBAL` and its per-statement placement; the cache key and the month-chunk layout; the state
schema; the coverage ledger; `sources/enablebanking.py`; per-bank failure isolation; and the
statement-level invariant of decision 4. **No API request is added, removed or changed by anything in
this ADR** — the request count of a `--combine` run is identical to the same run without it.

### 8. Out of scope

- **The stdout documentation defect** (§6, decision 5).
- **`ACCTTYPE`** — always `CHECKING` today because `Statement.__init__` defaults it and the bank's
  account type is never consulted. See *Open questions*.
- **A `CREDITCARDMSGSRSV1` path** in the writer. Decision 2 leaves room for it; nothing needs it
  until `ACCTTYPE` is settled.
- **Combining across *runs*** — appending to an existing combined file. Different problem, different
  dedup story.
- **Making it the default** (option C), which needs mileage this design cannot have yet.

---

## Consequences

**Accepted costs.**

- **A run's output is one file, so a crash mid-run leaves nothing rather than partial files.** Today
  each bank's files land as that bank completes. Failure *isolation* is preserved in substance —
  `BankFailure`s are collected rather than raised, so a failed bank contributes no statements and
  the file is still written from the banks that succeeded — but the *write* moves to the end of the
  run. A hard crash (not a bank failure) between the first bank finishing and the write loses what
  per-file output would have kept. Small, real, and it only bites with the flag on.
- **A dependency bound is added**, and with it the obligation to test an `ofxstatement` upgrade
  rather than accept it (decision 3).
- **One more thing that can be true of the output**, so a bug report now has to say which packaging
  was used. The flag being off by default keeps that cheap.
- **A written invariant changes wording** (decision 4). Anyone who learned the old sentence has to
  re-learn it, and the rewording has to reach three documents plus the code comment before the flag
  ships, or the invariant becomes folklore.
- **The benefit is 8–10 dialogs a month, not the figure the issue quotes** (§4). Worth having;
  worth not overselling.

**Cost to reverse: low for the code, high for a bad import.** These are different numbers and the
distinction is the point of decision 1.

- *Reversing the code* is deleting a flag, a subclass and four tests. Nothing here touches `FITID`,
  `BANKID` or `ACCTID`, so no account orphans and nothing re-imports; a user who imported a combined
  file and then reverts can import the per-account files for the same window and GnuCash's `FITID`
  dedup will discard the duplicates. Essentially free.
- *Reversing a silent merge* is not. If GnuCash routes several statements into one register, the
  damage is transactions in the wrong account in the user's book, with correct-looking amounts, no
  error anywhere, and discovery weeks or months later. Unpicking that is manual, per transaction,
  and there is no undo. **This asymmetry is the entire reason decision 1 gates on a human import
  rather than on `ofxdump`.**

---

## Open questions

### The gate: does GnuCash's importer match each statement to its own account?

**Settled — yes, on GnuCash 5.16.** Verified 2026-08-10 by a QA manual import of a regenerated
`merged_one_msgset.ofx` (built through the project's real `build_statement()`, message-set wrapping
composed by hand per decision 2, `ofxdump`-clean beforehand: exit 0, zero errors, three correct
`BANKID`+`ACCTID`+`CURDEF` triples) into a throwaway book, per the procedure below.

**What was observed:**

- **Three account-matching passes, one per statement.** GnuCash's initial-setup assistant offered
  each `BANKID`+`ACCTID` pair for matching/creation individually — none of the three statements were
  pre-matched, and the assistant did not treat the file as one account.
- **One combined transaction-import window followed**, not three — every transaction from all three
  statements listed together, each line carrying its own account in a per-line account column.
  Currencies displayed correctly per line (PLN, PLN, EUR).
- **Placement verified in the registers afterward**, per step 4 of the procedure, not from the
  assistant's summary: each transaction landed in the account named by its own `BANKID`+`ACCTID`.
  No transaction was routed to the first matched account, and no statement's lines bled into
  another's register.
- The register Description read `"Memo one; Payee One"` for every transaction. That is
  `compose_name()`'s remittance-first join (`ofxout.py:135`) doing exactly what it is documented to
  do — unrelated to message-set combination, and not a finding this ADR needs to carry.

**Round 2, same day, pre-matched path.** A second import, same book, same three accounts (now
existing from round 1), a fresh file (`merged_preexisting_accounts.ofx`) with four new transactions
and no `FITID` overlap with round 1:

- **Zero account-matching popups** — GnuCash matched all three `BANKID`+`ACCTID` pairs silently
  against the existing accounts, rather than prompting once per statement.
- **The single transaction-import verification window** came up as before, but this time with the
  target account column **pre-populated** per line instead of left for the user to choose.
- Placement in the registers was correct again, across all four transactions and both currencies.

**Pass**, by the bar this ADR set, **on both branches**: each statement's transactions land in its
own account whether the account is pre-matched (round 2) or offered for creation (round 1) —
neither branch showed cross-statement bleed or first-match routing. Decisions 2–8 are no longer
conditional.

**Scope of this pass:** one version (GnuCash 5.16, Build 2026-06-27), one throwaway book, three
statements, both the pre-matched and offered-for-creation account paths, no card accounts exercised,
no `FITID` collisions exercised. Decision 1 stands even though the gate passed: a pass is a statement
about one version, so re-verification is owed before the flag ships against whatever GnuCash version
is current then.

The four artefacts below were built during the original measurement and are **synthetic
throughout**:
placeholder `BANKID`s (`AAAAPLPW`, `BBBBPLPW`), placeholder `ACCTID`s
(`PL10000000000000000000AAAA` and siblings), `Payee One` / `Memo one`, round amounts, and the
universal Visa test number for the card case. Nothing from real `cache/` survives into them — the
script that read real data printed tag paths and counts only, and never wrote a document to disk.
These four are safe to attach to an issue or PR, **named individually**.

They live in a session scratchpad directory that is **not** wholly synthetic: it also holds a
`--dry-run` capture taken against the real `config.toml` and `state/`, carrying redacted-but-partial
account numbers paired with the bank that issued each. **Never attach or paste the directory** —
only the four files named below. Both the scratchpad path and its contents are session-scoped and
will be gone shortly; §1's two routes reproduce all four in minutes, which is the durable way to get
them back. They are not committed either way — `*.ofx` is gitignored.

| File | What it is |
|---|---|
| **`merged_one_msgset.ofx`** | **The one to import.** One `BANKMSGSRSV1`, three `STMTTRNRS`; two accounts sharing a `BANKID` and differing only by `ACCTID`; mixed PLN/EUR |
| `subclass_multi.ofx` | Route A's output, for confirming the writer and the hand-built file agree |
| `merged_bank_plus_cc.ofx` | Two message sets, for the `ACCTTYPE` question below |
| `merged_repeated_msgset.ofx` | The invalid variant of option B, kept so the DTD error can be re-observed |

**Procedure.** Retained below as the runbook for re-verifying against a future GnuCash version — see
the executed-run note after step 2 for how the 2026-08-10 run actually differed from step 2.

1. Create a **throwaway** GnuCash book. Never the real one.
2. Pre-create three accounts matching the three `BANKID`+`ACCTID` pairs in
   `merged_one_msgset.ofx` — two sharing a `BANKID`, one in EUR.

   **As executed 2026-08-10, round 1:** no accounts were pre-created. The import instead exercised
   the *unmatched-statement* path for all three — GnuCash's initial-setup assistant ran three
   account-matching passes and offered each pair for creation.

   **Round 2, same day:** a second file (`merged_preexisting_accounts.ofx` — same three
   `BANKID`+`ACCTID`+`CURDEF` triples, four new transactions with unique descriptions, no `FITID`
   overlap with round 1) was imported into the **same** book, whose accounts now existed from round
   1's creation. This exercised step 2 as originally written: **zero account-matching popups** —
   GnuCash matched all three accounts silently — followed by the single transaction-import
   verification window, with the target account column **pre-populated** for every line rather than
   left for the user to pick. Transactions landed in the correct registers, same as round 1. Both
   branches of the pass bar — pre-matched and offered-for-creation — are now independently
   confirmed on GnuCash 5.16.
3. Import the file. Record, in this order:
   - **how many assistant passes run** (one, or one per statement);
   - **whether each statement's transactions land in its own account**;
   - **what happens to a statement whose account has no prior match** — offered for creation,
     silently skipped, or folded into a matched account;
   - **whether any transaction is routed to the first matched account** rather than its own.
4. Verify placement **in the registers afterwards**, not from the assistant's summary.

**Pass:** each statement's transactions land in the account named by its own `BANKID`+`ACCTID`, and
an unmatched statement is either offered for account creation or reported — anything visible.
**This is what was observed 2026-08-10** — see above.

**Fail, stated as the issue states it: transactions from more than one statement landing in a single
register, with no error.** A fail closes the spike, the flag is not implemented, and the finding
goes into [`testing.md`](testing.md) with the GnuCash and libofx versions and the date.

Record the GnuCash version alongside the result either way. A pass is a statement about one version,
which is also why decision 1 stops at a flag rather than a default.

### Does a duplicated `<TRNUID>` matter to GnuCash?

**Settled by the same import — no.** `OfxWriter` hardcodes `TRNUID` to `"0"` for every aggregate, so
the combined file repeats it three times (§5); libofx already raised zero errors on this and does not
surface the field to its caller (§3). The 2026-08-10 import ran with all three `TRNUID`s unmodified
and identical, and the pass criteria above were met — answered by construction, exactly as this
question anticipated. GnuCash does not appear to key anything on it.

### Can `--dry-run` predict the combined file's path?

Decision 6 derives the name from the resolved window alone, which makes it **more** predictable than
a per-account name: it needs no account enumeration. But `--dry-run` cannot say whether the file will
be written *at all*, because that depends on at least one account having a transaction — the same
limitation the per-account prediction already has.

Separately, **5 of the 6 banks here are on the pre-`accounts` state layout** and have no link-time
currency, so `--dry-run` cannot predict their per-account paths today
([`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) §6). **That is a
pre-existing condition, not something this change causes**, and the combined form is unaffected by it
— which is a small argument in the flag's favour rather than against it.

**What would settle it:** a `--dry-run` prediction test written against a config with the flag on
and no session state at all, asserting the single predicted path. Local, free, and part of decision
3's four tests.

### Are there card accounts, mislabelled as `CHECKING`?

`ACCTTYPE` is `CHECKING` on every statement this project has ever written, because
`Statement.__init__` defaults it and the bank's account type is never consulted (§2). So the
measurement **cannot distinguish "no card accounts exist" from "card accounts exist and are
mislabelled"**, and decision 2's "one message set is enough" rests on what we emit rather than on
what is true.

**What would settle it:** read `cash_account_type`, `usage` and `product` from the Enable Banking
account objects already stored for the 19 linked accounts, and report **bare category counts** — no
identifiers, no names. That is a local read of existing state; **zero requests.** If any account is
a card, `ACCTTYPE` is a correctness bug in its own right (an issue, not this ADR), and decision 2's
group-by-message-set is what makes the combined file cope with it.

### Settled by measurement, recorded so they are not re-derived

- ~~Can several statements be written without composing the document ourselves?~~ **Yes** — ~25
  lines, one overridden method, and the N=1 output is identical to today's (§1).
- ~~Does string surgery differ from the subclass in output?~~ **No** — byte-for-byte identical, 228
  tokens each (§1). The choice is maintenance, not correctness (option D).
- ~~Does libofx parse a multi-statement file?~~ **Yes, with one message set** — exit 0, zero errors,
  three statements, three distinct `BANKID`+`ACCTID` pairs, correct per-statement currency (§3).
- ~~Can the message set simply be repeated per statement?~~ **No** — `document type does not allow
  element "BANKMSGSRSV1" here`, exit 1, two error lines; data recovered only via libofx's dummy
  container (§3).
- ~~Does anything account-specific sit at document level?~~ **No** — `CURDEF`, `BANKACCTFROM`,
  `BANKTRANLIST` and `LEDGERBAL` are all already per statement (§2).
- ~~Is the multi-currency multiplier real?~~ **No** — of 32 cached digests with a determinable
  currency, **zero** hold more than one. One account is one statement, always (§4).
- ~~Is the monthly import really "ten-plus" dances?~~ **8–10**, ceiling 19 (§4).
- ~~Would combining create `FITID` collisions?~~ **No** — no `FITID` appears in more than one
  current-session account; the 9 sharing groups are all stale post-re-link cache (§5).
- ~~How many existing tests would need editing?~~ **None** — 0 of 412 assert document structure, and
  the flag is off by default (§7).
- ~~Does GnuCash's importer match each statement to its own account?~~ **Yes, on GnuCash 5.16, on
  both the offered-for-creation and pre-matched paths** — two real manual imports (round 1: three
  new accounts; round 2: the same three, pre-existing) each placed every statement's transactions
  in its own account, confirmed in the registers afterward, with no cross-statement bleed
  (2026-08-10, *Open questions*).
- ~~Does a duplicated `TRNUID` matter to GnuCash?~~ **No** — three identical `TRNUID`s were present
  in the same passing import and GnuCash did not misroute anything on account of it (2026-08-10).

## References

- [#8](https://github.com/skolima/gnucash-ofx/issues/8) — the spike, and the source of the
  acceptance bar, the opt-in constraint and the silent-merge risk this ADR is built around.
- [`testing.md`](testing.md) — the Windows `ofxdump` harness used for §3, and where a fail branch's
  finding goes.
- [`decisions.md`](decisions.md) — *One OFX file per account, per currency* (reworded by decision 4);
  *Partial success is reported, not thrown away* (the failure-isolation rule decision 2 preserves).
- [`adr-currency-conversion-pairs.md`](adr-currency-conversion-pairs.md) — §1 established the
  `ofxdump` harness and §8 the rule that a parse success is not an import success; §7 the
  collect-then-write shape option F relies on.
- [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) — §6, the five banks
  on the pre-`accounts` layout behind the `--dry-run` open question.
- `ofxstatement` 0.9.3 `ofx.py` — `buildDocument`, `buildTransactionList`,
  `buildBankTransactionList`, and `self.statement` as a mutable attribute (§1); `Statement.__init__`
  defaulting `account_type` (§2).
- `ofx160.dtd` as shipped with GnuCash for Windows — the `BANKMSGSRSV1` content model behind §3's
  validation error.
- GnuCash
  [`gnc-ofx-import.cpp`](https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/ofx/gnc-ofx-import.cpp)
  — the caller whose statement-list iteration §8 cannot observe from here.

---

**Shipped.** Decisions 1–8 landed in [#31](https://github.com/skolima/gnucash-ofx/issues/31), which
implemented `fetch --combine` and, per decision 4, reworded the invariant in `AGENTS.md`,
`decisions.md` and `prd.md` in the same change — decisions 2–8 condensed into `decisions.md`'s *One
statement per account, per currency* section there. Decision 1's gate (the import verified) had
already been met 2026-08-10 on GnuCash 5.16, both branches
([#30](https://github.com/skolima/gnucash-ofx/issues/30)). This file stays as the record of the
rejected options and the measurements — most of all §3, which decides the shape of the document, and
§4, which corrects the size of the prize.
