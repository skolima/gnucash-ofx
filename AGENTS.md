# AGENTS.md

Guidance for AI agents and contributors working in this repo.

## What this is

A CLI that pulls bank transactions via Open Banking (Enable Banking — which also covers Wise),
then writes **OFX files** for import into GnuCash. The tool does **not** categorize — GnuCash's
import matcher does. Our job is clean, correct OFX.

## Layout

- `src/gnucash_ofx/cli.py` — `link` / `fetch` / `status` commands.
- `src/gnucash_ofx/config.py` — load `config.toml` + secrets from env/`.env`.
- `src/gnucash_ofx/state.py` — persist session ids, `valid_until`, and the per-account details
  captured at link time locally (gitignored).
- `src/gnucash_ofx/ofxout.py` — normalized `Txn` list → `ofxstatement` `Statement` → `.ofx`.
- `src/gnucash_ofx/run.py` — orchestration: config + secrets + client + state → OFX.
- `src/gnucash_ofx/diagnose.py` — pure lookup from an Enable Banking error code to an explanation
  and a suggested next step.
- `src/gnucash_ofx/coverage.py` — `state/coverage/<bank>.json`: which days have actually been
  fetched, per account, keyed on a digest of `ACCTID`. Dates only; never fails a fetch.
- `src/gnucash_ofx/runlog.py` — `state/fetch-log.jsonl`: every request a `fetch` sent, with its
  bank, rate-limit domain, status, error code and platform error name, and attempt number.
  Append-only apart from `scrub_log`, its one in-place redaction pass (see the invariant below).
  Diagnostics only; it must never be able to fail a run.
- `src/gnucash_ofx/sources/` — per-source fetchers (`enablebanking.py`; future `ibkr.py`). Each
  returns a list of normalized `Txn`; the OFX writer is shared.
- `src/gnucash_ofx/conversions.py` — `pair_conversions()`: pure, source-agnostic pairing of
  currency-conversion legs across every account of one bank's fetch, and the rate derived from the
  two booked amounts, never a source's prose. Never touches `FITID`/`BANKID`/`ACCTID`. See
  [`docs/adr-currency-conversion-pairs.md`](docs/adr-currency-conversion-pairs.md).
- `tools/evidence/` — repeatable censuses of `state/`, `cache/` and `state/fetch-log.jsonl`
  (`state_census.py`, `cache_census.py`, `coverage_reconciliation.py`, `filename_prediction.py`).
  Read-only by construction — no write-path function from `state.py`/`cache.py`/`coverage.py`/
  `runlog.py` is ever imported here, which is what a `grep` for `_write_atomic\|save_session\|
  save_coverage\|save_cached\|scrub_log` returning nothing checks — and every census returns a
  `frozen` dataclass whose field
  types cannot hold a raw identifier: counts, dates, and a dict only where **we** supply the keys,
  never an account-sized string. See [`docs/adr-evidence-tool.md`](docs/adr-evidence-tool.md).

## Commands

```sh
uv sync                       # install (incl. dev group)
uv run pytest                 # tests + coverage
uv run ruff check .           # lint
uv run ruff format .          # format (use --check in CI)
uv run mypy src               # type-check
uv run gnucash-ofx --help
```

The workflow below leans on the GitHub CLI for issues, ADR PRs and labels. On Windows it is often
installed without being on `PATH`, in which case call it by its full path from a POSIX shell rather
than concluding it is missing:

```sh
"/c/Program Files/GitHub CLI/gh.exe" issue view 37
```

## Conventions

- **Issue and PR numbers in the tracked tree cite the public `skolima/gnucash-ofx` tracker.**
  The tree was renumbered once at export (55 stubs, old→new order-preserving; the table lives on
  the private repo's export issue). A number taken from a pre-export document, commit message or
  branch name must be translated, never pasted — the old range overlaps the new, so a stale
  number links somewhere plausible rather than nowhere, and nothing looks broken.
- **TDD.** For every fetcher/mapper: write a failing test from a sanitized sample JSON fixture
  asserting the normalized `Txn` and the resulting OFX (parsed with `ofxtools.OFXTree`), then
  implement to green, then refactor. No live API calls in tests — use recorded fixtures.
- **Not every invariant below has a test, but every test that pins one says so.**
  `tests/test_invariants.py` holds the manifest: which bullets are machine-checked and which test
  enforces each. Adding a new invariant? Check whether it belongs in that manifest too, rather than
  leaving the pin undiscoverable from the test side.
- **A load-bearing line that looks wrong in isolation gets a two-line anchor, not a restated
  argument.** Right on (or immediately above) the specific line a "cleanup" diff would touch — not
  the whole enclosing function, which likely has its own docstring already:
  ```python
  # invariant: <one-line paraphrase of the AGENTS.md bullet>
  # AGENTS.md#invariants
  ```
  A pointer can't drift, because it carries no argument; a restated argument does, the next time
  the reasoning in `decisions.md` is refined. Only add one where a bullet below already covers it —
  an anchor with no matching bullet means one of the two is wrong. See `to_ascii` or the
  `BANKACCTTO` block in `ofxout.py` for the shape.
- **Python 3.12+**, fully type-annotated; keep `mypy --strict` clean.
- Keep runtime deps minimal; prefer the stdlib (`tomllib`, `dataclasses`).
- **A CI step that installs an external tool not guaranteed on every runner gets
  `continue-on-error: true` *and* a `timeout-minutes` bound, paired with a module-level
  `pytest.mark.skipif` on the tests that need it** (`shutil.which(...) is None`) — never let an apt
  mirror hiccup fail the whole job ahead of unrelated tests. `continue-on-error` covers the step
  *failing*, not hanging: two runs were measured wedged 15+/30+ minutes inside the ofxdump install
  against a 62s normal (2026-08-19), so the timeout is what converts a wedged mirror into the
  designed skip path. See `.github/workflows/ci.yml`'s `Install ofxdump` step and
  `tests/test_ofx_conformance_linux.py` for the shape; reuse it rather than reinvent it for the next
  optional, shell-out-based check.

## Further reading

- [`docs/enable-banking.md`](docs/enable-banking.md) — what the API actually does: endpoint
  quirks, rate limits, per-bank behaviour. Read this before debugging a fetch; most surprises are
  already written down.
- [`docs/decisions.md`](docs/decisions.md) — why the invariants below exist and what reversing
  them costs.
- [`docs/testing.md`](docs/testing.md) — how to check generated OFX against real libofx
  (`ofxdump`), on Windows, Linux, or via Docker, without launching GnuCash.
- [`docs/probes.md`](docs/probes.md) — every live probe spent against an ASPSP for design
  purposes: what it asked, what it cost, and what it settled. Check it before designing a new
  probe — the question may already be bought and paid for.

## Working pattern

The loop this repo runs, and the agents in [`.claude/agents/`](.claude/agents) that carry each
step:

```
issue → measure locally → (a counted probe, if the local data cannot settle it) → ADR
      → accept → TDD implementation → verify → sanitize → PR → condense
```

- **`local-evidence`** measures against the real `state/`, `cache/` and `fetch-log.jsonl` by
  running the actual functions over them. It is the only agent that reads real financial data, and
  it emits structural counts only.
- **`probe-designer`** designs a live probe and presents its bill in counted requests. It never
  spends the allowance itself. Every probe's requests carry a `probe-<slug>` `RunLog` tag.
- **`adr-author`** writes `docs/adr-*.md`, which ships as its own PR and is accepted before
  anything is implemented, and adds a [`docs/probes.md`](docs/probes.md) row for any probe the ADR
  used. It also applies the `adr` label to the linked issue, so that an issue with a decision
  record behind it is visible at a glance from the issue list, not only from inside `docs/`.
- **`invariant-guard`** reviews a diff against the invariants below in both directions: broken,
  and quietly tidied away.
- **`ofx-conformance`** runs generated files through real libofx (`ofxdump`) — on the **Windows**
  build for anything ASCII, since Linux CI cannot catch that regression (`to_ascii()` already ran
  before any fixture reaches it — [`docs/adr-ofx-ci-conformance.md`](docs/adr-ofx-ci-conformance.md)
  decision 4). Linux CI (`tests/test_ofx_conformance_linux.py`) now runs *alongside* it, on every
  push, proving parse acceptance, the `NAME`/`MEMO`/`CHECKNUM` buffer caps, and CRLF line
  endings — folding coverage is still Windows-only and still manual.
- **`leak-check`** is the last gate before anything reaches GitHub, and it reads the drafted PR
  body as well as the diff.
- **`doc-condenser`** folds a merged change back into this file, `decisions.md` and
  `enable-banking.md`, and, when a probe settled the change but no ADR carried its row yet,
  [`docs/probes.md`](docs/probes.md).

`/design`, `/ship` and `/condense` chain them. Two gates are human and stay human: accepting an
ADR, and approving a live probe.

## Invariants (do not break)

- **One statement per account, per currency.** An OFX statement is single-currency (`CURDEF`), so
  multi-currency positions (Wise, Alior FX) are separate accounts and produce one statement each.
  Packaging is separate, and there are three modes, not two: by default one statement per file;
  `--combine` puts every statement of a run into one file and changes nothing else about them
  (docs/adr-combined-ofx-file.md); `--batch-size N` splits one account's window into several
  statements — several files, or several `STMTTRNRS` under `--combine` — each an ordinary
  statement over the exact range it covers, by count and never by calendar. All three are
  packaging only: same requests, same identity fields (docs/adr-ofx-batch-splitting.md).
- **A booking date is never split across two batches; a day larger than the cap stays whole**, as
  one oversized batch — `--batch-size` is a soft cap, and a boundary that would cut a same-day run
  pulls back to the day break instead. Day coherence is the point (a payment and its fee, both
  legs of a transfer, reviewed in one sitting), and the deduction resting on it is load-bearing:
  batch date ranges are disjoint and strictly increasing, so `ofx_filename` needs no batch marker
  and gains no parameter. Break the first half and the second stops holding silently — two batches
  compute one filename and the second overwrites the first, a whole batch lost with no error
  anywhere. Pinned as the conjunction of four properties across 200 seeds × 6 caps in
  `tests/test_batching.py` (docs/adr-ofx-batch-splitting.md decisions 3/4).
- **Only the batching path sorts.** With no `--batch-size`, output is byte-identical to the
  pre-batching writer: upstream hands transactions cached-then-fetched, not date-ordered, and that
  order reaches the file (pinned by `test_unbatched_input_order_is_preserved_not_sorted`).
  Sorting anyway would silently reorder every ordinary fetch's bytes the first time a source
  delivers out-of-order data — equivalent for GnuCash, no longer the same bytes, and
  byte-identical-when-off is the contract that let batching ship as packaging only.
- **Stable `FITID` per transaction** (from the bank's transaction/entry reference). This is what
  makes GnuCash de-dup reliable on re-import — never synthesize a value that changes between runs.
- **Stable `BANKID` and `ACCTID`.** GnuCash derives an account's `online_id` from the pair, so
  changing either orphans already-imported accounts. `BANKID` comes from the bank's `bankid`
  config option (its BIC), else the truncated key; `ACCTID` resolves
  `iban` → `other.identification` → `identification_hash` → `uid` — unless the connection shares
  an IBAN, in which case the next bullet's uniform hash rule overrides the first two steps. Never
  let either depend on Enable Banking's account `uid` alone — it is regenerated on every re-link.
- **A connection in which any IBAN is shared resolves every account's `ACCTID` through
  `identification_hash` — the unique-IBAN sibling included**
  (docs/adr-revolut-onboarding.md decision 1; reversing re-collapses the pockets, where GnuCash's
  per-account `FITID` de-duplication was measured silently dropping a conversion leg, 2026-08-13).
  The regime is one per-connection fact (`_iban_shared_within`) computed from the stored account
  set and threaded to every resolution site — `_stored_acctids` for anything deciding from
  `state/<bank>.json` alone, `tools/evidence` included — because a site left out silently
  disagrees with the fetch about the same account's identity. Deliberately not scheme-validated:
  whatever the stored identifier field holds is what `ACCTID` resolves to, so two accounts sharing
  it *is* the collapse; gating on "is it really an IBAN" reintroduces the defect for a shared-BBAN
  connection. An account with no stored hash keeps its IBAN and warns (`_warn_shared_iban`,
  symptom-keyed like the stale-identity warning); it never falls to the re-link-volatile `uid`
  (shared-but-stable mis-files recoverably; per-link orphans every consent cycle), and only the
  **stored** hash counts in this branch — a fetch-time rescue would make fetch and `status`
  diverge about the same account. False for all 19 pre-existing accounts, so no configured bank's
  `ACCTID` moves.
- **`fetch` stdout is the file list; everything else is stderr.** With or without `--dry-run`: one
  path per line, bare — no header, no indent, and nothing at all when no file was written or
  predicted. Progress, warnings, the failure summary and the `Wrote N OFX file(s) to ...:` header
  all go to stderr, so `fetch > files.txt` stays a clean, parseable list of paths. Anything added
  to `_report`/`_report_dry_run` that a human reads rather than a program consumes belongs on
  stderr. `status`, `link` and `aspsps` are **not** the file list — their report *is* their output
  and stays on stdout; `status --check`'s attention summary is the one line that does not.
- **One bank's failure must not cost another bank its files.** `fetch_enablebanking` collects
  `BankFailure`s and returns a `FetchReport`; only genuinely global problems (credentials, config,
  an unconfigured `--bank`) raise `RunError`. Use `BankError` for anything bank-scoped, so the
  distinction lives in the type rather than in where a `try` happens to sit. A corrupted
  `state/<bank>.json` is bank-scoped too — `load_session` raises `StateError`, caught alongside
  `BankError` — never let a local file problem for one bank abort the run. Exit 1 if any bank
  failed, even when files were written.
- **Never invent an error explanation.** `diagnose()` covers codes verified in
  `docs/enable-banking.md`; anything else prints the bank's verbatim response and nothing more.
- **Diagnostics must never cost a fetch.** `RunLog` swallows every `OSError`, and the client
  swallows anything an observer raises. The log exists to protect the rate-limit allowance, so it
  must not be able to spend it by aborting a run that was going to succeed. It records no amounts,
  no counterparties and no account numbers — API paths have the account UID *and the session id*
  redacted — because the point of it is to be pasteable into an issue.
- **The fetch log is append-only apart from one in-place redaction pass, and `redact_uid_path` must
  stay idempotent.** `scrub_log` is that pass, run once per `fetch` ahead of the PSU/IP lookup:
  extending the redaction fixes only the *next* request, while a machine that has already fetched
  holds session ids written before the regex covered them
  ([`docs/adr-input-hardening.md`](docs/adr-input-hardening.md) §1 measured them live on this one),
  so decision 1 is remediation of a file and not only prevention. The mask keeps head-4 and tail-4,
  which makes re-masking a fixed point — any other shape would eat four characters per fetch until
  two accounts of one bank stopped being tellable apart, which is the property the head/tail shape
  exists to provide. **Every id-bearing API path belongs in `_ID_IN_PATH`'s alternation**: the path
  is the log's entire leak surface, and `/sessions/{id}` was missed once already. **Read the file with
  `read_bytes().decode()` and split on `"\n"`; never `str.splitlines()`, and never `read_text()`.**
  `api_code`, `error_name` and the kept header values are ASPSP-controlled, and U+2028/U+2029/NEL
  are line boundaries to `splitlines()` that `json.dumps` does not escape, so a crafted error code
  would tear a record in two and the scrub would write the tear back; `read_text()` reintroduces the same bug
  for a bare `\r` by normalising it to `"\n"` before any split sees it, and silently re-flavours the
  file's terminators to the running platform, which matters because `state/` is synced between
  machines and `_append` writes CRLF on Windows. The rewrite carries the terminators it read, so it
  writes with `newline=""` for the same reason `OfxWriter` does. **This binds `load_requests` as much
  as the scrub** — it is the instrument that proves a rewrite non-destructive, so a reader that split
  differently would mis-measure the fix with the fix's own tool. The accepted cost is that records
  separated by a bare `\r` read as one unparseable line and are left unscrubbed, silently; that is
  the price of never tearing a paid-for record, and `decisions.md` records which way the trade went.
  **A field added to a `request` record is optional forever** — old lines are paid-for evidence, so
  readers take it with `.get` and never treat absence as malformed (`error_name`, added 2026-08-19,
  is the precedent; a reader typing it required would count every earlier line as skipped and
  mis-measure the next scrub with the scrub's own instrument). And the log persists error *tokens*
  only (`error`, `detail.error_name`), never the `message`/`detail.message` prose — unbounded
  server text has no place in a file meant to be pasteable. Do not
  "restore" append-only by deleting `scrub_log`, do not add a second rewrite path, and do not scrub
  from `--dry-run`, which writes nothing under `state/` at all.
- **Filenames follow the shape of the connection**, not which accounts succeeded or had
  transactions. The disambiguation group counts every account in the session, so a failing account
  cannot silently rename its same-currency sibling. This is also what makes `fetch --dry-run`'s
  predicted paths exact. Under `--batch-size` the period components derive from each batch's
  actual first/last transaction dates instead of the requested window — identity components and
  the disambiguation group are unchanged — and the exactness claim survives only because the dry
  run refuses to predict at all while batching is on (the `--dry-run --batch-size` bullet below).
- **`--dry-run` spends nothing, structurally.** `dry_run_enablebanking` takes **no client and no
  cache dir** — not a `dry_run` flag threaded through `fetch_enablebanking` — so it cannot call the
  API, cannot populate the cache with data a later fetch would serve for free, and writes no
  `RunLog` entry (that log is an account of what was spent). It still shares the code that decides
  the answer: `_known_acctid`, `_disambiguators`, `ofx_filename`. It exits 0 whenever the config
  resolves; a lapsed consent is a reported `problem`, not a failure. **One account with no
  link-time currency suppresses its whole bank's prediction** — it may be a same-currency sibling,
  and joining a group does not merely add a suffix, it can flip the group from IBAN tails to
  digests. Never "improve" this by predicting the accounts that are known.
- **`--dry-run --batch-size` predicts no paths for the whole run, suppressed at the source.**
  Batch boundaries depend on transaction counts and dates, which a dry run structurally never
  sees, and the suppression lives in `dry_run_enablebanking`, not the reporting layer — a
  `DryRunReport.planned` still carrying unbatched filenames behind one `if` in a renderer is a
  value object that lies, the same argument that makes `FetchWarning` a type. The combined path
  alone would be derivable; announcing it anyway puts a path on stderr a few lines before the CLI
  says none were predicted. Problems are still reported. Pinned by the three
  `test_batched_dry_run_*` / `test_batched_combined_dry_run_*` tests in `tests/test_run.py`.
- **`[fetch]` is a closed key set, and `refresh` must never become a config option.** An unknown
  key is a `ConfigError` at startup — a typo'd key that silently did nothing would change the
  output shape for weeks — and `--refresh` spends rate-limit allowance, so it stays a deliberate
  per-run act no config file can turn on permanently (pinned by
  `test_refresh_is_not_a_config_option`). Precedence is CLI explicit > config > built-in;
  `--no-combine`/`--no-batch-size` are the real off-switches, because `store_true` cannot express
  "off" against a configured value, and `--batch-size 0` stays a validation error, never a magic
  off value. Non-default packaging is announced on stderr, since the typed command no longer
  describes the output shape.
- **What aborts a fetch aborts the dry run, from the same function.** `require_ordered_window`
  rejects `--from` later than `--to` (equal dates are a one-day window and stay valid) and is
  called by `fetch_enablebanking`, `dry_run_enablebanking` and the CLI — the CLI ahead of the
  public-IP lookup and the run log, or the answer is about rate-limit mode instead of the dates.
  One function, not a check written twice: a dry run that predicts a filename for a window no
  fetch can satisfy is worse than one that refuses, because it spends the user's next request for
  them. Any new global precondition belongs in both entry points for the same reason.
- **The coverage ledger is not the cache, and the cache can never become it.** The cache is keyed
  on the `uid` (regenerated on every re-link), lives in a directory the user may delete, and a
  chunk's claim cannot hold two disjoint spans — disjoint claims keep the wider one and **never
  claim the gap between them**, because the one thing a coverage record may never do is claim days
  it lacks. `state/coverage/<bank>.json` is keyed on a digest of
  `ACCTID`, stores **inclusive day ranges** (a month is the unit of retrieval; a day is the unit of
  loss), and holds dates and digests only so it stays pasteable. **Absent, unreadable or
  wrong-version means _unknown_, never "gap since the epoch"** — every existing bank starts there,
  so a first run must warn about nothing.
- **`save_cached_month` merges: authoritative inside the incoming claim, never lossy outside
  it.** The overwrite it replaced destroyed a fuller month chunk twice in two days on the default
  resume path (#35), and at the ~90-day-horizon banks that loss ages into unrecoverable at any
  price. The merge is keyed on `sources/enablebanking._transaction_id` — the identity `FITID`
  rests on, which is what makes Millennium's full-history responses merge idempotently — so **a
  future second source whose `FITID` rests on a different identity must not silently merge on the
  Enable Banking key**. A claim never derives from the widened wire request or the response's
  extent, only from the planned un-widened span; an empty claim (`null`/`null`) holds data and
  never satisfies `covers()`; a merge that retains days only the old fetch answered keeps the old
  `fetched_at`, or a crosser-only save would make a stale moving-tail claim read as fresh inside
  the 6h TTL. Under `_BAD_DATA_ERRORS` the merge degrades to the incoming set for that one chunk —
  keying an id-less entry hashes its amount fields, so a wrong-typed entry already on disk raises
  on every later save touching its chunk, and that must stay the account's problem, never the
  run's (docs/adr-transaction-date-window-margin.md decision 3).
- **Only a successful fetch advances coverage, at the mapping step, not at the write.** An account
  with no transactions writes no file and is still fully covered; recording at the write would
  manufacture the gap the ledger exists to find. Written per account as it lands, so a rate-limited
  sibling cannot discard coverage already earned. A failed account advances nothing. `--dry-run`
  records nothing, structurally — it fetches nothing.
- **An implied window is resolved per bank, by the one function the dry run also calls.** `--to`
  defaults to today, `--from` resumes from the ledger less `LATE_BOOKING_MARGIN` (imported from
  `cache.py`, never re-picked), clamped to 89 days; an explicit `--from` is never clamped — that
  is the *claim*; the margin-widened wire request is floored at the clamp for every window, see
  the wire bullet below. Per bank,
  because one bank's deeper history would otherwise drag another past the ~90 days it serves. The
  **least covered account decides**, and an unknown account means the full lookback rather than
  being skipped. 89 and not 90 is **not** a guess at the boundary — today−90 is measured as served
  at Alior and Erste; the day is for clock skew between local `date.today()` and the ASPSP's. The
  escalation horizon stays 90.
- **A warning is a `FetchWarning`, never a `BankFailure`, and never moves the exit code.** The type
  is what keeps "this did not fail" from being lost by a later edit to the reporting layer. Exit 1
  still means a bank actually failed. Gap warnings treat the window being fetched as covered while
  they are computed, or they would fire on every run. `status --check` is behind a flag because
  `status` has always exited 0.
- **The stale-identity warning is keyed on the symptom, not the state schema.** Warn when `ACCTID`
  resolves to the bare `uid`; do **not** warn on a pre-`accounts` state file. Measured: 5 of 6 banks
  are on the old layout and 0 of 19 accounts are in the harmful condition, so a schema check would
  cost five SCA dances and fix nothing. It runs after `GET /sessions` because `accounts_data`
  rescues most v1 accounts — which is why `--dry-run` stays silent about it rather than
  over-reporting. Never "fix" it by changing what `ACCTID` resolves to: that orphans exactly the
  history it protects.
- **Read account details from `link`-time state, not from a fetch-time call.** `POST /sessions` is
  the only place the full account resource appears, so `complete_link` captures every field plus
  the raw body into `state/<bank>.json`. When adding a consumer, take it from `SessionState` and
  degrade when it is absent — sessions linked under the old schema have only uid/IBAN/BIC, and
  changing what `ACCTID` resolves to for them would orphan already-imported accounts.
- **`ACCTTYPE` comes from the stored `cash_account_type` through `accttype_for()` and nowhere
  else.** `CACC`→`CHECKING`, `SVGS`→`SAVINGS`; absence and `OTHR` are the explicit `CHECKING`
  fallback, surfaced in `status` (informational, never `needs_attention`, never at fetch time);
  `CARD`/`CASH`/`LOAN` and any undocumented value refuse the **account**, before its first request
  is spent, in fetch and `--dry-run` alike — siblings still ship and still keep their
  disambiguated names. `Account` carries the bank's raw value, never the mapped OFX word, and the
  refusal is what keeps `_CombinedOfxWriter`'s single `BANKMSGSRSV1` block correct
  (docs/adr-accttype-mapping.md).
- **A currency string entering from ASPSP JSON, or read back from persisted state, is never
  trusted as fact without `currency_or_none()`.** ISO 4217's `"XXX"` ("no currency") has been
  observed live for a real account (`POST /sessions`, Alior, 2026-08-10); it is a non-empty
  string, so a bare truthy check reads it as known, skips the `/balances` discovery fallback below,
  and lets the placeholder stand in as the account's currency — silently misrouting every
  transaction at a bank serving only ~90 days of history, where the missed window ages out and
  cannot be refetched at any price. State load is a boundary too, not just the two ASPSP responses:
  a session linked before this fix has `"XXX"` already on disk, and it must keep re-normalizing on
  every load or it never self-heals without a re-link. A new site reading `.currency` off session
  JSON, `/balances`, or `state/*.json` goes through the shared helper in `models.py`, never a raw
  `entry.get("currency")` check.
- **`LEDGERBAL` carries the bank's balance only when the window ends today or later.** `/balances`
  takes no date and answers with the balance *now* (`reference_date` is the fetch date in every
  record measured), while `OfxWriter` dates `LEDGERBAL` from the statement end and GnuCash's
  importer hands the pair to its reconcile machinery (`gnc-ofx-import.cpp` →
  `recnWindowWithBalance()`) — so on a closed window that number is a live balance stamped with a
  past date, for any consumer that surfaces it. The one dialog measured (GnuCash 5.16, 2026-08-17,
  docs/adr-ofx-batch-splitting.md §12) pre-filled the register-computed total instead — the rule
  guards the code path that exists and every other libofx consumer and version, not one observed
  pre-fill. For a closed window the call is skipped and the statement
  carries its own running total. The tag itself is **not** optional: `ofx160.dtd` line 921
  requires it in `STMTRS`. The call may still be made on a closed window to discover the account's
  currency, which is the only way to learn it for a session linked under the old state schema —
  when it is, the result must **not** reach `end_balance`. Balances are cached per account, never
  per window. Under `--batch-size`, `LEDGERBAL`: only the final batch of a window reaching today
  carries the live balance; every earlier batch keeps its own zero-based running total, computed
  independently, never chained — so a batched `--combine` file deliberately carries several
  `LEDGERBAL`s for one account, and that is not a defect to "fix" (pinned by
  `test_only_the_final_batch_carries_the_live_balance`; measured harmless 2026-08-17 on GnuCash
  5.16, whose reconcile pre-fill is register-computed and consults no `LEDGERBAL` at all —
  docs/adr-ofx-batch-splitting.md §12).
- **The cache stores calendar months, not requests.** Keyed `(uid, YYYY-MM)` with the claim each
  chunk actually holds, so a differently-shaped window reuses it — the old exact-triple key made
  `--to <date>` then `--to <date+1>` a total miss. **Request size and storage size are separate
  concerns**: requests stay as large as the bank allows (≤90 days) and the response is filed into
  booking-month chunks, so monthly granularity costs no extra requests. **Every returned entry is
  filed into its booking month, including months outside the requested span** — data paid for with
  a counted request is never discarded by our own slicing; a crosser lands under an empty claim,
  because it proves its own existence, not that its day was served in full. Each month is written
  as it lands, atomically — saving at the end meant a 429 on the last chunk discarded everything
  already paid for.
- **Every wire request opens `TRANSACTION_DATE_MARGIN` earlier than the span it answers for, and
  the wire is bounded on both sides.** Alior filters the requested window by `transaction_date` —
  the purchase date — so an entry booked in-window but purchased before `date_from` is invisible
  to the exact-window request (`probe-txn-date-window`, 2026-08-13). The widened wire is floored
  at the 89-day clamp for **every** window, explicit `--from` included (Alior and Erste `400`
  past the horizon, and the ADR's own repair command puts an explicit `--from` exactly there),
  and capped at `claim_from`, so a floor above the whole window cannot invert the request into
  `date_from > date_to`, whose empty answer would claim days no request answered. Claims keep
  deriving from the un-widened span — claiming the margin days would repeat #35 one level down
  (docs/adr-transaction-date-window-margin.md).
- **`TRANSACTION_DATE_MARGIN` (7 days) is a policy value, and the booking-lag `FetchWarning` is
  its named revisit trigger.** 7 is the measured maximum lag (3 days, over a holiday-free sample —
  local census 2026-08-13) plus slack; re-pick it on the warning's evidence, never by re-guessing.
  The warning is `kind="coverage"` and never a failure — *this* run's files are correct; a
  narrower window elsewhere may have the hole — and its message carries day-counts only, never
  amounts or payees, because it reaches the stderr summary users paste. Its reach is roughly
  `LATE_BOOKING_MARGIN + TRANSACTION_DATE_MARGIN` (~21 days) on resumed accounts and it is
  censored at cold-backfill openings, so its silence is evidence about the resumed cadence, not
  proof about the censored region.
- **`CHUNK_VERSION` stayed 2 through the nullable claim, deliberately.** A bump discards every
  existing chunk — a full re-buy at the banks serving ~90 days, part of it no longer served at
  any price — while an older reader meeting a `null` claim merely fails to parse the chunk and
  refetches: a miss, never a wrong answer. Bump it only when an old reader would get a *wrong*
  answer, not merely a different schema.
- **The statement's `FITID` dedup takes the later copy's content, at the first copy's position.**
  `raw` is cached-then-fetched and the wire margin deliberately re-asks a served month's tail, so
  a freshly amended entry duplicates its cached copy — the cache merge keeps the fresh one, and a
  statement carrying the stale copy would import stale details under a `FITID` GnuCash then
  refuses to correct on any later import. Do not "simplify" back to first-copy-wins.
- **A settled month does not expire.** `chunk_ttl()` returns `None` beyond `LATE_BOOKING_MARGIN`,
  and the 6h TTL applies only to the moving tail. `LATE_BOOKING_MARGIN` is a **policy value, not a
  measured one** — the local cache showed no few-days lag at all, only a two-month aggregator
  backfill that no TTL would have caught. Do not re-tune it as though it were empirical, and do not
  "fix" settled months expiring: `--refresh` is the escape hatch.
- **`ASPSP_RATE_LIMIT_EXCEEDED` is never retried.** It is a daily cap with a ~6h recovery and the
  backoff ladder tops out at 31s, so every retry is certain to fail and is another counted request
  at an ASPSP that has just refused. Other 429s keep the ladder. Match on `error`, never `message`.
- **A server never picks how long we wait, and a local bound exceeded raises rather than
  truncating.** A numeric `Retry-After` is clamped to `_MAX_BACKOFF_SECONDS` — the retry ladder's own
  ceiling, reused and never re-picked, so there is one answer to "how long can one retry wait" — and
  a non-finite value is refused outright (`inf` passed a bare `>= 0` check and raised
  `OverflowError` inside `time.sleep`; `999999999` parked the run for ~31 years). Do not add a
  second, higher cap for the header: the number would be invented, and the one limit measured to
  need hours (`ASPSP_RATE_LIMIT_EXCEEDED`) is never retried at all. `_MAX_PAGES_PER_ACCOUNT` and
  `_MAX_RESPONSE_BYTES` are **policy values with a measured floor and no measured ceiling** (the
  `LATE_BOOKING_MARGIN` precedent) — re-tune them against a runlog census, never raise one to make a
  bank work. Exceeding either raises `ResponseLimitExceeded`, because a short answer that looked
  complete would advance the coverage ledger over transactions nobody wrote; the body check sits
  **before** `.json()`, where bytes become a structure many times their size, and it bounds the
  parse rather than the transfer (httpx has already buffered by then). The page budget is threaded
  across an account's request spans, not re-read per call — a gap in the cache splits one
  account-window into several spans, and a per-call constant would silently make the bound
  `cap x spans`. Scope differs by endpoint on purpose: an account's runaway response is
  account-scoped (nothing was *refused* — every one of those requests returned 200), while a session
  response has no account to blame and becomes a `BankError`.
- **One account's bad data costs that account only, and the guard is a named set.**
  `_BAD_DATA_ERRORS` is `(ValueError, ArithmeticError, TypeError, AttributeError)`:
  `Decimal("garbage")` raises `decimal.InvalidOperation`, an `ArithmeticError`, and a wrong-typed
  `transaction_amount` raises `AttributeError` from the `.get` chain, so `ValueError` alone did not
  deliver the containment the mapper guard's own comment promised. Deliberately **not** bare
  `Exception`: a programming error should still crash rather than be filed as a bank's bad data.
  The set lives in `models.py` so `run.py` and `cache.py`'s merge share the one definition — never
  re-declare a second copy that could drift.
  Amounts and balances go through `finite_decimal`, which rejects NaN and infinities rather than
  clamping or zeroing them — an invented amount in a financial file is worse than a missing file —
  and which **never quotes the rejected value**, only its length and character classes, because
  `InvalidOperation` fires on merely non-canonical *real* money and the message reaches the stderr
  summary users are asked to paste. An *absent* figure is not bad data: an unreadable `/balances`
  body degrades to the link-time currency and an unreadable closing balance degrades to
  `end_balance=None`, both with a `FetchWarning`, because `None` is the shape every closed window
  already uses and failing would discard a mapped statement *and* repeat on every later run.
- **Signed amounts**: credits positive, debits negative.
- **Write OFX with `newline=""`.** `OfxWriter` already emits CRLF; default text mode on Windows
  translates the `\n` again and produces malformed `\r\r\n` line endings.
- **Counterparty account numbers go in `MEMO` *and* `BANKACCTTO`.** GnuCash's matcher only
  tokenizes `NAME`/`MEMO`, and libofx does not parse `BANKACCTTO` — so the memo copy is the one
  that actually drives account routing. Do not "clean up" by dropping it.
- **The counterparty is read from the side the direction names (creditor on `DBIT`, debtor on
  `CRDT`), never "whichever side is populated".** N26 populates both `*_account.iban` on every
  transaction, own side included; a populated-side fallback puts our own IBAN into `MEMO` — the
  field that routes accounts — so GnuCash's matcher would learn the own-IBAN token on every
  transaction. Pinned by `test_n26_own_account_never_becomes_the_counterparty`.
- **`NAME` is composed (`compose_name`), remittance first, then the counterparty name — except a
  currency-conversion annotation (`compose_conversion`), which leads even the remittance** (see
  `docs/adr-currency-conversion-pairs.md` decision 4). GnuCash shows `NAME` as the register
  Description, so the bare payee hides the informative text. `MEMO` (`compose_memo`) still
  carries the full remittance plus the IBAN — the duplication is deliberate and free, because
  GnuCash tokenizes unique tokens only. Do not "de-duplicate" the two fields. The remittance
  reaching both is **prose only**: a machine reference is split out first. The conversion
  annotation is the one deliberate exception to remittance-first: for a paired conversion leg the
  FX detail *is* the substantive remittance, needed at the exact moment GnuCash's "Assign
  exchange rate" dialog asks for a number, and what it displaces is boilerplate repeated
  verbatim on every deal — the 96/390 caps below truncate that boilerplate, never the annotation.
- **A Revolut `EXCHANGE` leg's deal key is `revolut:<entry_reference>` — the same field `FITID`
  falls through to — so extraction is read-only, and a reference-less `EXCHANGE` row gets *no*
  key.** Consuming or mutating the field moves the `FITID` and re-imports every Revolut
  transaction; an empty key would falsely cluster every reference-less row into one group for
  `pair_conversions` — no key means the leg ships as plain text, today's behaviour (pinned by
  `test_an_exchange_row_without_an_entry_reference_gets_no_key`, manifest entry "m"). Never assert
  the `bank_transaction_code` vocabulary is closed: a new code on conversion legs simply does not
  fire the rule — a measurement to bring back to #50, not a bug. And `_validate`'s
  one-booking-date check stays strict on purpose — a cross-date deal, if Revolut ever books one, is
  refused into unannotated text, never mis-paired; the working assumption and its revisit trigger
  are in `decisions.md` (docs/adr-revolut-exchange-pairing.md decision 2).
- **A source's machine reference goes in `CHECKNUM` + `REFNUM`, never in `NAME`/`MEMO`.** Wise
  sends `TRANSFER-<id>`, `CARD-<id>` and `BALANCE_CASHBACK-<uuid>` as their own
  `remittance_information` elements; `_split_reference()` lifts the bare id into `Txn.reference` and
  `compose_check_number()` writes it to both OFX fields, so it shows in the register's Num column
  instead of leading the Description. Such an id recurs on at most a payment and its fee, so in a
  tokenized field it is dead weight that can never earn a Bayesian match. Match it as a **whole
  element**, never positionally: the first element is ordinary prose in most two-element arrays.
- **An opaque id never stays in a human-readable field, even when it cannot be emitted.**
  `BALANCE_CASHBACK-<uuid>` is 36 characters and can never reach `CHECKNUM`; it is still lifted out,
  and the cap then discards it. Losing it beats showing it — `FITID` keeps the row identifiable
  either way. Do **not** add a `MEMO` fallback for references that overflow the field. The
  counter-case is a reference a person actually uses — invoice numbers, tax remittance
  (`/NIP/…`, `/TI/…`), merchant descriptors (`BOLT.EU/O/…`) — which stays put. The test is opacity,
  not shape.
- **A fee row's reference is removed, not relocated** (`_FOREIGN_REFERENCE`). `FEE-CARD-<id>` names
  the id of the transaction being charged for, so it must never become that row's `CHECKNUM` — it
  would be a false unique id — and it does not stay in the text either. The prose beside it
  (`Wise Charges for: CARD-<id>`) carries the link, so no token is lost.
- **Reference prefixes are an explicit set with per-prefix id patterns**, not a generic
  `<PREFIX>-<digits>` rule. `BALANCE-<digits>` is the same shape as `CARD-<digits>` but identifies
  the balance and recurs across transactions, which makes it matcher signal worth keeping;
  `ACCRUAL_CHECKOUT-invoice-<digits>` has a prefix that means something to a reader. Both must keep
  failing to match.
- **`CHECKNUM` is capped at 12, and an over-long value is dropped, not truncated.** libofx's
  `check_number` buffer is `12 + 1` with the same unterminated-`strncpy` hazard as `NAME`, and
  `ofxtools` types the field as a strict `String(12)` (not the lenient `NagString` that lets an
  over-long `ACCTID` through with a warning), so an over-long value is a hard parse failure
  elsewhere. Truncating is right for prose and wrong for an identifier: a cut id is no longer
  unique but still looks like one. This is why the `TRANSFER-` prefix is dropped — 19 characters
  do not fit, the bare 10-digit id does. `REFNUM` alone is not an option: GnuCash tests
  `data->reference_number_valid` and then assigns `data->check_number` in that branch
  (`process_bank_transaction()`), so a `REFNUM`-only transaction gets an empty Num.
- **`NAME` is capped at 96 characters and `MEMO` at 390.** These are libofx's buffer sizes, filled
  with a `strncpy` that does not NUL-terminate at exactly the buffer size — an over-long value is a
  read past the end of the struct member, not a cosmetic issue. Not the spec's A-32/A-255.
- **Payee and memo text is folded to ASCII** (`to_ascii`). GnuCash **for Windows** silently drops
  every non-ASCII character on import; emitting UTF-8 "correctly" loses data. Folding is
  deterministic, which keeps the matcher's tokens stable. Scope, measured — do not widen it back:
  Linux builds of libofx keep the characters (0.10.3 and 0.10.9 tested), so this is the Windows
  build, not libofx's version and not OpenSP's. A `non SGML character number` error does **not**
  mean data was lost — it fires on the platforms that work too. Numeric character references
  (`&#220;`) fail everywhere and are not a way out. Before treating any upstream libofx release
  as the fix that retires this, re-run the fixtures against the **Windows** build specifically;
  libofx#60 closing is not that signal. Re-verified against Windows libofx 0.10.5 on 2026-08-13:
  17 characters of Polish/German/Estonian text arrive as 4 when the folding is bypassed.
- **`to_ascii` also drops C0 controls and DEL, and the raw-passthrough fields are validated
  rather than repaired.** The `< 128` keep-filter passed control characters, and one in a memo
  aborted the writer's `minidom` step with an uncaught `ExpatError` — the whole run, since the write
  loop has no per-account guard. A control that is one of `\t\n\v\f\r` becomes a space so a word
  boundary survives; the rest are dropped. Use that explicit set, **not `str.isspace()`**, which
  also calls U+001C–001F whitespace. **Five** fields reach the file without passing through
  `to_ascii` — the count was wrong three times, so re-derive it against the writer rather than
  trusting this line: `ACCTID`, `FITID`, `CURDEF`, `BANKID` and the counterparty number in
  `BANKACCTTO`. `validate_raw_fields` checks the first four at the **mapping step** — not in the
  writer, where coverage has already advanced and a rejection would leave the ledger claiming a day
  whose file was never written. `BANKID` is in that list although its provenance is *config*:
  `bank_id_for` only strips, upper-cases and truncates, so a typo in a hand-written `bankid` reaches
  the file exactly as an ASPSP's control character would — a likelier source, not a rarer one. Printable ASCII, deliberately **not** a tight alphanumeric whitelist: `entry_reference`
  is the majority `FITID` source and carries `- . _ / |` at four of seven banks (measured at three
  of six for #37; N26's bare-UUID `entry_reference`, measured 2026-08-13, carries `-`), and all
  95 printable code points round-trip through libofx byte-identically (measured 0.10.5). Never *repair* one of
  these: measured on Windows, a non-ASCII `FITID` comes back from libofx **mutated** rather than
  rejected (`TX-Ü-001` → `TX-\x1ce-001`), which re-imports as a new transaction, and a mutated
  `ACCTID` orphans the account. `BANKACCTTO` is the one exception: it is a counterparty's number, not
  this account's
  identity, and libofx does not parse the aggregate at all — so an unusable one is **omitted**
  rather than failing the account, and the folded `MEMO` copy (the one that actually routes
  accounts) is untouched. `CURDEF`'s `[A-Z]{3}` is the one rule libofx does not force — it
  parses `pln` happily — so it rests on ISO 4217 being a closed contract, not on the parser.
  Accepted cost, so a future outage is diagnosable: an ASPSP sending a lower-case code would
  fail those accounts on every run rather than being upper-cased, and `currency_or_none`
  normalizes `XXX` but not case. 100% of measured values conform and none has been observed;
  if one ever is, normalize at that boundary **and** in the mapper's currency comparison,
  which would otherwise start mismatching.
- **Account numbers are normalized to IBAN form** (`normalize_account_number`). A country prefix
  is added only when the IBAN checksum passes, so a value we cannot prove is an IBAN is left
  alone. Read account numbers with `account_identifier`, never `acct["iban"]` directly: several
  ASPSPs leave `iban` null and put the number under `other.identification`.
- **A census field whose *keys* come off the wire is gated to a vocabulary and bucketed, never
  counted as received.** `StateCensus.identification_scheme_kinds` keys on an ASPSP's
  `scheme_name`; anything failing `_SCHEME_TOKEN` is counted under `<non-conforming>`.
  `FilenamePrediction.disambiguator_kind` needs no gate because its keys are ours. Decision 2 of
  [`docs/adr-evidence-tool.md`](docs/adr-evidence-tool.md) is that a raw identifier is
  *structurally* unable to reach a census's output — a bare `Counter` over an ASPSP string rests
  that on the ASPSP instead, and this repo is public. **The gate excludes digits, not length**: an
  account identifier of any form carries them, while a 15-character Norwegian IBAN is
  token-*shaped*. Do not relax it to an allow-list of the names seen so far either — `PLKNR` is in
  our own fixture and in no code list, and reporting an unknown bank's vocabulary is the field's
  whole purpose.
- **`tools/evidence/` may traverse `SessionState.raw`, but only for structural facts.** A length, a
  presence, a count — never a value, and never a value used as a dict key: that body holds account
  numbers and account names, same sensitivity as the cache dir. `all_account_ids_lengths` keys on
  the length deliberately, not on the scheme. An internal helper may hold `dict[str, Any]`; the
  frozen-dataclass boundary is what makes that safe, so nothing off it may be returned.

## Data

**This repository is public.** Everything below applies to code, tests, fixtures, docs, commit
messages, PR descriptions and issues alike — anything that reaches the remote.

- **Never** include real account numbers, payee names, employer or client names, invoice numbers,
  balances, or transaction counts and volumes. A transaction count is a financial-scale signal even
  with the amounts removed.
- One exception to "invented": a value published as a per-country documentation example may be
  kept when the comment where it is defined cites where it is published and when that was
  verified (`EXAMPLE_IBAN` in `tests/test_coverage.py` is Poland's). It is a documented example,
  not a real account; `leak-check` treats a so-labeled value like the synthetics rather than
  re-sanitizing it, and re-sanitizing one destroys the provenance work with green tests
  throughout.
- Use invented substitutes. `tests/fixtures/enablebanking_transactions.json` is the reference:
  `PL00000000000000000000001`, `ACME Sp. z o.o.`. A well-known retail brand inside an obviously
  fabricated transaction is fine; an individual, an employer or a client is not, however
  fabricated the amount next to it.
- Where a test needs an account number that survives `normalize_account_number()`, random digits
  will not do — the `PL` prefix is added only when the IBAN checksum passes. Generate one:

  ```python
  body = "99999999" + "0000000000000001"     # unassigned bank code + account part
  nrb = f"{98 - int(body + '252100') % 97:02d}{body}"   # -> "PL" + nrb is checksum-valid
  ```

  Label the result as synthetic in a comment where it is defined.
- **Never paste a `state/<bank>.json`.** It holds the raw link response: account numbers, account
  names and labels. It looks like config and is not.
- When diagnosing a real fetch, sanitize before quoting. `run._redact_account()` (`run.py`) masks
  account numbers in progress output, but a hand-pasted sample in an issue bypasses it entirely —
  and the pasted register lines are the part that leaks a counterparty, not the code.

## Security

- Secrets (Enable Banking app id + private key) come from env/`.env` only and must
  **never** be logged or committed. `.env`, `*.pem`, `*.key`, local state, and `*.ofx` are
  gitignored — keep it that way.
- `output/`, `state/` and `cache/` hold real financial data. They are gitignored; never quote their
  contents into a commit, an issue, or a test fixture.
