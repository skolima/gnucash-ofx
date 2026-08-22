# ADR: Revolut's five currency pockets resolve to two `ACCTID`s — that, not `BANKID`, is what must be settled before the first import

**Status:** **Accepted 2026-08-14 — option M, the uniform hash policy, chosen for decision 1 —
implemented the same day in [#49](https://github.com/skolima/gnucash-ofx/issues/49) (merged
2026-08-14), and completed the same day: the re-fetch, the first real import, and a re-link all
happened 2026-08-14, closing decision 2 and turning decision 1's hash-stability caveat into a
first observation (5 of 5 hashes unchanged, 0 `ACCTID`s moved — see decision 1's first caveat and
[`enable-banking.md`](enable-banking.md)). Only decision 9's deferral
([#50](https://github.com/skolima/gnucash-ofx/issues/50)) remains open.** Revised twice
on 2026-08-12, both times by measurement overturning the plan, completed by the GnuCash gate on
2026-08-13, which confirmed it, then accepted on 2026-08-14 with the one choice measurement could
not make:

1. **After `link`.** The first draft made the `BANKID` choice load-bearing and blocked it on
   inspecting `account_servicer.bic_fi` in the link body. `account_servicer` is null on all 5
   accounts — the whole object, not merely a null `bic_fi` — so that evidence will never arrive.
   What the body *does* show is a collision one level down: **four of the five pockets resolve to a
   single `ACCTID`**, which no choice of `BANKID` can fix, because the colliding component is the
   other half of `online_id`.
2. **After the first `fetch`.** The collapse turns out to cost more than mis-matched accounts.
   `transaction_id` is null on every Revolut transaction, so `FITID` falls through to
   `entry_reference` — which is the field both legs of a currency conversion share byte-identically.
   **Conversion pairs land as two transactions sharing one `FITID` under one byte-identical
   account identity**, and GnuCash de-duplicates on `FITID`. So the collapse **may cost
   transactions, not only tidiness** — *conditionally*, because the de-duplication's scope is
   unmeasured: if GnuCash de-duplicates per account the collapse is what causes the loss and
   decision 1 fixes it, and if it de-duplicates per book the loss survives the fix. **That is open
   question 1 and it is not settled here.** What is unconditional is the coverage-ledger conflation.
   None of this could have been predicted from the link body: it needs `transaction_id` null *and*
   `entry_reference` as the pair key, both first observable in a transactions response.
3. **After the GnuCash gate (2026-08-13, GnuCash 5.16, build 2026-06-27).** Unlike the first two,
   this revision confirms the plan rather than overturning it: `FITID` de-duplication is measured
   **per account**, so decision 1 is a real fix and rejected option J stays rejected. The gate also
   measured two things nobody predicted: the two import shapes **diverge** — a single combined
   import loses nothing, while the per-pocket sequential default **silently drops** the second leg
   of a colliding pair — and foreign-currency statements funnelled into the collapsed account land
   as **zero-amount unbalanced transactions**, destroying values, not merely mis-filing them. Full
   record under open question 1.
4. **At acceptance (2026-08-14).** The owner chose **option M** — if any IBAN is shared within a
   connection, every account of that connection resolves through `identification_hash` — over the
   narrow per-account rule the previous drafts proposed as decision 1. Decision 1's wording was
   replaced wholesale, as the option M entry prescribed; the narrow rule moved to rejected option M
   with both cases preserved. No new measurement was involved: the gate informed the choice, the
   owner made it.

Five bodies of measurement stand behind this file, the first four from 2026-08-12: the catalog,
from a live `GET /aspsps` for `PL` and `LT` (Enable Banking's own catalog, **not** an ASPSP call — it consumes
no bank rate-limit allowance); a local-evidence pass over the real `state/`, `cache/`, `output/` and
`state/fetch-log.jsonl`; a post-link pass over the real `state/revolut.json`, run through the
project's own resolvers (`run._known_acctid`, `run._acctid_from_hash`, `coverage.account_key`,
`cache._uid_digest`, `ofxout.bank_id_for`) with no network call; and the **first `fetch`** — window
2026-05-15 → 2026-08-12, **11 counted requests, all `200`**, `attempt` 0, `api_code` null, zero 429
and zero 400 — followed by a local-evidence pass over its cached responses and an `ofx-conformance`
run of the real Windows `ofxdump` 0.10.5 over all six written files; and fifth, the **GnuCash import
gate** of open question 1, run 2026-08-13 against GnuCash 5.16 (build 2026-06-27) on synthetic
artefacts built by this branch's own writer. Every Revolut claim below comes from one of those five
or is labelled unmeasured.
**Date:** 2026-08-12 (drafted, revised after `link`, revised after `fetch`); gate measured
2026-08-13; accepted 2026-08-14, option M chosen for decision 1.
**Scope:** `run.py` — `_known_acctid` (`run.py:395-404`) and **every one of its seven call sites**,
which is more than earlier drafts of this Scope admitted: the `acctids` map (`run.py:1154`, read by
the fetch path at `run.py:1225`), the per-account `BankFailure` (`run.py:1203`),
`_unfetched_siblings` (`run.py:1331`), the window resolver (`run.py:1434`), the dry-run planner
(`run.py:1580`), `status` (`run.py:1742`) and the coverage summary (`run.py:1860`). Four of the
seven pass `{}` for the fetch-time hashes. See decision 1 for why this is not a one-line edit.
Deliberately not written. Plus `config.example.toml` (one
`[banks.revolut]` entry), the README *Sources* table, [`enable-banking.md`](enable-banking.md) (a
per-institution row and several new quirks), `tests/fixtures/revolut_*.json` and the tests reading
them, and `config.toml` on the owner's machine (not in the repo). **Explicitly not in scope:**
`FITID` **composition** — `_transaction_id`'s fallback order is measured here and deliberately not
changed, see decision 1 — the cache key shapes, the state schema, `cache.py`, `state.py`,
`ofxout.py`'s composition functions, the coverage ledger's *format*, and the `BANKID` or `ACCTID` of
any bank already configured. `sources/enablebanking.py` stays untouched, now on measurement rather
than expectation (decision 7).
**Issue:** [#32](https://github.com/skolima/gnucash-ofx/issues/32), open as
[#36](https://github.com/skolima/gnucash-ofx/issues/36). Inherits the `ACCTID` and `BANKID` rules
from [`decisions.md`](decisions.md#acctid-identity) and
[`decisions.md`](decisions.md#bankid-identity); the `(aspsp, country, psu_type)` grouping from
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) decision 1; the coverage ledger's
`ACCTID` keying from [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md);
and the GnuCash import-gate method — throwaway book, named artefact, a pass being a statement about
one version — from [`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) decision 1. Feeds
[#50](https://github.com/skolima/gnucash-ofx/issues/50) (successor to #5, which closed with the
Wise/Kantor pairing) through decision 9, which defers rather than
decides.

---

## Where this stands

- [x] **1.** If any IBAN is shared within a connection, **every** account of that connection
      resolves its `ACCTID` through `identification_hash` — never the shared IBAN, **never `uid`**;
      an account with no hash keeps its IBAN and warns. *The uniform policy, chosen at acceptance
      2026-08-14; the narrow per-account rule is recorded under rejected option M* —
      [#49](https://github.com/skolima/gnucash-ofx/issues/49)
- [x] **2.** No Revolut file is imported into the real GnuCash book until decision 1 has landed —
      the full sequence completed 2026-08-14: decision 1 landed in
      [#49](https://github.com/skolima/gnucash-ofx/issues/49), the conflated coverage ledger and
      the six stale OFX files were deleted, then re-fetch → first import, in that order, on the
      owner's machine
- [x] **3.** `bankid = "REVOLT21"`, decided now rather than deferred — `bic_fi` is measured absent
      and is never coming ([#49](https://github.com/skolima/gnucash-ofx/issues/49))
- [x] **4.** Register Revolut as `country = "PL"`; the LT-majority IBAN prefixes do not change it
      ([#49](https://github.com/skolima/gnucash-ofx/issues/49))
- [x] **5.** Exactly one of PL / LT is registered at a time — never both
      ([#49](https://github.com/skolima/gnucash-ofx/issues/49))
- [x] **6.** Personal only. `revolut_business` is named as the follow-on shape and not added
      ([#49](https://github.com/skolima/gnucash-ofx/issues/49))
- [x] **7.** `sources/enablebanking.py` is unchanged — **both** risks named in earlier drafts are now
      retired by measurement ([#49](https://github.com/skolima/gnucash-ofx/issues/49))
- [x] **8.** Both fixtures are authored from the real redacted responses, preserving five named
      structural properties ([#49](https://github.com/skolima/gnucash-ofx/issues/49))
- [x] **9.** Conversion pairing is deferred to [#50](https://github.com/skolima/gnucash-ofx/issues/50)
      (successor to closed #5)
      with a measured join key; the remittance-text rule is killed here so it is not re-proposed —
      defers rather than decides; #50 is where this ticks, not #49. *Ticked 2026-08-19: #50
      shipped in [#53](https://github.com/skolima/gnucash-ofx/issues/53) via
      [adr-revolut-exchange-pairing.md](adr-revolut-exchange-pairing.md) decision 1 —
      `revolut:<entry_reference>`, the measured key this deferral required*
- **10.** Untouched — not a decision to ship, nothing to tick. `FITID` composition, the cache key,
      the state schema, the coverage format, and every other bank's identity fields

Decisions 1 and 2 are the file's centre. Both of decision 1's consequences are measured: the
coverage-ledger conflation is a fact on disk, unconditional and independent of any importer, and the
transaction loss was observed directly by the GnuCash gate (open question 1, 2026-08-13:
de-duplication is per account, and the default per-pocket path silently drops a colliding leg). The
one choice measurement could not make — the uniform hash policy versus the narrow per-account
rule — **was made at acceptance, 2026-08-14: the owner chose the uniform policy**, and decision 1
below now states it. The narrow rule this file originally proposed is preserved under rejected
option M. Implementation shipped in
[#49](https://github.com/skolima/gnucash-ofx/issues/49) (merged 2026-08-14), and decision 2's
tail — re-fetch, then first import — completed on the owner's machine the same day. One thing #49 shipped that this file's
Scope did not cover: the `tools/evidence` resolvers also carried the old per-account rule, a
scope gap the implementation inherited and the invariant-guard review caught — they now resolve
through `_stored_acctids` like every `run.py` site, or the census would mis-measure the fix with
the fix's own instrument.

---

## Context

### What the link produced, measured 2026-08-12

`link revolut` completed. `state/revolut.json`, schema version 2, `psu_type` personal, consent
`valid_until` 2027-02-08 — `days_until_expiry` 180, the full catalog maximum. `access.balances` and
`access.transactions` true, `access.accounts` null. The verbatim body is persisted (`session_raw`,
`state.py:138`). `accounts_data` is absent — the hashes arrived on the account objects and
`linked_accounts` handled that through its either-location logic.

**Five account objects, one per currency: AUD, EUR, GBP, PLN, USD.** Field fill out of 5:

| Field | Fill | Against the 19-account baseline |
|---|---|---|
| `iban` (via `account_identifier()`) | 5 / 5 | matches (19/19) |
| `identification_hash` | 5 / 5 | baseline 11/19 |
| `identification_hashes` (plural) | 5 / 5, lengths 6, 6, 8, 4, 4 | not persisted by `LinkedAccount` — see decision 1 |
| `name`, `usage`, `cash_account_type`, `currency` | 5 / 5 each | baseline 3/19, 3/19, 11/19, 3/19 |
| `all_account_ids` | 5 / 5, lengths 2, 2, 2, 1, 1 | baseline `{1: 8, 2: 3}` |
| `bic` (`account_servicer.bic_fi`) | **0 / 5** | matches (0/19) |
| `product`, `details`, `psu_status`, `credit_limit`, `legal_age`, `postal_address` | 0 / 5 — keys present, all null | — |

**This is the richest fill measured in this deployment**, at 100% on every field where the baseline
was partial. Two consequences, recorded under *Settled by measurement* so nobody re-derives them:
`currency_or_none` returns a real ISO code 5/5, so the `"XXX"` placeholder case and the
`"no currency, skipping"` path (`run.py:1226-1228`) are both **retired for Revolut**, and
`/balances` is never needed to discover a currency here.

**The persisted body contains no `status` or `authorized` key**, so `status` cannot report an
authorization state for Revolut from local state alone; consent expiry and coverage it can. A line
for the implementing PR, not a decision.

### The IBAN evidence

`account_id.iban` present 5/5; `account_identifier()` returns a value 5/5 and None for none. Country
prefixes: **LT on 4, PL on 1**; bank codes LT/32500 on the four, PL/2910 on the one.
`account_id.other` on exactly one account (GBP, `scheme_name: "BBAN"`). Across all five accounts'
`all_account_ids` there are **3 distinct identifier values in total** (schemes: IBAN ×7, BBAN ×1;
one account lists the same value twice under IBAN), and:

- **4 of the 5 accounts carry one single identical IBAN** — the LT one.
- **That same LT IBAN appears in all 5 accounts'** `all_account_ids`.
- The PLN pocket carries both the shared LT IBAN *and* its own PL IBAN; the GBP pocket additionally
  carries a BBAN.

So: **one master IBAN with currency pockets hanging off it, plus one local PL collection IBAN** —
structurally *not* Wise's five separately-serviced balances, where no identifier is shared and
`account_servicer` is null because there genuinely is no single servicer. The Wise-derived reason
for withholding `bankid` — "no single BIC describes this connection" — is **not supported here**,
and where earlier drafts leaned on that analogy it is corrected in decision 3 and rejected option C
rather than quietly dropped.

### The collision, measured with the project's own functions

| Measure | Result |
|---|---|
| `run._known_acctid` over the 5 stored accounts | **2 distinct `ACCTID`s** — four pockets collapse to one |
| Accounts resolving to a bare `uid` | 0 |
| `coverage.account_key` over the 5 resolved `ACCTID`s | **2 distinct coverage keys** |
| `cache._uid_digest` over the 5 uids | 5 distinct — the month/balance cache is unaffected |
| `bank_id_for("revolut", None)` | `revolut`, 7 chars, untruncated, colliding with no configured `BANKID` |
| Filename prediction | predictable 5, unpredictable 0, `disambiguator_kind` `{}`, 5 distinct filenames |

`_known_acctid` (`run.py:401-404`) reads
`(stored.iban) or (_acctid_from_hash(stored_hash) if stored_hash else uid)` — the IBAN wins whenever
present, and it is present 5/5. `build_statement` (`ofxout.py:291-295`) sets `account_id` verbatim
and `bank_id=bank_id_for(...)`. **Four of the five statements carry an identical `BANKID`+`ACCTID`
pair, differing only in `CURDEF`** — confirmed after the fetch by `ofxdump` over the real files, not
merely predicted. GnuCash derives `online_id` from `BANKID`+`ACCTID`.

**The collision is not in the filenames — it is inside the files.** Five distinct names, no
disambiguator, because the filename is built from bank key + currency.

**What is downstream of the collapsed `ACCTID`**, each verified in code and now, post-fetch, on
disk:

- **The coverage ledger conflates the four shared-IBAN pockets.** `with_span(coverage, acct_id, …)`
  runs per account (`run.py:1281`) and `coverage.account_key` digests the `ACCTID`
  (`coverage.py:64-72`). **`state/coverage/revolut.json` now holds 2 account entries for 5 pockets**,
  each covering 2026-05-15 → 2026-08-12: three pockets are recorded as covered by a fetch of a
  different pocket. This is no longer a prediction.
- **`_unfetched_siblings` is `ACCTID`-keyed** (`run.py:1324`), so a fetched pocket marks its
  shared-IBAN siblings done for sibling purposes.
- **`_disambiguators` is keyed on `ACCTID`** (`run.py:1337-1357`) and physically cannot hold two
  entries for two accounts sharing one. Inert today because all five currencies differ.
- **Nothing in the tool notices any of this.** `_warn_stale_identity` (`run.py:872-895`) fires only
  when `acctid == uid`, which is 0 of 5. There is no uniqueness check anywhere.
- **The cache is the one mechanism that is safe** — it keys on `uid` (`cache.py:118-128`), 5 distinct
  digests, so months and balances never mix between pockets.

### The `FITID` finding: the collapse destroys transactions, it does not merely mis-file them

This is the strongest argument in the file and nothing before the first fetch could have produced
it.

`_transaction_id` (`sources/enablebanking.py:329-334`) prefers `transaction_id`, then
`entry_reference`, then a hash of stable fields. **Revolut returns `transaction_id` null on every
transaction**, so every `FITID` is the `entry_reference`. And `entry_reference` is **exactly the
field both legs of a currency conversion share, byte-identical** (a 36-character lowercase UUID,
populated on every transaction).

So **the two legs of a conversion carry the same `FITID`.** Per pocket that is harmless — OFX scopes
`FITID` uniqueness to the account, and within each of the five pockets the ids are distinct. But
`_known_acctid` collapses five pockets into two account identities, and **conversion pairs exist
whose two legs both sit inside the collapsed group**. Confirmed independently by `ofxdump` over the real
combined file: **every conversion pair's legs carry one byte-identical `FITID`, and the pairs whose
two pockets both sit in the collapsed group are reported by libofx under a single account
identity** — so within that one identity, the id is not unique.

GnuCash de-duplicates on `FITID`, and the gate observed the consequence directly: imported
per-pocket in the tool's own order, the second leg of the colliding pair was **silently discarded as
already imported** — one side of a currency exchange present, the other simply absent, no row in
the matcher, no error anywhere.

**The scope of that de-duplication was this ADR's load-bearing assumption for three revisions, and
is now measured: per account.** Everything above says "within an account" because that is what the
OFX spec scopes `FITID` uniqueness to — but the spec is not the importer, and this project had never
measured GnuCash's scope; its own invariant is silent on it (`AGENTS.md:134-135` claims only
stability across runs, nothing about per-account versus per-book). Open question 1's control pair —
one shared `FITID` under two **distinct** `ACCTID`s — settled it on 2026-08-13, GnuCash 5.16: the
control leg survived both the sequential per-pocket import and a re-import of the combined file,
while the colliding pair's second leg was silently dropped in the sequential path.

So the sentence this ADR most wanted to write — *under decision 1's rule the duplication disappears,
because with five distinct `ACCTID`s the ids are distinct within every account* — **is a measurement
now, not an inference from the spec**. The per-book branch, under which decision 1 would not have
fixed the loss and rejected option J would have reopened, is dead. Full record, including the
import-shape divergence the gate also found, under open question 1.

The reason the gate ran before the irreversible step rather than after stands as method: the whole
argument of this ADR is that its first two drafts went wrong by reasoning from a plausible premise
instead of measuring it. Shipping an irreversible `ACCTID` change on an untested assumption about
de-duplication scope would have repeated that pattern exactly one level down.

Subject to that, this is **support for decision 1, not a new problem for it**, and it is why
`_transaction_id`'s fallback order is explicitly *not* being changed (decision 10): on the
per-account reading the id is fine and only the account identity was wrong.

**Ranking the two arguments honestly:** the coverage-ledger conflation **mis-records a window**; the
`FITID` collision **destroys transactions**. The second outranks the first. Both are now
unconditional: the first was already true on disk, and the second was observed directly — the gate's
variant B imported the colliding statements sequentially and the second leg never appeared, no row,
no flag, no error.

### What `ofxdump` says: the libofx half passes cleanly

Real `ofxdump` 0.10.5 (the GnuCash-bundled Windows build,
[`testing.md`](testing.md)) over all six written files — five per-pocket plus the `--combine` one:

| Measure | Result |
|---|---|
| Exit status | **0 on all six**, zero errors, zero OpenSP parse errors |
| The combined file's structure | 5 `STMTTRNRS`, 5 `BANKACCTFROM`, four carrying an identical `BANKID`+`ACCTID`. **No deduplication** |
| What libofx hands the caller | **5 distinct accounts, 5 distinct statements**, correct per-statement `CURDEF`, correct per-statement attribution of every transaction — partitioning exactly as nested, no cross-attribution |
| Only stderr output | the known benign `ofxdate_to_time_t()` "unable to parse time part" warning, one per date-only field |

**So the parse-level objection does not exist: libofx merges nothing and damages nothing.** This
settles what was open question 2, and it confirms the code reading of `combine_statements` /
`_CombinedOfxWriter` (`ofxout.py:381-425`), whose documented invariant is that packaging several
statements into one file never merges them.

Three further `ofxdump` facts worth carrying:

- **`CURDEF` is the only field separating the four colliding statements** at the libofx API level.
  libofx does surface it (on both `OfxStatementData` and `OfxAccountData`), but `Account ID`,
  `Account name`, `Account #`, `Bank ID`, `Account type` (`CHECKING`) and the statement dates are
  **byte-identical across the four**. Whether GnuCash's importer consults currency when matching is
  open question 1.
- **libofx truncates `ACCTID` to 23 characters** in `OfxAccountData.account_number` while keeping the
  full value in the composite `Account ID` — so a 28-character Polish IBAN loses 5 characters in one
  field and the two libofx fields disagree about the account number. `ofxtools` types `ACCTID`
  leniently and never surfaces this. A measured consequence of the A-22 wart recorded in
  [`decisions.md`](decisions.md#acctid-identity), and it bears directly on rejected option I.
- **Splitting the output per pocket does not avoid the collision.** Each per-pocket file presents the
  identical `Bank ID` + `Account #` to libofx, so importing the four separately collides in one
  *book* exactly as they collide in one *file*. See rejected option K.

### What the first fetch cost, and what it says about limits

11 counted requests (1 `GET /sessions/{id}` + 5 `/balances` + 5 `/transactions`), **all 200**,
`attempt` 0, `api_code` null, **zero 429 and zero 400**. The rate-limit domain is recorded as
`Revolut|PL|personal` — a sixth domain in this deployment, consistent with decision 5. The
**2026-05-15 → 2026-08-12 window was accepted**, so Revolut is not the Alior/Erste refusal shape at
that depth. For
calibration: this deployment sent 64 requests on 2026-08-09 and 172 on 2026-08-10, with zero 429s
across all logged requests.

---

## Decisions

### 1. If any IBAN is shared within a connection, every account of that connection resolves through `identification_hash` — the uniform policy, chosen at acceptance

**Commits to** the rule argued at full strength as option M and **chosen by the owner at
acceptance, 2026-08-14**, over the narrow per-account alternative earlier drafts proposed here. In
its condensable form: *an `ACCTID` identifies an account, so a value shared by several accounts of
one connection is not an `ACCTID` for any of them — and a connection where that happens abandons
IBANs wholesale: every account of it resolves through `identification_hash`, including any whose
own IBAN is unique.* The connection is the unit, not the account, and no account's identity is
selected by reference to which siblings exist — which is the property the choice was made for.

**Why this side won**, condensed from the two cases preserved under rejected option M: no account's
`ACCTID` can change because something happened to a *different* account — under the narrow rule the
PLN pocket kept its IBAN only while no sibling shared it, an ASPSP-side property that could flip on
any re-link, silently, with no uniqueness warning anywhere in the tool to catch it. It sits square
with `AGENTS.md:136-140`'s stability requirement, where the narrow rule's identifier was stable
only while the sibling set was. One scheme per connection is easier to reason about, in
`state/revolut.json` and in the import dialog. And the price — one more orphaned identity than the
narrow rule, the PLN pocket — is exactly **zero today**, because decision 2 holds and nothing has
been imported. Blast radius was the narrow rule's whole advantage, and it is an advantage that only
exists once a first import does; choosing before the first import is what made the uniform policy
affordable.

**It is not a one-line edit, and earlier drafts implied it was.** `_known_acctid`
(`run.py:395-404`) takes one account, its uid and the fetch-time hash map; it has **no visibility
into the other accounts of the connection**, so "any IBAN is shared within the connection" cannot be
a condition inside it as it stands. The determination has to be computed **once per connection** and
threaded in — a new parameter, or resolution moved up to the callers. The uniform policy is if
anything easier to thread than the narrow rule, because the outcome is a single per-connection fact
("shared-IBAN regime or not") rather than a per-account verdict — but the threading is the same
work. Either way it must reach **all seven
call sites** (`run.py:1154`, `1203`, `1331`, `1434`, `1580`, `1742`, `1860`), not just the `acctids`
map: implementing it at the map alone would leave `status`, the dry-run planner, the window
resolver, the coverage summary and the sibling logic still resolving to collapsed `ACCTID`s — a
silent divergence between what `fetch` writes and what `status` reports **about the same accounts**,
which is a worse failure than the one being fixed, because it is invisible from either side. Four of
the seven pass `{}` for the hashes, so any threading has to work when the fetch-time map is empty.

**The in-code assumption this connection breaks** is explicit, not accidental. `bank_id_for`'s
docstring (`ofxout.py:248-267`) states it outright: *"Uniqueness is carried by `ACCTID` (the IBAN),
so two banks sharing a `BANKID` — e.g. two connections to the same institution — still yield
distinct accounts."* The whole `BANKID` design leans on `ACCTID` being unique; Revolut is the first
connection measured where it is not.

For this connection: **all five pockets** resolve through `_acctid_from_hash(identification_hash)` —
measured **5 distinct hashes, one per pocket** — giving 5 distinct `ACCTID`s. The PLN pocket's own
PL IBAN, unique today, is deliberately **not** used: under the narrow rule its identity would have
depended on no sibling ever sharing it, an ASPSP-side property this project does not control.
`_acctid_from_hash` (`run.py:373-381`) yields `eb-<16 hex>`, 19 characters: inside OFX's A-22 limit,
and inside the **23-character boundary libofx was measured truncating at**, unlike the 28-character
Polish IBAN — and now uniformly so across the whole connection, so libofx's two account-number
fields agree on every Revolut statement.

**Two consequences it addresses**, in order of severity — both now measured; earlier drafts had to
flag that they differed in how well established they were:

1. **Transaction loss — the fix is now measured, not assumed (gate, 2026-08-13).** Conversion pairs
   currently present two transactions sharing one `FITID` under one account identity, and GnuCash
   de-duplicates on `FITID` **per account**: the gate's sequential import silently dropped the
   second colliding leg while the control pair under distinct `ACCTID`s survived every path. This
   rule makes the ids distinct within every account, which removes the collision the loss depends
   on.
2. **A lying coverage ledger — measured, on disk, unconditional.**
   `state/coverage/revolut.json` holds 2 entries for 5 pockets today; three pockets are recorded
   covered by a fetch of a different pocket, silently, in the mechanism built specifically to know
   what has *not* been fetched. No importer behaviour is involved.

**No longer gated at all.** Earlier drafts gated this decision on the GnuCash observation; the
intermediate draft un-gated it on the strength of consequence 2 alone. The observation has now been
made (open question 1, 2026-08-13): de-duplication is per account, so this rule solves consequence 1
as well as consequence 2. Both consequences are measured; nothing about this decision rests on an
assumption any more.

**Rules out:**

- **Deciding it after the first import.** `ACCTID` is half of `online_id`. Decision 2.
- **Keeping the shared IBAN** — rejected option H.
- **Appending the currency to the `ACCTID`** — rejected option I.
- **Changing `FITID` composition to break the conversion-leg tie** — rejected option J, now
  unconditionally: the gate measured per-account de-duplication, so the `FITID` is correct and only
  the account identity was wrong.
- **The narrow per-account rule** — reject only the IBANs that actually collide and let an account
  whose IBAN is unique (here, PLN) keep it. This was the rule this file proposed until acceptance;
  the owner chose the uniform policy instead. Recorded under rejected option M — the letter that
  originally argued *for* this uniform policy; the two swapped places at acceptance and the entry
  preserves both cases.
- **Any change to a connection in which no IBAN is shared.** The trigger condition is false for all
  19 existing accounts, so every configured bank resolves exactly as it does today. This is a new
  branch, not a reordering.

**On `identification_hashes` (plural), and whether its absence matters: it does not.**
`LinkedAccount` (`state.py:42-67`) persists the singular hash, not the plural list, which survives
only inside `session_raw`. That would matter if the plural list were what distinguishes the pockets;
it is not. The **singular** hash is already 5 distinct values, is the field Enable Banking documents
as stable "for matching accounts between multiple sessions", and is already what `_known_acctid`
falls through to. No state-schema change, no re-link. Recorded because "the richer field is not
persisted" is exactly what a later reader would assume was blocking the fix.

**Two caveats carried, not buried.**

- **The hash's stability is now observed once, not merely documented — and the tripwire stays.**
  At acceptance it was documented as stable across sessions and *assumed* stable, never seen
  across a Revolut re-link. The re-link of **2026-08-14** made the first observation (compared
  locally as SHA-256 digests via `state.load_session` + `run._stored_acctids`, before and after):
  **5 of 5 hashes unchanged, 0 `ACCTID`s moved, 0 of 5 uids survived** — the design bet confirmed,
  for one re-link cycle on one ASPSP. Under this rule **all five pockets ride on the hash, not
  four**: if it ever moves, the whole connection orphans once per consent cycle — the failure
  `_warn_stale_identity` exists to catch for bare UIDs. So repeat the comparison at each future
  re-link rather than retiring it on one data point; compare **locally** and publish only the
  outcome as counts — the digests and identifiers are account data and never reach this public
  repo.
- **The UID-fallback hazard, which nothing in the first drafts recorded.** `_known_acctid` falls
  through `iban` → `identification_hash` → **`uid`**, and a session stored under an older schema may
  carry no `identification_hash` at all (measured: 11 of 19 accounts in this deployment have one, so
  8 do not). A uniform policy applied to a shared-IBAN connection containing a hashless account must
  not push that account onto a **UID-derived `ACCTID`** — Enable Banking regenerates UIDs on every
  re-link, so it would orphan itself once per consent cycle. That is precisely the instability
  `_warn_stale_identity` exists to detect and `AGENTS.md:136-140` forbids in as many words: *"Never
  let either depend on Enable Banking's account `uid` alone."*

  **So the rule carries an explicit floor, and it is part of the rule, not a caveat on it: an
  account with no `identification_hash` keeps its IBAN — shared or not — and warns; it never falls
  through to `uid`.** A shared-but-stable identifier is strictly better than a unique-but-regenerating
  one, because the first mis-files accounts recoverably and the second orphans them every cycle.
  Revolut itself is not exposed (5 of 5 pockets carry a hash), but any implementation of this
  decision must handle it, and a test should pin it.

### 2. No Revolut file is imported into the real GnuCash book until decision 1 has landed

**Commits to:** GnuCash gate (open question 1, **run and passed 2026-08-13**) → decision 1 lands →
re-`fetch` → import. Fetching before decision 1 was correct and has already paid for itself: it
settled five open questions for 11 requests. But the files it wrote carry the collapsed `ACCTID`,
and importing them **loses transactions to `FITID` de-duplication — measured, no longer a risk**:
the gate's sequential import of that exact shape silently dropped a colliding leg. They must be
regenerated, not imported.

**What the re-fetch actually costs — earlier drafts said "free" and that was wrong.** The cache
keying is unaffected by an `ACCTID` change (it keys on `uid`, measured: 5 distinct digests), but key
stability is necessary, not sufficient: `chunk_ttl` (`cache.py:131-140`) returns `None` — never
expires — **only for a settled month**, and keeps `DEFAULT_TTL` (6 hours, `cache.py:41`) for any
month whose end falls within `LATE_BOOKING_MARGIN` (14 days, `cache.py:53`) of today. For the
fetched window, evaluated against the fetch date:

| Cached month | `chunk_ttl` on 2026-08-12 | Becomes settled |
|---|---|---|
| 2026-05, 2026-06 | `None` — never expires | already |
| **2026-07** | **6 hours** | 2026-08-15 |
| **2026-08** | **6 hours** | 2026-09-15 |

So **two months, not one, are on a six-hour clock** — July's month end (07-31) is inside the 14-day
margin from 2026-08-12, which is easy to miss. The honest claim: **the settled months are free
forever; the recent tail costs one `/transactions` request per pocket once its six hours lapse**,
plus the session call, plus `/balances` while the window is open. An implementation landing more
than six hours after the fetch — which is to say, any realistic one — pays that, and the bill grows
with the number of pockets, not with the number of files being regenerated. It is small and it is
not zero, and a reader planning the work should know which of the two it is.

**Rules out** treating the first import as reversible. Recovery is manual re-mapping or re-importing
account by account, and a leg dropped as a `FITID` duplicate is not visible as an error at all.

**Delete `state/coverage/revolut.json` when the rule changes.** It currently holds the conflated
2-for-5 entries. It is a local file and deleting it costs nothing by itself, but **rebuilding it is
not free**: coverage is recorded from the requested window at fetch time, so the rebuild happens on
the next fetch and carries whatever that fetch costs under the table above — nothing for the settled
months, one request per pocket for each recent-tail month past its TTL.

**Status 2026-08-14:** decision 1 landed
([#49](https://github.com/skolima/gnucash-ofx/issues/49)); the conflated coverage ledger and the
six stale OFX files were deleted on the owner's machine the same day. The re-fetch and the first
import remain the owner's next steps — this decision stays unticked until they happen.

### 3. `bankid = "REVOLT21"`, decided now rather than deferred

**Commits to** setting it in `config.toml` before the first import, alongside decision 1.

**Rules out** waiting for `account_servicer.bic_fi` — the plan the first draft proposed, killed by
measurement: `account_servicer` is null on **all 5** accounts, the whole object. `POST /sessions` is
the only place the full `AccountResource` appears (`GET /accounts/{uid}` is 404 in Restricted Mode,
`GET /sessions/{id}` carries hashes only), so there is nowhere else to look. A plan that waits for it
waits forever.

**Reasoning — three facts and one labelled inference:**

- The catalog publishes `bic: "REVOLT21"` under both PL and LT (measured 2026-08-12), **8 characters,
  already the primary-office form**, so `bank_id_for` (`ofxout.py:248-267`) neither truncates nor
  alters it.
- Nothing in the link body contradicts it; `bic_fi` is absent 5/5, so `linked_accounts` stores
  `bic=None` 5/5. The catalog value is neither confirmed nor refuted.
- The connection carries **one master IBAN present in all five accounts** — positive structural
  evidence of a single servicer, which Wise never had.
- **Inference, not measurement:** that the PL-coded pocket (2910) shares a legal entity with the four
  LT-coded ones (32500). Open question 3, unclosable locally.

**Why decide rather than leave it unset.** Leaving `bankid` unset is not deferral — `bank_id_for`
falls back to the config key, so `[banks.revolut]` yields a permanent `BANKID` of `revolut` with the
*identical* orphan-on-change cost. Both are stable strings that must be right before the first
import; one names the institution and is what a second Revolut connection would have to agree with.

**This does not change the standing rule** that `BANKID` is configured explicitly per bank and never
auto-filled ([`decisions.md`](decisions.md#bankid-identity)). A human typing a reasoned value is not
code copying a field; only the second drifts. Rejected option B.

### 4. Register Revolut as `country = "PL"`; the LT-majority IBAN prefixes do not change it

**Commits to** one `[banks.revolut]` entry: `aspsp = "Revolut"`, `country = "PL"`,
`source = "enablebanking"`, no `psu_type` (default `"personal"`). Now confirmed end to end — the
link surfaced five pockets and the fetch returned 200 on all 11 requests under `Revolut|PL|personal`.

**Rules out** re-registering under LT on the IBAN evidence. `country` is required at link time
(`run.py:611-617`), must match the catalog exactly, and is one component of
`BankConfig.rate_limit_domain`.

**The counter-evidence, weighed and rejected.** 4 of 5 IBANs carry the LT prefix and bank code
32500 — the best evidence LT has ever had, and exactly what "Revolut Bank UAB is Lithuanian"
predicts. It loses: the account is held under Poland, the PL registration surfaced all five pockets
including the LT-coded ones, and an IBAN's country prefix describes where the account was issued,
not which catalog entry a consent belongs under. Registering PL and receiving LT IBANs is a measured
fact about this institution and belongs in [`enable-banking.md`](enable-banking.md) precisely
because the next reader will expect otherwise.

### 5. Exactly one of PL and LT is registered at a time — never both

**Commits to** a single entry. **Rules out** an LT sibling.

Two connections to one institution differing in `country` would be two distinct
`rate_limit_domain` values over one allowance — the `alior` / `alior_kantor` configuration whose
sharing behaviour two deliberate probes on 2026-08-10 (56 and 110 requests, both **inconclusive**,
both in [`probes.md`](probes.md)) still has not settled. With PL working there is no reason left to
want the LT entry. If PL is ever abandoned for LT, do not re-add PL.

### 6. Personal only. `revolut_business` is the named follow-on shape, not added now

**Commits to** one key, `psu_type` at its default `"personal"` (confirmed in the persisted session).
**Rules out** a speculative business entry in `config.example.toml`. The catalog carries both
`psu_types`, and per `config.example.toml` lines 33-34 and the `alior` / `alior_business` precedent
a business connection needs its own link run and its own key.

```toml
[banks.revolut_business]
source = "enablebanking"
aspsp = "Revolut"
country = "PL"
psu_type = "business"
```

It inherits **nothing** from decisions 1 and 3: a different login is a different set of accounts,
whose IBAN-sharing structure is its own measurement and whose first import is gated on its own.

**No new config knob is needed.** A `[banks.<key>]` entry has exactly `source` (required —
`config.py:97-99`), `aspsp` and `country` (required at link time, `run.py:611-617`), `psu_type`
(optional, default `"personal"`), and `bankid` (optional, consumed at `run.py:1464` and
`run.py:1605`).

### 7. `sources/enablebanking.py` is unchanged — both named risks are now retired by measurement

Earlier drafts named two ways the bank-agnostic mapper could break for Revolut. **Both are now
measured closed**, so this decision rests on evidence rather than expectation:

- **Currency mismatch — unreachable.** `transaction_amount.currency` equals the pocket currency on
  **100%** of transactions, and **`instructed_amount` is absent entirely** (not a null, not a key),
  so a foreign-currency amount cannot hide there either. The `ValueError` at
  `sources/enablebanking.py:353-357` is unreachable on this data. Separately,
  `currency_or_none` returns a real ISO code 5/5, so the `"XXX"` path and `"no currency, skipping"`
  are both unreachable too.
- **Machine references — none exist in the remittance.** Zero `remittance_information` elements match
  `<PREFIX>-<something>`; a token census found no digit runs, no hex runs, no long alnum tokens —
  words plus one ALLCAPS ISO code per conversion leg. `_split_reference` set `Txn.reference` on
  none, `compose_check_number` emitted nothing, and the written files contain **zero `CHECKNUM` and
  zero `REFNUM`**, confirmed by `ofxdump`.

**Two caveats to carry rather than bury.** Revolut *does* have a machine reference —
`entry_reference` — but it arrives as a **field, not a remittance element**, so `_OWN_REFERENCES`
was never the mechanism at risk. And at 36 characters it exceeds `_MAX_CHECKNUM_LEN` (12), so
lifting it into `CHECKNUM` would **drop** it — `compose_check_number` drops an over-long reference
rather than truncating it (`ofxout.py:234-243`), exactly as Wise's cashback UUID is dropped.

**The mapper survived intact:** `map_transactions` raised nothing, zero empty `NAME`, `NAME` max 75
against the 96 cap, `MEMO` max 85 against 390, no truncation anywhere.

### 8. Both fixtures are authored from the real redacted responses, preserving five named structural properties

A real transactions response now exists, so the earlier gate on the transactions half is lifted.
`tests/fixtures/revolut_session.json` and `tests/fixtures/revolut_transactions.json` are written in
this work, sanitized per `AGENTS.md`'s Data section — synthetic IBANs, hashes, UUIDs and
counterparty names throughout — and **must preserve**:

1. **The shared-IBAN structure:** five accounts, five currencies, one master identifier shared by
   four of them plus one unique local one, `account_servicer` null on all five, one account carrying
   a BBAN under `other`. This is what makes decision 1 testable offline.
2. **The shared-`FITID` conversion pair:** at least one conversion whose two legs sit in two
   different pockets with `transaction_id` null and one byte-identical `entry_reference`, and at
   least one such pair whose pockets fall inside the collapsed group. **This is what makes the
   data-loss regression testable offline**, and it is the property a fixture built from the shape
   alone would have missed.
3. **The `bank_transaction_code` vocabulary as observed, without claiming it is closed.** `EXCHANGE`,
   `TOPUP` and `TRANSFER` come from one fetch of one connection over one window; whether Revolut
   also emits `CARD`, `DEPOSIT` or others is unmeasured. The fixture's `_comment` must say so, and
   no test may assert the set is exhaustive.
4. **The absence of machine references** — no remittance element of `<PREFIX>-<id>` shape, so a test
   asserting zero `CHECKNUM`/`REFNUM` for Revolut stays honest.
5. **Synthetic account numbers that take the same code path as the real ones.**
   `normalize_account_number` (`sources/enablebanking.py:200-216`) is checksum-sensitive in exactly
   one place, and it is narrower than it first looks: a **bare 26-digit** value gets a `PL` prefix
   *only if* `PL<digits>` passes the ISO 13616 mod-97 check (`_iban_checksum_ok`,
   `sources/enablebanking.py:183-196`); anything else — including any value that already carries a
   country prefix — is returned merely whitespace-stripped and upper-cased, **with no checksum test
   at all**. So the requirement applies to the fixture's *bare domestic* numbers: a synthetic
   26-digit value that fails the checksum silently stays un-prefixed and exercises a different
   branch than production does. Prefixed synthetic IBANs pass through regardless, but should still
   be checksum-valid so that any external validator, or a future stricter branch, does not diverge
   from what the fixture claims to represent.

**Rules out** a fixture derived from the Wise one, or from the catalog entry: four of the five
properties above are Revolut-specific and two of them were invisible before the fetch.

### 9. Conversion pairing is deferred to #5 — now [#50](https://github.com/skolima/gnucash-ofx/issues/50), its successor — with a measured join key and one alternative killed here

**Commits to** recording the measurement as the pairing issue's input (#5 at acceptance; #50
carries it since #5 closed with the Wise/Kantor work), and to **not** implementing pairing in this
ADR. **Rules out** joining Revolut's conversion legs on remittance text — see rejected option L,
which is killed by a measured false pair, not by argument.

**What was measured**, and it is unusually clean:

- **A conversion is always two transactions in two different pockets** — one `DBIT` in the source,
  one `CRDT` in the target. Never a single transaction, and **never both legs in the same pocket**
  (zero same-pocket pairs). The set contains conversion legs alongside `TOPUP` and `TRANSFER` rows.
- **`entry_reference` is the reliable join:** populated on every transaction, a 36-character
  lowercase UUID, byte-identical on both legs. Clustering on it gives **every cluster exactly size
  2, every cluster a true pair, no unpaired leg**, with non-conversion rows as singletons.
- **`transaction_id` (null on all), `booking_date` and `value_date` cannot join** — each yields one
  cluster containing everything. `booking_date` and `value_date` are equal on 100%, date-only.
- **`exchange_rate` is a key that is null on every transaction.** No rate is stated; it is derivable
  from the pair, but only after joining.
- **`_conversion_key` returns `None` for the entire Revolut set.** Revolut's code is `EXCHANGE`, not
  `CONVERSION`, and there is no `BALANCE-<digits>` element, so neither the Wise nor the Alior Kantor
  branch of `_conversion_key` (`sources/enablebanking.py:80-91`) fires. The existing
  `compose_conversion` / `Conversion` machinery is **inert** here.

**A Revolut rule would be the narrowest yet:** `code == "EXCHANGE"` → deal key = `entry_reference`.
**One caution to record before anyone writes it:** that same field is also the `FITID`, so the deal
key and the transaction id would be the same string. Read-only extraction is fine — that is what
`_conversion_key` already does — but any code assuming "one `entry_reference`, one transaction" is
wrong on this data.

**The practical argument for doing it at all**, measured: `compose_name` produces **fewer distinct
`NAME`s than there are transactions** — several rows share one `NAME`, and others share another — and
**every conversion leg's Description is word-for-word identical to at least one other row's**, of
the form `Exchanged to <CUR>`. Conversions are not distinguishable in the GnuCash register by
Description alone, and since `conversion_key` is `None` for all of them the existing annotation
never fires.

### 10. Untouched

**`FITID` composition specifically** — `_transaction_id`'s `transaction_id` → `entry_reference` →
hash order is measured here and deliberately not changed; the shared id between conversion legs is
correct behaviour and the fix belongs in `ACCTID` (decision 1, rejected option J). Also untouched:
the cache key shapes, the state schema, the coverage ledger's format, the `NAME`/`MEMO`/`CHECKNUM`
composition, the stdout-is-the-file-list contract, and the `BANKID`/`ACCTID` of every bank already
configured — decision 1's condition is false for all 19 existing accounts. Recorded so that if one
of these moves it reads as scope drift rather than onboarding.

---

## Rejected options

### A. Downgrade #32 to a README stretch item

**Rejected by measurement, three times over.** The catalog lists Revolut under PL (1 of 33) and LT
(1 of 26); the PL link returned five accounts; the first fetch returned 200 on all 11 requests and
wrote six `ofxdump`-clean files.

### B. Auto-fill `BANKID` from the catalog `bic`

**Rejected as a mechanism**, unchanged by the new evidence. An auto-filled identity drifts: a bank
with no BIC today that gains one later would silently change `BANKID` and orphan every imported
account. The Wise measurement — the catalog publishes `TRWIBEBB`, wrong for the GB and PL balances
of one connection — remains why the catalog cannot be trusted as a servicer identifier in general.
**What changed is the value, not the mechanism**: decision 3 adopts `REVOLT21` by hand on this
connection's own evidence.

### C. Leave `bankid` unset, giving Revolut the Wise treatment

**Rejected — and the reasoning that made this attractive in the first draft is measured wrong, so it
is corrected here rather than dropped.** The Wise treatment is documented as being for "a connection
whose accounts span several institutions" (`config.example.toml` lines 25-26), and Wise qualifies on
evidence: five balances, no shared identifier, `account_servicer` null because there genuinely is no
single servicer. Revolut's five pockets **share one master IBAN present in all five
`all_account_ids` lists** — the structural opposite. And it would not even be deferral: the fallback
`revolut` is just as permanent and just as expensive to change.

### D. Register under `LT`

**Rejected**, and closer than it was: 4 of 5 IBANs carry the LT prefix and bank code 32500. It loses
to the PL registration having *worked*, end to end. Decision 4.

### E. Register both PL and LT

**Rejected.** Two consents and two `rate_limit_domain` values over one institution — the
configuration two probes on 2026-08-10 left inconclusive — to answer a question the PL link already
answered. Decision 5.

### F. Write the fixtures from the catalog and the existing Wise fixture

**Rejected, and now moot.** Both real responses exist; decision 8 authors both from redacted reality.
The point survives as a warning: four of decision 8's five required properties are Revolut-specific
and two were invisible before the fetch, so a fixture built from the shape alone would have encoded a
guess that passes forever.

### G. Add a Revolut-specific source module

**Rejected.** 19 accounts across four ASPSPs already share one mapper, and **both** Revolut-specific
risks are now measured retired (decision 7). The only Revolut-specific rule anyone might write is a
one-line conversion key, deferred to #5.

### H. Keep the shared IBAN as the `ACCTID` and accept four pockets landing in one GnuCash account

**Rejected, and the case against it is now measured on disk rather than predicted.**

- **It destroys transactions — observed, not predicted.** Conversion pairs present two transactions
  sharing one `FITID` under one account identity (confirmed by `ofxdump` over the real combined
  file); GnuCash de-duplicates on `FITID` within an account, and the gate's sequential import
  **silently dropped the second leg** — no row in the matcher, no flag, no error. This is the
  argument that outranks everything else in this ADR.
- **It destroys values even where it keeps rows.** Foreign-currency statements funnelled into the
  collapsed account arrived as **zero-amount transactions flagged "New, UNBALANCED"** — 6 of the 8
  matcher rows in the gate's collapsed account, their real values stranded in a currency the account
  cannot hold, awaiting a manual transfer assignment. Not a silent conversion; a booked 0.00.
- **It lies in the coverage ledger.** `state/coverage/revolut.json` holds **2 entries for 5
  pockets**, each covering the full fetched window — three pockets recorded as covered by a fetch of
  a different pocket, in the mechanism built to know what has *not* been fetched. Silent, and the
  gap warnings stay quiet.
- **It keeps three mechanisms correct only by coincidence.** `_unfetched_siblings`' `done` set and
  `_disambiguators`' return map are `ACCTID`-keyed and under-count pockets; neither bites today only
  because all five currencies differ.

The libofx-level objection that might have been added here does **not** exist: `ofxdump` parses the
colliding file cleanly and reports five distinct accounts. The damage is downstream of the parser,
which is precisely why "it validates" was never going to settle this.

### I. Append the currency to the `ACCTID` (e.g. `<IBAN>-EUR`)

**Rejected**, and the length argument is now measured rather than theoretical:

- **It invents an identifier.** `ACCTID` resolves through `iban` → `other.identification` →
  `identification_hash` → `uid`, every step something the bank said. `<IBAN>-EUR` is something this
  tool made up, and would be the first such value in any OFX file it writes.
- **It makes a measured truncation worse.** `ofxdump` 0.10.5 truncates `ACCTID` to **23 characters**
  in `OfxAccountData.account_number` while keeping the full value in the composite `Account ID` — so
  a 28-character Polish IBAN already makes libofx's two fields disagree. `<IBAN>-EUR` is 32 and
  widens the gap; `_acctid_from_hash`'s 19-character `eb-<16 hex>` sits under the boundary with room.
- **It binds identity to currency.** If a pocket's currency were ever re-reported differently — and
  [`adr-xxx-currency-placeholder.md`](adr-xxx-currency-placeholder.md) is the standing proof a
  currency field can change under you between links — the account orphans.

### J. Change `FITID` composition so conversion legs stop sharing an id

**Rejected — now unconditionally; the condition was open question 1 and it resolved against
reopening.** Tempting, because the shared id is the proximate cause of the data loss. The argument
against it is that the id is *correct*: OFX
scopes `FITID` uniqueness to the account, within each of the five pockets the ids are already
distinct, and the duplication exists only because five pockets were collapsed into two accounts.
Fixing `ACCTID` would then remove it entirely. Changing `_transaction_id` instead would alter the id
of **every Revolut transaction and potentially every other bank's**, which is a re-import of
everything already imported anywhere — an unbounded reversal cost to work around a bug in a
different field.

That whole argument rested on GnuCash de-duplicating per account, which was unmeasured when this
option was first rejected. **Open question 1's control pair measured it on 2026-08-13: per
account.** The reopening branch — per-book de-duplication, under which this would have become the
only remaining fix at unbounded re-import cost — is dead, and the rejection stands on measurement.

### K. Ship only per-pocket files and never `--combine`, so the collision cannot arise

**Rejected, by measurement.** Each per-pocket file presents libofx the **identical `Bank ID` +
`Account #`**, so importing the four separately collides in one *book* exactly as they collide in
one *file*. The collision lives in the account identity, not in the packaging, and `--combine` is
neither its cause nor its cure.

### L. Join Revolut's conversion legs on the remittance text

**Rejected by a measured false pair, not by argument.** The remittance text is identical on both
legs, which makes it look like a join key — but it names **only the credit (target) pocket's ISO
code, never the debit side** (on every pair). Two conversions into the same currency on the same day
are therefore textually identical, and clustering on the text **produced a false cluster
spanning two distinct conversions in this very set**. Adding `booking_date` does not fix it
(`booking_date` and `value_date` are equal on 100% and date-only). `entry_reference` clusters the
same set into pairs with no false positives and no unpaired leg. Decision 9.

### M. The narrow per-account rule: reject only the IBANs that collide; an account whose IBAN is unique keeps it

**Rejected at acceptance, 2026-08-14 — and the letter swap needs saying first.** Until acceptance
this entry argued the *uniform hash policy* as the live alternative to a decision 1 that proposed
the narrow rule. The owner chose the uniform policy; decision 1 was replaced wholesale, as this
entry prescribed, and the two rules swapped places — the uniform policy is now decision 1 and the
narrow rule is recorded here, under the letter every earlier draft and commit message used for its
opponent. Both cases are preserved at the strength they were argued, because the choice was close
and a future reader deserves to see what lost and why.

**The narrow rule, as it stood:** the resolution order already in the code — `iban` →
`identification_hash` → `uid` — with one condition added to the first step: an IBAN is used only if
no other account in the same connection resolves to it. For this connection: the PLN pocket keeps
its own PL IBAN, and only the four sharing the LT master fall through to the hash. It carried the
same floor decision 1 now carries: a rejected IBAN never falls through to `uid`; with no hash
available, keep the shared IBAN and warn.

**The case for the narrow rule — what lost:**

- **Blast radius.** It changes the `ACCTID` only of accounts that are actually ambiguous — four
  pockets instead of five. Since every `ACCTID` change is an orphaning, the narrow rule orphans
  strictly fewer accounts, and only where there is a measured defect.
- **The invariant argument cuts both ways.** `AGENTS.md:136-140` fixes the resolution order
  `iban` → `other.identification` → `identification_hash` → `uid`, with the IBAN *preferred*. The
  uniform policy demotes the IBAN for accounts whose IBAN is unique and correct — its own departure
  from the documented order, not obviously smaller than making the first step conditional.
- **The hazard was answered, not merely survived.** With the `uid` floor written into the rule, the
  narrow rule's UID-fallback path was closed by construction, so the uniform policy's advantage
  there was real but not decisive.
- **The fragility the uniform policy buys out is unobserved.** No ASPSP in this deployment has been
  seen changing which identifier it reports as an account's primary `iban`. The narrow rule's risk
  was hypothetical; the uniform policy's extra orphaning was immediate and certain.

**Why it lost anyway:**

- **The sibling dependency is real even if unobserved, and it fires silently.** Under the narrow
  rule the PLN pocket's identity depended on no sibling ever sharing its IBAN — an ASPSP-side
  property, changeable on any re-link, with no uniqueness warning anywhere in the tool to catch it.
  It was the one reversal in this ADR that would have fired without anybody choosing it.
- **The extra orphaning was free at the moment of choice.** Decision 2 holds: nothing has been
  imported, so re-identifying five pockets costs exactly what re-identifying four does — nothing.
  Blast radius only becomes an argument after a first import exists, and the point of deciding now
  was to be inside the window where it is not one.
- **Stability beats minimalism in this codebase's own terms.** `AGENTS.md:136-140` requires a
  *stable* `ACCTID`; an identifier whose *selection* depends on the current sibling set is stable
  only as long as the sibling set is. One scheme per connection is also simpler to reason about, in
  `state/revolut.json` and in the import dialog.

**Cost of reversing into it later:** the two rules disagree about exactly one account — the PLN
pocket. Adopting the narrow rule after an import would flip PLN from hash back to IBAN and orphan
it; everything else is identical under both rules. The reversal is bounded to one account, in
either direction.

---

## Cost to reverse

**`ACCTID` — high, and the highest thing here.** Half of `online_id`; changing it after an import
orphans every affected account, recoverable only by manual re-mapping or re-import. This applies to
*every* option in decision 1's space **including doing nothing** — keeping the shared IBAN is a
choice with the same reversal cost as changing it. That symmetry is the whole argument for decision
2: while nothing has been imported, all options cost zero to switch between; after the first import,
all of them cost the same manual repair — plus, for the affected conversion legs, a silent loss that no
error surfaces.

**The sibling dependency — removed by the acceptance choice.** The narrow rule would have made the
PLN pocket's `ACCTID` depend on which *other* accounts the connection reports — a reversal trigger
sitting on an ASPSP-side change, firing on any re-link, silently, the one reversal in this ADR that
would have fired without anybody choosing it. The uniform policy chosen at acceptance (decision 1)
dissolves it: no account's identifier is selected by reference to its siblings. What replaces it is
narrower and named in decision 1's caveats: **hash stability is now load-bearing for all five
pockets** — observed once at the 2026-08-14 re-link (5 of 5 hashes unchanged, 0 `ACCTID`s moved)
and to be re-observed at each future re-link: compare the hash digests and the resolved `ACCTID`
set before and after **locally**, publishing only the outcome as counts, never the digests or
identifiers themselves.

**`BANKID` — high.** Same `online_id` consequence. Setting a previously-unset `bankid` costs exactly
as much as changing one.

**`country` — medium.** PL → LT means a re-link: new consent, new session, regenerated UIDs. It does
not orphan GnuCash accounts (`ACCTID` is bank-side, `BANKID` config-side), and the coverage ledger
survives — measured 2026-08-10 across the Alior re-link. What changes is the `rate_limit_domain`
label, making fetch-log entries either side non-comparable.

**The `revolut` config key — high only if decision 3 is reversed** to leave `bankid` unset, in which
case the key name becomes the permanent `BANKID`.

**Coverage ledger for Revolut, and the six already-written OFX files — cheap, but not free, and
earlier drafts said free.** Deleting either costs nothing locally, and regenerating them spends no
allowance for any **settled** month, because `chunk_ttl` never expires those. But both are rebuilt
by a fetch, and a fetch re-requests any month still inside `LATE_BOOKING_MARGIN` whose six-hour TTL
has lapsed — **two such months as of the fetch date** (2026-07 until 2026-08-15, 2026-08 until
2026-09-15; see decision 2's table). So the real bill is **one `/transactions` request per pocket
per stale recent month**, plus the session call and `/balances` while the window is open. Scale it
by pocket count, not by file count.

**Fixtures, README row, `enable-banking.md` rows — low.**

---

## Open questions

Two of the original questions are closed by measurement and have moved to *Settled by
measurement* below. **Question 1 — the gate — has now been run** (2026-08-13, no API request, at a
keyboard, the same shape of gate [`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) decision 1
used and closed on GnuCash 5.16); its full record stays below because acceptance read it. Of what
remains open, **none is a probe** — with one named exception that would only become one later.

### 1. ~~What does GnuCash do with statements sharing one `BANKID`+`ACCTID`, and what is the scope of its `FITID` de-duplication?~~ — **measured 2026-08-13, GnuCash 5.16 (build 2026-06-27)**

**Answer: de-duplication is per account; the colliding shape silently loses a transaction in the
default per-pocket path; the control pair survives everywhere. Decision 1 is a real fix.** Read
against the outcomes table the procedure carried before the run: the first outcome fired (control
survives, colliding pair loses one → per-account scope, option J stays rejected), and so did the
divergence outcome (variant A and variant B disagree). A pass is a statement about one version and
about the import shapes actually run.

**The artefact**, built as the procedure prescribed — synthetic throughout, generated with this
branch's own `build_statement`/`combine_statements`, `ofxdump` 0.10.5 exit 0 and zero errors on all
seven files before any import: six statements — four sharing one `BANKID`+`ACCTID` (a synthetic LT
master IBAN) in AUD/EUR/GBP/USD, PLN under its own synthetic PL IBAN, and a sixth CHF statement
under a third `ACCTID` in the decision-1 hash shape (`eb-<16 hex>`). The **colliding pair**: one
byte-identical `FITID` on an EUR debit and a USD credit, both inside the collapsed identity. The
**control pair**: a different byte-identical `FITID` on a PLN debit and a CHF credit — two distinct
`ACCTID`s. Eight fillers with unique `FITID`s so any loss is attributable. Both variants carried
identical statements and ids; every offered identity was mapped to a **new** account, in two
separate throwaway books.

**The matching step:**

- The account picker keys on `online_id` alone: **three pickers for six statements**, one per
  distinct `BANKID`+`ACCTID`, identifying the account only as "REVOLT21 LT08…" with **no currency
  shown anywhere** — a user at that dialog has nothing to distinguish four pockets by. In variant
  B, the second through fourth shared-identity files imported with **no picker at all**, straight
  into the account created for the first.
- `CURDEF` is consulted **exactly once**, as the pre-selected commodity in the new-account creation
  dialog (AUD, PLN, CHF — the first statement of each identity), and never again: no re-prompt and
  no warning when EUR/GBP/USD statements landed in the AUD-denominated account.
- **Value destruction in the collapsed account:** the six non-AUD transactions arrived in the
  matcher as **0.00 with "New, UNBALANCED (need acct to transfer …)"** — values stranded in a
  currency the account cannot hold, not silently converted. The collapse books zeros, on top of
  mis-filing.

**The de-duplication step:**

- **Variant A (combined, one import run):** no within-run de-duplication — both colliding legs
  present in the matcher, neither flagged duplicate, all 8 rows offered for the collapsed account.
  Re-importing the same file into the same book suppressed everything already imported **except** a
  previously-skipped exchange leg whose `FITID` already existed in a *different* account — the
  first evidence of per-account scope, and the pre-matched branch of procedure step 4 in the same
  pass.
- **Variant B (per-pocket sequential, the default shape, tool order):** the USD file's colliding
  leg was **silently absent from the matcher** — no row, no flag, no error; the collapsed account's
  register holds **7 of 8** transactions. The CHF file's control leg **appeared and imported**
  despite its `FITID` already sitting in the PLN account; the PLN and CHF registers are complete.
- **The divergence:** a single combined import loses nothing; the sequential default drops the
  second colliding leg silently. **The default packaging is the dangerous one.** On condense this
  belongs in [`decisions.md`](decisions.md) as a property of the packaging choice rather than of
  Revolut; do not average the two results.

**Not run:** the reverse-order rerun the procedure named for a lost leg. The loss is explained by
the first-committed leg winning, and no conclusion here depends on *which* leg dies; recorded as
unmeasured rather than inferred.

**Observed in passing, for #5:** the control pair's two legs import as two separate transactions —
one against a manually assigned transfer account, one against Imbalance — because nothing pairs
them at import time. The pairing gap decision 9 defers is visible in the register, not only in the
data.

### 2. Is one BIC correct for the whole connection?

**Inference, not evidence.** Two distinct (country, bank-code) pairs exist: LT/32500 on four
pockets, PL/2910 on one. Whether the PL-coded pocket shares a legal entity with the LT-coded ones is
inferred from the master IBAN appearing in all five accounts.

**Nothing local can close it**, and no live call would either: `account_servicer` is null 5/5 so
`bic_fi` is not coming, and a catalog re-read would only re-confirm `REVOLT21`, which is the brand's
value and says nothing about per-pocket servicing. Decision 3 proceeds on the inference and records
it as one. If a future re-link ever populates `account_servicer`, that is the moment to check — and
a disagreement costs the full `BANKID` orphaning price, which is why it is written down.

### 3. History horizon — refusal is ruled out at this depth; *delivery* is unmeasurable on this account

**Rewritten rather than closed, and the distinction is the point.** The first fetch requested
**2026-05-15 → 2026-08-12 and it was accepted** — 200 on every request, no `400 ASPSP_ERROR`. So
Revolut is **not** the Alior/Erste refusal shape at that depth, and the three-request single-day
method this ADR previously prescribed would now measure only refusal.

**But every transaction returned fell on the window's final date, and nothing was returned for any
earlier date in the window.** That is equally consistent with the account simply having no older
activity, and that is the reading the evidence supports. (Earlier drafts put a day count on this and
got it wrong in both directions; the endpoint fact is the whole observation and the arithmetic added
nothing.) So **delivery at any depth cannot be measured on this account
until it accumulates history** — no method available today distinguishes "the ASPSP does not serve
it" from "there is nothing to serve".

This is the one question in this file that is a genuine `probe-designer` candidate if the horizon
ever matters before the account has history — for instance before a backfill of a period this tool
did not cover. Until then it is not worth a counted request, and it is not one now.

### 4. Does the conversion pairing generalize?

The set is small and **every conversion was booked on one day**.
Unmeasured: whether `entry_reference` still pairs 1:1 when legs fall on **different booking dates**,
and whether a **multi-leg deal or a fee row** ever shares one `entry_reference` with the transaction
it belongs to. Either would break the "every cluster is exactly size 2" property the join relies on
— and the fee case has precedent, since Wise fee rows repeat the parent's id.

**Settled for free by a second fetch**, over a window containing conversions booked on different
days. No probe, no extra requests beyond ordinary use. #50 should not commit to the join key until
this has been seen at least once more.

**Update 2026-08-19: the gate above was replaced, not satisfied.** #50 committed to the key
without the second sighting — [adr-revolut-exchange-pairing.md](adr-revolut-exchange-pairing.md)
rejected option A measures why the wait had an unbounded horizon while the failure mode of
proceeding is refusal (an unannotated pair, today's behaviour), never mis-pairing; its decision 2
carries the working assumption and the named revisit trigger. The question itself **stays open**:
the 2026-08-19 pass is still censored on cross-date pairing (every `EXCHANGE` leg in the corpus
books on one date), and no fee or multi-leg row has shared a reference yet. Do not read the tick
on decision 9 as settling this.

### 5. Is the `bank_transaction_code` vocabulary closed?

**No, and nothing here should claim otherwise.** `EXCHANGE`, `TOPUP` and `TRANSFER` come from one
fetch of one connection over one window. Only `TRANSFER` was already known to this project;
`EXCHANGE` and `TOPUP` are new values, and **`CONVERSION` — Wise's word — is specifically not
Revolut's**. Whether Revolut also emits `CARD`, `DEPOSIT` or others is unmeasured. Settled by
ordinary use over time; decision 8 requires the fixture to say so rather than assert a closed set.

### 6. Does the ASCII folding hold for Revolut?

**Not answered, and this must not be read as coverage.** `to_ascii` changed nothing — **zero
non-ASCII bytes** in the whole transaction set and in all six written files. That is a fact about
*this* fetch, not about Revolut: a multi-currency wallet with non-Polish counterparties is exactly
where the folding question will be met, and this window did not exercise it. Recorded explicitly
because a "0 non-ASCII" line in a measurement table is easy to mistake for a pass.

### 7. Does Revolut ever return 429, and what is its effective daily cap?

Unmeasured, no experiment proposed. There is no quota counter to read (measured 2026-08-09: no
`X-RateLimit-*`, no `RateLimit-*`, no `Retry-After`), so an allowance can only be spent, and the
cheapest reading is the next natural refusal. The first fetch spent 11 requests in the new
`Revolut|PL|personal` domain with zero refusals; across this deployment, zero 429s have been logged
at any bank. `state/fetch-log.jsonl` will record a Revolut refusal with its `domain` and `attempt`
the moment one happens, at no cost.

### Settled by measurement — recorded so they are not re-derived

**From the catalog and the link:**

- ~~Does Revolut appear in `GET /aspsps`?~~ **Yes** — PL (1 of 33) and LT (1 of 26), byte-identical
  apart from `country`, `beta: false`, 180-day consent, no `required_psu_headers`.
- ~~Does a PL registration surface the accounts?~~ **Yes** — five pockets, full 180-day consent.
- ~~Is `account_servicer.bic_fi` populated?~~ **No.** `account_servicer` is null on all 5 — the whole
  object. The evidence the first draft gated `bankid` on does not exist.
- ~~Does each currency pocket surface as a separate account object?~~ **Yes** — 5 objects, one per
  currency, but *not* independently identified.
- ~~Is `account_id.iban` present on every pocket?~~ **Yes, 5/5** — and 4 of them share one value.
- ~~Can `"XXX"` or a missing currency bite here?~~ **No.** Real ISO codes 5/5.
- ~~Is `identification_hashes` (plural) needed to distinguish the pockets?~~ **No** — the singular
  hash, already persisted, is 5 distinct values.

**From the fetch and `ofxdump`:**

- ~~Does `--combine` deduplicate the identical `BANKACCTFROM` aggregates, and does libofx object?~~
  **No, and no.** 5 `STMTTRNRS` with 5 `BANKACCTFROM`, four identical; `ofxdump` 0.10.5 exits 0 on
  all six files with zero errors and reports **five distinct accounts and five distinct statements**,
  correct `CURDEF` per statement, correct attribution of every transaction, no cross-attribution.
  The code reading was right and there is **no parse-level objection** — the damage is downstream of
  the parser.
- ~~Does splitting the output per pocket avoid the collision?~~ **No** — each per-pocket file presents
  the identical `Bank ID` + `Account #`, so four separate imports collide in one book. Rejected
  option K.
- ~~Is `transaction_id` populated?~~ **No, null on every transaction** — so `FITID` is the
  `entry_reference`, and the transaction-details endpoint is uncallable.
- ~~Do conversion legs share a `FITID`, and does it matter?~~ **Yes, and yes.** Every pair's legs
  carry one id, and **pairs land under one byte-identical account identity**. What decision 1's
  rule does about it was deliberately left out of this list while it depended on GnuCash's
  de-duplication scope — **now measured per account by the gate (open question 1, 2026-08-13), so
  decision 1's rule removes the loss**.
- ~~Does Revolut send machine references in `remittance_information`?~~ **No** — zero
  `<PREFIX>-<id>` elements, zero `CHECKNUM`, zero `REFNUM` in the written files. Its machine
  reference is `entry_reference`, a field rather than a remittance element, and at 36 characters it
  would be dropped by `compose_check_number` anyway.
- ~~Can the currency-mismatch `ValueError` fire?~~ **No** — `transaction_amount.currency` equals the
  pocket currency on 100%, and `instructed_amount` is absent entirely. Revolut is #5's **pairing**
  case only, not its mismatch case; the two halves are now separable by measurement.
- ~~Does the existing conversion machinery fire?~~ **No** — `_conversion_key` returns `None` for the
  entire set. Revolut's code is `EXCHANGE`, not `CONVERSION`, and there is no `BALANCE-<digits>`
  element.
- ~~Was the 2026-05-15 → 2026-08-12 window refused?~~ **No** — 200 on all 11 requests, zero 429,
  zero 400, in the new
  `Revolut|PL|personal` domain.
- ~~Did the mapper survive?~~ **Yes** — nothing raised, zero empty `NAME`, `NAME` max 75 / 96,
  `MEMO` max 85 / 390, no truncation; filename prediction unchanged at 5 predictable, 0
  unpredictable, no disambiguator.

**Field coverage, one fetch** — for the `enable-banking.md` row:

| Field | Observed |
|---|---|
| `transaction_id` | null on all |
| `entry_reference` | populated on all; 36-char lowercase UUID; **byte-identical on both legs of a conversion** |
| `bank_transaction_code.code` | `EXCHANGE`, `TOPUP`, `TRANSFER` — vocabulary **not closed** (open question 5); `sub_code` null throughout |
| `creditor` / `debtor` name | populated on exactly the non-conversion rows, direction-consistent |
| `creditor_account` / `debtor_account` | **presence tracks transaction type, not direction** — present on all non-conversion rows, absent on every conversion leg; all under `iban`, `other` null throughout |
| `remittance_information` | present on all; usually 1 element, sometimes 2; no machine reference |
| `status` | `BOOK` on all — **no `PDNG` returned at all** |
| `booking_date` / `value_date` | equal on 100%, date-only |
| `exchange_rate` | key present, null on all — no rate is stated |
| `instructed_amount` | **absent entirely** — not a null, not a key |
| `merchant_category_code`, `reference_number`, `reference_number_schema`, `balance_after_transaction`, `note`, `transaction_date`, `*_agent`, `*_account_additional_identification` | key present, null on all |

**Contrast worth carrying into `enable-banking.md`:** the counterparty-account pattern is a
**different shape from Wise's**. Wise is asymmetric *by direction* — absent on outgoing payments,
usually present on incoming ones
([`enable-banking.md`](enable-banking.md#wise-counterparty-ibans-are-mostly-unavailable) has the
proportions); Revolut is complete *by type* — every non-conversion row has one, every conversion leg
has none. Two different absence patterns, and a reader who knows only the Wise row would predict the
wrong one. Two smaller notes from the same pass: Alior PLN and Wise EUR incoming transfers both
carried `debtor.name` and `debtor_account` **under `iban`** (the bare-NRB path was not exercised;
the August 2026 Alior fix holds), while another incoming Wise row (`code` `UNKNOWN`) had **neither**
name nor account — so "incoming is populated" is a tendency, not an invariant.

---

On condense, the decisions fold into [`decisions.md`](decisions.md) — most usefully as one line
under `ACCTID` identity, that a value shared by several accounts of one connection is not an
`ACCTID` for any of them and the **whole connection** then resolves through `identification_hash`
(the uniform policy, option M, chosen at acceptance 2026-08-14), with the orphan-on-change cost and
the `FITID`-de-duplication consequence attached. Two gate findings
condense independently of the Revolut decisions, because they are properties of GnuCash and of
packaging, not of this bank: **`FITID` de-duplication is per account and applies across import
runs, not within one** — so sequential per-pocket import (the default) silently drops a
shared-`FITID` transaction where a single combined import keeps both — and GnuCash's OFX matching
consults `CURDEF` only as a new-account default, never to distinguish accounts. This file stays for
the rejected options, the shared-IBAN and shared-`FITID` measurements, the gate record, and the
record that the `BANKID` question was the wrong one to have been blocked on.
