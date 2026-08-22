# Testing OFX output against real libofx

`pytest` parses generated files with `ofxtools`, which checks the OFX is well-formed. It does not
check what GnuCash actually does with one — GnuCash uses **libofx**, a separate parser with its
own character-set handling and buffer limits, and the GnuCash GUI swallows its parse errors
entirely.

`ofxdump` — libofx's own CLI — closes that gap without launching GnuCash. It loads the same
libofx GnuCash does and prints exactly what libofx hands to its caller: the register has been
confirmed to show the identical string, character for character, that `ofxdump` reports. Run it
before proposing any change to `ofxout.py`, and especially before touching `to_ascii()`,
`compose_name()`, `compose_memo()`, or `compose_check_number()`.

```sh
ofxdump file.ofx > out.txt 2> err.txt; echo "exit=$?"
```

- **Exit code** is `1` on parse errors, `0` when clean — assertable without parsing output.
- **`stdout`** carries `Name of payee or transaction description:` and `Extra transaction
  information (memo):` — exactly what lands in the register's Description and Memo.
- **`stderr`** carries the OpenSP parse errors the GnuCash UI never shows.

A distro's `libofx` package is not necessarily the version GnuCash bundles, so anything that
shells out to `ofxdump` (a test, a script) should skip when it is absent rather than pin a
version.

## Where to get `ofxdump`

**Windows** — it ships with GnuCash for Windows and loads the same DLL GnuCash itself uses:

```
C:\Program Files (x86)\gnucash\bin\ofxdump.exe
```

The SGML declaration and DTDs it parses against are alongside it, in
`C:\Program Files (x86)\gnucash\share\libofx\dtd\` (`opensp.dcl`, `ofx160.dtd`, `ofx201.dtd`).

**Debian / Ubuntu**:

```sh
apt-get install ofx
```

## Testing on Linux without a Linux machine

A container is enough — no GnuCash install required, just `ofxdump` itself:

```dockerfile
ARG BASE=debian:stable-slim
FROM ${BASE}
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ofx python3
COPY variants/ /work/variants/
```

`BASE` picks the libofx version under test — `debian:stable-slim` and `ubuntu:22.04` are both
known to work. Build, then run `ofxdump` per file inside the container the same way as above.

This matters because **libofx's behaviour is not uniform across platforms** — the Windows build
of libofx/OpenSP transcodes bytes through CP437 before the character-set check, which the Linux
build does not do, so the same file can parse cleanly on one and lose data on the other. If a
change is meant to affect character handling, test both; if it is not, testing either is
representative for structural issues (buffer truncation, tag validity, exit code).

## Automated: `tests/test_ofx_conformance_linux.py`

CI installs `ofx` (`apt-get install ofx`, no version pinned — skips rather than fails if the
package is unavailable) and runs this module against a handful of fixtures: a normal case, the
`NAME`/`MEMO` boundary truncations, and an over-long `CHECKNUM` being dropped rather than
truncated. It also checks the written file for `\r\n` without `\r\r\n`.

**It proves parse acceptance and the buffer/line-ending behavior above — nothing about ASCII
folding.** `to_ascii()` already runs before any fixture in that module is written, so every string
CI hands to `ofxdump` is already pure ASCII; Linux libofx keeping non-ASCII characters is true and
irrelevant to a file with none left in it. The folding check stays exactly as described above:
manual, Windows-gated, via the `ofx-conformance` agent. See
[`docs/adr-ofx-ci-conformance.md`](adr-ofx-ci-conformance.md) for the reasoning behind this split.

## Manual: importing a batched account into GnuCash

`ofxdump` proves what libofx hands its **caller**. GnuCash is a different caller, so two questions
about `--batch-size --combine` needed a person and a throwaway book. Neither could misplace a
transaction, but the first affects a balance you would reconcile against. **Both were answered
2026-08-17 for GnuCash 5.16** — the result is at the end of this section and in
[`adr-ofx-batch-splitting.md`](adr-ofx-batch-splitting.md) §12; the procedure below stays as the
re-verification recipe, because a pass is a statement about one GnuCash version.

Build the fixtures (`*.ofx` is gitignored, so regenerate rather than hunt for them):

```sh
uv run python tools/gnucash_import_fixtures.py /tmp/gnucash-batch-test
```

Two files, each carrying **one `BANKID`+`ACCTID` pair across three `<STMTTRNRS>`** — the shape
`--batch-size` and `--combine` together produce, and the one
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md)'s own import never exercised, since every
statement there belonged to a different account. Each batch carries a distinct `LEDGERBAL`:

| file | batches (date → `LEDGERBAL`) | transaction total |
|---|---|---|
| `batch_new_account.ofx` | 05-04 → **-30.00**, 05-11 → **-300.00**, 05-18 → **5000.00** (live) | -333.00 |
| `batch_existing_account.ofx` | 06-01 → **-20.00**, 06-08 → **-750.00**, 06-15 → **7000.00** (live) | -777.00 |

Every row's Description names the batch it came from, so the register check is unambiguous.

**Use a throwaway book, never the real one.** A PLN book avoids conversion prompts.

**Round 1 — `batch_new_account.ofx`, into a book where the account does not exist.**

1. **Count the account-matching prompts.** One pair appears three times: **1 prompt** means
   GnuCash de-duplicates pairs within a file, **3** means it does not (cosmetic, not a misroute).
2. Confirm it offers to create **one** account, not three.
3. Note whether one combined transaction-import window follows, with a per-line account column.
4. In the register afterwards — not the assistant's summary — confirm all six transactions landed
   in that one account, in date order, balance **-333.00**.
5. **Actions → Reconcile**, and read the pre-filled *Ending Balance*. This is the whole point:

   | value | what it means |
   |---|---|
   | **5000.00** | the **last** statement's `LEDGERBAL` wins — what decision 5 assumes |
   | **-30.00** / **-300.00** | an **earlier** batch's balance wins; a closed sub-window would mislead reconciliation |
   | **-333.00** | `LEDGERBAL` ignored, computed from the register instead — harmless |

   Then **cancel** the reconcile.

**Round 2 — `batch_existing_account.ofx`, into the same book.** Expect **zero** matching prompts
now that the pair exists; any prompt is a finding. Register afterwards: twelve transactions,
balance **-1110.00**. Reconcile should offer **7000.00** if round 1 answered "last statement wins".

**Record the GnuCash version either way.** A pass is a statement about one version — the same bar
`adr-combined-ofx-file.md` set, and the reason re-verification is owed against whatever version is
current later.

**Answered 2026-08-17 for GnuCash 5.16** (Build ID 5.16+(2026-06-27), Windows): one prompt in
round 1, zero in round 2, and the reconcile pre-fill was the register-computed total both times —
the table's third row, `LEDGERBAL` ignored. Full record in
[`adr-ofx-batch-splitting.md`](adr-ofx-batch-splitting.md) §12; this procedure stays as the
re-verification recipe for future GnuCash versions.

## Related reading

- [`docs/decisions.md`](decisions.md) — why `to_ascii()`, the `NAME`/`MEMO` composition, and the
  `CHECKNUM` length cap are shaped the way they are; several of those decisions were made using
  this harness.
- [#1](https://github.com/skolima/gnucash-ofx/issues/1) and
  [#4](https://github.com/skolima/gnucash-ofx/issues/4) — the investigations that produced this
  harness, including the platform-specific root cause (in progress).
