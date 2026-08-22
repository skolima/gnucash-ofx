# gnucash-ofx

Pull bank transactions through Open Banking (PSD2) and emit **OFX files** ready to import into
[GnuCash](https://www.gnucash.org/) — instead of logging into each bank every month, downloading
statements by hand, and re-typing them.

It is a personal-scale command-line tool: you run it, on your own machine, against **your own
accounts**. It needs a free [Enable Banking](https://enablebanking.com/) account in *Restricted
Mode*, which is limited to accounts you own and requires no commercial contract. There is no
server, no hosted component, and nothing leaves your machine except the API calls to your banks.

OFX rather than CSV, because it carries stable per-transaction IDs (`FITID`) for reliable
de-duplication and rich payee/memo fields that feed GnuCash's Bayesian import matcher. This tool
only produces clean OFX — **categorization stays in GnuCash**, where the history you already have
trains the matcher far better than any rule this tool could apply.

The banks listed below are the ones actually exercised; any PSD2 institution Enable Banking covers
should work, and reports of other banks are welcome.

> [!WARNING]
> Generated files contain **real financial data**. `output/`, `state/` and `cache/` are gitignored
> for that reason — see [SECURITY.md](SECURITY.md) before sharing logs, files, or bug reports.

## Sources

| Source | Mechanism | Status |
|--------|-----------|--------|
| Alior Bank, Bank Millennium, Erste (Santander Bank Polska), N26, Revolut, Wise (personal + business) | [Enable Banking](https://enablebanking.com/) PSD2 API (free Restricted Mode for your own accounts) | v1 |
| mBank IKE/IKZE | TBD (likely outside PSD2 scope) | stretch |
| Interactive Brokers | Flex Web Service + `ibflex2` (not a PSD2 bank) | stretch |

## Quick start

```sh
uv sync                       # create venv, install deps
uv run gnucash-ofx --help
```

### One-time setup

1. **Enable Banking** — create an account, generate an RSA keypair, register a *production*
   application, enable **Restricted Mode** and whitelist your own accounts. Put the application
   id and private-key path in `.env` (see `.env.example`). This covers every bank, **including
   Wise** — no separate Wise API token.
2. Copy `config.example.toml` to `config.toml`. Find the exact bank names to use with
   `uv run gnucash-ofx aspsps --country PL` (and your other countries) and paste them into the
   `aspsp`/`country` fields.

### Monthly runbook

```sh
uv run gnucash-ofx status                         # consent expiry and coverage per bank
uv run gnucash-ofx link alior                     # only when consent has expired (~every 180 days)
uv run gnucash-ofx fetch                          # resumes where the last run got to
# import the generated OFX files from ./output into GnuCash
```

`--from` and `--to` are optional. With neither, each bank resumes from what it has already
fetched — with a few days of deliberate overlap, because a bank can book a transaction with a
booking date earlier than when it appears, and `FITID` de-dups the overlap away on import. A bank
with no record yet starts 89 days back, which is as far as PSD2 guarantees anywhere. Give either
end explicitly to override it:

```sh
uv run gnucash-ofx fetch --from 2026-05-01 --to 2026-05-31
```

Retyping the range every month is the main way a month gets skipped, so the default exists to stop
you having to. What has been fetched is recorded per account in `state/coverage/<bank>.json` — dates
and opaque account digests only, no amounts and no account numbers.

### When something needs attention

`fetch` warns on stderr, without failing, when it can see that silence is about to cost you data:

- **a coverage gap** — days that will still be unfetched after this run, with the date they age out
  of the ~90 days PSD2 guarantees, and the exact command that closes them;
- **a consent within 45 days of expiring** — on a bank serving only ~90 days of history, a lapse
  nobody notices costs the months in between, permanently;
- **an account still identified by an opaque id**, which the bank regenerates on every re-link, so
  GnuCash would see brand-new accounts each consent cycle and orphan the imported history. One
  `link` fixes it.

Exit codes do not change: 1 only when a bank actually failed. For a scheduled run that wants to be
told, `status --check` exits 1 when any bank needs attention:

```sh
uv run gnucash-ofx status --check || echo "something needs a look"
```

### Checking a run before you spend it

```sh
uv run gnucash-ofx fetch --from 2026-05-01 --to 2026-05-31 --dry-run
```

`--dry-run` reports what the same command without it would do — per bank, each account, its
currency, and the exact path of the OFX file it would write — and then stops. It makes **no API
call** and neither reads nor writes the 6h cache, so adding a bank to `config.toml` or
sanity-checking a date range costs nothing against your rate-limit allowance. It is not recorded
in `state/fetch-log.jsonl` either: that file is an account of what was spent.

The paths are the real ones. They are built by the same code the fetch uses, including the
suffix that disambiguates two accounts of one bank in the same currency — which is grouped over
every account in the session, so the prediction does not change depending on which accounts
happen to have transactions. What a dry run cannot know is *which* accounts have transactions in
the range, and an account with none writes no file; so the list is what would be written, an
upper bound.

A bank linked before account currencies were recorded (see `link` above) is reported rather than
guessed at, and reported *for the whole bank*: the currency is part of the filename, and an
account whose currency is unknown might be the same-currency sibling that decides whether its
siblings get a disambiguation suffix at all. Re-`link` that bank and the prediction comes back.

A bank whose consent has lapsed, is not linked, or has an unreadable `state/<bank>.json` is
reported as such instead of being listed as if it would succeed. That does not make the command
fail: **`--dry-run` exits 0 whenever the configuration resolves**. The fatal cases are the same
global ones as a real fetch — missing `EB_APP_ID`/`EB_PRIVATE_KEY`, a malformed `config.toml` or
date, `--from` later than `--to`, or `--bank` naming a bank that is not configured.

As with a real fetch, predicted paths go to stdout and everything else to stderr, so
`fetch ... --dry-run > files.txt` previews the set of files you would be importing.

Wise is just two more linked banks (`wise_personal`, `wise_business`) — each is a separate Wise
login, so `link` each once. Multi-currency Wise balances produce one OFX file per currency.

Consent (PSD2 SCA) needs a one-time browser login per bank roughly every 180 days — not monthly,
and never per-download. Monthly `fetch` runs inside that window are unattended.

`link` asks for as long a consent as the bank allows, reading `maximum_consent_validity` from
`GET /aspsps` (180 days for every bank listed above). Run `gnucash-ofx status` to see days
remaining per bank.

`link` is also the only chance to record what each account *is* — its number, currency and name.
The API does not return those again, so they are saved to `state/<bank>.json` and reused on every
fetch. Banks linked with an older version keep working on what was saved then, and pick up the rest
at their next re-link; nothing needs re-linking early.

### Combining every account into one file

```sh
uv run gnucash-ofx fetch --combine
```

By default `fetch` writes one `.ofx` per account per currency, which means one pass through
GnuCash's import assistant per account — 8–10 dialogs in a typical month across every linked
account. `--combine` writes everything a run produced into a single file instead, so importing is
one pass. It changes nothing about what is fetched (same requests, same rate-limit cost) or about
what each statement contains — only how many statements share a file.

**Off by default**, and verified against GnuCash 5.16 specifically, not every version — see
[`docs/adr-combined-ofx-file.md`](docs/adr-combined-ofx-file.md) for the measurements and the real
manual-import test the flag is gated on. Per-file output is the path with years of real mileage on
it and stays what a bare `fetch` produces; reach for `--combine` once you've confirmed on your own
GnuCash version that it imports the way you expect. `--dry-run --combine` predicts the single
combined path the same way a plain `--dry-run` predicts the per-account ones.

### Splitting a large catch-up into review-sized batches

```sh
uv run gnucash-ofx fetch --batch-size 120
```

Every file this tool writes ends at GnuCash's import assistant, where you accept or correct each
proposed match — and that dialog is what trains GnuCash's matcher. A catch-up after a lapsed
consent or a holiday can put months of transactions in one list, which is where rows get
rubber-stamped or the review gets abandoned halfway.

`--batch-size N` splits each account's transactions into files of at most `N`, oldest first, so
one `fetch` produces several right-sized imports instead of one unwieldy one. Each file is named by
the range it actually covers, exactly as if you had run `fetch --from … --to …` by hand for that
sub-window — a batch is an ordinary statement, and looks like one.

**A booking date is never split**, so `N` is a cap rather than a guarantee: a single day with more
than `N` transactions stays whole in one file. Splitting a day would separate related entries — a
payment and its fee, the two sides of a transfer — across two review sittings, which is exactly
what you want to see together.

**Start at 120** if you are unsure. That leaves an ordinary run alone and splits only a genuine
multi-month catch-up; pick lower if you prefer shorter sittings. Off by default, and it changes
nothing about what is fetched — same requests, same rate-limit cost. It composes with `--combine`:
a batched account contributes its several statements to the one combined file.

**`--dry-run` cannot predict batch filenames.** Batch boundaries depend on transaction counts and
dates, which a dry run never sees, so `--dry-run --batch-size N` predicts **no paths at all** and
says so rather than guessing. Everything else a dry run reports — a lapsed consent, a coverage gap —
still applies.

### Setting packaging in `config.toml`

`--combine` and `--batch-size` are preferences rather than per-run choices, so both can be set once
instead of retyped:

```toml
[fetch]
combine = true
batch_size = 120
```

A command-line flag always wins over the file. To turn a configured setting off for a single run,
use `--no-combine` or `--no-batch-size` — `--batch-size 0` is rejected as invalid rather than
treated as "off". `refresh` is deliberately **not** settable here: it re-fetches from the bank and
spends rate-limit allowance, so it stays a deliberate per-run act.

Because settings in the file are invisible in the command you typed, a run whose packaging is not
the default states it on stderr before it starts.

### Rate limits

PSD2 banks cap **background** data fetches (no end-user present) at roughly **4 times per day**.
This tool fetches in **online** mode — it sends `Psu-Ip-Address`/`Psu-User-Agent` headers because
you, the end user, are the one running it — which carries much higher limits.

Your public IP is detected automatically, across several lookup services. **You do not need to
configure it**, and on a dynamic address you should not: `EB_PSU_IP` in `.env` is only a fallback
for when every lookup fails, so a value pinned there goes stale and is ignored the moment detection
disagrees with it. If no address can be determined at all, `fetch` **stops** rather than quietly
spending the ~4/day background allowance; `fetch --allow-background` overrides that when you mean
it.

Fetched transactions are cached **a calendar month at a time**, so a window overlapping one you
already fetched only pays for the months it adds — asking for May, then for May and June, fetches
June. A month that has closed does not expire, so backfills, retries and re-imports are cheap; only
the current month and the couple of weeks behind it are re-fetched, since that is the part still
moving. `fetch --refresh` ignores all of it and re-fetches, which is the right tool if you have
reason to think the bank changed something older.

If you still hit the limit, the bank's allowance resets after about 6 hours — retrying sooner
cannot help and spends requests finding that out, so the tool stops rather than looping. Every run
records what it sent to `state/fetch-log.jsonl`, including whether it was online or background —
that file is the first thing to read when a `429` shows up.

### When one bank fails

A bank failing does not cost you the others. `fetch --bank all` records the failure, carries on,
writes every file it can, and then reports what went wrong — per bank, and per account within a
bank. The files that were written are real and importable; nothing is rolled back. A corrupted
`state/<bank>.json` for one bank is treated the same way: that bank is reported as failed, and its
siblings still get their files.

**The exit code is 1 whenever any bank failed**, even though files were written, so a cron job or
script still notices. Exit 0 means every requested bank succeeded. (Exit 2 is argparse's, for a
malformed command line.)

Failed accounts cache nothing, so **re-running the same command retries only the failures** — the
banks that worked are served from cache and spend no further rate-limit allowance.

Written paths go to stdout, one per line and nothing else — no header line, no indent; the summary
of how many went where, the progress and the failure report all go to stderr. So
`gnucash-ofx fetch ... > files.txt` gives you a list of files to import that needs no cleaning up
before you feed it to something, and an empty file when there was nothing to write.

Some failures are still fatal for the whole run, because no bank could survive them: missing
`EB_APP_ID`/`EB_PRIVATE_KEY`, a malformed `config.toml` or date, or `--bank` naming a bank that is
not configured. Each of these prints a clear one-line error and exits — never a raw traceback, even
for a `config.toml` syntax error.

### How far back you can fetch

PSD2 guarantees only ~90 days of history without a fresh consent, and banks enforce it
differently. **Alior, Alior Kantor and Erste reject any window starting more than ~90 days ago**
(`400 ASPSP_ERROR`); Millennium and Wise serve much longer history. So a full-year backfill is
only possible for some banks — for the rest, fetch regularly and keep the OFX files, because that
history becomes unreachable once it ages out.

### What shows up in the register

GnuCash's single-line register shows only the Description, which its OFX importer fills from `NAME`
(falling back to `MEMO`). Sending the bare counterparty name there hides the remittance information
— usually the part that says what a payment actually was — and leaves repeat payments to the same
counterparty indistinguishable without opening each one.

So `NAME` is composed, **remittance first, then the counterparty**:

```
Monthly payment for hosting; ACME Sp. z o.o.
```

This mirrors `gnc_ab_description_to_gnc()` in GnuCash's own AqBanking importer, which builds the
Description the same way from the same kind of SEPA data — including skipping a component already
contained in what came before it, so `Sent money to ACME Sp. z o.o.` does not gain a `; ACME Sp. z
o.o.` tail. `MEMO` keeps the remittance plus the counterparty IBAN, so nothing is lost when `NAME`
hits its length cap.

Some sources add a **machine reference** of their own — Wise tags transfers, card payments and
cashback with one. That is not remittance text: it teaches the matcher nothing, and in the
Description it would take the leading characters the remittance is meant to occupy. It is split out
and written to `CHECKNUM`/`REFNUM` instead, which GnuCash shows in the register's **Num** column —
still visible, still what tells two otherwise identical payments to the same counterparty apart,
but out of the way of the text.

Where a reference cannot be written there — Wise's cashback id is a 36-character UUID, and the
field holds 12 — it is dropped rather than left in the Description. An opaque id is not text, and
the transaction stays identifiable through its `FITID` either way. A reference a *person* uses is
different and stays put: invoice numbers, the tax remittance Polish banks send, or a card
descriptor like `BOLT.EU/O/…` that carries the only copy of the merchant's name.

### Account numbers and GnuCash matching

GnuCash routes imported transactions to accounts with a Bayesian matcher that tokenizes the
description (`NAME`) and memo (`MEMO`). libofx — which GnuCash uses to parse OFX — does not expose
the `BANKACCTTO` aggregate at all, so counterparty account numbers are written **twice**: into
`MEMO`, which is the token the matcher actually learns, and into `BANKACCTTO`, the standard field,
for auditing and other OFX tools.

Account numbers are normalized to IBAN form, because ASPSPs report them inconsistently — some send
a proper IBAN, some the domestic number (in Poland a 26-digit NRB with no country prefix), and some
put it under `other.identification` rather than `iban`. A `PL` prefix is only added when the result
passes the IBAN checksum, so foreign accounts are left untouched. This keeps a counterparty token
byte-identical to that same account's `ACCTID` in its own OFX file, which is what lets GnuCash link
transfers between your own accounts.

How much counterparty data you get depends on the bank. Alior returns it only since Enable Banking
fixed their integration (August 2026); before that its transactions had no counterparty fields at
all.

Payee names and memos are folded to ASCII (`Kämpf OÜ` → `Kampf OU`, `własny` → `wlasny`). This is
not cosmetic pedantry: **GnuCash for Windows silently drops every non-ASCII character on import**
— `Kämpf OÜ` arrives as `Kmpf O`, with no error anywhere in the UI. Folding keeps names readable
and, more importantly, stable, since the matcher learns whatever tokens it consistently sees.

The cause is the Windows build of libofx/OpenSP, and it is worth being precise about the scope,
because it is narrower than it looks:

- **Windows loses the characters. Linux does not.** The identical file keeps its diacritics under
  libofx 0.10.3 and 0.10.9 on Linux, and loses them under the 0.10.5 that GnuCash for Windows
  bundles. libofx's version is not the variable — the *older* Linux build works — and OpenSP is
  1.5.2 on both. The Windows build transcodes each byte through CP437 before OpenSP's character
  check, after which the original byte cannot be recovered; the Linux build passes bytes through
  unchanged and they reassemble into valid UTF-8.
- **`non SGML character number` in a log is not, by itself, data loss.** That error appears on
  every platform, including the ones where the text survives intact.
- **Numeric character references (`&#220;`) fail everywhere**, Windows and Linux alike, with
  `"220" is not a character number in the document character set`. That one really is the
  US-ASCII document character set in libofx's SGML declaration, and it is why escaping is not an
  escape route.
- **OFX 2.x (XML) output changes nothing** on either platform — see
  [#4](https://github.com/skolima/gnucash-ofx/issues/4).

So the folding is a workaround for one platform, applied unconditionally. If you run this on
Linux and import there, it is costing you diacritics you would otherwise keep — see
[#1](https://github.com/skolima/gnucash-ofx/issues/1).

Each bank's OFX `BANKID` comes from its optional `bankid` config option — normally the bank's BIC
(`bankid = "ALBPPLPW"`), falling back to the bank key truncated to OFX's 9-character limit. GnuCash
derives an account's `online_id` from `BANKID`+`ACCTID`, so **set this before your first import**;
changing it later orphans the accounts already imported. Leave it unset for a connection whose
accounts span several institutions (Wise services each currency through a different local bank).

> **Re-link banks linked before v0.1.** An account's `ACCTID` is its IBAN, captured at `link` time.
> Sessions created earlier fall back to an opaque UID — and UIDs change on every re-link, so GnuCash
> would see a brand-new account each consent cycle. Run `gnucash-ofx link <bank>` once per bank.

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```

Test-driven: every fetcher/mapper starts from a sanitized JSON fixture and an OFX assertion. No
live API calls in tests. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started,
[AGENTS.md](AGENTS.md) for conventions,
[docs/enable-banking.md](docs/enable-banking.md) for how the API really behaves, and
[docs/decisions.md](docs/decisions.md) for why the odd-looking choices are what they are.

## Security

Never commit `.env`, private keys, local state, or generated `.ofx` files — all are
`.gitignore`d. Secrets are read from the environment only and must never be logged.

`output/`, `state/` and `cache/` hold real transaction data, account numbers and consent state.
Treat them like the statements they are made of. Before pasting anything from a real run into an
issue, read [SECURITY.md](SECURITY.md) — it says what to strip and how to report a vulnerability
privately.

## License

MIT — see [LICENSE](LICENSE).
