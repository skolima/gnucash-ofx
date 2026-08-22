# ADR: `tools/evidence.py` becomes a package, reads only, and reports only what its dataclasses can hold

**Status:** Accepted. All nine decisions implemented — see *Where this stands* below. The
measurements were taken 2026-08-09 against this machine's `state/`, `cache/` and
`state/fetch-log.jsonl`, and against the code as of
[#23](https://github.com/skolima/gnucash-ofx/issues/23) (commit `ba637fc`).
**Date:** 2026-08-09.
**Scope:** a new `tools/evidence/` package (`state_census.py`, `cache_census.py`,
`coverage_reconciliation.py`, `filename_prediction.py`, `cli.py`, `__init__.py`); `runlog.py` gains
one additive reader function and two record types. `pyproject.toml` gains `tools` to `pythonpath`,
`mypy`'s `packages`, and `ruff`'s `src`, so the sanitization guarantee in decision 2 is actually
checked rather than merely written down. `state.py`, `coverage.py`, `cache.py`, `run.py`,
`ofxout.py` and `cli.py` are untouched — no format change, no new write path, no behaviour change
to `link`, `fetch` or `status`. `tests/` is untouched by this ADR; the implementing PR adds its own.
**Issue:** [#20](https://github.com/skolima/gnucash-ofx/issues/20). Depends on
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md)
([#15](https://github.com/skolima/gnucash-ofx/issues/15)) having shipped — decision 5 below reads
`coverage.py`'s ledger, which did not exist when that ADR's own measurements were taken by hand.

---

## Where this stands

- [x] **1.** `tools/evidence/` is a package split by concern, not one flat module
- [x] **2.** Sanitization is enforced by the shape of a per-census frozen dataclass, not by a
      redaction pass at the boundary
- [x] **3.** The state census reports schema-version distribution, identity-resolution counts and
      expiry — bank-keyed, never account-keyed
- [x] **4.** The cache census reports chunk and month *counts*, and never calls a count "coverage"
- [x] **5.** Coverage per account is read from `coverage.load_coverage`, never reconstructed from
      cache chunks
- [x] **6.** `runlog.py` gains `load_requests()`, tolerant of a malformed line; the run-log↔ledger
      join goes through session state's `uid → ACCTID` mapping, not through the log's own redacted
      path
- [x] **7.** Filename prediction calls `_known_acctid` / `_disambiguators` / `ofx_filename`
      directly, not the public `dry_run_enablebanking` wrapper, and reports counts, never a name
- [x] **8.** The package is read-only by construction: no write-path function from any module it
      touches is ever imported
- [x] **9.** The canonical chunk-stranding denominator is month chunks; balance chunks and the
      combined total are secondary fields, never merged into one ratio
- [x] **10.** Untouched — not a decision to ship; recorded so nothing here is mistaken for having
      changed `link`, `fetch`, `status`, or any on-disk format

Mapped onto the issue's four bullets: the state census → decision 3; the cache census → decision 4;
"coverage per account... against what the run log says" → decisions 5–6, and the issue's own framing
of that bullet turned out to need correcting (§2 below); "what `ofx_filename` and `_disambiguators`
predict" → decision 7.

**Extended after acceptance by [#39](https://github.com/skolima/gnucash-ofx/issues/39)** — not a
decision this ADR proposed, so nothing above changes state. Two boundaries moved: `StateCensus`
gained identification-shape fields, one of which (`identification_scheme_kinds`) is the first
census field whose dict *keys* come off the wire, so decision 2's guarantee is kept there by a
gate (`_SCHEME_TOKEN`, letters only; non-conforming values bucketed, never emitted) rather than by
field types alone — now AGENTS.md invariant "c". And `CoverageReconciliation` gained
`distinct_coverage_keys`/`accounts_sharing_a_key`, which carry decision 3's lower-bound label for
the same `id_hashes`-empty reason `would_resolve_to_bare_uid` does. The arguments live in
[`decisions.md`](decisions.md#a-ledger-key-collapse-is-reported-as-a-pair-of-counts-never-by-redefining-covered_accounts);
the dataclass sketch under decision 2 below is the shape as of #23, not the current field list.

---

## Context

[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) (PR
[#15](https://github.com/skolima/gnucash-ofx/issues/15)) carried three measurements that each
overturned the reasonable guess: that `save_cached_month` overwrites a month's chunk with whatever
the latest request covered, wider *or narrower*; that 24 of 43 cached account digests belonged to no
current session; that five of six state files predated the `accounts` schema layout and yet none of
them was in the condition that actually orphans anything. Every one of those was produced by
importing the real functions and running them over the real local directories, by hand, once. None
of the scripts that produced them survived past that ADR.

The cost is concrete, not aesthetic. This ADR itself needed three of the same four questions again
— the state schema census, the cache-vs-session cross-reference, and (newly) whether coverage should
be read from the ledger or reconstructed from the cache — and every one of them had to be re-derived
from scratch rather than re-run. The state and cache censuses reproduced #15's own numbers exactly
(24 of 43 stranded digests, five of six pre-`accounts` state files, 45 of 157 month chunks
stranded ≈29%), which is itself the evidence that a repeatable tool would have saved the work: had
one existed, this ADR would have run it and moved directly to the one genuinely new question, rather
than re-deriving three settled ones to get there.

**What re-deriving those three found, along the way.** `fetch-log.jsonl` did not exist at #15's
time; it now holds 68 lines (4 `run`, 64 `request`), all valid JSON, 0 parse failures — the first
real data available for a run-log census. And `coverage.py` (shipped in
[#16](https://github.com/skolima/gnucash-ofx/issues/16), after #15) already answers "coverage per
account" directly, in days, from `state/coverage/<bank>.json` — 6 of 6 files populated, 19 of 19 live
accounts with exactly one `AccountCoverage` record. The issue's own third bullet, "coverage per
account as the cache reports it," is worded for a tool that predates the ledger; §5 below explains
why that framing is now wrong and what replaces it. Finding that out required reading `coverage.py`
and cross-referencing it against the local `state/coverage/` directory — exactly the kind of
one-off archaeology this ADR exists to stop needing.

`.claude/agents/local-evidence.md` already does this work today, by writing throwaway scripts to the
scratchpad directory. Its own description text names the target functions —
`save_cached_month`, `cached_window`, `chunk_ttl`, `ofx_filename`, `_disambiguators`,
`_known_acctid`, `days_until_expiry` — which is this issue's proposal already written down as an
agent's mental model, missing only the module that would make it literally true.

---

## Decisions

### 1. `tools/evidence/` is a package split by concern, not one flat module

The issue's own wording, and the `local-evidence` agent's target list, both say `tools/evidence.py`
— a single file. Rejected in favour of a package: `state_census.py`, `cache_census.py`,
`coverage_reconciliation.py`, `filename_prediction.py`, one `cli.py` for a human entry point, and
`__init__.py` re-exporting the four census functions the agent calls.

The four censuses read genuinely different files with genuinely different parsing concerns —
`state/<bank>.json`, `cache/*.json`, `state/coverage/<bank>.json`, `state/fetch-log.jsonl` — which is
exactly why `src/gnucash_ofx` itself is already split into `state.py`, `cache.py`, `coverage.py` and
`runlog.py` rather than one `storage.py`. A flat `evidence.py` mixing JSONL parsing with filename
disambiguation would be the file that this project's own module boundaries argue against. The human
CLI entry point (`cli.py`) also needs argument parsing that has nothing to do with what the
`local-evidence` agent imports, and putting it in the same file as the four import targets is the
one place a redaction slip is likeliest — see decision 2.

`pyproject.toml` gains `tools` under `[tool.pytest.ini_options] pythonpath` and under
`[tool.mypy] packages`/`[tool.ruff] src`, so the implementing PR's tests can import
`tools.evidence.*` the way tests already import `gnucash_ofx.*`, and so `mypy --strict` actually
runs over it — decision 2's guarantee is only as good as the type checker that enforces it.

### 2. Sanitization is enforced by the shape of a per-census dataclass, not by a redaction pass

The issue explicitly rejects "a redaction step left to the caller." Each census function returns a
`frozen(slots=True)` dataclass whose field types are structurally incapable of holding a raw
identifier: counts, ratios, `date`, a closed set of already-public strings (a configured bank key,
an Enable Banking `error` code verbatim), and nothing wider. No field is `dict[str, Any]`, no field
is a bare `str` sized for an account number, and no census returns a list of per-account raw values
— only aggregate counts, matching the `local-evidence` agent's own rule of giving the shape instead
of the value ("5 of 6 state files...", never which five).

```python
@dataclass(frozen=True, slots=True)
class StateCensus:
    bank: str
    schema_version: int | None       # None = v1 fallback (absent key)
    account_count: int
    with_iban: int
    with_identification_hash: int
    with_currency: int
    would_resolve_to_bare_uid: int    # see decision 3 — a lower bound, not the fetch-time answer
    days_until_expiry: int
```

This is the same trade `session.raw` in `state.py` deliberately does *not* make: that field is a
`dict[str, Any]` because `state.py`'s job is to preserve an API field nobody thought to name.
`evidence.py`'s job is the opposite one, so the type that is right for one is the type that is wrong
for the other — see rejected option G.

### 3. The state census reports schema-version distribution, identity counts and expiry — never a per-account list

Built from `load_session` and `days_until_expiry`, bank-keyed. **`would_resolve_to_bare_uid` is a
lower bound, not the fetch-time answer**, for the same reason `--dry-run` cannot perform this check
today ([`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) §6): the real
check also consults `identification_hash` from the fetch-time `accounts_data` array, which only a
live `GET /sessions` call produces, and this tool never calls the network. The field is labelled for
what it is — computed from stored data only — so a reader does not mistake it for `run.py`'s own
`_known_acctid(stored, uid, id_hashes)` result with `id_hashes` silently empty.

### 4. The cache census reports chunk and month *counts* — and never calls a count "coverage"

Built from the cache directory's own filenames and `CachedMonth`/balance-chunk payloads: total month
chunks, total balance chunks, distinct account digests, and how many of each belong to no
currently-linked account (cross-referenced against the live sessions' `uid`s via `cache._uid_digest`,
already exported for exactly this by `run.py`'s own cross-references). It reports "months held", not
"months covered" — [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) §1
is the reason a month chunk cannot honestly be read as evidence of coverage, and this census must not
launder that reading back in by naming a field wrong. See decision 5 for what "coverage" actually
means here.

### 5. Coverage per account is read from the ledger, never reconstructed from cache chunks

The issue's own third bullet — "coverage per account as the cache reports it" — is worded for a tool
that predates `coverage.py`. It shipped in [#16](https://github.com/skolima/gnucash-ofx/issues/16),
*after* [#15](https://github.com/skolima/gnucash-ofx/issues/15) wrote that reconstruction by hand
because nothing else existed yet. On this machine, 6 of 6 `state/coverage/<bank>.json` files exist
and are populated; 19 of 19 live accounts have exactly one `AccountCoverage` record, in the unit
(days) that #15 §1 argued the cache cannot honestly provide at all.

So `coverage_reconciliation.py` calls `coverage.load_coverage(state_dir, bank)` directly. Rebuilding
coverage from cache chunks would redo work #16 already did, in a *weaker* unit (months, not days),
and would reintroduce the cache's own forgetting bug as a source of error in the one component whose
entire purpose is not having that bug. A narrower, different use case — sanity-checking the ledger's
own correctness against the cache's raw chunks — is real but is not this census, and is out of scope
here (see *Rejected options*, C).

### 6. `runlog.py` gains `load_requests()`; the run-log↔ledger join goes through session state, not the log's own redaction

No reader for `fetch-log.jsonl` exists today; `runlog.py` only writes it. `load_requests()` (or
`iter_records()`) is added to `runlog.py` itself — the module that owns the file format, and the
natural home for any future consumer, not just this one. It is **tolerant of a malformed line**:
skip and count, never raise, matching `RunLog`'s own stated principle that a diagnostic must never
be able to cost a run — evidence-gathering is a different context, but the file format's contract
should not depend on who is reading it. (On this machine there is nothing to be tolerant of yet — 68
of 68 lines parse — so this is precautionary, not evidence-driven, and is labelled as such.)

**The join is not free.** The obvious plan — match a `request` record's `path` to a ledger entry —
does not work, because the two sides key on different things entirely.
`runlog.redact_uid_path` keeps only `uid[:4]` and `uid[-4:]` of the Enable Banking account `uid`;
`coverage.account_key` is `sha1(ACCTID)[:16]`, and `ACCTID` is (usually) the IBAN — a value with no
derivation path back to `uid` at all except through the live session. So reconciliation has to load
`SessionState.by_uid` for the bank in question, map each `LinkedAccount.uid → LinkedAccount.iban`,
apply `coverage.account_key` to get the ledger key, and then match that back to a run-log request by
recomputing the *same* `uid[:4]…uid[-4:]` truncation the log already applied and comparing strings —
never by touching a full `uid` that only exists in memory for the length of one bank's loop. Whether
that truncated match is actually unique across a bank's own accounts is not yet known; see *Open
questions*.

### 7. Filename prediction calls the private functions directly, not the public dry-run wrapper

The issue's fourth bullet needs `run._known_acctid`, `run._disambiguators` and
`ofxout.ofx_filename` — the same three functions `run.py`'s own `_planned_files` already composes
for `--dry-run`. Reusing `dry_run_enablebanking()` wholesale was considered and rejected (*Rejected
options*, D): it calls `require_credentials()` even though it sends no request, so a read-only
evidence tool would fail in an environment with no `EB_APP_ID`/`EB_PRIVATE_KEY` configured, for a
check that has nothing to do with reading `state/`. Its `PlannedFile.path` also bakes the account's
disambiguator — an IBAN tail or a digest — into a path string, which is exactly the "filenames from
`output/` carry an account prefix" case the `local-evidence` agent is already forbidden from
emitting.

So `filename_prediction.py` imports `_known_acctid`, `_disambiguators` and `ofx_filename` directly,
matching the issue's own list of target functions, and reports only:

```python
@dataclass(frozen=True, slots=True)
class FilenamePrediction:
    bank: str
    predictable_count: int
    unpredictable_count: int          # unknown currency at link time
    disambiguator_kind: dict[str, int]  # {"iban_tail": n, "digest": n}
```

never a composed filename, never a path. `_known_acctid`/`_disambiguators` stay private for now:
`tests/test_cli.py` already imports `run._redact_account` directly across a module boundary, which
is the precedent for a second private cross-import rather than a promotion — but `test_ofxout.py`
tests disambiguation only through the already-public `account_disambiguators`, so there is no
precedent either way for these two specifically. Left private; revisit if a third consumer appears
(see *Open questions*).

### 8. Read-only, structurally: no write-path function is ever imported

`save_session`, `save_coverage`, `save_cached_month`, `scrub_log` and every module's own
`_write_atomic` are never imported anywhere in `tools/evidence/`. This is what makes "never touches
the network, never writes" a property of the import graph rather than a rule someone has to
remember to follow — a `grep` for
`_write_atomic\|save_session\|save_coverage\|save_cached\|scrub_log` over `tools/evidence/` staying
empty is the test.

`scrub_log` joined that list after this ADR was written: `runlog.py` had no writer to name when
decision 8 was framed around "every module's own `_write_atomic`", and it now has both a private
one and a *public* rewrite. The name matters because a `runlog_census.py` is the next census this
repo wants ([`adr-input-hardening.md`](adr-input-hardening.md) open question 1) and its obvious
first line is an import from `runlog` — where the only two safe names are `load_requests` and
`redact_uid_path`.

One consequence: [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) §1's
"forgetting bug" demonstration — write two overlapping windows, watch the second overwrite the first
— is a controlled *simulation*, not a census of what is really on disk, and it needs a write to
demonstrate. It stays out of `evidence.py` and stays where it already lives: the original ADR's
narrative and the test suite. Running it "safely" against a temp copy of the real `cache_dir` was
considered and rejected — the read-only contract is worth more as an absolute than as an absolute
with one documented, careful exception (*Rejected options*, F).

### 9. The canonical chunk-stranding denominator is month chunks; balance chunks are a secondary field

Re-measured today: 45 of 157 month chunks stranded (≈29%, matching
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) §4 exactly); 1 of 20
balance chunks stranded; 46 of all 177 chunks together (≈26%) — a different number from either. Month
chunks are the canonical denominator, because they are what #15's §4 already established as the
comparison point and what future ADRs will want to compare against, and because balance chunks are a
different kind of thing by construction — account-keyed only, a 6-hour TTL, no window, no relation to
"coverage" at all (`adr-aspsp-rate-limit-domain.md` §5). `CacheCensus` reports
`month_chunks_stranded`/`month_chunks_total` as the headline ratio and
`balance_chunks_stranded`/`balance_chunks_total` plus the combined totals as separate fields, never
folded into one number.

### 10. Untouched

`link`, `fetch`, `status`, `state.py`, `coverage.py`, `cache.py`, `run.py`, `ofxout.py`, `cli.py` —
no format change anywhere, no new write path, no new network call, no change to what any existing
command does or prints. This ADR adds a read path and nothing else.

---

## Rejected options

### A. A flat `tools/evidence.py`, as the issue and the `local-evidence` agent's own text describe

**Rejected** — decision 1. Four unrelated file formats behind one file is the shape this project's
own `src/gnucash_ofx` module boundaries already argue against.

### B. A redaction pass at the output boundary

**Rejected by the issue itself.** A shared serializer that strips fields by name is one missed field
away from a leak the moment a census gains a new field — the same failure shape as a machine
reference that "almost fits" and gets left in `NAME`/`MEMO` by habit rather than by rule. Decision 2
makes it structurally impossible for a raw value to reach a return type in the first place, rather
than trusting a pass over it afterward.

### C. Reconstruct coverage-per-account from cache chunks, as `adr-coverage-ledger-and-warnings.md` §1–2 did by hand

**Rejected** — decision 5. The ledger this ADR would be reconstructing already exists, is populated
on every bank measured here, and answers the question in the correct unit (days) rather than the
cache's unit (months, and only the *last* window that touched one). Redoing that work here would
also inherit the forgetting bug as a source of error in a tool whose whole point is not having one.
A cache-based reconstruction remains a legitimate, narrower thing — a sanity check of the ledger's
own correctness — but it is a different question from "what does this account's coverage say", and
is not built here.

### D. Reuse `dry_run_enablebanking()` / `_planned_files()` wholesale for filename prediction

**Rejected** — decision 7. Measured in `run.py`: `dry_run_enablebanking` calls
`require_credentials()` (`run.py:207`) before doing anything else, even though it sends no request —
so a read-only evidence tool built on it would fail on a machine with no `EB_APP_ID`/
`EB_PRIVATE_KEY` set, for a reason unrelated to reading `state/`. Its `PlannedFile.path` also
composes `output_dir / ofx_filename(...)`, embedding the account's disambiguator in a string the
`local-evidence` agent is already told never to emit.

### E. Match a run-log `request` record to a ledger entry using `runlog.redact_uid_path`'s own output

**Considered, not usable as stated.** It looks like it should let reconciliation skip loading session
state entirely. It cannot: `redact_uid_path` keeps a truncated `uid`; `coverage.account_key` is a
digest of `ACCTID`, a different value with no derivation from `uid` except through the live session's
own mapping. Decision 6 routes the join through `SessionState.by_uid` instead, and flags the
remaining risk — whether the truncated match is unique per bank — as an open question rather than an
assumption.

### F. A "safe" version of the forgetting-bug simulation, run against a temp copy of the real cache

**Rejected** — decision 8. `adr-coverage-ledger-and-warnings.md` §1's demonstration needs a write to
demonstrate; copying `cache_dir` to a temp directory first would make that write harmless in
principle, but it turns "never writes" from an absolute, `grep`-checkable property of the import
graph into a rule with one exception that has to be gotten exactly right every time it's touched. The
demonstration already lives in that ADR's narrative and belongs in the test suite if it needs to be
re-run; it does not need a new home in a tool whose entire safety case rests on doing one thing
consistently.

### G. Each census returns a plain `dict`/parsed JSON rather than a typed dataclass

**Rejected** — decision 2. A `dict[str, Any]` is the right type for `state.py`'s `session.raw`,
whose whole purpose is to keep whatever field nobody thought to name. It is the wrong type for
everything this tool returns, for the same reason in reverse: nothing should be able to arrive in a
census's output that its author did not deliberately name a field for.

---

## Cost to reverse

**Low.** Everything in scope is additive and read-only. `tools/evidence/` can be deleted with no
loss beyond the tool itself — no state, cache or coverage format changes, no consent spent, no
account re-linked. `runlog.py`'s `load_requests()` is a pure addition to a module that already
exists; removing it costs nothing already written to `fetch-log.jsonl`. The `pyproject.toml` changes
(a `pythonpath`/`mypy`/`ruff` entry) revert in one line each. Nothing here touches `FITID`, `BANKID`,
`ACCTID`, the cache key or the state schema.

---

## Open questions

**1. Does `uid[:4]…uid[-4:]` uniquely identify every live account within a bank, so decision 6's join can trust it?**
`redact_uid_path` was designed to keep an account distinguishable from its siblings in a diagnostic
log, not to serve as a join key back to a different keying scheme. **What would settle it:** a
`local-evidence` measurement, zero network, over each bank's live session — compute the truncated
form of every account `uid` and check for a collision within that bank. Cheap on this machine (at
most 5 accounts in any one bank, 19 total across 6 banks today) and needs re-checking whenever a
bank's account count grows.

**2. When reconciliation finds a mismatch — a day the run log shows as requested but the ledger does not show as covered — is that a bug in evidence.py, or ADR #15 decision 2's known, silent "coverage write failed, degrades to unknown" case?**
Not settleable from what is on disk today: `fetch-log.jsonl` records `run` and `request` events only;
a coverage-write failure surfaces as a `FetchWarning(kind="ledger")` on stderr at run time
(`run.py:1063`) and is never persisted anywhere. **What would settle it:** nothing local, right now —
the durable fix would be persisting `FetchWarning`s to the run log, which is a real gap this
reconciliation feature exposes but is out of this ADR's scope. Until then, the first time
reconciliation actually finds a mismatch, treat it as inconclusive rather than as a finding, and note
that this ADR's own decision 6 is the reason a future ADR might want warnings in the log.

**3. Should `_known_acctid`/`_disambiguators` be promoted out of `run.py`'s private surface now that `evidence.py` is a second cross-module importer?**
Not a measurement question. Decided for now by the existing precedent of `tests/test_cli.py`
importing `run._redact_account` directly: leave both private, and revisit only if a third consumer
needs them. **What would settle it:** nothing to probe — this is a judgment call to revisit if it
recurs, not before.

---

On acceptance: because this ADR is about tooling rather than product behaviour, its decisions do not
condense into [`docs/decisions.md`](decisions.md), which holds only decisions about what the tool
does to a user's data. They condense instead into a `tools/evidence/` line in `AGENTS.md`'s Layout
section, and into `.claude/agents/local-evidence.md`, which should be rewritten to call the new
module's census functions instead of writing a throwaway script every time. This file stays for the
rejected options and the measurements behind decisions 5, 6 and 9.

## References

- [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md)
  ([#15](https://github.com/skolima/gnucash-ofx/issues/15)) — the ADR whose measurements this issue is
  named for, and the ledger decision 5 depends on having shipped in
  [#16](https://github.com/skolima/gnucash-ofx/issues/16); its §1 is the forgetting-bug simulation kept
  out of scope by decision 8.
- [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — §5, why a balance chunk is a
  different kind of thing from a month chunk (decision 9).
- [#20](https://github.com/skolima/gnucash-ofx/issues/20) — the issue.
- `.claude/agents/local-evidence.md` — the agent this tool exists to stop needing throwaway scripts
  for; its target function list is where this ADR's scope comes from.
- `src/gnucash_ofx/coverage.py`, `runlog.py`, `cache.py`, `state.py`, `run.py`,
  `ofxout.py` — the functions each census calls, and (decision 8) the ones it must never import.
