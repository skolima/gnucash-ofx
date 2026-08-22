# ADR: input hardening — redact session ids, clamp what the server dictates, and contain one account's bad data to that account

**Status:** Accepted ([#38](https://github.com/skolima/gnucash-ofx/issues/38)). **All six decisions
implemented** — decision 1 in [#40](https://github.com/skolima/gnucash-ofx/issues/40) with its
follow-up [#41](https://github.com/skolima/gnucash-ofx/issues/41), decisions 2, 3, 5 and 6 in
[#44](https://github.com/skolima/gnucash-ofx/issues/44), and decision 4 in
[#45](https://github.com/skolima/gnucash-ofx/issues/45), which shipped on its own because it is the
only one that changes what reaches the OFX file and so had to clear the **Windows** libofx
conformance pass Linux CI cannot perform. Measurements taken 2026-08-12 on this machine by executing
the real functions
(`redact_uid_path`, `extract_balance`, `_currency_from_balances`, `map_transaction`) over the local
`state/` (6 session files), `cache/` (21 month chunks) and `output/` via scratch scripts —
denominators stated per section; transaction counts and volumes withheld by policy
([`AGENTS.md`](../AGENTS.md#data)) — measurements are stated as universally-quantified claims and
one-sided bounds instead.
**Date:** 2026-08-12; re-measured 2026-08-13 after acceptance, against a much larger corpus and a
now-populated fetch log — corrections and re-measurements are marked inline with that date.
**Scope:** would change: `src/gnucash_ofx/runlog.py` (+ `tests/test_runlog.py`) and — unforeseen
when this was written, and the one place decision 1 exceeded this list — `src/gnucash_ofx/cli.py`,
which is where the scrub is called from and why (see decision 1),
`src/gnucash_ofx/sources/enablebanking.py` (`_retry_delay`, `iter_transactions` and the response
path feeding it), `src/gnucash_ofx/run.py` (the per-account mapper guard and what sits inside it),
`src/gnucash_ofx/ofxout.py` (`to_ascii` and validation of the fields that bypass it),
`.github/workflows/ci.yml`. Explicitly would **not** change: the *values* of `FITID`, `BANKID` and
`ACCTID` (decision 4 validates, never mutates), the cache key and semantics, the state schema, the
coverage ledger, `config.toml`.
**Issue:** [#37](https://github.com/skolima/gnucash-ofx/issues/37), findings 1–6. The
file-permission hardening the issue names as out of scope stays out of scope here too. Depends on
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — the run log it introduced is
decision 1's subject, decision 2 reuses its backoff cap (`_MAX_BACKOFF_SECONDS`) rather than
picking a second constant, and its never-retry rule for `ASPSP_RATE_LIMIT_EXCEEDED` bounds decision
2's worst case — and on [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md),
whose "only a successful fetch advances coverage" rule is what makes decision 5's fail-loudly the
only safe option.

---

## Where this stands

- [x] **1.** `redact_uid_path` also masks `/sessions/{id}`, **and the same change scrubs the raw
      `/sessions/` lines already in the existing log** — the 2026-08-13 re-measurement (§1) found
      the leak live, not latent, so this is remediation, not prevention. Shipped first, in
      [#40](https://github.com/skolima/gnucash-ofx/issues/40) and completed in
      [#41](https://github.com/skolima/gnucash-ofx/issues/41), which closed the line-splitting hole
      the first cut left: `scrub_log` runs once per `fetch` and reports the changed-line count, so a
      log pasted anywhere *before* that ran should still be treated as having leaked its session ids
      (re-link and rotate). Condensed into [`decisions.md`](decisions.md).
- [x] **2.** `_retry_delay` rejects non-finite `Retry-After` and clamps the numeric header path to
      `_MAX_BACKOFF_SECONDS` — [#44](https://github.com/skolima/gnucash-ofx/issues/44)
- [x] **3.** The per-account guard catches `ArithmeticError`/`TypeError`/`AttributeError` alongside
      `ValueError`; non-finite `Decimal` amounts and balances are rejected at the mapper; balance
      extraction moves inside the guard — [#44](https://github.com/skolima/gnucash-ofx/issues/44),
      with two amendments review forced. There were **two** unguarded balance sites, not one:
      `_currency_from_balances` as well as `extract_balance`. And both **degrade rather than fail** —
      an unreadable `/balances` body is the "no usable balance" case the link-time currency fallback
      exists for, and `end_balance=None` is the shape every closed window already uses. The
      rejection stands for *amounts*, where there is no absent-value shape to fall back to; refusing
      to write a mapped statement over a figure the file does not require would also have repeated
      on every later run, since a failure advances no coverage
- [x] **4.** `to_ascii` strips C0 controls and DEL; `ACCTID`/`FITID` are validated as printable
      ASCII and `CURDEF` as `[A-Z]{3}`, with a violation failing the account, never mutating the
      value — [#45](https://github.com/skolima/gnucash-ofx/issues/45), verified against Windows
      libofx 0.10.5. Two things the conformance pass established that this decision only assumed:
      the `ExpatError` is exactly as described and produces *no file at all*, and 4b is **stronger**
      than stated — a non-ASCII `FITID` is not refused by libofx but returned **mutated**
      (`TX-Ü-001` → `TX-\x1ce-001`), so the alternative to validating is silent identity corruption
      rather than a crash. `CURDEF`'s `[A-Z]{3}` is the one rule libofx does not force (it parses
      `pln` happily); it rests on ISO 4217 being a closed contract. Shipped wider than written — see
      the amendment under decision 4 — and condensed into [`decisions.md`](decisions.md)
- [x] **5.** Pagination capped at 100 pages per account-window and response bodies at 10 MB, both
      failing that account loudly, never truncating silently —
      [#44](https://github.com/skolima/gnucash-ofx/issues/44). The body cap is checked before
      `.json()`, so it bounds the parse rather than the transfer: httpx has already buffered the
      response by then, which is a smaller claim than "capped memory" and the honest one
- [x] **6.** `actions/checkout` and `astral-sh/setup-uv` pinned to 40-char commit SHAs —
      [#44](https://github.com/skolima/gnucash-ofx/issues/44). Both resolved to the commit `v7`
      already pointed at, so CI runs exactly what it ran before; note `setup-uv`'s `v7` is an
      *annotated* tag, so the pin is the commit it dereferences to, not the tag object

One hardening batch, but not one PR necessarily — the only ordering constraint was that decision 1
ship first (its original "before any fetch runs" framing did not survive the 2026-08-13
re-measurement; see §1), which it did. The implementing PR ticks its own box.

---

## Context

A security audit ([#37](https://github.com/skolima/gnucash-ofx/issues/37)) surfaced six
input-handling findings. The calibration matters and the issue states it plainly: **none is a
trust-boundary escape** — path traversal and OFX injection are both closed — and findings 2–5 are
availability and data-integrity issues. The threat model is a compromised or malicious ASPSP (or
Enable Banking itself) sending hostile values down a channel the tool already trusts, plus one
genuine leak of an access-granting identifier into a file users are told to paste into issues.

What the current behaviour costs, concretely:

- **Finding 1** puts each bank's full session id — classified access-granting in `SECURITY.md` —
  verbatim into `state/fetch-log.jsonl`, whose module docstring promises it is safe to paste and
  which `diagnose.py` steers users to share. Not hypothetically: `get_session` builds
  `f"/sessions/{session_id}"` (`enablebanking.py:613`), every response flows through the observer
  into `RunLog.record_request`, and `redact_uid_path` (`runlog.py:43-58`) matches only
  `/accounts/([^/]+)`. Executed against the real function 2026-08-12: a `/sessions/<36-char>` path
  comes back unchanged; `tests/test_runlog.py:89` pins that unmasked behaviour as *correct*.
- **Finding 2** hands the sleep duration to the server. `_retry_delay`
  (`enablebanking.py:393-409`) returns a numeric `Retry-After` verbatim; the 60s cap applies only
  to the backoff ladder. `Retry-After: 999999999` sleeps for ~31 years; `Retry-After: inf` passes
  the `>= 0.0` check and raises an uncaught `OverflowError` in `time.sleep`.
- **Finding 3** lets one account's malformed amount abort the whole multi-bank run. The mapper
  guard at `run.py:1233-1250` catches `ValueError` only, and its comment states the intent it
  fails to deliver: "bad data in one account … must not cost the sibling accounts their files".
  `Decimal("garbage")` raises `decimal.InvalidOperation` (an `ArithmeticError`); a wrong-typed
  `transaction_amount` — the string `"100"` instead of an object — raises `AttributeError` in
  `_amount_str` (`enablebanking.py:97-98`). Both escape every handler up the stack.
  `extract_balance` and `_currency_from_balances` (called at `run.py:1219` and `run.py:1264`) sit
  outside any per-account try at all. `Decimal("NaN")` that reaches `build_statement` crashes its
  `>= 0` comparison (`ofxout.py:309`); `Decimal("Infinity")` serializes into `<TRNAMT>`.
- **Finding 4**: `to_ascii` (`ofxout.py:93-106`) keeps every code point `< 128`, C0 controls and
  DEL included; `\x02` in a memo aborts the writer's minidom step with an uncaught `ExpatError` —
  again the whole run, not the account. `ACCTID`, `FITID` and `CURDEF` bypass `to_ascii` entirely
  and are written raw.
- **Finding 5**: `iter_transactions` (`enablebanking.py:619-639`) follows `continuation_key` in a
  `while True` with no page cap, and nothing bounds a response body before `response.json()`. A
  server that always returns a fresh key holds the loop forever.
- **Finding 6**: `.github/workflows/ci.yml:19,22` reference `actions/checkout@v7` and
  `astral-sh/setup-uv@v7` by mutable tag. Blast radius is genuinely small — the workflow has
  `permissions: contents: read` and no secrets — which is what calibrates this Low: a retargeted
  tag runs attacker code in CI, but that code can read a public repo and lie about test results,
  nothing more.

---

## Constraints, measured rather than assumed

### 1. The fetch log — corrected 2026-08-13: it existed all along, and the leak is live

**Correction (2026-08-13).** The 2026-08-12 measurement claimed `state/fetch-log.jsonl` did not
exist. That was wrong: the log's first line is dated 2026-08-09, and by the ADR's own measurement
date it already held several days of `fetch` and probe runs. The claim below it — that the
session-id leak was *latent* and decision 1 could merge "before any fetch runs" — was therefore
already violated when this ADR was accepted. Re-measured against the real log (307 lines, 279 of
them requests, none unparseable):

- **31 request lines carry a raw session id in a `/sessions/` path** — 36-char UUIDs, none masked.
  9 distinct session ids appear; 7 are live right now, i.e. access-granting per `SECURITY.md`.
  Decision 1 is therefore **remediation of an existing file, not prevention**: the regex change
  alone leaves 31 leaked lines on disk, so the implementing change also scrubs the existing log in
  place (same head-4/tail-4 shape) — done in
  [#40](https://github.com/skolima/gnucash-ofx/issues/40), whose verification changed exactly these
  31 lines on a *copy* of this log, the original left byte-identical. The real file is scrubbed by
  the next `fetch` that runs, so between that merge and that fetch the leak is still on disk.
- `/accounts/{uid}` masking works as designed: 248 of 248 account-path lines are exactly
  `head4***tail4`, zero raw uids anywhere in any line.
- No `Retry-After` value has been observed, even now: 278 of 279 requests returned 200, one
  returned 400 (`ASPSP_ERROR`, no `retry-after` header), zero 429s, zero retries (every line is a
  first attempt — note for any future runlog census that `attempt` is **0-based**). Any claim
  about typical values would still be invention.
- Pages per account **is** now measurable: each continuation page is its own `request` line. See
  §5 for what it settles.

### 2. Identifier shapes make the redaction unambiguous

All session ids and account uids in current state files (6 sessions / 19 uids measured
2026-08-12; 7 / 24 on 2026-08-13, after a seventh bank was linked and three sessions re-linked
under the v2 state schema) are exactly 36 characters, lowercase hex plus hyphens (UUIDs).
Head-4/tail-4 masking is therefore unambiguous, and keeping the `/sessions/` vs `/accounts/` path
prefix keeps the two masked kinds distinguishable in a log being read months later. Leak channels
other than the path: none by construction — `record_request` persists only method, path, status,
attempt, elapsed, `api_code` and a header allow-list; query parameters (`date_from`,
`continuation_key`) and bodies never reach the logged path. Verified in the wiring 2026-08-12 and
re-verified against every real logged line 2026-08-13 (zero paths carry a query string; `window`
fields hold only ISO-date pairs; the one UUID-shaped header, `x-request-id`, is Enable Banking's
own request id — deliberately allow-listed, and its values overlap zero session ids and zero
account uids): **the path regex is the whole surface** — it just has a `/sessions/` hole in it.

### 3. Every amount ever received here is canonical — which justifies nothing being relaxed

Over every transaction in all 21 cached entries (all banks) and all 33 balance entries: zero
anomalies of any type. `transaction_amount` is always an object; `amount`
always a string, alphabet strictly `[0-9.]`, exactly two decimal places, no sign, no exponent,
never null or numeric. `credit_debit_indicator` present on 100%, closed vocabulary
`{CRDT, DBIT}`. Every balance object has a string amount and string currency;
`_currency_from_balances` resolved 21 of 21, `extract_balance` returned a `Decimal` 21 of 21;
currency fields strictly `[A-Z]`, always length 3. End to end, `map_transaction` over every cached
transaction with its entry's real currency raised zero exceptions.

So decision 3 is stated honestly: **it is justified by the API contract and the
malicious-ASPSP threat model, not by any locally observed breakage.** No exotic amount shape has
ever come through this deployment; what the measurement settles is that the hardening breaks no
real data, not that the hardening is needed against these six banks as they behave today.

Re-verified 2026-08-13 on a corpus roughly twelve times larger (244 cache entries, a seventh
bank): still zero anomalies of any kind, `map_transaction` still raised zero exceptions over every
cached transaction, every balance resolved to a finite `Decimal`.

### 4. Field alphabets — and the measurement that kills the obvious FITID whitelist

Scanned every string value inside every cached transaction:

- **C0, C1 and DEL control characters: zero occurrences** across every string field. Stripping
  them (decision 4a) breaks nothing that has ever existed here. (Re-verified 2026-08-13 over the
  twelve-times-larger corpus: still zero, C1 range included.)
- Memo/`NAME`-bound fields carry letters, digits, spaces, diacritics and the punctuation
  `% ( ) * , / : = . - _` — all already folded by `to_ascii`.
- `ACCTID`-bound values: IBANs (5 values, one bank) strictly `[A-Z0-9]`; uids strictly
  `[a-z0-9-]`; the derived `eb-<hex16>` form is `[a-z0-9-]` by construction.
- `FITID`-bound values are the real finding. `transaction_id`, where present, is strictly
  `[A-Za-z0-9]` — but it is a present-but-null key on most transactions. `entry_reference` is the
  `FITID` source for the large majority of observed transactions (`transaction_id`, where
  non-null, appears alongside it in most of the remainder) and its observed alphabet is
  `[A-Za-z0-9]` **plus `- . _ / |`**: Erste uses
  `-` and `/`, Millennium `-` and `|`, Wise `-` `.` and `_`. A `[A-Za-z0-9-]`-style whitelist —
  the first thing anyone would write — would reject real FITIDs at three of the six banks today.
  Re-verified 2026-08-13 on the larger corpus: the alphabet is unchanged, and decision 4's
  validation replayed over every real value passes 100% — zero FITID-source values fail
  printable-ASCII-no-controls, and every currency field matches `[A-Z]{3}`.

### 5. Response sizes bound a floor for decision 5's caps, not a ceiling

Over all 21 cached account-windows (windows 25–31 days): the largest whole-window response ever
observed here is well under 1 MB, and every observed window sits far below what a 100-page loop
implies. A 10 MB body cap and a 100-page cap therefore carry well over 10x headroom against
everything ever observed, while still bounding a malicious infinite-continuation loop to minutes
rather than forever. The observation base is thin — it
settles a floor, not a ceiling — which is why the constants are labelled policy picks (open
question 1).

**Re-measured 2026-08-13**, now with a real request log and a cache roughly twelve times larger:
still one-sided bounds, but no longer thin. Every page chain provably due to pagination stayed
within 2 pages; no account-window has ever needed more than 4 requests even under the worst
reading of the pre-`window` log lines (early lines predate the `window` field, so a multi-span
window and pagination cannot always be told apart there); the largest cache entry remains well
under 1 MB. The 100-page and 10 MB caps carry more than 25x headroom over everything ever
observed.

---

## Rejected options

### A. Do nothing, on the strength of §3–§4's clean data

**Rejected.** The clean data explains why nothing has broken yet; it says nothing about a
compromised ASPSP or aggregator, which is the audit's threat model. Findings 1 and 2 do not even
need the threat model: the leak is a plain bug against the log's own written contract, and the
uncapped sleep is a plain bug against the retry ladder's.

### B. A separate, higher cap for the `Retry-After` header path (e.g. 300s), to honour a genuine long value

**Rejected — the number would be invented.** No `Retry-After` value has ever been observed on this
machine (§1): zero log lines, and the cache stores only successes. The one hard limit that *is*
known, `ASPSP_RATE_LIMIT_EXCEEDED`, is never retried at all (rate-limit ADR decision 2), so the
clamp's worst case is a handful of too-early attempts under `_MAX_RETRIES = 5` — versus a hostile
value hanging a run for years or crashing it. And the log's header allow-list already keeps
`retry-after`, so if genuinely long values ever appear in ordinary use, raising the clamp becomes
an evidence-backed one-line change. Honouring a fabricated number today buys nothing that waiting
for a real one does not.

### C. Reject control characters instead of stripping them

**Rejected on proportionality.** §4 measured zero control characters across every real string
value scanned, so stripping breaks no real data and keeps the folded output deterministic — which the
matcher-token stability invariant needs. Rejecting would turn one hostile byte in one memo into a
failed account (or, done naively, a failed run) over a character the OFX file could never carry
anyway. Note the asymmetry with decision 4b: memo text is *display* data, where silent repair is
cheap; identity fields are not, and get the opposite treatment.

### D. Whitelist `FITID`/`ACCTID` against a tight alphabet like `[A-Za-z0-9-]`

**Rejected by measurement — this is the one the audit's fix would plausibly have shipped.** §4:
`entry_reference`, the majority FITID source, uses `- . _ / |` across Erste, Millennium and Wise.
The tight whitelist rejects real FITIDs at three banks on day one. The property actually required
is "cannot break the OFX writer or smuggle controls into the file": printable ASCII with no
controls, which every observed value satisfies with room for bank variation. `CURDEF` is different
— ISO 4217 is a real, closed contract, and §3 measured 100% conformance — so it alone gets the
strict `[A-Z]{3}`.

### E. Normalize (strip/replace) an invalid `FITID` or `ACCTID` instead of failing the account

**Rejected — this is the expensive direction.** A mutated `FITID` breaks the stable-FITID
invariant: GnuCash dedups re-imports on it, so the same transaction re-imports as new. A mutated
`ACCTID` orphans the GnuCash account (stable `BANKID`/`ACCTID` invariant). Both are silent damage
to the user's books in exchange for tolerating a value no bank has ever sent (§4). A loud
account-scoped failure costs one account's files for one run and is honest about why.

### F. Truncate silently when the page or size cap is hit

**Rejected on the coverage invariant.** Coverage advances when an account's transactions were
fetched and mapped (coverage ADR decision 2). A silently truncated statement would advance the
ledger over transactions never written, and the ledger claiming coverage it does not have is the
one failure mode that ADR names as worse than silence. Exceeding a cap is therefore an
account-scoped failure through the same reporting path as decision 3 — loud, coverage not
advanced, siblings unaffected.

### G. A live probe to observe real `Retry-After` values or the pages-per-account distribution

**Rejected — the probe would purchase information that changes no decision.** The two locally
unanswerable questions (§1) do not discriminate between designs: the clamp is correct whether real
Retry-After values are 1s or 3000s (option B shows why), and the caps are correct across the
entire plausible range of page counts (§5 gives >10x headroom over everything observed). Both
numbers will be measured *for free* by the accumulating fetch log — `retry-after` is already in
the header allow-list, and each continuation page is its own request line — provided decision 1
lands first so those lines carry no live session ids. A counted request against an ASPSP daily cap
is this project's unit of cost, and spending it to re-learn what the log will record anyway fails
that test. **`docs/probes.md` gets no new row**, and that is deliberate, not an omission.

---

## Decisions

Numbered as the issue's findings. The unifying principle for 3–5: **per-account failure
containment.** The existing invariant says one bank's failure must not cost another bank its
files; these extend it downward — one account's hostile data must not cost its sibling accounts
theirs — and every violation reports through the existing `BankFailure` path with
`scope="account"`. A malicious ASPSP should be able to cost at most its own bank's files, and
diagnostics must never cost a fetch (both existing invariants; nothing here weakens either).

### 1. `redact_uid_path` also masks `/sessions/{id}` — and the same change scrubs the existing log

Generalize the regex in `runlog.py` to mask `/sessions/([^/]+)` with the identical head-4/tail-4
shape and the identical ≤8-chars-pass-unmasked rule as `/accounts/`. Update
`tests/test_runlog.py:89`, which currently pins the unmasked behaviour. §2 shows the mask is
unambiguous on every real identifier and that the path regex is the entire logged surface — this
decision therefore rules out any broader *ongoing* log-scrubbing layer as unnecessary.

**Amended 2026-08-13.** The original second half — "merges before any fetch runs" — was already
unsatisfiable when the ADR was accepted: §1's correction found raw session ids on disk from runs
that predate the first measurement. The decision's second half is therefore a **one-off scrub of
the existing `state/fetch-log.jsonl`**: the implementing change rewrites the file's `/sessions/`
paths through the same extended redaction (append-only history preserved, only the id segments
masked), so the file's pasteable contract is restored for lines already written, not just future
ones. Shipped in [#40](https://github.com/skolima/gnucash-ofx/issues/40); the argument, the
post-scrub verification and the `splitlines()` trap review found in it are condensed in
[`decisions.md`](decisions.md). This is what makes the log safe to accumulate the evidence for open
question 2.

### 2. `_retry_delay` rejects non-finite values and clamps the header path to `_MAX_BACKOFF_SECONDS`

Guard the parsed header with `math.isfinite` and clamp the accepted value to
`_MAX_BACKOFF_SECONDS` (60.0) — the cap the backoff ladder already has, *reused, not re-picked*,
so there is exactly one answer to "how long will this tool ever sleep for one retry". Rules out a
separate header cap (option B) and rules out parsing HTTP-date `Retry-After` values, which
continue to fall through to the ladder as today. Worst case if a genuine long Retry-After exists
somewhere: up to `_MAX_RETRIES = 5` attempts arrive early and fail, bounded and visible in the
log; the never-retried `ASPSP_RATE_LIMIT_EXCEEDED` path is unaffected.

### 3. Amounts: broaden the guard, reject non-finite, and bring balances inside it

Three moves, one guard:

- The mapper guard at `run.py:1235` broadens from `except ValueError` to
  `except (ValueError, ArithmeticError, TypeError, AttributeError)`. `decimal.InvalidOperation`
  is an `ArithmeticError`; a wrong-typed `transaction_amount` raises `AttributeError` in
  `_amount_str`; `TypeError` covers the remaining wrong-shape cases. Rules out catching bare
  `Exception`: a `KeyboardInterrupt`-adjacent or programming error should still crash.
- The mapper rejects non-finite `Decimal`s (NaN, ±Infinity) by raising `ValueError` with a clear
  message, for amounts and balances both — so `Decimal("NaN")` can never reach
  `build_statement`'s `>= 0` comparison and `Infinity` can never serialize into `<TRNAMT>`. Rules
  out clamping or zeroing: an invented amount in a financial file is worse than a missing file.
- `extract_balance` and `_currency_from_balances` (run.py:1219/1264, currently outside any
  per-account try) move under the same per-account guard, same `BankFailure`, same scope.

§3's measurement is the honesty clause: every amount ever received here is canonical, so this is
contract-and-threat-model hardening, not a bug observed in the wild.

### 4. Strip controls in `to_ascii`; validate — never mutate — the fields that bypass it

- **(a)** `to_ascii` strips C0 controls and DEL alongside its existing `< 128` keep-filter. Strip,
  not reject (option C): §4 measured zero occurrences across every real value scanned, stripping keeps
  folding deterministic for the Bayesian matcher, and a whole-run abort over one byte fails
  proportionality.
- **(b)** The raw-passthrough fields are validated at the boundary: `ACCTID` and `FITID` must be
  printable ASCII with no control characters — deliberately *not* a tight whitelist, per option
  D's measurement — and `CURDEF` must match `[A-Z]{3}`. A violation is an account-scoped
  `BankFailure`, never a silent mutation (option E): the value the bank sent is wrong, and only
  the bank can send a right one. This decision rules out ever "fixing" an identity field in
  flight; the stable-`FITID` and stable-`BANKID`/`ACCTID` invariants stand untouched.

**Amended 2026-08-13 — the field enumeration above is incomplete, and that is the finding.** "`ACCTID`,
`FITID` and `CURDEF`" was a completeness claim, and review corrected it twice: the counterparty
number reaches `BANKACCTTO` unfolded (a control there reproduced this decision's own `ExpatError`
*after* coverage had advanced for every mapped account, so under `--combine` one crafted value cost
every bank in the run its file), and `BANKID` reaches the file raw as well — from *config*, where
`bank_id_for` only strips, upper-cases and truncates, which makes a typo a likelier source than a
hostile server rather than a rarer one. Shipped as five fields: four validated at the mapping step,
`BANKACCTTO` gated and **omitted** rather than failing the account, because libofx does not parse the
aggregate and the folded `MEMO` copy is what routes accounts. `compose_check_number` also had to move
its gate onto the *raw* reference, since folding now deletes controls and `R\x027` would otherwise
fold to the valid-looking `R7`. The transferable half is method, not the list: re-derive the
enumeration against the writer, never from a previous statement of it.

### 5. 100 pages per account-window, 10 MB per response body — exceeded means that account fails loudly

`iter_transactions` counts continuation pages and stops at 100 per account-window; the response
body is bounded at 10 MB before `response.json()`. Exceeding either raises, fails that account
through the decision-3 reporting path, and advances no coverage — silent truncation is ruled out
by option F. Both constants are **policy picks with a measured floor and no measured ceiling**,
labelled as such (the `LATE_BOOKING_MARGIN` precedent): §5's arithmetic is a largest-ever window
well under 1 MB, so the caps carry >10x headroom. *(2026-08-13:
the page cap originally rested on the transactions-per-window measurement because pages were
unmeasurable; the log has since measured them directly — provable pagination never exceeded 2
pages, no chain ever exceeded 4 requests, headroom now >25x. See §5.)* The definition-site comment
must say the constants are policy picks, and open question 1 names what re-tunes them.

### 6. CI actions pinned by commit SHA

`actions/checkout` and `astral-sh/setup-uv` in `.github/workflows/ci.yml:19,22` move from `@v7`
tags to 40-char commit SHAs with a `# v7` trailing comment. Verified 2026-08-12:
`.github/dependabot.yml` already runs a weekly `github-actions` group, so the pins stay current
without manual tending. The workflow has `permissions: contents: read` and no secrets — small
blast radius, which is what keeps this Low and keeps the fix this small.

---

## Cost to reverse

Low across the board, and worth recording per decision because "hardening" tends to be treated as
irreversible on principle:

- **1** — additive redaction; reverting re-opens the leak but loses nothing (already-written log
  lines were masked, which is the point). The information forgone — real session ids in old logs —
  is information the log's contract says it must never hold.
- **2, 5** — named constants with definition-site labels; each reversal or re-tune is a one-line
  change, and the fetch log is accumulating exactly the evidence that would justify one.
- **3, 4** — exception-handling breadth and boundary validation; reverting restores today's
  crash-the-run behaviour and nothing else. No identity field changes value in either direction,
  so no account orphans and no transaction re-imports — decision 4 rules out the only expensive
  mistake available in this area (option E).
- **6** — reverting a SHA pin to a tag is a one-line edit per action.

---

## Open questions

1. **Are 100 pages and 10 MB the right constants?** ~~Policy picks with a measured floor (§5) and
   no measured ceiling.~~ **Substantially settled 2026-08-13** by the first runlog census: on a
   real request log and a twelve-times-larger cache, provable pagination never exceeded 2 pages,
   no account-window chain exceeded 4 requests, and no entry approached 1 MB — the constants stand
   with >25x headroom and now rest on direct page counts, not the transactions-per-window proxy.
   Still a floor rather than a ceiling in principle; re-tune only against a future census, never
   against fresh probes. That census was hand-scripted for the second time to answer this —
   `tools/evidence/` now demonstrably wants a `runlog_census.py` (which must know that `attempt`
   in the log is 0-based; a first-attempt filter written as `attempt == 1` counts nothing).
2. **Do real `Retry-After` values ever exceed 60s in ordinary use?** Still unknown — the
   2026-08-13 census found zero 429s, zero retries and zero `retry-after` headers across every
   request ever logged, so the question remains genuinely open. **Settled by:** the first logged
   429 carrying the header; `retry-after` is already in the log's allow-list. Until then the clamp
   stands on option B's argument.
3. **Should `to_ascii` also strip C1 controls (0x80–0x9F)?** Likely moot — the existing `< 128`
   filter already excludes them post-NFKD — but "likely" is doing work there.
   **Partly answered by [#45](https://github.com/skolima/gnucash-ofx/issues/45), and the test this
   asked for is *not* in it.** What shipped: one C1 character is now handled deliberately rather
   than incidentally — NEL (U+0085) joins U+2028/U+2029 in `_WHITESPACE_CONTROLS` and folds to a
   *space*, because those three are separators decision 1 established that ASPSPs really do send,
   and dropping them silently joined two tokens. The rest of 0x80–0x9F still fall to the same
   `>= 128` branch as before. **Now settled**, by the test this asked for rather than by the
   prediction: `test_no_c1_control_survives_folding` asserts the whole 0x80–0x9F range, and the
   answer is that `to_ascii` needs no C1-specific strip — NEL keeps the word boundary it stands
   for, every other C1 control vanishes through the `>= 128` branch, and nothing in the range
   reaches the file. "Likely moot" turned out to be right; it is no longer doing the work.
   (2026-08-13: zero C1 bytes in any real cached value either, so nothing real was ever affected.)

No probe was run for any of these and none is planned — option G records why, and
[`probes.md`](probes.md) deliberately gains no row.

---

All six decisions have now condensed into [`decisions.md`](decisions.md) with their reversal costs
attached — decision 1 under *The run log masks session ids too*, decisions 2/3/5/6 under *A server
never picks how long we wait*, decision 4 under *Controls never reach the file*. This file stays for
the rejected options — option D's FITID-alphabet measurement above all — and for the measurements of
2026-08-12; the API-and-bank facts (no `Retry-After` ever observed, pagination depth, the response-size
floor) are in [`enable-banking.md`](enable-banking.md).

## References

- [#37](https://github.com/skolima/gnucash-ofx/issues/37) — the audit findings this decides.
- [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — the run log (decision 1's
  subject), `_MAX_BACKOFF_SECONDS` and the retry ladder (decision 2 reuses, not re-picks), the
  never-retry rule for `ASPSP_RATE_LIMIT_EXCEEDED` (bounds decision 2's worst case), and the
  `LATE_BOOKING_MARGIN` precedent for labelling policy values.
- [`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) — "only a
  successful fetch advances coverage", the rule that forces decision 5's fail-loudly; its *To
  verify* already flagged silent short-serving as the worst failure mode, which silent truncation
  would have reproduced client-side.
- [`decisions.md`](decisions.md) — *Partial success is reported, not thrown away* (the
  containment principle decisions 3–5 extend); the stable-`FITID` and `BANKID`/`ACCTID` identity
  rules (why decision 4 validates and never mutates).
- [`AGENTS.md`](../AGENTS.md) — the invariants (one bank's failure must not cost another bank its
  files; diagnostics must never cost a fetch) and the data policy this file's numbers follow.
- `SECURITY.md` — session ids classified as access-granting, the classification behind finding 1's
  Medium.
