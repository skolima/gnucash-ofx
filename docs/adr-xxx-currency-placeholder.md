# ADR: `"XXX"` is normalized to no currency at the boundary, not trusted downstream

**Status:** Accepted and implemented — [#29](https://github.com/skolima/gnucash-ofx/issues/29).
Found incidentally while live-probing Alior's rate limit for
[#9](https://github.com/skolima/gnucash-ofx/issues/9) on 2026-08-10, then run to ground with a
local-evidence pass over `state/`, `cache/` and `fetch-log.jsonl` on this machine, same day. No
live request was needed to settle anything below — see *Open questions*.
**Date:** 2026-08-10.
**Scope:** A single `currency_or_none()` normalizer, added to `src/gnucash_ofx/models.py` (the one
module both `enablebanking.py` and `state.py` can import without a cycle — `enablebanking.py`
already imports `state.LinkedAccount`, so `state.py` cannot import back from it), applied at
**three** ingestion points, not the two originally scoped: `enablebanking.py`'s `linked_accounts()`
(`POST /sessions`, currently `enablebanking.py:290`), `run.py`'s `_currency_from_balances()`
(`/balances`, currently `run.py:354-360`), and — added during implementation, not in the original
scope — `state.py`'s `_accounts_from_payload()` (currently `state.py:170`), which re-normalizes on
every load from disk. That third site turned out to be load-bearing for a claim this ADR already
made: decision 1's "self-heals... no migration step" is only true if a file that already has
`"XXX"` persisted from before this fix stops reading as `currency_known` on its very next load —
and `linked_accounts()` only runs at link time (`complete_link`, `run.py:637-658`), never at fetch
time, so without the state-load boundary the two already-corrupted Alior connections would stay
broken until someone re-links them by hand. `_accounts_from_payload()` is not itself a new
ingestion point from an ASPSP's perspective, but the value it reads was never validated when it
first landed on disk, so it is exactly the same "untrusted string, must not be handed downstream
as fact" case as the other two.

Everything downstream of all three sites is still explicitly **not** touched, because it is already
correct for an absent currency and only needs to stop being handed a currency that lies:
`currency_known` (`run.py:1168`), `_account_balances`'s skip condition (`run.py:759`),
`fetch_bank`'s discovery-fallback assignment (`run.py:1197`), the same-currency sibling lookup
`_unfetched_siblings` (`run.py:1303-1307`), and the dry-run filename prediction in
`_planned_files` (`run.py:1529-1551`). `map_transaction()`'s mismatch check
(`enablebanking.py:346-357`) is explicitly untouched — see rejected option C. `LinkedAccount`'s
shape, `cache.py`, `ofxout.py` and the state schema version are all untouched: this is a
value-level fix at the points a currency string is read as fact, not a schema change — the state
file format is identical, and a currently-corrupted `state/*.json` needs no migration, just a
normal load.
**Issue:** [#28](https://github.com/skolima/gnucash-ofx/issues/28). Depends on the
currency-discovery fallback built by
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) decision 4 (implemented in
[#11](https://github.com/skolima/gnucash-ofx/issues/11)) and recorded in
[`decisions.md`](decisions.md) under *`LEDGERBAL` only when the window is still open* — this ADR
routes a case that mechanism was never designed to see into the existing path, rather than building
anything new. Also depends on the link-time capture decided in `decisions.md` under *Everything the
link response carries is captured*, which is what makes the bogus value durable across every
closed-window fetch until the next re-link.

---

## Where this stands

- [x] **1.** Normalize a currency string to absent at every point it enters as fact —
      `linked_accounts()`, `_currency_from_balances()`, and `_accounts_from_payload()` (added
      during implementation) — not at any downstream consumer —
      [#29](https://github.com/skolima/gnucash-ofx/issues/29)
- [x] **2.** The check matches the literal string `"XXX"`, not a full ISO 4217 code table —
      [#29](https://github.com/skolima/gnucash-ofx/issues/29)

---

## Context

Enable Banking's currency for an account can be known in exactly two ways: captured verbatim at
link time from `POST /sessions` (`linked_accounts()`, `enablebanking.py:266-300`, storing whatever
`entry.get("currency")` reports via `_str_or_none` with no validation against real ISO 4217), or
discovered later from `/balances` when link time recorded nothing (the fallback
`adr-aspsp-rate-limit-domain.md` decision 4 built). `fetch_bank` treats a non-empty stored string as
settled: `currency_known = bool(stored and stored.currency)` (`run.py:1168`), and
`_account_balances` uses exactly that to skip the `/balances` call outright whenever the window has
already closed (`run.py:759`, `not ledger_balance_wanted and currency_known`) — which,
per `adr-aspsp-rate-limit-domain.md` §1, is the *common* case: 121 of 154 fetches in the local cache
had a `date_to` already in the past. Skipping `/balances` there is deliberate and correct when the
stored currency is real; nothing checks that it is.

**What broke it.** A re-link of `alior` and `alior_kantor` on 2026-08-10 made `POST /sessions`
report `currency: "XXX"` — ISO 4217's own reserved code for "no currency" — for every account in
both connections (5 in `alior`, 3 in `alior_kantor`; `state/alior.json` and
`state/alior_kantor.json` both show it on all of them). `"XXX"` is a non-empty string, so
`currency_known` reads `True`, `/balances` is never called for a closed window, and the account's
authoritative currency falls through to `stored.currency` — `"XXX"` — at the fallback assignment
(`run.py:1197`). `map_transaction()` then compares each transaction's real currency (EUR or PLN,
here) against the account's `"XXX"` and raises, by design (`enablebanking.py:346-351`: *"a
transaction reporting a different currency means it was grouped under the wrong account, so we
raise rather than silently mix currencies"*) — reproduced live, 8 of 8 fetch rounds today, all
requesting the same closed window (`2026-07-01..2026-07-31`), zero `/balances` requests attempted
(confirmed absent from `fetch-log.jsonl`), zero OFX files written for either of the two accounts
that had transactions in that window.

**Why "silently corrupting" is the right word, not "loudly failing."** The raise is caught and
surfaces as a `BankFailure` — but with the message *"the bank's transaction data could not be
read"* (`run.py:1221`), which is true of the symptom and wrong about the cause: nothing about the
bank's transaction data is unreadable, the tool's own stored currency is bogus. A user seeing that
message has no reason to suspect a re-link-day placeholder rather than an ASPSP data problem. And
for the six accounts among the eight that had *no* transactions in the closed window requested
today, there is no raise, no failure, no file, and nothing else to notice: `stored.currency` stays
`"XXX"` in `state/*.json`, `currency_known` stays `True`, and every subsequent closed-window fetch
will keep trusting it and keep skipping `/balances` — silently, indefinitely, until the next
re-link happens to overwrite it or a transaction finally lands in a requested window and turns the
silence into a misleadingly-labeled failure.

**What that costs, concretely.** `alior` and `alior_kantor` are two of the three connections
`adr-coverage-ledger-and-warnings.md` names as serving only ~90 days of history before an ASPSP
`400`s — at those, a missed window is not a chore deferred, it is data that ages out and becomes
unobtainable from the API at any price. This bug sits exactly there: every closed-window fetch at
these two connections is either failing outright (visibly, but for the wrong stated reason) or
silently producing no file while the horizon keeps moving forward underneath it.

**The discovery mechanism this bypasses is not itself broken.** The cache holds 8 `bal-*.json`
responses from 2026-08-09 — one session generation earlier, same accounts by institution and count,
un-cross-referenceable by `uid` because Enable Banking regenerates it on every re-link — and every
one reports a real ISO currency (GBP, EUR, PLN, USD for `alior`; USD, PLN, EUR for `alior_kantor`),
none `"XXX"`. `/balances` is a reliable source for these accounts; the bug is that `"XXX"` reads as
"already known" and the call structurally never happens, not that the call would have returned
garbage. Among the six banks currently linked, only `alior` and `alior_kantor` show `"XXX"` — of the
three state files that even have a `currency` field (`wise_business` is the third; the other three
predate the field entirely), `wise_business`'s accounts all carry real ISO codes. This is
Alior-specific today, not a general schema problem, though nothing rules out a different ASPSP doing
the same in the future — see *Open questions*.

---

## Decision

### 1. Normalize a currency string to absent at every point it enters as fact — not at any downstream consumer

The fix lives at `linked_accounts()` (`enablebanking.py:290`, the `POST /sessions` account object),
`_currency_from_balances()` (`run.py:354-360`, the `/balances` response), and
`_accounts_from_payload()` (`state.py:170`, loading a previously-persisted `state/*.json`) — the
three places an untrusted currency string crosses into anything this tool treats as fact. The third
was added during implementation, not scoped originally; see *Scope* above for why it turned out to
be required, not optional.

**Commits to:**

- A currency string is validated once, at each boundary, via one shared `currency_or_none()`
  helper (`models.py` — the only module both `enablebanking.py` and `state.py` can import without a
  cycle). Downstream, "`None` means unknown" already means exactly that everywhere it is consumed
  today — `currency_known` (`run.py:1168`), the fallback assignment (`run.py:1197`), the
  same-currency sibling lookup (`run.py:1303-1307`), and the dry-run filename prediction
  (`run.py:1529-1551`) — because each of those was already written correctly for the "currency
  absent" case (the pre-currency state schema has always produced it) and simply never exercised
  against a currency that lies. None of the five needs to change.
- All three boundaries, not one. `_currency_from_balances()` and `_accounts_from_payload()` each
  read the same shape of untrusted string from a different source and, before this fix, validated
  neither. Fixing only `linked_accounts()` closes the bug for a *future* re-link — it enters through
  link-time state — but leaves two things unaddressed: the identical trust gap in the mechanism that
  is supposed to be the fallback of record for exactly this kind of failure (`/balances`), and the
  fact that `alior` and `alior_kantor`'s state files already have `"XXX"` on disk *today*, which
  `linked_accounts()` alone cannot touch, because it only runs at link time. Nothing in the local
  evidence shows `/balances` has ever returned `"XXX"` for these accounts (the eight cached balance
  responses from the day before the re-link are all real ISO currencies); the point is closing every
  boundary a bogus value can persist through, not reacting to a second observed failure.

**Rules out:**

- Special-casing inside `currency_known`, `_account_balances`, the fallback assignment, the
  disambiguator, or `_planned_files` — a currency-shaped check does not belong scattered across the
  five call sites that read `stored.currency`, each for a different purpose, when one normalization
  at each of the three ingestion points makes all five already-correct.
- Any change to `LinkedAccount`'s shape or to the state file version. This is a value at parse time,
  not a schema change; existing `state/*.json` files with a good currency are unaffected, and a file
  currently holding `"XXX"` self-heals the next time an account with `currency_known == False`
  triggers a `/balances` call and it succeeds — no migration step.

### 2. The check matches the literal string `"XXX"`, not a full ISO 4217 code table

**Commits to:** the minimal fix for the observed problem — one string comparison shared by all
three boundaries, no code table to fetch, ship or keep in sync with future ISO 4217 revisions.

**Rules out, for now:** rejecting any other non-empty string that happens not to be a real currency
code. A second ASPSP inventing a *different* bogus placeholder would not be caught by this fix.

**Reasoning.** `"XXX"` is not an arbitrary bad value to pattern-match defensively against — it is
ISO 4217's own reserved code meaning "no currency," which is precisely what Enable Banking is
reporting for these accounts. Treating that one documented meaning as the trigger is a correctness
fix for a known, named condition, not a heuristic. Validating against a maintained ISO 4217 table
would be defending against a different, so-far-hypothetical failure — a different ASPSP returning a
different bogus code that is not `"XXX"` — and conflating the two turns a one-line fix for an
observed bug into a small dependency for a problem nobody has seen. See *Open questions* for when
that call should be revisited.

---

## Rejected options

### A. Do nothing

**Rejected.** This is not a theoretical risk: 8 of 8 fetch rounds against `alior` and
`alior_kantor` failed or silently mis-recorded coverage today, 2026-08-10, and both connections are
among the ~90-day-horizon banks where a missed window is unrecoverable, not merely deferred.

### B. Special-case `"XXX"` only at the `currency_known` check (`run.py:1168`)

**Rejected.** The narrowest possible change, and it patches only the one call site the *current*
bug happens to go through — but `stored.currency` is read directly at, at minimum, four other
sites: the discovery-fallback assignment (`run.py:1197`), the same-currency sibling lookup
(`run.py:1303-1307`), and the dry-run filename prediction (`run.py:1529-1551`, `_planned_files`),
which would keep receiving `"XXX"` verbatim and keep predicting a `"_XXX_"` filename with **no
warning at all** — dry-run's entire value is being trustworthy without spending a request, and this
fix would leave it silently wrong in exactly the same way the real fetch currently is. It also does
nothing for the identical, currently-unobserved gap in `_currency_from_balances()` described in
decision 1 — a fix at this granularity does not even reach the discovery path it is meant to
protect.

### C. Relax the mismatch check in `map_transaction()` to let a transaction's real currency override a known-bogus stored value

**Rejected.** This treats the symptom — the raise — not the cause, which is that a bad value was
trusted as authoritative in the first place. The docstring at `enablebanking.py:346-351` states
exactly why the check exists: a transaction reporting a different currency than the account means it
was grouped under the wrong account, and the raise is what catches that class of bug. Weakening it
to tolerate one known-bad stored value would tolerate the same mismatch for a transaction that
really *was* grouped under the wrong account, with nothing inside `map_transaction()` able to tell
the two cases apart. It would also leave `"XXX"` sitting in `state/*.json` and read verbatim by
every call site listed under option B — unfixed, only no longer loud.

### D. Normalize `"XXX"` to absent at every external-data boundary

**Accepted** — decisions 1 and 2, above. Implemented at three boundaries, not two; see *Scope*.

---

## Cost to reverse

**Low.** Decision 1 is a small, local change — a few lines split across the three call sites
already reading untrusted external or previously-untrusted persisted JSON, no dataclass or
state-schema change. Nothing here touches `FITID`, `BANKID`, `ACCTID`, the cache key or the state
schema, so reversing it orphans no account and re-imports no transaction.

If a future ASPSP turns out to use `"XXX"` to mean something other than "no currency," the failure
mode of this decision being wrong is cheap and self-announcing rather than silent: the account's
stored currency reads as absent, which routes it onto the already-proven discovery fallback
(`adr-aspsp-rate-limit-domain.md` decision 4) on every closed-window fetch — paying one `/balances`
call per fetch instead of amortizing it once, the same cost a legacy pre-currency-schema session
pays today — not silent data corruption. Reverting is deleting the normalization at the three call
sites and the shared helper; nothing downstream needs to change back, because nothing downstream
changed in the first place.

---

## Open questions

**Should the check generalize from the literal `"XXX"` to full ISO 4217 validation?**

What would settle it: a future entry in `state/*.json` (or, once decision 1 ships, a `/balances`
response) carrying a *different* non-ISO placeholder from some other ASPSP. Nothing in today's
local evidence shows this has happened: among the six banks currently linked, only `alior` and
`alior_kantor` are affected (finding 4 above), and both report the one documented ISO reserved code,
not an arbitrary string. Generalizing now would be solving a problem not yet observed, and the cheap,
self-announcing failure mode described under *Cost to reverse* is exactly what makes waiting for it
the right call — in the same spirit as `adr-aspsp-rate-limit-domain.md`'s *"Parked: what closes
this"*: don't chase it, be ready to widen decision 2 the day a second bogus code actually shows up
on disk. This does not need a live probe; it needs nothing but noticing the next time it happens
during ordinary use.

---

Decisions 1 and 2 have condensed into [`decisions.md`](decisions.md) as one entry — a currency
string is normalized to absent at the boundary where it enters from ASPSP JSON, never trusted
downstream on the strength of being non-empty — and this file stays for the reproduction, the
rejected options, and the boundary argument behind decision 1.

The live verification run against the real `alior` connection before merge (`probe-xxx-currency-fix`
in `fetch-log.jsonl`) has its own row in [`probes.md`](probes.md); it verified this ADR's fix, it
did not inform the design decision above, which local evidence alone already settled.
