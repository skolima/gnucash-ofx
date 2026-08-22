# ADR: a Linux CI step runs generated OFX through real libofx, and says what it cannot catch

**Status:** Accepted and implemented — [#26](https://github.com/skolima/gnucash-ofx/issues/26).
All measurements below were taken 2026-08-10 by reading
`.github/workflows/ci.yml`, `docs/testing.md`, `AGENTS.md` and `tests/test_ofxout.py` directly —
no financial data is involved (no `state/`, `cache/` or `fetch-log.jsonl` claim is made), so
`tools/evidence/` and the `local-evidence`/probe-designer path do not apply, the same reasoning
[`adr-invariant-tests.md`](adr-invariant-tests.md)'s method note gives for a pure code-structure
question.
**Date:** 2026-08-10.
**Scope:** a new `tests/test_ofx_conformance_linux.py`; one new step in the existing `checks` job
in `.github/workflows/ci.yml`, inserted before the `Tests (pytest)` step; a short addition to
`docs/testing.md` pointing at the new automated check. Does **not** touch: `src/` (nothing here
changes program behavior); `.claude/agents/ofx-conformance.md` (the Windows/manual procedure,
already shipped as [#17](https://github.com/skolima/gnucash-ofx/issues/17)); `decisions.md` (no
decision here changes what the tool does to a user's file — see the closing line). `AGENTS.md`'s
`ofx-conformance` bullet (lines 99-100) is **flagged for `doc-condenser`, not edited here** — it
should note the new Linux CI job exists *alongside* the manual Windows check, not instead of it.
**Issue:** [#19](https://github.com/skolima/gnucash-ofx/issues/19). Depends on nothing already
shipped as an ADR decision. Inherits three constants it verifies but does not re-derive: `NAME`
capped at 96 / `MEMO` at 390 (`decisions.md#name-composed-from-remittance--counterparty-name`),
`CHECKNUM` capped at 12 and dropped rather than truncated over that
(`decisions.md#machine-references-go-to-checknum--refnum-not-into-name`), and ASCII folding via
`to_ascii()` (`decisions.md#payeememo-text-folded-to-ascii`). Complements, without
duplicating, [#17](https://github.com/skolima/gnucash-ofx/issues/17)'s `ofx-conformance` agent,
which is what covers the Windows build this ADR explicitly does not.

---

## Where this stands

- [x] **1.** The check lives in one new step in the existing `checks` job — no new job, no matrix
      ([#26](https://github.com/skolima/gnucash-ofx/issues/26)). **Shipped with one addition beyond
      what this decision specified**: `continue-on-error: true` on the install step. Its first draft
      had none, which meant an apt-mirror hiccup would have failed the entire `Tests (pytest)` step —
      all tests, not just the four new ones — directly contradicting rejected option C below.
      `invariant-guard` caught this during review of #26, before merge; the fix is what is actually
      in `.github/workflows/ci.yml` now. Not a rethink of the decision, just a gap in its first
      implementation.
- [x] **2.** `pytest.mark.skipif(shutil.which("ofxdump") is None, ...)` at module level — the first
      use of this pattern in the suite ([#26](https://github.com/skolima/gnucash-ofx/issues/26))
- [x] **3.** Fixtures and expected strings are reused from `tests/test_ofxout.py`, not invented —
      at minimum a normal case, the `NAME` word-boundary truncation, the `MEMO` cap, and the
      `CHECKNUM`-dropped-over-12 case ([#26](https://github.com/skolima/gnucash-ofx/issues/26))
- [x] **4.** No non-ASCII/diacritic fixture is added as folding coverage — deliberate exclusion,
      not an oversight; recorded so a future contributor does not "helpfully" add one
      ([#26](https://github.com/skolima/gnucash-ofx/issues/26))
- [x] **5.** The module's docstring and the CI step's name both state, in their own text, that this
      is Linux-only and does not cover ASCII folding, and point at `docs/testing.md` and the
      `ofx-conformance` agent rather than re-deriving the explanation
      ([#26](https://github.com/skolima/gnucash-ofx/issues/26))
- [x] **6.** No libofx version is pinned; the step skips rather than fails when the package is
      unavailable ([#26](https://github.com/skolima/gnucash-ofx/issues/26))

---

## Context

`docs/testing.md` already documents the `ofxdump` procedure in full — the exit-code contract, the
two stdout lines that map to what the register actually shows, the "skip rather than pin a
version" rule for anything that shells out to it, and even a Dockerfile for running it without a
Linux machine. None of that runs anywhere except a contributor's own terminal, and there is no
record of when it was last run at all. The gap it leaves has a name: `ofxtools` proving a file
well-formed is not the same claim as libofx accepting it, and the two diverge in ways `pytest`
today cannot see — `ofxtools` types `CHECKNUM` as a lenient value while libofx's buffer is a hard
12-byte `strncpy` target, and `ofxtools` has no notion of libofx's 96/390 `NAME`/`MEMO` buffers at
all. `tests/test_ofxout.py` already asserts the *intended* boundary behavior at each of those caps
(the tests cited in Scope, above); nothing today confirms libofx's own buffer agrees with the code
that composed the value for it.

`.github/workflows/ci.yml` (read 2026-08-10) runs exactly one job, `checks`, on `ubuntu-latest`:
checkout, `setup-uv`, `uv sync`, `ruff check`, `ruff format --check`, `mypy`, `uv run pytest`. No
matrix, no second job, nothing that shells out to an external binary. `AGENTS.md` (lines 99-100)
already states, as a fact about the current repository, that "CI is Linux-only and structurally
cannot catch that regression" — referring to the ASCII-folding regression `to_ascii()` exists to
prevent. That line is true today only because there is no CI libofx check of *any* kind; this issue
is what makes "CI is Linux-only" literally true by adding one, rather than vacuously true by adding
nothing.

The cost of the status quo is concrete: a parse failure libofx would reject, a `strncpy` overrun
behind the 96/390/12 caps, a strict `CHECKNUM` type violation, or a `\r\r\n` line ending can all
ship — silently, because the GnuCash GUI swallows libofx's own parse errors — until someone
remembers to run `ofxdump` by hand.

---

## Decisions

### 1. One new step in the existing `checks` job; no second job, no matrix

Insert `Install ofxdump` (`sudo apt-get update && sudo apt-get install -y ofx`) immediately before
the existing `Tests (pytest)` step, and let `uv run pytest` pick up the new module as part of the
same run. `apt-get install ofx` is the command `docs/testing.md` already documents for
Debian/Ubuntu, unchanged.

**Why not a second job.** The issue frames the whole ask as "for the price of an apt install," which
reads as one job, one extra step — not new isolation. Nothing else in `checks` would be invalidated
by `ofxdump`'s absence: decision 2 makes the new module skip cleanly rather than fail, so a runner
image that lacks the package degrades to "this one module contributed nothing," not to a red build.
A second job would only buy independent pass/fail reporting and a separate runner, at the cost of a
second `uv sync` and a second checkout for a step that takes one `apt-get install` and one `pytest`
module. Not worth it for what is, functionally, one more assertion inside the existing test suite.

**GitHub-hosted `ubuntu-latest` runners execute steps as a non-root user with passwordless `sudo`**
— so the install step needs `sudo`, unlike `docs/testing.md`'s bare `apt-get install ofx` written
for a contributor's own machine or a Dockerfile's `RUN` (both already root). Confirmed by a real
run on `ubuntu-latest` before acceptance, not merely asserted; see *Open questions*, question 1.

### 2. `pytest.mark.skipif(shutil.which("ofxdump") is None, ...)`, module level

```python
pytest.mark.skipif(
    shutil.which("ofxdump") is None,
    reason="ofxdump not installed; see docs/testing.md",
)
```

Applied at module scope so the whole file skips together rather than each test skipping
individually. **This is the first use of this pattern anywhere in `tests/`** — grepped 2026-08-10
across `tests/` for `skip|shutil.which|importorskip`, no matches. Future contributors adding a
check against another external binary should reuse this pattern rather than reinvent it.

The consequence that matters beyond CI: a contributor running `pytest` locally without `ofxdump`
installed gets a silent skip, not a failure. This extends `docs/testing.md`'s existing "skip rather
than pin a version" rule for anything that shells out to `ofxdump` to "skip rather than fail when
it is absent at all" — the same philosophy, one step further.

### 3. Fixtures and expected strings are reused from `tests/test_ofxout.py`, not invented

The new module builds the same `Account`/`Txn` objects and calls the same `build_statement` /
`statement_to_ofx` helpers `test_ofxout.py` already uses, writes the result to a real file with
`write_account_ofx`, runs `ofxdump` against it, and asserts libofx's own stdout `Name of payee or
transaction description:` / `Extra transaction information (memo):` lines equal the same strings
the unit test already asserts `compose_name`/`compose_memo`/`compose_check_number` produced. At
minimum, four cases, each already backed by an existing unit test with a known expected value:

| Case | Existing unit test (expected value) |
|---|---|
| A normal composed name/memo | any of the passing cases already in `test_ofxout.py` |
| `NAME` cut at a word boundary at 96 | `test_compose_name_truncates_at_a_word_boundary` |
| `MEMO` capped at 390 | `test_compose_memo_caps_at_the_libofx_buffer` |
| `CHECKNUM` over 12 dropped, not truncated | `test_compose_check_number_drops_an_over_long_reference` |

The `CHECKNUM`-dropped case is listed deliberately: the file this produces has no `CHECKNUM` tag at
all, so the assertion is exit code `0` and the tag's absence — not a rejection. A naive first
attempt at this module might expect `ofxdump` to fail here; it must not, and the test should say
why in a comment, so the assertion is not later "corrected" into the wrong shape.

Also, in the same module because it is the same category of libofx-facing structural concern the
issue lists: a direct byte check on the written file that `\r\n` appears and `\r\r\n` does not.
This needs no `ofxdump` at all — it is a property of `newline=""` at write time — but belongs here
rather than in `test_ofxout.py`, so a reader of this module sees the full list of things "libofx
conformance in CI" means without needing to also open the unit-test file.

Reusing rather than inventing fixtures means this module cannot drift from what the unit tests
already assert `compose_name`/`compose_memo`/`compose_check_number` produce without both changing
together — see *Consequences* for the coupling this creates.

### 4. No non-ASCII/diacritic fixture, and this is deliberate

`to_ascii()` runs at compose time, strictly before the OFX is written — by the time any fixture
reaches `ofxdump`, it is already pure ASCII. A fixture built from "Kämpf OÜ" or "Przelew własny"
would arrive at `ofxdump` as "Kampf OU" / "Przelew wlasny", identical to a fixture that was never
non-ASCII in the first place. Linux libofx keeping non-ASCII characters
(`non-ascii-loss-is-windows-only`, measured 2026-08-08) is true and irrelevant here: there is
nothing non-ASCII left in the file for it to keep.

Stated as its own decision, not left as a silent omission, because a diacritic fixture is the
*tempting* addition — it looks like it closes exactly the gap the issue names — and would instead
manufacture a specific instance of the risk the issue itself warns about: "a green tick that seems
to cover the folding is worse than no job at all." A future contributor reading only the test names
should not be able to conclude folding is covered here.

### 5. The module docstring and the CI step name both carry the limitation, in their own text

The module docstring states plainly: this runs on Linux only, proves parse acceptance and the
96/390/12 buffer behavior and line endings, and does **not** prove anything about ASCII folding,
because `to_ascii()` already ran before any fixture here was written — see decision 4. It points at
`docs/testing.md` and the `ofx-conformance` agent (`.claude/agents/ofx-conformance.md`,
[#17](https://github.com/skolima/gnucash-ofx/issues/17)) for the Windows-gated manual check, rather
than re-deriving the reasoning `AGENTS.md` (lines 99-100) and that agent's own file already state.

The CI step's `name:` field carries a short version of the same limitation (e.g. `Tests (ofxdump —
Linux libofx, no ASCII-folding coverage)` for the pytest invocation, or a comment above the install
step), so the limitation is visible in the CI log and the GitHub Actions UI, not only in a file a
reader has to already know to open. This is its own numbered decision, not a footnote, because it
is the direct answer to the issue's stated risk: a false sense of coverage costs more than the gap
it is covering for, and the fix is that the job's own name and the module's own text say so, every
time either is seen.

### 6. No libofx version is pinned

Whatever `apt-get install ofx` resolves to on `ubuntu-latest` at run time is what runs, exactly as
`docs/testing.md` already prescribes for a contributor's own machine. See *Rejected options* for
why pinning was considered and dropped.

### 7. Untouched

Everything under `src/` — no production code changes here, only test/CI infrastructure. The
Windows-side folding check, which stays manual and gated on changes to `to_ascii` and the
`compose_*` functions, per the `ofx-conformance` agent
(`.claude/agents/ofx-conformance.md`, [#17](https://github.com/skolima/gnucash-ofx/issues/17)) —
not automated here, and not proposed to be. `docs/decisions.md` — nothing here changes what the
tool writes to a user's file; see the closing line for why this ADR's decisions condense
elsewhere.

---

## Rejected options

### A. A second CI job, possibly with a version matrix

**Rejected.** Isolation is not needed: nothing else in `checks` depends on `ofxdump` being present,
and decision 2's skip means its absence degrades to "no additional coverage this run," not to a
broken build. A matrix over multiple libofx versions (as `docs/testing.md`'s illustrative
Dockerfile section gestures at, with `debian:stable-slim` vs `ubuntu:22.04`) would be the natural
next step if the single `ubuntu-latest` package version were ever shown to diverge meaningfully
from what it's checking — but that has not happened, and building it speculatively is exactly the
kind of premature generalization `adr-invariant-tests.md`'s rejected option C argues against
elsewhere in this repo. Cheap to add later; see *Cost to reverse*.

### B. Assert exit code only

**Rejected**, and explicitly by the issue's own framing. Exit code `0` passes on a file whose
`NAME` arrived mangled but still SGML-valid — a wrong payee is not a parse error. The stdout
`Name of payee...` / `Extra transaction information (memo):` lines are what the register actually
shows, per `docs/testing.md`, so those are the strings that have to match, not just the process's
return code.

### C. Fail the build when `ofxdump` is unavailable, rather than skip

**Rejected.** A distro's `libofx` package is not the version GnuCash bundles, and `docs/testing.md`
already treats "skip rather than pin a version" as settled policy for anything that shells out to
`ofxdump`. Failing the build on the package's absence would make CI's green/red status depend on an
apt mirror having a particular package on a given day, for a check that is additive coverage, not a
correctness gate on its own. Skip keeps the existing checks (`ruff`, `mypy`, `pytest`'s other
modules) meaningful regardless.

### D. Pin a specific libofx version, or build it from source / from a fixed container image

**Rejected**, and this is the issue's own explicit instruction. `docs/testing.md`'s Docker section
already exists as an illustration of how to test a *specific* version deliberately, not as
something CI should do routinely — pinning one would mean CI verifies against a libofx that is not
necessarily what GnuCash for Windows bundles, buying false confidence in exchange for build
stability that the skip-on-absence policy (decision 6, option C above) already provides more
cheaply.

### E. Include a non-ASCII/diacritic fixture as "folding coverage"

**Rejected.** Covered in full as decision 4 — restated here because it is the option most likely to
be re-proposed by a future contributor who has not read decision 4's reasoning, and the two-part
argument (folding already happened by write time; Linux libofx keeping non-ASCII characters is
irrelevant to a file with none left in it) belongs in both places.

### F. Run this same check on the Windows build inside CI, closing the folding gap entirely

**Rejected**, on the issue's own terms. `ofxdump.exe` ships with GnuCash for Windows, not as a
standalone package — there is no apt-equivalent "for the price of an install," and matching the
exact DLL GnuCash's own Windows build loads inside a CI runner is a materially larger undertaking
than this issue's scope. The `ofx-conformance` agent already exists for this, deliberately manual
and gated on `to_ascii`/`compose_*` changes rather than run-every-PR — [#17](https://github.com/skolima/gnucash-ofx/issues/17)'s
own scope, not reopened here.

---

## Cost to reverse

**Low.** Everything in scope is additive: one new test module, one new CI step, a short doc
addition. Deleting `tests/test_ofx_conformance_linux.py` and the CI step restores the exact status
quo — the manual `ofxdump` procedure in `docs/testing.md` and the `ofx-conformance` agent are
untouched either way. No cache, state, or on-disk format is touched; no request against any bank's
allowance is spent by anything here. The one thing that would need re-deriving if reversed and
later re-proposed is decision 3's mapping from `test_ofxout.py` cases to expected libofx stdout
lines — cheap, since the unit tests it points at are themselves stable.

---

## Open questions

**1. Does `sudo apt-get install -y ofx` succeed cleanly on `ubuntu-latest`'s current image, or does
it need `apt-get update` first, a different flag set, or fail to resolve the package at all?**
**Settled 2026-08-10, before acceptance, with a real run.** A throwaway `push`-triggered workflow
(not `workflow_dispatch` — that event requires the workflow file to already be merged to the
default branch before GitHub will dispatch it against another ref, which a one-off probe branch
cannot satisfy) ran `sudo apt-get update && sudo apt-get install -y ofx` on `ubuntu-latest` and
succeeded: `Setting up ofx (1:0.10.9-1.1build2) ...`, `/usr/bin/ofxdump` present immediately after,
`ofxdump --version` reporting `libofx 0.10.9`. Matches the noble-archive lookup done first
(`packages.ubuntu.com`: `ofx` 1:0.10.9-1.1build2 in noble's `universe` component, and `ubuntu-latest`
currently maps to 24.04/noble) — the archive check and the live run agree, and the live run is the
authoritative one. `sudo` is required, confirming the note under decision 1. Decision 1's exact step
commands (`Install ofxdump`: `sudo apt-get update && sudo apt-get install -y ofx`) are implementable
as written; no rethink needed. The probe workflow and its branch were deleted after the run —
nothing from it ships.

**2. Should the module eventually run against more than one libofx version (rejected option A)?**
Not a measurement question yet — there is no evidence in this repository that `ubuntu-latest`'s
`ofx` package has ever diverged from what this check needs to catch. Settled the same way
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) treats its own parked questions:
wait for a real case (a CI pass here that a hand-run Windows `ofx-conformance` check on the same
fixture would have caught differently, for a reason that isn't the folding gap this ADR already
names) rather than build a matrix speculatively.

---

Because this ADR is about test and CI infrastructure rather than what the tool writes to a user's
file, its decisions do not condense into [`docs/decisions.md`](decisions.md) — the same reasoning
[`adr-invariant-tests.md`](adr-invariant-tests.md) gives for its own closing line. Condensed on
acceptance ([#26](https://github.com/skolima/gnucash-ofx/issues/26)): `AGENTS.md`'s `ofx-conformance`
bullet now states that a Linux CI check runs alongside the manual Windows one and what each proves;
a Conventions bullet records the `continue-on-error: true` + module-level `skipif` pairing for the
next optional, shell-out-based CI step; `docs/testing.md` carries the pointer this ADR specified.
This file stays for the rejected options and the per-decision reasoning.

## References

- [#19](https://github.com/skolima/gnucash-ofx/issues/19) — the issue this decides.
- [#17](https://github.com/skolima/gnucash-ofx/issues/17) — the `ofx-conformance` agent
  (`.claude/agents/ofx-conformance.md`), the Windows/manual complement this ADR does not duplicate.
- [`docs/testing.md`](testing.md) — the existing `ofxdump` procedure, the exit-code/stdout/stderr
  contract, the Debian/Ubuntu install command, and the "skip rather than pin a version" rule this
  ADR extends to "skip rather than fail when absent."
- `AGENTS.md`, lines 99-100 — "CI is Linux-only and structurally cannot catch that regression,"
  which this ADR makes literally true by adding a Linux CI job, and lines 270-276 — the `NAME`/
  `MEMO`/`CHECKNUM` caps and the ASCII-folding scope this ADR verifies but does not re-derive.
- [`docs/decisions.md`](decisions.md) — *`NAME` composed from remittance + counterparty name*,
  *Machine references go to `CHECKNUM` + `REFNUM`, not into `NAME`*, and *Payee/memo text folded to
  ASCII* — the three decisions whose libofx-facing constants this ADR's fixtures verify.
- `tests/test_ofxout.py` — `test_compose_name_truncates_at_a_word_boundary`,
  `test_compose_memo_caps_at_the_libofx_buffer`,
  `test_compose_check_number_drops_an_over_long_reference`, and
  `test_written_file_has_clean_crlf_line_endings` — the existing unit tests decision 3 reuses.
- [`adr-invariant-tests.md`](adr-invariant-tests.md) — precedent for the local-evidence exemption
  method note and for a test-infrastructure ADR condensing into `AGENTS.md` rather than
  `decisions.md`.
- [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) — precedent for "wait for a
  real case rather than build speculatively," cited in open question 2.
- `non-ascii-loss-is-windows-only` (memory, measured 2026-08-08) — Linux libofx keeps diacritics;
  cited in decision 4 for why that fact is irrelevant to a fixture that is ASCII before it ever
  reaches `ofxdump`.
