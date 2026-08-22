# ADR: `tests/test_invariants.py` holds a manifest and two new tests; the nine invariants already pinned stay where they are

**Status:** Accepted and implemented — [#24](https://github.com/skolima/gnucash-ofx/issues/24).
Measurements below were taken 2026-08-09 by reading `AGENTS.md`'s "Invariants (do not break)"
section (lines 94-270) and the current test suite directly, file by file, plus targeted `grep`
over `tests/`. This is a pure code-structure question — no financial data is involved — so
`tools/evidence/` and the `local-evidence` agent do not apply; both are scoped to `state/`,
`cache/` and `state/fetch-log.jsonl`.
**Date:** 2026-08-09.
**Scope:** a new `tests/test_invariants.py` (two tests plus a manifest); one-line citation
comments/docstrings — matching each file's own existing convention — added to nine existing tests
across `tests/test_ofxout.py` (3), `tests/test_run.py` (2), `tests/test_cli.py` (1),
`tests/test_enablebanking_client.py` (3), `tests/test_cache.py` (2), `tests/test_runlog.py` (1),
`tests/test_enablebanking_mapper.py` (1) — 13 citations across 7 files for 9 candidates, some of
which are pinned by more than one test. Nothing under `src/` changes: no new precondition
function, no registry added to `run.py` (decision 4), no promotion of `run._known_acctid` /
`run._disambiguators` out of the private surface (that question is `adr-evidence-tool.md`'s to
answer, not this one's). `AGENTS.md` itself is not edited here — the prose stays exactly as it
is; what changes is which of its bullets have a machine check next to them.
**Issue:** [#18](https://github.com/skolima/gnucash-ofx/issues/18). Depends on nothing already
shipped as an ADR decision; rule (b)'s three call sites and their ordering are documented in
[`decisions.md`](decisions.md) under *"Whatever aborts the fetch must abort the dry run, from the
same code"* — cited below, not re-derived.

---

## Where this stands

- [x] **1.** The nine already-pinned candidates are cited in place — a one-line comment/docstring
      naming the `AGENTS.md` bullet each existing test already enforces — not moved, not
      duplicated ([#24](https://github.com/skolima/gnucash-ofx/issues/24))
- [x] **2.** `tests/test_invariants.py` holds a manifest (every candidate + rule (b), mapped to
      `file::test_name` or "pinned here") and exactly two new tests
      ([#24](https://github.com/skolima/gnucash-ofx/issues/24))
- [x] **3.** The manifest is self-checking: a parametrized test asserts every cited
      `module.test_name` still exists, so a rename or deletion elsewhere fails here instead of
      the citation rotting silently ([#24](https://github.com/skolima/gnucash-ofx/issues/24))
- [x] **4.** Candidate 7 (`dry_run_enablebanking` takes no client, no cache dir) is pinned on the
      signature, via `inspect.signature`, not by calling the function
      ([#24](https://github.com/skolima/gnucash-ofx/issues/24))
- [x] **5.** Rule (b) is pinned by a spy-based parametrized test over named precondition
      functions, reached through all three real entry points; not a registry added to `run.py`
      ([#24](https://github.com/skolima/gnucash-ofx/issues/24))
- [x] **6.** The "deliberately left unpinned" list, for this batch, is empty — **not a decision to
      ship**; recorded because the issue asks for the list explicitly and an empty one is still an
      answer, not an omission ([#24](https://github.com/skolima/gnucash-ofx/issues/24))

---

## Context

`AGENTS.md`'s Invariants section is roughly 40 bullets across lines 94-270 — dense enough that a
reviewer, human or agent, skims it rather than holding all of it in mind on every diff. The issue's
premise is that a mechanically assertable subset of those bullets should be pinned by tests whose
docstrings *name* the bullet, so that (a) an accidental reversal fails loudly instead of shipping,
and (b) what remains unpinned says exactly where review attention has to be manual.

Before writing anything, I read `AGENTS.md`'s Invariants section and the current test suite
directly, one candidate at a time, to find out how much of the issue's list already has a test and
how much genuinely does not. The answer is lopsided: **9 of the issue's 10 named candidates are
already covered**, by well-named, single-purpose tests scattered across five files — but none of
those tests says, in its own text, which `AGENTS.md` bullet it exists to protect. The connection
between a bullet and the test that pins it lives only in whoever wrote both, or in whoever is
willing to re-derive it by reading the test body. That is the actual cost the issue is naming: not
missing coverage, but coverage that cannot be found from the doc, or from the test, without
reading both and guessing.

Rule (b) is the one place that gap is not just inconvenient but structural. Five tests already
exercise `require_ordered_window` by name at all three entry points — but every one of them is a
hand-written, per-entry-point test of *that specific function*. Nothing today would notice a
*second* global precondition wired into only two of the three places, because nothing iterates
"the preconditions" as a set; each test only knows to check the one function its author remembered
to check. That is exactly the "remembered, not enforced" failure mode `AGENTS.md` already names in
its own bullet: *"Any new global precondition belongs in both entry points for the same reason."*

---

## Measured: what's already pinned, and what isn't

Method: for each of the issue's 10 candidates, read the relevant `AGENTS.md` bullet, then read the
test file(s) most likely to cover it and confirm the test exercises the real function (not a
reimplementation of its logic). For candidate 7 and rule (b), additionally `grep`ped the whole
`tests/` tree.

| # | Candidate (issue's wording) | Pinned by | Reimplements the code under test? |
|---|---|---|---|
| 1 | `newline=""` — no `\r\r\n` | `tests/test_ofxout.py::test_written_file_has_clean_crlf_line_endings` — reads raw bytes, asserts `b"\r\r\n" not in raw` and `b"\r\n" in raw` | No — reads the written file's bytes |
| 2 | `NAME`≤96 / `MEMO`≤390 / `CHECKNUM`≤12, drop not truncate | `tests/test_ofxout.py::test_compose_name_truncates_at_a_word_boundary`, `::test_compose_memo_caps_at_the_libofx_buffer`, `::test_compose_check_number_drops_an_over_long_reference` | No — each calls the real `compose_*` function |
| 3 | `LEDGERBAL` only when window ends today+, tag always present | `tests/test_run.py::test_a_closed_window_spends_no_balance_call`, `::test_a_live_window_still_carries_the_banks_ledger_balance` | No — parses the written OFX and asserts the `LEDGERBAL` value in both branches |
| 4 | stdout = file list only, rest on stderr | `tests/test_cli.py::test_stdout_is_the_written_paths_and_nothing_else` | No — captures real stdout/stderr from a run |
| 5 | `ASPSP_RATE_LIMIT_EXCEEDED` never retried, matched on `error`, other 429s keep the ladder | `tests/test_enablebanking_client.py::test_the_daily_cap_is_not_retried`, `::test_api_error_code_reads_error_not_message`, `::test_retries_on_429_then_succeeds` | No — `test_api_error_code_reads_error_not_message` builds a response with the string in `message` and a *different* code in `error`, so it can only pass if the real field is matched |
| 6 | settled month has no TTL; only the moving tail expires | `tests/test_cache.py::test_a_settled_month_does_not_expire`, `::test_a_stale_tail_is_refetched_but_a_settled_month_is_not` | No — calls the real `chunk_ttl` |
| 7 | `dry_run_enablebanking` takes no client, no cache dir | **nothing** — `grep -rl "import inspect\|inspect.signature" tests/` returns zero files | — |
| 8 | filenames group over every account in the session | `tests/test_run.py::test_surviving_sibling_keeps_the_filename_it_has_in_a_full_run`, `::test_v1_state_cannot_reserve_the_group_slot` | No — its docstring already states the invariant almost verbatim |
| 9 | `RunLog` swallows `OSError`, cannot fail a run | `tests/test_runlog.py::test_a_broken_log_never_breaks_a_fetch` — points the log at a path where a directory is expected, then calls `record_run`/`record_request` with no `try`/`except` around either call | No — a real `OSError`-inducing condition, no mock |
| 10 | credits positive, debits negative | `tests/test_enablebanking_mapper.py::test_debit_is_negative_credit_is_positive` | No |
| b | precondition reachable from all three entry points | **behaviorally** covered 5×: `tests/test_run.py::test_fetch_enablebanking_rejects_a_reversed_window`, `::test_dry_run_rejects_a_reversed_window`; `tests/test_cli.py::test_fetch_rejects_a_reversed_window`, `::test_dry_run_still_aborts_on_a_reversed_window`, `::test_a_reversed_window_is_still_refused` — but **not structurally**: every one of the 5 only knows to check `require_ordered_window` by name | — |

Confirmed against the code itself, not just the tests: `require_ordered_window` is called directly
in `run.py` at line 1358 (`fetch_enablebanking`) and line 1637 (`dry_run_enablebanking`), and in
`cli.py` at line 432, inside `_parse_window` — whose own docstring already explains the ordering
rule (b) names: *"`_cmd_fetch` looks up the public IP over the network and opens the run log before
reaching the orchestration layer's own `require_ordered_window`, so a reversed window left to be
caught there is reported as a rate-limit-mode problem instead."* `_dispatch` calls `_parse_window`
before `_cmd_fetch` runs, so the ordering already holds; nothing here changes it.

One structural detail that matters for decision 5: `cli.py` imports the function by name —
`from gnucash_ofx.run import (..., require_ordered_window, ...)` (line 32) — which binds a
*separate* module-level name in `cli.py`, pointing at the same function object at import time.
Monkeypatching `gnucash_ofx.run.require_ordered_window` after that import does not change what
`cli._parse_window` calls, because Python resolves a bare name against the calling module's own
globals, not the module it was imported from. A spy that only patches `run.require_ordered_window`
would silently fail to observe a CLI-only omission — exactly the case rule (b) exists to catch.

---

## Decisions

### 1. The nine already-pinned candidates are cited in place, not moved or duplicated

Each of the 13 existing tests in the table above gains a one-line citation of the `AGENTS.md`
bullet it enforces, in whichever form (`#` comment or docstring) that file already uses for its
other tests — `test_ofxout.py`, `test_runlog.py` and `test_enablebanking_client.py` use leading
comments; `test_cli.py` already uses docstrings on some tests. For example:

```python
def test_written_file_has_clean_crlf_line_endings(tmp_path: object) -> None:
    # AGENTS.md: "Write OFX with newline=''." — default text mode on Windows turns "\r\n" into
    # the malformed "\r\r\n" this asserts against.
    ...
```

No test body changes. This is the option that costs nothing beyond the citation itself; see
*Rejected options* A and B for why relocating or duplicating these tests was not the alternative.

### 2. `tests/test_invariants.py` holds a manifest plus exactly two new tests

The manifest is a small module-level data structure — one entry per candidate (1-10) plus rule
(b) — each carrying a short label, the `AGENTS.md` bullet it names, and either a
`(module, test_name)` pair for a candidate pinned elsewhere or `"pinned here"` for the two that are
local. This is the file a reviewer opens to see the whole picture in one place, per the issue's own
ask (*"the two can be read against each other"*), without moving or duplicating a single existing
test.

The file's own docstring states plainly, at the top, the one fact decision 6 below expands on:
every one of the issue's 10 named candidates, plus rule (b), is assertable without reimplementing
the function it pins — so this file's "deliberately left unpinned" list is empty for this batch,
and says so rather than omitting the section.

### 3. The manifest is self-checking

A test in `test_invariants.py`, parametrized over every manifest entry that names an external
`(module, test_name)`, imports the module and asserts `getattr(module, test_name)` exists and is
callable. This does not re-test the invariant — it only proves the citation still points at
something real. Without it, a rename of e.g.
`test_a_settled_month_does_not_expire` in `test_cache.py` would silently orphan its row in the
manifest, and the manifest would keep claiming a pin that no longer resolves. This check costs one
`getattr` per row and never inspects what the cited test does, so it does not risk becoming a
reimplementation of anything.

What it does not check: whether the cited test's *docstring/comment still matches* the `AGENTS.md`
wording it quotes. `AGENTS.md`'s prose can be reworded by a future `doc-condenser` pass without the
citation string being touched, and catching that would mean parsing `AGENTS.md`'s prose in a test —
lower value than the behavioral check above, and exactly the kind of fragile, cosmetic assertion
the issue's "worse than no test" line is warning against. Left as an accepted cost; see
*Consequences*.

### 4. Candidate 7 is pinned on the signature, not by calling the function

```python
def test_dry_run_takes_no_client_and_no_cache_dir() -> None:
    """AGENTS.md: "`dry_run_enablebanking` takes no client and no cache dir" — not a flag threaded
    through `fetch_enablebanking`, a fact about the function's own parameter list."""
    params = inspect.signature(dry_run_enablebanking).parameters
    assert "client" not in params
    assert "cache_dir" not in params
```

This is the one candidate the issue itself frames as "assertable on the signature itself," and it
is: the invariant is about what the function *cannot* be handed, which a signature inspection
states directly without invoking anything, needing no fixtures, and without duplicating any of
`dry_run_enablebanking`'s actual logic.

### 5. Rule (b) is pinned by a spy-based parametrized test over named precondition functions

`PRECONDITIONS = ("require_ordered_window",)` — one tuple, defined once in `test_invariants.py`,
shared by the manifest's rule-(b) row and by the test below, so there is exactly one list to
extend when a second precondition is added, not two.

```python
@pytest.mark.parametrize("name", PRECONDITIONS)
def test_precondition_reached_from_all_three_entry_points(
    name: str, monkeypatch: pytest.MonkeyPatch, ...
) -> None:
    """AGENTS.md: "Any new global precondition belongs in both entry points for the same reason" —
    and the CLI is the third. Reached, not merely present: each entry point is invoked for real,
    with a spy substituted for the named precondition, through the same fixtures the existing
    reversed-window tests already use."""
    calls = {"run": 0, "cli": 0}
    monkeypatch.setattr(run, name, lambda *a, **k: calls.__setitem__("run", calls["run"] + 1))
    monkeypatch.setattr(cli, name, lambda *a, **k: calls.__setitem__("cli", calls["cli"] + 1))
    # invoke fetch_enablebanking, dry_run_enablebanking and the CLI's _parse_window with a valid
    # window, using the fixtures test_run.py's and test_cli.py's own reversed-window tests already
    # set up, and assert each incremented its side of `calls`.
```

Patching **both** `run.<name>` and `cli.<name>` is load-bearing, not defensive style — the
structural detail in *Measured* above is exactly the failure this test would otherwise miss: a
patch of `run.require_ordered_window` alone cannot observe whether the CLI's own bound name was
ever called, because `cli.py`'s `from ... import ...` already resolved to the original function
object before the test ever runs.

This does not replace the five existing reversed-window tests — those prove each precondition
*rejects a reversed window correctly*; this proves each precondition is *reached at all* from every
entry point, which is the property that generalizes to a function whose specific check nobody has
written a per-site test for yet.

### 6. The "deliberately left unpinned" list, for this batch, is empty

Every one of the issue's 10 named candidates, plus rule (b), clears the "assertable without
reimplementing the code under test" bar — decisions 1-5 above account for all of them. So the
answer to the issue's own requirement — *"the list of what was deliberately left unpinned is part
of the deliverable"* — is that the list is empty this time, stated as a fact in
`test_invariants.py`'s module docstring rather than left for a reader to infer from its absence.
This is not a decision to ship as code; it is a finding this ADR is committing to write down
honestly rather than invent a placeholder example to fill a section.

### 7. Untouched

`AGENTS.md`'s prose (unedited — the Invariants section stays exactly as written); every other
invariant in that section not named by the issue's 10 candidates or rule (b) (~30 bullets,
deliberately out of scope — see *Constraints*); `run._known_acctid` / `run._disambiguators`'
private status (`adr-evidence-tool.md`'s open question, not reopened here); every file under
`src/`.

---

## Rejected options

### A. Relocate the nine already-pinned tests into `tests/test_invariants.py`

**Rejected.** `AGENTS.md`'s own TDD convention puts a fetcher/mapper's tests next to the module it
tests; moving `test_debit_is_negative_credit_is_positive` out of `test_enablebanking_mapper.py`
breaks that co-location for zero new coverage, churns git blame on tests that are already
well-established and well-named, and risks a merge conflict with any in-flight PR touching those
same files — all to gain nothing decision 1's one-line citation does not already provide.

### B. Duplicate the nine already-pinned tests into `tests/test_invariants.py`

**Rejected**, and directly by the issue's own constraint: a test that reimplements the code under
test is worse than no test, and two tests asserting the same behavior in two files is a milder
version of the same problem — pure maintenance cost, with the added risk that the two drift (one
gets updated when the code changes, the other doesn't), which is a worse failure mode than the
"incidental, unattributed" coverage this ADR is fixing.

### C. A `_GLOBAL_PRECONDITIONS: list[Callable]` registry in `run.py`, iterated by all three entry points

Would make the *next* precondition addition literally impossible to wire into only two of three
places — add it to the list once, all three sites pick it up automatically — which is a genuinely
stronger guarantee than decision 5's spy, whose `PRECONDITIONS` tuple is still a list a human must
remember to extend.

**Rejected for now.** It is a production-code change to `run.py` and `cli.py` motivated entirely by
a single-member list today; `require_ordered_window` is still the only precondition, so there is
nothing yet for the registry to iterate that a direct call does not already do more simply. This
ADR does not touch `src/` at all (per the issue's framing — a test file, not a refactor), and
introducing new production structure to make a test easier to write the *second* time is the kind
of premature generalization the rest of this repo's ADRs argue against elsewhere. Recorded as the
first thing to reach for if a second precondition is ever added and the spy's tuple turns out to be
forgotten in practice — see *Open questions*.

### D. Prove reachability by static inspection (`co_names` / AST) instead of a runtime spy

Considered as a way to avoid needing fixtures at all: assert the literal name
`"require_ordered_window"` appears in `fetch_enablebanking.__code__.co_names`,
`dry_run_enablebanking.__code__.co_names` and `cli._parse_window.__code__.co_names`. Cheaper to
write today, since all three call sites happen to call the precondition directly with no
indirection.

**Rejected.** It is brittle to a legitimate refactor that changes nothing observable: if a future
change wraps several preconditions behind one shared helper (`_check_globals(date_from, date_to)`)
called from each entry point, the helper's own `co_names` would contain the precondition name while
each entry point's no longer would — a false failure on code that is provably still correct. A gate
that can fail on a change that broke nothing is worse than the silence it replaces, because the
fix people reach for is disabling the gate, not fixing the (non-existent) bug. A runtime spy
observes what actually executes, which survives that refactor unchanged.

---

## Consequences

**Accepted costs.**

- **Two places carry the same citation** for each of the 9 already-pinned candidates: the comment
  at the test itself (decision 1) and the manifest row (decision 2). This is citation duplication,
  not test-logic duplication — the two can only drift in *wording*, and decision 3's self-check
  only catches the case where they drift into pointing at nothing at all, not a wording mismatch.
  Judged acceptable because the failure mode of a stale comment is "misleading," not "silently
  broken invariant," which is the harm the issue is actually trying to prevent.
- **The `PRECONDITIONS` tuple in decision 5 is still a remembered list**, one level up from the
  five hand-written call-site tests it replaces — cheaper to extend (one tuple instead of writing
  new tests at three sites) but not structurally incapable of being forgotten. See *Open questions*.
- **`inspect.signature`-based tests are shallow by design**: candidate 7's test would not catch a
  `client` parameter renamed to `eb_client` while still accepting a client object — it checks for
  the literal names in the current signature, which is what the invariant actually says
  (`AGENTS.md`: *"takes no client and no cache dir"*), not a semantic property beyond it.

**Cost to reverse: none beyond deleting what was added.** Everything here is additive to `tests/`;
nothing under `src/` changes, no on-disk format changes, no consent spent, no request made. Deleting
`tests/test_invariants.py` and reverting the 13 citation comments restores the exact status quo —
the 9 candidates stay exactly as covered as they are today, incidentally.

---

## Open questions

**1. Is a single remembered tuple (decision 5) an acceptable terminus, or does rule (b) need to
escalate to the registry (rejected option C) once a second precondition actually exists?**
Not settleable by measurement — there is no data on this machine that predicts whether a future
contributor will remember to extend one tuple in one file. This is a judgment call for whoever adds
the second global precondition: if extending `PRECONDITIONS` at that point is done without being
prompted, the spy has done its job and stays; if it is forgotten even once — caught by review or,
worse, not caught — that is the concrete signal to spend the registry's one-time cost in `run.py`
and `cli.py`. Recorded here so that decision does not have to be re-argued from nothing when the
day comes.

**2. Should the citation comments (decision 1) be checked against `AGENTS.md`'s actual wording, to
catch drift when the prose is reworded?**
Also not a measurement question — decision 3 already covers the failure mode that actually matters
(a citation pointing at a test that no longer exists); checking wording match would mean parsing
`AGENTS.md`'s prose inside a test, which trades a small, real gap for a new, larger one for fairly
little benefit. Left as an accepted cost (see *Consequences*) rather than pursued further; revisit
only if stale citations turn out to be a recurring, noticed problem rather than a theoretical one.

---

On acceptance: because this ADR is about test infrastructure rather than product behavior, its
decisions do not condense into [`docs/decisions.md`](decisions.md), which holds only decisions
about what the tool does to a user's data. They condense into a short note in `AGENTS.md`'s
Conventions section pointing at `tests/test_invariants.py` as the manifest of which invariants are
machine-checked, and this file stays for the rejected options and the per-candidate measurement
that produced the manifest.

## References

- [#18](https://github.com/skolima/gnucash-ofx/issues/18) — the issue.
- `AGENTS.md` — "Invariants (do not break)", lines 94-270; the specific bullets cited above are
  quoted verbatim from that section as it reads on 2026-08-09.
- [`decisions.md`](decisions.md) — *"Whatever aborts the fetch must abort the dry run, from the
  same code"* — the origin of rule (b)'s three-entry-point requirement and the CLI's ordering
  relative to the public-IP lookup and the run log.
- [`adr-evidence-tool.md`](adr-evidence-tool.md) — decision 7's open question on whether
  `run._known_acctid` / `run._disambiguators` should be promoted out of the private surface, left
  open there and not reopened by this ADR.
- `tests/test_ofxout.py`, `tests/test_run.py`, `tests/test_cli.py`,
  `tests/test_enablebanking_client.py`, `tests/test_cache.py`, `tests/test_runlog.py`,
  `tests/test_enablebanking_mapper.py` — the 13 existing tests cited in the manifest.
