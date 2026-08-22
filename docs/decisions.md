# Design decisions

Choices that are non-obvious, or expensive to reverse, with the reasoning that produced them.
Invariants live in [AGENTS.md](../AGENTS.md); this file explains *why* they are invariants.

Several decisions are load-bearing for GnuCash's import matcher. GnuCash derives an account's
`online_id` from **`BANKID` + `ACCTID`**, and its Bayesian destination-account matcher tokenizes
only the transaction **description (`NAME`) and memo (`MEMO`)**. Anything that changes those
values between exports either orphans imported accounts or resets what the matcher has learned.

Many of the sites below also carry a two-line `# invariant: ... / # AGENTS.md#invariants` comment
in the code itself, right on the line a "cleanup" diff would touch. That anchor is a pointer, not
a summary — the argument stays here. (This said "ten" when the convention landed in
[#25](https://github.com/skolima/gnucash-ofx/issues/25); the count grew with every invariant since
and is not maintained — `grep -rn "# invariant:" src/` is the census.)

---

## `BANKID` identity

**Decision.** `BANKID` comes from an explicit per-bank `bankid` option (normally the bank's BIC),
falling back to the config key truncated to 9 characters.

**Why not auto-fill from Enable Banking's `bic_fi`?** It would drift. A bank with no BIC today
that gains one later would silently change `BANKID`, and with it `online_id`, orphaning every
account already imported from that bank. An explicit value is stable regardless of what the
aggregator does, works for banks that never supply a BIC, and can be set without re-linking.

**Why 9 characters?** OFX types `BANKID` as A-9. Config keys like `wise_personal` (13) exceeded
it and made files fail strict OFX validation. An 11-character BIC is reduced to its 8-character
primary-office form, which identifies the same institution.

**Cost to change:** high — pick before the first import. Changing it later means re-mapping or
re-importing every affected account in GnuCash.

**Deliberately unset** for connections whose accounts span institutions (Wise), where no single
BIC is correct.

---

## `ACCTID` identity

**Decision.** `ACCTID` is the account's IBAN, resolved as
`iban` → `other.identification` → `identification_hash` → `uid` — except in a connection where
any IBAN is shared, which resolves wholesale through the hash (next section).

**Why.** Enable Banking regenerates account UIDs on **every re-link**. Using the UID meant GnuCash
saw brand-new accounts each consent cycle (~every 180 days) — silently, since the import simply
created new accounts. The IBAN is stable, human-recognisable in the import dialog, and already
the real account number. `identification_hash` is Enable Banking's own cross-session identifier
and covers accounts that report no account number at all.

**Cost to change:** high — same `online_id` consequence as `BANKID`.

Accepted wart: IBANs (28 chars for Poland) exceed OFX's `ACCTID` limit of A-22. GnuCash and
libofx tolerate it and the value round-trips intact; `ofxtools` emits a warning only.

---

## A value shared by several accounts of one connection is not an `ACCTID` for any of them

**Decision.** Once any stored identifier is shared between the accounts of one connection, the
**whole connection** resolves `ACCTID` through `identification_hash` — the unique-IBAN sibling
included (the uniform policy, option M, accepted 2026-08-14;
[adr-revolut-onboarding.md](adr-revolut-onboarding.md) decision 1, shipped in
[#49](https://github.com/skolima/gnucash-ofx/issues/49)). The determination is one per-connection
fact (`_iban_shared_within`, computed from the stored account set) threaded to every resolution
site; the callers that decide from `state/<bank>.json` alone — window resolution, the dry run,
`status`, `tools/evidence` — go through `_stored_acctids`, so none can drift from what the fetch
writes about the same accounts.

**Why.** Revolut reports one master LT IBAN on four of five currency pockets, so the IBAN-derived
`ACCTID` named a group: the coverage ledger held 2 entries for 5 pockets (measured on disk,
2026-08-13), and — because Revolut's `FITID` is `entry_reference`, the field both legs of a
currency conversion share byte-identically — GnuCash's per-account `FITID` de-duplication
silently dropped the second leg in the default per-pocket import (gate, 2026-08-13, GnuCash 5.16;
next section). Data loss with no error anywhere.

**Why not the narrow per-account rule** — reject only the IBANs that actually collide, let the
unique-IBAN PLN pocket keep its own? Because it made PLN's identity depend on its *siblings*: the
IBAN stayed its `ACCTID` only while no sibling shared it — an ASPSP-side property, changeable on
any re-link, flipping silently with no uniqueness warning anywhere in the tool. Chosen at
acceptance, 2026-08-14, while nothing had been imported and the extra orphaning was therefore
free; both cases are preserved at full strength under the ADR's rejected option M.

**The floor is part of the rule.** An account with no stored `identification_hash` keeps its
IBAN — shared or not — and warns (`_warn_shared_iban`, keyed on the symptom like the
stale-identity warning); it never falls to `uid`, which Enable Banking regenerates on every
re-link: a shared-but-stable identifier mis-files statements recoverably, a per-link one orphans
the account every consent cycle. And only the **stored** hash counts in the shared branch
(invariant-guard finding, 2026-08-14): a fetch-time rescue would make a floored account's
`ACCTID` depend on `GET /sessions` still reporting a hash — a value the state-only callers can
never see — so fetch and `status` would silently diverge about the same account.

**Deliberately not scheme-validated.** Whatever the stored identifier field holds is what
`ACCTID` resolves to, so two accounts sharing it *is* the collapse regardless of scheme; gating
on "is it really an IBAN" would reintroduce the defect for a shared-BBAN connection.

**Cost to change:** high — the `online_id` orphaning above, plus the measured silent transaction
loss for any connection whose conversion legs share a `FITID`. The trigger is false for all 19
pre-existing accounts, so no configured bank's `ACCTID` moved when this shipped.

---

## GnuCash's `FITID` de-duplication is per account and across import runs, not within one

Two measured properties of GnuCash and of packaging — not of Revolut or any bank — recorded here
so they survive the fix that occasioned measuring them. Both measured 2026-08-13 on GnuCash 5.16
(build 2026-06-27), synthetic artefacts imported into throwaway books; method and full record in
[adr-revolut-onboarding.md](adr-revolut-onboarding.md) open question 1. A pass is a statement
about that one version and the import shapes actually run.

- **De-duplication is scoped to the account and applies across import runs, not within one.** A
  single combined import of statements carrying one `FITID` twice under one `BANKID`+`ACCTID`
  keeps both transactions; sequential per-file import — the default packaging — silently drops
  the second: no row in the matcher, no flag, no error. **The default packaging is the dangerous
  one; do not average the two results.** The same `FITID` under two *distinct* `ACCTID`s survives
  every path.
- **OFX matching consults `CURDEF` exactly once, as the pre-selected commodity when creating a
  new account — never to distinguish accounts.** Statements differing only in `CURDEF` land in
  whichever account the `BANKID`+`ACCTID` pair names, and foreign-currency transactions arrive
  there as 0.00 UNBALANCED rows — values destroyed, not converted.

Any future change that can put one `FITID` under one account identity twice re-runs this loss,
whatever bank is involved.

---

## Counterparty account number in **both** `MEMO` and `BANKACCTTO`

**Decision.** Write it to the standard `BANKACCTTO` aggregate *and* append it to `MEMO`.

**Why.** `BANKACCTTO` is the correct OFX field, but **libofx does not parse it at all** — it is
absent from libofx's transaction struct, so GnuCash never sees it. The matcher only tokenizes
`NAME`/`MEMO`, so the memo copy is the one that actually drives own-transfer detection and
recurring-payee classification. The `BANKACCTTO` copy is for correctness, auditing, and any
other OFX consumer.

**Do not "clean up" by dropping the memo copy** — that silently disables account routing.

---

## The counterparty is the side the direction names, never "whichever side is populated"

**Decision.** `_counterparty()` and `_counterparty_iban()` (`sources/enablebanking.py`) read the
**creditor on `DBIT`, debtor on `CRDT`**, keyed on `credit_debit_indicator` — never by checking
which side of the payload happens to be populated.

**Why.** N26 populates **both** `creditor_account.iban` and `debtor_account.iban` on every
transaction, own side included — Millennium's both-sides pattern, but under `iban` rather than
`other.identification`. Measured 2026-08-13 over a real fetch (one account, a very small same-day
SEPA sample; "every" means "in every transaction observed"). Millennium fills both sides on the
wire too, but no fixture before N26's carried an own-side account — so a "whichever side is
populated" simplification **passes every other bank's fixture** while being wrong at two banks,
and at N26 it silently writes the user's *own* IBAN into `MEMO` on every transaction.

**Why that failure is expensive.** `MEMO` is the copy that actually drives account routing (see
*Counterparty account number in both `MEMO` and `BANKACCTTO`* above), so the Bayesian matcher
would learn the own-IBAN token from every N26 transaction and own-transfer detection would degrade
bank-wide — quietly, nothing fails. And the damage outlives the code: the learned associations
live in the GnuCash book's imap, so reverting the regression does not untrain them.

**The name reader carries the same rule for the same reason.** At N26 only the counterparty's
party object carries a `.name` (the own-side `creditor`/`debtor` object is null, as measured
above), so a populated-side fallback in `_counterparty()` would happen to pick the right side
today — but only the direction rule guarantees it, and a bank that fills both names would put the
account holder's own name into `NAME`, the register Description.

**Cost to reverse:** no identity field moves — `FITID`/`BANKID`/`ACCTID` are untouched, so nothing
orphans or re-imports. The cost is the matcher poisoning above, plus the fact that no fixture
other than N26's can reveal the regression. `test_n26_own_account_never_becomes_the_counterparty` (manifest
entry "i" in `tests/test_invariants.py`) is the only thing standing between the simplification and
a green suite; both code sites also carry the two-line invariant anchor.

---

## `NAME` composed from remittance + counterparty name

**Decision.** `NAME` is `"<remittance>; <counterparty name>"`, ASCII-folded, with any component
already contained in the accumulated text dropped, capped at 96 characters. `MEMO` is unchanged:
still the full remittance plus the counterparty IBAN.

**Why.** GnuCash's OFX importer sets the transaction Description from `NAME`, falling back to
`MEMO` only when `NAME` is absent (`fill_transaction_description()` in `gnc-ofx-import.cpp`). The
default single-line register shows nothing else, so a bare counterparty name pushed the remittance
out of view — and several payments to the same counterparty became indistinguishable in the
register without opening each one.

**Why this shape.** It is what GnuCash itself does with the same data. `gnc_ab_description_to_gnc()`
in the AqBanking importer joins remote name, purpose and ultimate creditor/debtor with `"; "` via
`gnc_g_list_stringjoin_nodups()`, and because that builds its list with `g_list_prepend` the emitted
order is **purpose before name**. `nodups` is not exact-match dedup: `utf8_strstr()` appends a
component only when it is not already a *substring* of what has accumulated — case-sensitively.
Both properties are ported as-is, including the asymmetry that containment only looks backwards
(a later component never retroactively removes an earlier one).

**Why the remittance stays in `MEMO` too.** `NAME` is capped, `MEMO` is not tight, so `MEMO` is the
copy that cannot lose text. The duplication costs nothing: `tokenize_string()` in
`import-backend.cpp` appends only tokens it has not already seen, so a repeated word gains no extra
weight in the Bayesian matcher.

**Retraining impact: near zero.** The matcher tokenizes the description and every split memo, and
for bank transactions `process_bank_transaction()` always sets the split memo from `MEMO`. Since
`MEMO` is unchanged and `NAME` only gains text that `MEMO` already carried, the *set* of tokens per
transaction is identical to before — existing imap training still applies. (The exception is text
lost to the 96-character cap, and the non-Bayesian exact-match imap, which keys on the whole
Description string. That path is only used when the Bayes preference is off, and it is on by
default.)

**Why 96 and 390, not the spec's A-32 and A-255.** libofx reads `NAME` into
`char name[OFX_TRANSACTION_NAME_LENGTH]` (`96 + 1`) and `MEMO` into `char memo[OFX_MEMO2_LENGTH]`
(`390 + 1`), both via `STRNCPY`, which is `std::strncpy(dest, src, sizeof(dest))`. `strncpy` does
**not** NUL-terminate when the source is at least as long as the buffer, so a value at exactly the
buffer size leaves the struct member unterminated and GnuCash reads past it. These are hard safety
ceilings, not style limits. The spec's A-32 is advisory here — libofx's own header comments that
`NAME` "can be the name of the payee or the description of the transaction", and `ofxtools` only
warns.

Truncation prefers a whitespace boundary so the matcher is never handed a partial token. In `MEMO`
the IBAN is reserved out of the budget rather than appended and hoped for — it is the last
component, so a naive cut would drop precisely the token that account routing depends on.

**Not `EXTDNAME`.** OFX has an A-100 extended-name field for exactly this, but libofx does not parse
the tag at all (`ofx_container_transaction.cpp` handles `NAME`, `MEMO`, `PAYEEID` and no
`EXTDNAME`), so GnuCash would never see it.

---

## Machine references go to `CHECKNUM` + `REFNUM`, not into `NAME`

**Decision.** A source's machine reference — Wise sends `TRANSFER-<id>`, `CARD-<id>` and
`BALANCE_CASHBACK-<uuid>`, each as its own `remittance_information` element — is split out in the
mapper into `Txn.reference`, kept out of `NAME` and `MEMO` entirely, and written to OFX `CHECKNUM`
**and** `REFNUM` as the **bare id**, capped at 12 characters, dropped rather than truncated above
that. A reference belonging to a *different* transaction (a fee row's `FEE-CARD-<id>`) is removed
and emitted nowhere.

**Why not leave it in the remittance.** It recurs on at most a payment and its fee, so it can never
contribute a Bayesian match — it only accumulates near-one-shot entries in the imap. Worse, it took
the leading characters of `NAME`'s 96, which is exactly the position the remittance-first ordering
exists to protect: a machine reference is the least useful thing a narrow register column or a
truncation cut can preserve. It also made the same token lead two adjacent columns, since `MEMO`
carried it too.

**Why `CHECKNUM` when it is not a check number.** libofx parses `CHECKNUM` into `check_number`, and
`process_bank_transaction()` passes that to `gnc_set_num_action()`, so it lands in the register's
Num column (or the split's Action field, per the book option). The id therefore stays *visible* and
stays unique, which is what makes repeat payments to one counterparty distinguishable — the
original complaint in [#2](https://github.com/skolima/gnucash-ofx/issues/2) — while the
Description goes back to prose and the tokenized fields stay clean. `ofxstatement` already writes
the tag, in the DTD-correct position between `FITID` and `NAME`.

**Why `REFNUM` as well, and why it cannot stand alone.** `REFNUM` is the semantically correct field
(A-32, and libofx's buffer matches). But GnuCash tests `data->reference_number_valid` and then
assigns `data->check_number` inside that branch — an upstream bug — so a `REFNUM`-only transaction
gets an empty Num. Writing both costs nothing (libofx prefers `check_number` when valid) and is the
same "correct field plus the one that works" split as `MEMO` + `BANKACCTTO`.

**Why the bare id, and why 12.** `OFX_CHECK_NUMBER_LENGTH` is `(12 + 1)` — libofx's smallest
buffer, filled by the same non-NUL-terminating `STRNCPY` as `NAME` and `MEMO`. Measured with the
GnuCash-bundled `ofxdump` 0.10.5:

| `CHECKNUM` written | libofx delivers |
|---|---|
| `TRANSFER-1234567890` (19) | `TRANSFER-1234` — silently truncated |
| 14, 15 characters | 13 characters |
| 13 characters | 13 — fills the buffer with no terminator |
| 12 characters | intact |

So the prefixed token was never an option. `TRANSFER-` is a constant carrying no information, and
the id itself is 10 digits in every transaction observed, which clears both this ceiling and OFX's
A-12. `ofxtools` matters here too: it types `checknum` as a strict `String(12)`, **not** the
`NagString` that lets an over-long `ACCTID` through with a warning, so an over-long value is a hard
`OFXSpecError` for any consumer that parses our files — including our own round-trip tests.

**Dropped, not truncated.** Truncation is correct for prose, where the surviving words still say
something, and wrong for an identifier: a cut id is no longer unique but still looks like one, and
the only reason to emit it is uniqueness. A reference that does not fit, or is not a plain token, is
simply absent — it is deliberately *not* smuggled back into `MEMO`, which would reintroduce exactly
the noise this removes.

**An unemittable reference is lost rather than shown.** `BALANCE_CASHBACK-<uuid>` is 36 characters
and can never reach `CHECKNUM` under any encoding, so lifting it out deletes it from the file. That
is the intended outcome, decided deliberately: the Description is for text a person reads, and an
opaque id occupying it costs readability and capped `NAME` budget while teaching the matcher
nothing. `FITID` still identifies the row, so nothing that matters is lost. Accepted cost: a
cashback Description is exactly `Cashback`, so several in one month differ only by amount and date.

The counter-case is a reference somebody actually *uses* — invoice numbers, the Elixir tax
remittance the Polish banks send (`/NIP/…`, `/TI/…`), Millennium's `BOLT.EU/O/<id>` merchant
descriptor, which contains the only copy of the merchant name. Those stay. The test is **opacity,
not shape**.

**Why a whole-element match, not a substring.** Fee rows carry `FEE-CARD-<id>` as an element plus
`Wise Charges for: CARD-<id>` as prose, where the id belongs to the *transaction the fee is charged
for*. The element is recognised only in order to be removed — putting it in this row's `CHECKNUM`
would be a false unique id, and it is not text — while the prose copy is left untouched, because
there the id reads as part of a sentence and carries the link to the payment. Stripping ids out of
surrounding prose would gut exactly that. The rule is also not positional: the reference happens to
arrive first when present, but element 0 is ordinary prose in the large majority of two-element
arrays.

**Why an explicit prefix set with per-prefix id patterns.** Not a generic `<PREFIX>-<digits>`
rule: `BALANCE-<digits>` is the same shape as `CARD-<digits>` but identifies the *balance* and
recurs across transactions, which makes it matcher signal worth keeping, and
`ACCRUAL_CHECKOUT-invoice-<digits>` has a prefix that means something to a reader. A rule keyed on
shape cannot tell any of these apart; a table of prefixes can, and it is the artifact to read when
a new source arrives.

**Matcher impact: none, and the exact-match path improves.** The removed token appears on one
transaction, or on a payment and its fee, so Bayes never learned anything from it. The non-Bayesian
imap keys on the whole Description string — which, with a near-unique id inside it, could never have
matched a later transaction anyway. Removing it is the first time that path can work for these. The
fee rows lose no token at all: their prose already repeats the id.

**FITID is untouched.** `_transaction_id()` still hashes the *full* remittance, reference included,
so a future source without its own ids cannot suffer `FITID` churn from this change. It never fires
for Wise (`transaction_id` is null, `entry_reference` always present — and the reference digits are
already inside `entry_reference`, so the id was in the file all along; what changed is that a human
can now see it).

---

## Currency-conversion legs are paired; the rate comes from the two booked amounts, never prose

**Decision.** A currency conversion arrives as two transactions in two currencies, and therefore
two statements (*One statement per account, per currency*, above) — two files by default, or two
`STMTTRNRS` in one file under `--combine`. A per-source rule
(`sources/enablebanking.py`'s `_conversion_key()`) extracts a deal key from a transaction's own
data — Wise's `BALANCE-<n>` reference, gated on `bank_transaction_code.code == "CONVERSION"` so it
does not also catch `BALANCE-<n>`'s ordinary, matcher-useful recurrence; Alior Kantor's `Kantor
Walutowy <n>` in the remittance prose; Revolut's `entry_reference`, gated on
`bank_transaction_code.code == "EXCHANGE"` (shipped in
[#53](https://github.com/skolima/gnucash-ofx/issues/53), the Revolut paragraph below). `conversions.pair_conversions()` groups every `Txn` sharing
a key across one bank's whole fetch, validates the group — exactly two legs, two distinct
accounts, two distinct currencies, one booking date, opposite signs, both amounts non-zero — and
on success derives `rate = credit / debit` from the two *booked* amounts, quantized to 6dp. The
same `Conversion` object is attached to both legs and rendered identically, ahead of the
remittance, in `NAME`/`MEMO` (see *`NAME` composed from remittance + counterparty name* above for
the exception this carves out). Any check failing leaves the group completely untouched — no
annotation, no partial output, no warning shaped like an error. `FITID`/`BANKID`/`ACCTID` are
never touched; pairing changes description text only.

**Why.** Neither source's own file states a usable rate. Wise's prose gives the *wrong* one — gross
of its fee (`346.65 / 300.00 = 1.1555` EUR/GBP) rather than what the booked legs actually balance
against (`345.00 / 300.00 = 1.1500` exactly) — 0.48% off, and only the second reconciles. Alior
Kantor's prose names neither the counter-amount nor the counter-currency at all. Enable Banking's
own `exchange_rate` field is `null` on every conversion leg observed, across every bank, with no
other endpoint to ask. Deriving the rate from the booked amounts instead recovers the dealer's
quoted rate exactly — checked against four Kantor deals, each exact to four decimal places — and
is the only value that reconciles inside GnuCash regardless of what either source's prose says.

**Why not fix this via OFX's own `<ORIGCURRENCY><CURRATE><CURSYM>`.** Three independent upstream
blockers, each confirmed against the actual GnuCash/libofx source
(`docs/adr-currency-conversion-pairs.md` §1–§3): libofx implements `CURRATE`/`CURSYM` only inside
the investment container and discards them on an ordinary bank transaction; GnuCash's
bank-transaction import path never reads `currency_ratio` even when libofx does deliver it; and the
OFX importer's C entry point (`gnc_gen_trans_list_add_trans`) has no channel for a price at all —
so even a fixed libofx would still need a GnuCash change. Modeling conversions as OFX investment
transactions instead was rejected too: it would change the statement type, and therefore the
account semantics, for every transaction in the file, to serve the minority that are conversions.

**Why not a sidecar GnuCash-CSV import for the paired legs, omitting them from the OFX.** GnuCash's
CSV importer is the one first-party path that natively expresses a priced cross-currency transfer —
but GnuCash's own wiki documents that exact path as producing "strange" values for a multi-currency
transfer, which is worse than manual entry because it is wrong *quietly*. Confirmed end-to-end
(`docs/adr-currency-conversion-pairs.md` §8, and again through the shipped pipeline) that the plain
OFX path costs only **one manual rate entry per deal** — the user assigns the rate on whichever leg
imports first, in GnuCash's "Assign exchange rate" dialog, and the second leg matches itself with
no second entry — which is materially cheaper than assumed when the options were weighed, and
closed the CSV sidecar option rather than merely deferring it.

**Why validation is all-or-nothing, never a guess.** A generic heuristic (same bank, same date,
opposite signs, two currencies) would pair a genuine same-day payment out against an unrelated
receipt. Both sources hand us an explicit shared deal key, so there is no reason to guess: anything
that fails validation keeps today's plain text rather than getting a wrong annotation.

**The Revolut row: `entry_reference`, read-only, and no key when the reference is absent**
([adr-revolut-exchange-pairing.md](adr-revolut-exchange-pairing.md), shipped in
[#53](https://github.com/skolima/gnucash-ofx/issues/53)). Revolut's conversion legs carry no deal
reference in the remittance at all; the join is the `entry_reference` both legs share
byte-identically — which is also this source's `FITID` (`transaction_id` is null on every measured
row), so extraction must stay read-only, and one `entry_reference` naming two transactions is a
fact of this data by construction. Chosen by measurement, twice (2026-08-12 and 2026-08-19, over
all cached Revolut data with the real functions): clustering on `entry_reference` gave every
`EXCHANGE` cluster exactly size 2 with zero unpaired legs and all six `_validate` checks passing,
and the key was byte-stable across the 2026-08-14 re-link on 5 of 5 pockets — it survives the
event that regenerates every uid. The remittance-text join is killed by a measured false pair (two
same-day conversions into one currency are textually identical); the date and `transaction_id`
joins have nothing to join on. A reference-less `EXCHANGE` row gets **no** key — an empty key
would falsely cluster every such row into one group — and ships as plain text, today's behaviour.
Working assumption, adopted unmeasured because its failure mode is refusal: both legs book on one
date, so `_validate`'s date check stays strict, and a cross-date deal — if Revolut ever books
one — is refused into unannotated text, never mis-paired; the named revisit trigger is a
date-refused `EXCHANGE` cluster or a size-1 `EXCHANGE` leg found by the zero-request local
re-clustering pass, routed back to
[#50](https://github.com/skolima/gnucash-ofx/issues/50) before any loosening is proposed. The
rejected options — above all the issue's original wait-for-a-cross-date-sighting gate — and the
full measurement record stay in the ADR.

**Deferred, not shipped: also emitting `<ORIGCURRENCY><CURRATE><CURSYM>`.** Measured end-to-end
(§8) that the tag changes nothing for GnuCash's behaviour once the `NAME`/`MEMO` annotation is
already there. Unlike the `MEMO` + `BANKACCTTO` and `CHECKNUM` + `REFNUM` precedents above, the
"correct field" here is not free: `ofxstatement`'s own writer emits the wrong tag (`<ORIG_CURRENCY>`
with reversed child order), so shipping this needs a writer override with no measured payoff. See
`docs/adr-currency-conversion-pairs.md` decision 5 for what would justify reopening it.

**Cost to reverse:** low. Nothing here touches `BANKID`, `ACCTID` or `FITID`, so no imported
account is orphaned and no transaction re-imports; backing it out changes description text only.
The cost is user-visible rather than technical: the one manual rate entry per deal returns, without
the number already in front of the user when GnuCash's dialog asks for it.

---

## Account numbers normalized to IBAN form

**Decision.** `normalize_account_number()` adds a country prefix only when the result passes the
ISO 13616 mod-97 checksum.

**Why.** Polish banks report counterparty accounts as bare 26-digit NRBs while our own `ACCTID`s
are `PL`-prefixed IBANs. GnuCash compares exact tokens, so the two forms would never match and
own-account transfers could not be linked. The checksum gate means anything we cannot prove is an
IBAN (foreign accounts, non-account strings) is left untouched.

---

## Payee/memo text folded to ASCII

**Decision.** `to_ascii()` folds `NAME`/`MEMO` to ASCII before writing.

**Why.** GnuCash **for Windows** silently **removes** every non-ASCII character on import, with no
error in the UI. Verified against GnuCash 5.16 across four input variants (UTF-8, CP1250, CP1252,
and SGML numeric character references); all failed, including CP1252, which is libofx's own default
input encoding and therefore rules out a decoding/locale explanation. OFX 2.x (XML) was tested
later and fails identically — see "Not done, and why" below.

Emitting "correct" UTF-8 loses data at import. Folding is deterministic, which is what the matcher
needs. IBAN tokens were always ASCII, so matching is unaffected.

**The scope is one platform, and the earlier wording here was too broad.** The same six fixtures
run through `ofxdump` on three builds:

| Build | `NAME` delivered | `non SGML character number` codes | exit |
|---|---|---|---|
| Windows, GnuCash-bundled 0.10.5 | `Testowa Firma O` — **lost** | 9500, 9532, 9604 | 1 |
| Ubuntu 22.04, 0.10.3 | `Testowa Firma OÜ` — **intact** | 130…197 | 1 |
| Debian stable, 0.10.9 | `Testowa Firma OÜ` — **intact** | 130…197 | 0 |

libofx's version is not the variable: 0.10.3 is *older* than the broken 0.10.5 and keeps the
characters. Nor is OpenSP's: 1.5.2 on both sides. The codes say what differs — Linux reports the
raw byte values (195 is `0xC3`, 197 is `0xC5`), so bytes pass through by identity and reassemble
into valid UTF-8, while Windows reports CP437 transcodings (`0xC3` → U+251C = 9500), after which
the original byte cannot be recovered and is dropped. Same `SP_ENCODING=ms-dos`, resolved
differently by the mingw build of OpenSP.

Three corollaries worth keeping:

- A `non SGML character number` error is **not** evidence of data loss. It fires on the platforms
  where the text survives intact.
- Numeric character references (`&#220;`) fail on *every* platform, with a different message:
  `"220" is not a character number in the document character set`. That is genuinely the US-ASCII
  document character set in `dtd/opensp.dcl`, where nothing above code point 127 exists and
  `SP_CHARSET_FIXED=1` pins it there. It is the reason escaping is not an escape route.
- The folding is therefore unconditional protection against a defect only one platform has. A
  Linux user of this tool loses diacritics their GnuCash would have imported correctly.

The check does not need the GnuCash GUI: `ofxdump`, libofx's own CLI, ships with GnuCash for
Windows (`bin\ofxdump.exe`) and is `apt-get install ofx` on Debian. It loads the same libofx,
exits non-zero on parse errors, and prints the `NAME`/`MEMO` libofx delivers — which the register
was confirmed to reproduce character for character.

**Revisit if upstream fixes it** — tracked in
[#1](https://github.com/skolima/gnucash-ofx/issues/1). Not
[libofx#60](https://github.com/libofx/libofx/issues/60), which is a **different** defect: there
the characters survive and the complaint is the spurious error and non-zero exit. Its agreed fix
shipped in 0.10.9 (exit 0, plus an explanatory warning) and changes nothing for Windows. Watching
that issue for the revert signal would have produced a false positive; the signal is the Windows
build delivering the characters intact.

---

## CRLF line endings written verbatim

**Decision.** OFX files are written with `newline=""`.

**Why.** `OfxWriter` already emits CRLF. Writing in default text mode on Windows translated the
`\n` a second time, producing `\r\r\n` on every line — malformed OFX that `ofxtools` happened to
tolerate, so it went unnoticed for a while.

---

## The cache stores calendar months, not requests

**Decision.** The unit of storage is `(uid, YYYY-MM)`, with the coverage each chunk actually holds
recorded alongside it. Requests stay as large as the bank allows (≤90 days) and the response is
sliced into months before writing. Each month is persisted as it lands. Chunks carry a `version`;
anything else is ignored exactly as an unreadable file is. Entries written by older versions are
converted in place rather than discarded.

**Why.** Keying on the exact `(uid, date_from, date_to)` triple made the cache a memo of questions
once asked rather than a record of what is known. `--to 2026-05-31` followed by `--to 2026-06-01`
was a total miss even though the first window is a strict subset of data already on disk, and any
"`--to` today" habit misses the following day by construction. Measured over the cache that
produced: **83 of 154 entries were fully covered by another entry for the same account**, across
only 11 distinct window ends. A month is a fact about the world — the same month whatever window
asked for it — so chunks are identical between runs, and serving a window becomes "do I hold every
month it touches, far enough, fresh enough?" with no interval algebra.

**Why request size and storage size are different things.** How much can be asked for in one call
is a *bank constraint*; how finely it is stored is a *reuse concern*. Conflating them — making
requests month-sized to get month-sized chunks — would turn a cold 90-day fetch from one request
into three or four, at exactly the bank that caps and whose history only reaches ~90 days back.
The slicing that keeps them separate already existed: `_txn_in_window`, written because Millennium
ignores server-side date filters.

**Why spans never split a month.** A month then belongs to exactly one request, so saving as each
chunk lands needs no merging into a half-written chunk. The cost is at most one extra request per
~90 days versus flat day-counting, and it removes artefacts like a trailing one-day request.

**Why spans never widen past `date_from`.** Alior and Erste refuse any window starting more than
~90 days back, so rounding the first month outwards would turn a working fetch into a `400` at
precisely the banks this is meant to help. Partial coverage is recorded honestly instead.

**Why each month is written as it arrives.** The old save ran after the whole chunk loop, so a 429
on the last chunk discarded every chunk already paid for — on exactly the accounts that were rate
limited — and the next run re-spent all of it.

**Why the old cache is converted rather than dropped.** Discarding it would make the upgrade
itself cost a full re-fetch at the bank that can least afford one. The conversion is best-effort:
where several old entries touch one month the widest coverage wins rather than being merged, and
anything left uncovered is simply re-fetched.

---

## A settled month does not expire; the recent tail does

**Decision.** A chunk whose month ended more than `LATE_BOOKING_MARGIN` ago is served regardless of
age. The current month, and any month within that margin of today, keeps the 6-hour TTL.

**Why.** The 6 hours are tied to the ASPSP's *recovery* guidance, which is the wrong thing to key
retention on. A closed month is immutable apart from late bookings; only the recent tail moves. An
unbounded TTL on settled months is what makes backfills, retries and re-imports cheap instead of
full-price.

**Why not simply a longer TTL.** Looking for a late-booking lag in the local cache found none:
across every pair of fetches re-covering an already-covered window, exactly **two** transactions
ever arrived late, and both were an aggregator-side backfill about **two months** after their
booking date. No TTL anyone would pick catches that — a 30-day TTL would have re-fetched that month
a month early and still missed it — so paying to re-fetch every settled month everywhere buys
nothing. `fetch --refresh` is the instrument for "I have reason to think the bank changed
something".

**`LATE_BOOKING_MARGIN` is a policy value, not a measured one**, and is documented as such where it
is defined. There was nothing in the data to derive it from. It is defined once so the overlap rule
in [#6](https://github.com/skolima/gnucash-ofx/issues/6) can share it rather than re-pick it.

---

## Online mode + 6-hour fetch cache

**Decision.** Send PSU headers on data-retrieval calls, and cache successful fetches for 6 hours.

**Why.** PSD2 caps *background* fetches at ~4/day per ASPSP; PSU headers mark a user-triggered
fetch as *online*, which carries far higher limits. The cache stops re-runs, retries and
re-imports from re-spending whatever allowance remains. Backoff alone cannot beat the daily cap —
once exhausted, only time helps — so 6 hours matches the documented recovery window for the part
of the cache that still expires.

**The one 429 that is never retried.** `ASPSP_RATE_LIMIT_EXCEEDED` is that daily cap. The client's
ladder tops out at 31 seconds against a ~6-hour recovery, so all five retries are certain to fail —
and each is another counted request sent at an ASPSP that has just said it has had enough. It is
raised on the first response. Generic and platform-level 429s keep the backoff, because those are
genuinely transient; matching is on the `error` field, never the prose `message`.

The PSU IP comes from `EB_PSU_IP` or a best-effort public-IP lookup, and must parse as a valid
address; otherwise **no** PSU headers are sent, because a partial set is rejected outright.

---

## Consent requested at the bank's maximum

**Decision.** `link` reads `maximum_consent_validity` from the ASPSP catalog and requests it,
capping any explicit `valid_days`, falling back to 90 days if the lookup fails.

**Why.** Every bank used here allows 180 days; the previous hardcoded 90 meant twice as many
manual browser SCA rounds for no benefit. Discovery failure must never block linking.

---

## Partial success is reported, not thrown away

**Decision.** A bank that fails is recorded and skipped; the run continues, keeps the files the
other banks (and the other accounts of the failing bank) produced, and exits **1** if anything
failed. Written paths go to stdout; the failure summary, and everything else a human reads, to
stderr — see "stdout is the file list, and a header is not a path" below for what may go on stdout.

**Why.** The failure modes here are routine, per-bank, and mostly not the user's fault: a
per-ASPSP rate limit, a bank that refuses a window reaching further back than it serves, a consent
that expired on one connection. Aborting the run threw away banks that had nothing wrong with them
— and worse, the files already written stayed on disk while the user was told the run had failed.
Silent partial success is the worst of both.

Exit 1 rather than 0 because cron and scripts are the audience for the code, and "some of your
statements are missing" is not success. Not exit 2: argparse owns that for usage errors.

**Scope of a failure.** Per account by default, so one bad account keeps its siblings' files. A
`429` is the exception — it abandons the whole bank, because that allowance is per ASPSP and the
remaining accounts would each burn minutes of backoff to fail identically.

**What still aborts everything:** missing credentials, bad config or dates, and `--bank` naming an
unconfigured bank. Those are global, or an explicit request that cannot be satisfied at all.

**Retry cost.** A failed account caches nothing while its successful siblings stay cached, so
re-running the same command re-fetches only what failed. That property is why the summary can
honestly tell the user to just run it again.

---

## stdout is the file list, and a header is not a path

**Decision.** `fetch` puts the written paths — or, under `--dry-run`, the predicted ones — on
stdout, one per line, bare. The `Wrote N OFX file(s) to <dir>:` header, the two-space indent that
used to precede each path, and the "No OFX files written…" sentences all moved to stderr with the
progress, warnings and failure summary. A run that writes no file writes **nothing** to stdout.

**Why.** stdout here exists for one consumer: `fetch > files.txt`, and whatever reads that file.
Every line on it that is not a path is a line that consumer has to be taught to skip, and it is the
teaching that fails. The old shape needed `tail -n +2 | sed 's/^  //'`, and on a run that wrote
nothing it needed a reader that could tell prose from a filename — otherwise
`No OFX files written (no transactions in the requested range).` is handed to an importer as a path.
The human loses nothing by the move: stderr already carried every other human-facing line, and in a
terminal both streams land in the same place. The bare shape is the only one that serves both
readers, which is why it is the one three documents and both docstrings already described.

**Do not add to stdout "for readability."** Anything appended to `_report`/`_report_dry_run` that a
human reads rather than a program consumes goes through `_progress`. The header is the worked
example: it cost the human nothing to move and cost every consumer a preprocessing step to keep.

**The silent empty run is the decision, not a side effect.** "Only the written paths" means nothing
written, nothing listed, so `> files.txt` on a fetch that found no transactions is an empty file.
The prose is not lost — it is on stderr, where a person is already looking.

**`status`, `link` and `aspsps` are exempt, and the reason is not "they are older."** Their report
*is* their product; no pipeline consumes them as data, so stdout is where their output belongs. The
predicted mistake is a contributor generalising this decision to "stdout is the file list", grepping
for bare `print(` in `cli.py` and finishing the job on `_cmd_status` — after which
`status > report.txt` yields nothing and `test_status_output_is_ascii` asserts against an empty
string, a green suite over a broken command. `status --check`'s attention summary is the one line of
those commands that is not their report, and it is on stderr.

**The `sys.stdout.flush()` after the path loop is load-bearing, and no test defends it.** stdout is
block-buffered when it is a file or a pipe, so under `2>&1 | tee` the paths would otherwise surface
*after* the failure blocks they precede. `capsys` captures above the buffering layer, so deleting
the flush breaks no test — the comment beside it is its entire defence. This would rather have been
a test and cannot be one. If the path loop ever moves into a helper shared by `_report` and
`_report_dry_run`, the flush travels with it, and `_report_dry_run`'s "see `_report`" cross-reference
becomes a dangling pointer at that moment.

**Measured 2026-08-09**, by the method that found the mismatch in the first place and the one to
re-run before trusting this section: a real `fetch --dry-run` in a subprocess with the two streams
redirected to separate files. Two linked accounts gave 82 bytes on stdout — two bare paths, no
header, no indent — against the header, the per-account detail and the dry-run caveat on stderr; a
bank with no session gave 0 bytes on stdout and exit 0. `capsys` cannot see redirection, so the
suite's exact-equality assertions on both happy paths pin the text but not the stream behaviour;
only a subprocess does.

**Cost to change: low in data terms, and paid entirely downstream.** No identity field moves,
nothing re-imports, no rate-limit allowance is spent — so nothing in this repo makes putting a line
back on stdout feel expensive. The cost lands on every `fetch > files.txt` consumer written since:
each needs its stripping restored, and one that does not get it either feeds a header to an importer
or treats a sentence as a filename. That asymmetry — free here, breaking there — is why both happy
paths assert *exact* stdout equality rather than substring containment. A future `--combine`
([#8](https://github.com/skolima/gnucash-ofx/issues/8)) changes how many paths are listed, never
what a listed line looks like.

---

## Local file errors fail cleanly, and a corrupt state file is a per-bank problem

**Decision.** A malformed `config.toml` raises `ConfigError`. A `state/<bank>.json` that fails to
parse — corrupt JSON, missing fields, an unparseable `valid_until` — raises `StateError`. Both turn
into a one-line `SystemExit` message instead of a raw traceback. `fetch_enablebanking` treats
`StateError` exactly like `BankError`: the affected bank is recorded as failed and the run
continues, so one bank's broken state file cannot cost its siblings their files. `status` reports
it as a status line instead of crashing — a command that touches no network and is meant to be
side-effect-free should not fail on a state file it only reads.

**Why.** Both are ordinary things to happen to a local file: a hand-edited `config.toml` missing a
bracket, or a state file corrupted by something outside the atomic-write path (manual editing, a
crash mid-write on an older version, disk corruption). Neither should ever surface as a stack
trace — that tells the user nothing actionable, and for `fetch` it used to abort banks that had
nothing wrong with them. A corrupted state file is scoped to exactly one bank (the file it lives
in), so it gets the same per-bank treatment as "not linked" or "consent expired" — see "Partial
success is reported, not thrown away" above.

**A malformed `config.toml` is still fatal for the whole run** — unlike a bad state file, it is
read before any bank-specific work begins, so there is nothing to isolate it from. What changed is
only that the failure is now a clean message, not a crash.

---

## Errors carry an explanation only when we have one

**Decision.** `diagnose()` maps a small set of verified Enable Banking codes to an explanation and
a next step. Everything else gets no explanation at all — just the bank's verbatim response and a
pointer to `docs/enable-banking.md`.

**Why.** A wrong explanation is worse than none: it sends the user to fix something that is not
broken. The raw payload is the genuinely useful artifact when debugging, so it is always printed
in full (to 300 chars) rather than being replaced by prose. What was missing was never the
payload — it was the context around it: which bank, which account (redacted), what was being
attempted, over what window.

Explanations never name institutions. Bank keys are user-chosen and the ASPSP set differs per
deployment, so "this bank" is the only phrasing that is true everywhere.

**Expired consent is detected locally**, from the stored `valid_until`, before any request is
made. That is why the table needs no entry for whatever an ASPSP returns for a dead session — we
never get that far, and guessing the code would have been exactly the invented explanation this
decision rules out.

**Declining to grow the table is also a decision, made once so far — and half of that one was
reversed the same day** (N26 envelope, [#54](https://github.com/skolima/gnucash-ofx/issues/54);
the measurement itself lives in [enable-banking.md](enable-banking.md#error-envelope), not here).
Both obvious code responses were declined at first: a `diagnose()` entry for the `HttpException` /
"Service unavailable" shape, and persisting `detail` into `state/fetch-log.jsonl`. The fetch-log
half was reversed by [#55](https://github.com/skolima/gnucash-ofx/issues/55), which persists the
`detail.error_name` token: the field's *shape* was attested twice, not once — `RateLimitException`
on the verified live 429, `HttpException` on this 400 — so what was missing was retention, not
evidence, and this entry's own "re-weigh if it recurs" trigger was unactionable from a log in
which a recurrence is only another bare `400 ASPSP_ERROR` (the persisted token's constraints live
with the fetch-log invariant and the enable-banking.md bullet, not here). The `diagnose()` half
**stands**: one observation per `error_name` is a vocabulary, not a contract, `diagnose()`
receives only `status_code`/`api_code`/`operation` so recognising that shape means threading
`detail` through the client and `FetchFailure` (cross-cutting), and on that exact failure the
current behaviour is already the designed one — `diagnose()` returns `None`, the CLI prints the
verbatim payload and points at the doc that now describes the shape, which completes the loop
this decision promises. **Named revisit trigger** for the threading, now observable from the log
itself instead of costing a live re-spend: the shape recurring on a `transactions` call, where
the verified `ASPSP_ERROR` + 400 + `transactions` → window-refused mapping would mis-explain a
bank-down as a window refusal — `detail` is the only field in the response that can tell those
apart, and since #55 a recurrence lands as `error_name` in `fetch-log.jsonl`, so the second
observation that buys the threading will already be on disk when it happens.

---

## Everything the link response carries is captured

**Decision.** `link` persists every account field `POST /sessions` returns — IBAN, BIC, currency,
`identification_hash`, the `other.scheme_name`, and the `name`/`product`/`usage`/
`cash_account_type` labels — *and* the whole response body verbatim, into `state/<bank>.json`.

**Why.** That response is the only place the full account resource ever appears:
`GET /accounts/{uid}` is 404 in Restricted Mode, and `GET /sessions/{id}` returns bare UID strings
with no IBAN, no BIC and no currency. A field not read at link time is unrecoverable until the next
browser SCA dance, ~180 days away. The project already learned this twice — first for the IBAN,
then for the BIC — each time paying a re-link to get it back. Keeping the raw body ends the
pattern: the next field we discover we need is already on disk, including ones added by future API
versions.

Two things the stored data buys today: `ACCTID` resolution no longer depends on `GET /sessions`
still reporting `identification_hash`, and an account whose ASPSP returns no usable balance gets
its currency from state instead of being silently skipped with no file written.

**Cost to change:** low to stop capturing, high to recover anything dropped — one browser SCA per
bank, and the older data is simply gone.

Consequences accepted:

- `state/<bank>.json` now holds account numbers and account names, not just a session id. It was
  already gitignored and already sensitive; it is now sensitive in the way `cache/` is. See
  [SECURITY.md](../SECURITY.md).
- Credential-shaped top-level keys (`refresh_token`, `access_token`, ...) are dropped before
  writing. None has ever been observed in a session response; the file outlives the consent, so it
  is not worth the bet.
- Banks linked before this change keep working with what v1 stored, and fill in the rest at their
  next re-link. `ACCTID` must not move when that happens — a session with no stored hash resolves
  through `accounts_data` exactly as before, which is asserted by test.
- The legacy `account_ids`/`account_ibans`/`account_bics` keys are still written for one release,
  so a downgraded build finds the IBAN instead of falling back to `uid` as `ACCTID`.
- Session writes go through a temp file + `os.replace`. A torn write used to cost a re-link, which
  is too high a price for a full disk.

---

## `LEDGERBAL` only when the window is still open

**Decision.** `/balances` is called when the requested `date_to` is today or later. For an earlier
window the call is skipped and `LEDGERBAL` becomes the statement's own running total from zero.
Balances are cached per **account**, not per window.

**Why.** `/balances` takes no date, and there is no historical-balance endpoint —
`GET /accounts/{uid}` is 404 in Restricted Mode. Enable Banking answers with the balance *now*:
across every balance record in the local cache, `reference_date` equalled the fetch date without
exception, and in 73 of 90 it was *later* than the requested `date_to`. Every dated record was
`ITAV`, interim available.

That value was being written as history. `OfxWriter` dates `LEDGERBAL` from the statement's end
date, and `ofxout.py` back-computes the opening balance from it — so a fetch for a window that
closed weeks ago asserted today's balance as the ledger balance on that past date, and derived a
wrong opening balance to match. It was the common case, not a corner: 121 of 154 cached fetches
had a `date_to` already in the past.

**And GnuCash's code consumes it.** `gnc-ofx-import.cpp` passes `ledger_balance` and
`ledger_balance_date` to `recnWindowWithBalance()`, which pre-fills the reconcile dialog. The one
dialog actually measured — GnuCash 5.16, 2026-08-17, by the throwaway-book import
[testing.md](testing.md) prescribes ([adr-ofx-batch-splitting.md](adr-ofx-batch-splitting.md)
§12) — pre-filled the register-computed total instead, consulting no `LEDGERBAL` at all. Both
facts stand: the code path is real, other GnuCash versions and other libofx consumers may surface
the value, and a pass is a statement about one version — so the number is treated as handed to
the user, not as inert documentation, whatever 5.16's dialog happened to read.

**Why not simply omit the tag.** `ofx160.dtd` line 921 makes `LEDGERBAL` mandatory in `STMTRS`
(`BANKTRANLIST` is optional; this is not). A file without it is not OFX.

**So this is a choice between two false values, not a fix.** No correct closing balance for a past
window exists anywhere in this API. Today's live balance is a *plausible real balance at the wrong
date*, which is what makes it dangerous in the reconcile dialog; a period movement from zero is
wrong in a way anyone reconciling an account with an opening balance notices immediately. The
honest fix — a closing balance carried forward per account — needs the coverage ledger in
[#6](https://github.com/skolima/gnucash-ofx/issues/6), which is the only thing that could know
it.

**The call has a second job, and that is why it is not skipped outright.** It is also how the
account's currency is discovered, and an account with no currency is skipped entirely, writing no
file. Sessions linked before the currency was captured at link time have no other source, so for
those the call still happens whatever the window — but the balance it returns is used *only* for
the currency, never as `LEDGERBAL`. For that use a stale answer is perfect, since a currency does
not change, so the cache lookup drops the TTL and a legacy session pays for one balance call ever
rather than one per fetch.

**Why balances moved out of the window-keyed cache entry.** A transaction list belongs to the
window requested; a balance belongs to the account and the moment of the call. Holding both under
one key meant two fetches of different ranges each paid for an identical balance.

**Under `--batch-size` this rule narrows to the final batch** of a window reaching today; every
earlier batch takes the existing running-total path, independently per batch — see
*`--batch-size` splits by transaction count, never by calendar* below for the reasoning and the
GnuCash 5.16 measurement that made it moot for the reconcile pre-fill.

**Cost to reverse:** low — no identity field moves, and nothing re-imports. Anyone who was reading
`LEDGERBAL` on a historical export as an approximate current balance loses that.

---

## `"XXX"` is normalized to no currency at the boundary, never trusted downstream

**Decision.** A currency string is validated once, via a shared `currency_or_none()` helper
(`models.py`), at every point one enters as fact: `linked_accounts()` (`POST /sessions`, link
time), `_currency_from_balances()` (`/balances`), and `_accounts_from_payload()` (loading a
previously-persisted `state/*.json`). ISO 4217's reserved `"XXX"` ("no currency") normalizes to
`None` at each of the three; nothing downstream changes, because "`None` means unknown" already
means exactly that everywhere it is consumed. The check matches the literal string `"XXX"`, not a
maintained ISO 4217 table.

**Why.** A re-link of `alior` and `alior_kantor` on 2026-08-10 made `POST /sessions` report
`currency: "XXX"` for every account in both connections. That is a non-empty string, so it read as
a known currency, which skipped the `/balances` discovery fallback (above) on every closed-window
fetch and let the placeholder stand in as the account's authoritative currency — every real
transaction then failed the cross-currency mismatch check in `map_transaction()`, either as a
visibly failed fetch (wrongly blamed on unreadable bank data) or, for an account with no
transaction in the requested window, silently: nothing to notice, and every later closed-window
fetch keeps trusting the placeholder. `alior` and `alior_kantor` are two of the three connections
serving only ~90 days of history, so a missed window there is not deferred work, it is data that
ages out of the API for good.

**Why at the boundary, not at each of the five downstream consumers** (`currency_known`, the
discovery-fallback assignment, the same-currency sibling lookup, the dry-run filename prediction,
and — deliberately untouched — `map_transaction()`'s mismatch raise). Each of those already does
the right thing for an absent currency; the pre-currency state schema has produced that case since
before this bug existed. One normalization at each of the three ingestion points makes all of them
correct, instead of scattering a currency-shaped check across five call sites built for different
purposes.

**Why state load is a boundary too, not just the two ASPSP responses.** `linked_accounts()` only
runs at link time, so fixing just that would leave `alior` and `alior_kantor`'s already-corrupted
`state/*.json` broken until someone re-links by hand. Re-normalizing on every load means a file
that already has `"XXX"` on disk self-heals the next time an ordinary fetch runs — no migration
step, no state-schema change.

**Why the literal string, not a maintained ISO 4217 table.** `"XXX"` is ISO 4217's own reserved
code for "no currency", which is precisely what was observed — a correctness fix for a named,
documented condition, not a heuristic. A code table would defend against a different,
so-far-hypothetical failure (a different ASPSP inventing a different bogus placeholder); among the
six banks linked locally, only the two Alior connections showed it. Revisit if a second, different
placeholder is ever observed on disk.

**Why `map_transaction()`'s mismatch check stays untouched.** Relaxing it to tolerate a known-bad
stored value was considered and rejected: it would equally mask a transaction that really was
grouped under the wrong account, with nothing inside the function able to tell the two cases
apart. The raise stays the safety net; the fix is upstream of it.

**Cost to reverse:** low. A currency string at parse time, not a schema or dataclass change —
nothing here touches `FITID`, `BANKID`, `ACCTID`, the cache key or the state schema, so reversing
it orphans no account and re-imports nothing. If a future ASPSP turns out to use `"XXX"` for
something other than "no currency", the failure mode of this decision being wrong is cheap and
self-announcing: the stored currency reads as absent and the account pays one `/balances` call per
fetch instead of amortizing it once — not silent corruption. See
[`adr-xxx-currency-placeholder.md`](adr-xxx-currency-placeholder.md) for the reproduction, the
rejected options and the boundary argument in full.

---

## One statement per account, per currency

**Decision.** Never merge currencies into one statement. Never merge two accounts' lines into one
statement either — that constraint held by construction while a statement and a file were the
same thing, and stays written down now that they no longer are (`--combine`,
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md)).

**Why.** An OFX statement is single-currency (`CURDEF`). Multi-currency positions (Wise, Alior FX)
are therefore separate accounts and produce one statement each. Where a bank has several accounts
in the same currency, a suffix derived from `ACCTID` disambiguates the *filename* only — GnuCash
matches on `BANKID`+`ACCTID` inside the file, so renaming is invisible to import.

**Packaging is separate from the statement/account invariant above.** By default one statement per
file, as it has always been. `fetch --combine` puts every statement a run produced into one file —
one `BANKMSGSRSV1`, N `STMTTRNRS` — and changes nothing else about them: `CURDEF`, `BANKACCTFROM`,
`BANKTRANLIST` and `LEDGERBAL` all stay exactly where they already were, inside each statement. Off
by default, and gated on a real GnuCash import before it can be relied on at all — see
[`adr-combined-ofx-file.md`](adr-combined-ofx-file.md) for the measurements, the gate, and why a
`--combine` run costs no different number of API requests than the same run without it.
`fetch --batch-size N` is the third packaging mode, also off by default: one account's window
split into several statements — several files, or several `STMTTRNRS` under `--combine` — each an
ordinary statement over the exact range it covers. Still packaging only, still the same requests;
see *`--batch-size` splits by transaction count* below.

---

## Output filenames: account first, then the period covered

**Decision.** `{bank_key}_{currency}[_{disambiguator}]_{YYYY_MM_DD}-{YYYY_MM_DD}.ofx`, e.g.
`alior_PLN_2026_05_01-2026_05_31.ofx`.

**Why.** Importing is per-account — each file targets one GnuCash account — so the natural unit of
work is "every file for `wise_personal` EUR". Leading with the date grouped the directory by month
instead and scattered a single account across the listing. Fixed-width dates make lexicographic
order chronological within an account's group.

Naming the full range, not just the end month, also stops two fetches with different `--from` from
landing on the same filename and silently overwriting each other. The range matches
`DTSTART`/`DTEND` exactly.

The disambiguator is the last four characters of `ACCTID` (an IBAN's tail — recognisable, and
already a stable invariant), falling back to `eb-<hash8>` when that tail is not plain alphanumeric
or when two accounts in the group share it. It is *not* a positional index: an account with no
transactions writes no file, so a counter over pending accounts renumbered the others depending on
which happened to have activity that period, and the same account could change its number between
runs.

**Batching extends this entry without changing it** ([adr-ofx-batch-splitting.md](adr-ofx-batch-splitting.md)
decision 4, shipped in [#46](https://github.com/skolima/gnucash-ofx/issues/46)): a batch is an
ordinary statement over an exact range, so it is named like one — each batch's period components
are its own first/last transaction dates, and a batch file is indistinguishable from a hand-ranged
`fetch --from … --to …`. A booking date is never split, so an account's batch ranges are disjoint
and strictly increasing and the range alone stays unique: no batch marker, no index, and the
positional-index mistake above never re-arises because batching never reaches the tiebreaker
clause.

---

## `--batch-size` splits by transaction count, never by calendar, and never splits a booking date

**Decision.** Opt-in `fetch --batch-size N` splits each account's OFX output into chronological
batches of at most `N` transactions, oldest first, remainder last, no rebalancing. A boundary that
would cut a same-day run pulls back to the day break, and a single day holding more than `N`
becomes one oversized batch — `N` is a soft cap with exactly that one exception. Off by default,
packaging only: no extra request, no identity field moves, and it composes with `--combine` (a
batched account hands `write_combined_ofx` several per-item periods; `combine_statements` never
learns batching exists). The measurements, the rejected options — `_partN` markers, splitting an
oversized day, calendar splitting — and the still-open matcher question live in
[adr-ofx-batch-splitting.md](adr-ofx-batch-splitting.md); shipped in
[#46](https://github.com/skolima/gnucash-ofx/issues/46).

**Why count-based.** A review batch should stay a predictable size however bursty an account is —
a calendar split's size tracks burstiness, which is exactly backwards, and running `fetch` monthly
already *is* calendar splitting. One global `N` is correct rather than merely convenient because
`N` is a cap on review size, not a target: a count-based split self-adjusts, so a quiet account
still writes one file and a heavy one writes several with no per-account configuration. Measured
2026-08-12: a ~30× activity spread sits *inside* one bank key, so a per-bank override could never
separate the accounts it would exist for (ADR §9).

**Why a day is never split.** At every plausible `N`, a majority-to-near-majority of naive count
boundaries land inside a run of same-day transactions (measured 2026-08-10 on 28 cached accounts;
reproduced 2026-08-12 on an independent 24-series, 12-institution corpus spanning 2012–2026 — ADR
§2). A day's transactions are the grouping a reviewer least wants split — a payment and its fee,
both legs of a transfer, in one sitting — and splitting the day only when it is busy (the ADR's
own second draft, option E′) would abandon that principle exactly where it matters most.

**The no-marker deduction is the load-bearing one.** Days never split, therefore batch date ranges
are disjoint and strictly increasing, therefore two batches can never compute one filename — so
`ofx_filename()` gains no component and no parameter, against the issue's own premise that a third
filename component was needed. The accepted price is that a set of batch files is not recognisable
as a set. Reversing the "never split" half breaks the deduction *silently*: the second batch
overwrites the first, a whole batch lost with no error anywhere. `tests/test_batching.py` pins the
conjunction of the four properties that make it safe, across 200 seeds × 6 batch sizes.

**`LEDGERBAL` narrows to the final batch** of a window reaching today; every earlier batch keeps
its own zero-based running total, computed independently, never chained — chaining is new balance
logic the issue rules out. Measured moot for the reconcile pre-fill on 2026-08-17, GnuCash 5.16
(Build 5.16+(2026-06-27)), by the throwaway-book import [testing.md](testing.md) prescribes: the
dialog pre-fills the register total and consults no `LEDGERBAL` at all, and a first-ever import of
one `BANKID`+`ACCTID` pair across three statements prompts once, not three times (ADR §12). The
rule stands as defence in depth for libofx callers and versions that do honour an earlier batch's
balance — unobservable in this GnuCash, not wrong.

**`--dry-run` predicts no paths while batching, and says why.** Batch boundaries depend on
transaction counts and dates, which a dry run structurally never sees, and a prediction that might
be wrong is worth less than none. The suppression lives in `dry_run_enablebanking` itself, not the
reporting layer: a `DryRunReport.planned` carrying unbatched filenames the run would never write,
guarded by one `if` in a renderer, is a value object that lies — and it had stderr announcing the
combined path a few lines before the CLI said no paths were predicted.

**Only the batching path sorts.** With no `--batch-size` the cached-then-fetched input order
reaches the file untouched, because "no `--batch-size`" is a byte-identical guarantee — the
contract that let this ship as packaging only. No numbered decision rides on it (it came out of
the review round), which is why it is recorded here; pinned by
`test_unbatched_input_order_is_preserved_not_sorted`.

**Cost to reverse: low.** No identity field, cache key or state schema moves, so nothing orphans
and nothing re-imports. An unknown `[fetch]` key is reported rather than ignored, so a reverted
install tells the user their setting no longer exists instead of silently changing the output.

---

## `[fetch]` holds packaging preferences, and `refresh` is locked out of it

**Decision.** `combine` and `batch_size` are settable in a `[fetch]` section of `config.toml`,
global to the run. Precedence is CLI explicit > config > built-in, and the built-in defaults do
not move: no combining, no batching. `--no-combine` and `--no-batch-size` are the off-switches; an
unknown `[fetch]` key raises `ConfigError`; `refresh` is deliberately outside the closed key set
and must stay there ([adr-ofx-batch-splitting.md](adr-ofx-batch-splitting.md) decision 9, shipped
in [#46](https://github.com/skolima/gnucash-ofx/issues/46)).

**Why config at all.** Both flags are preferences, not per-run decisions — a user who wants
combined output wants it every run, and *forgetting* a retyped flag silently changes the output
shape, which is the worse failure than the friction of retyping.

**Why real off-switches.** `store_true` cannot express "off" against a configured value. And
`--batch-size 0` is rejected as invalid input rather than documented as a magic "off": a magic
integer would need documenting, and `N <= 0` should stay a plain validation error.

**Why `refresh` can never join the section.** It spends rate-limit allowance, so it has to stay a
deliberate per-run act — a config file that turns it on permanently makes every run full-price at
the banks with the tightest caps. Pinned by `test_refresh_is_not_a_config_option`.

**The named cost, and its mitigation.** Behaviour set in config is invisible in `--help` and in
the command the user typed, so `fetch` and `--dry-run` state any non-default packaging on stderr —
the same stderr-is-for-humans split as everything else a person reads.

---

## Coverage is a ledger of days, in `state/`, keyed on `ACCTID`

**Decision.** `state/coverage/<bank>.json` records, per account, the day ranges that have actually
been fetched — merged inclusive intervals, keyed on a digest of the same value the OFX `ACCTID`
resolves to. Absent, unreadable or wrong-version means **unknown**, never "gap since the epoch".

**Why not the cache.** It looks like a coverage record and is not one. When this was written,
`save_cached_month` overwrote a month with whatever the latest request covered, wider *or
narrower* — measured by simulation against the real functions: fetch `07-01..07-24`, then
`07-25..07-31`, and `cached_window` afterwards reported July as missing for days already paid
for. [#47](https://github.com/skolima/gnucash-ofx/issues/47) replaced the overwrite with a merge
(see *Requests open a transaction-date margin* below), which removed the *loss* but not the
reasons the ledger exists: a chunk's claim still cannot hold two disjoint spans (the wider wins
and the gap is never claimed — a coverage record may never claim days it lacks), the cache is
keyed on the account `uid`, which is regenerated on every re-link (24 of 43 account digests in
the local cache belong to no current session), and `cache/` is the one directory a user is told
they may delete.

**Why `ACCTID`.** A re-link falls *inside* the ~90-day window a coverage warning exists to protect,
so a `uid`-keyed ledger would blank itself exactly when it mattered. Keying on `ACCTID` also makes
the ledger unable to lie: `ACCTID` is what GnuCash derives `online_id` from, so if it changes,
GnuCash sees a new account and the ledger sees an unknown one — the same event, never one without
the other.

**Why days, not months.** The cache stores months because a month is the unit of *retrieval*; a day
is the unit of *loss*. A month map cannot express two disjoint spans inside one month, so it would
have to widen (claiming coverage it lacks — the one thing a warning system may never do) or keep
the last, which is the cache's forgetting rebuilt inside the fix for it.

**Why not inside `state/<bank>.json`.** That file is written once per link and losing it costs a
browser SCA dance; this one is written once per account per fetch and losing it costs a re-fetch.
A ledger whose corruption can cost a consent has the risk backwards, so it is a separate file — and
in a subdirectory, because `state/*.json` already means "the session files" to anything globbing it.

**Only a successful fetch advances it**, at the point the account's transactions are mapped — not
where the file is written. An account with no transactions in the window writes no file and is
nonetheless fully covered; recording at the write would manufacture the exact gap this exists to
find. Written per account as it lands, for the reason `save_cached_month` is: a rate-limited
sibling must not discard coverage already earned. A write failure is a warning, never a failure —
and unlike `RunLog`, not a silent one, because a lost coverage write has a running cost.

**Digested keys.** The file holds dates and `sha1(ACCTID)[:16]` only. A file of dates looks
harmless and will be pasted into an issue, so it has to actually be harmless.

---

## `--from`/`--to` are optional, and the default window is resolved per bank

**Decision.** `--to` defaults to today; `--from` resumes from the ledger, stepped back by
`LATE_BOOKING_MARGIN`, clamped to 89 days. Resolution is per **bank**, by one function
(`resolve_window`) called by `fetch_enablebanking` and `dry_run_enablebanking` alike. An explicit
`--from` is used exactly as given and never clamped.

**Why.** Retyping the range every month is itself the main source of the mistake — a window that
starts on the 11th because that is where the last one ended. The overlap is deliberate and biased
hard: a bank can book a transaction with a booking date days before it appears, so resuming exactly
where coverage ended can skip one, while re-fetching the margin is close to free. Measured: for the
two banks whose coverage is complete, a 30-day, 90-day and 8-month window all cost exactly **2
month-requests per account** — a settled month does not expire, so the extra cost lands only on
months genuinely not held, which is the repair rather than overhead.

**Per bank, not per run**, or Millennium's deeper history would drag Alior's window past the ~90
days it serves and turn a working fetch into a `400`. **The least covered account decides**, and an
account with no record makes the answer unknown rather than being skipped — the default has to be
safe for the least covered account, and one nobody has a record for is the least covered there is.

**89 days, not 90.** Measured 2026-08-09: Alior and Erste both serve `date_from` at exactly
today−90, and Alior refuses today−120, so 90 is the last day served rather than the first refused.
The day held back is not a guess at that boundary — it covers the difference between the local
`date.today()` this tool computes from and whatever day the ASPSP thinks it is, which west of UTC
runs in the direction that makes the local answer one day too old. The escalation horizon stays at
the measured **90**: a margin on what we ask for has no business shortening what we tell the user is
still recoverable.

**The clamp is reported, not silent**, and the resolved window is printed per bank. An implied
window has to be visible — it is the difference between "this run fetched what I meant" and "this
run fetched what some file remembered". This is also why `--dry-run` resolves it through the same
function: making the window implicit is only safe because it can be previewed for free.

---

## Warnings are a third channel, and never move the exit code

**Decision.** `FetchWarning` (`bank_key`, `kind`, `message`, optional redacted `account`) in
`FetchReport.warnings` and `DryRunReport.warnings`, rendered to stderr grouped by bank. `fetch`
still exits 1 only when a bank actually failed.

**Why a type, not a reused `BankFailure`.** "This did not fail" becomes a property of the value and
cannot be lost by a later edit to the reporting layer — the same reason `BankError` is a type
rather than a well-placed `try`. Before this there were exactly two channels: a failure (wrong —
the files were written and are importable) or a progress line (invisible in a run that prints one
line per account).

Three things use it. **Coverage gaps**, computed before any account data is requested, from the
ledger and the session response that was already paid for; the window being fetched counts as
covered while they are worked out, or the warning would fire on every run. **Consent expiry**, at
`CONSENT_WARNING_DAYS = 45` — derived, not picked: a warning must be seen at least one run before
expiry, the longest observed gap between runs was 38 days, and a re-link needs the user at a
browser. Larger is not free, since acting on the first warning means re-linking every `180 − N`
days and every re-link strands that bank's cache. **Stale identity**, below.

`status --check` exits 1 when any bank needs attention. Behind a flag because `status` has always
exited 0 and `gnucash-ofx status && …` is a plausible thing to have written.

---

## The stale-identity warning is keyed on the symptom, not the state schema

**Decision.** `fetch` warns when an account's `ACCTID` resolves to the bare Enable Banking `uid` —
no stored IBAN and no `identification_hash` from either link time or the fetch-time `accounts_data`
array. It does **not** warn on the state file's schema version, and it does not change what
`ACCTID` resolves to.

**Why.** Measured over the six local banks: **five are on the pre-`accounts` layout, and not one of
the 19 accounts is in the condition that orphans anything** — they all carry `account_ibans`, so
`ACCTID` resolves to an IBAN exactly as under the current schema. A schema check would fire on five
banks, cost five browser SCA dances and five stranded caches, and fix nothing.

**Fetch-only, deliberately.** The `accounts_data` hashes rescue most v1-schema accounts, and they
arrive with `GET /sessions` — a call `fetch` already makes and `--dry-run` may not. So the dry run
says nothing here rather than over-reporting; the asymmetry is the honest one.

Changing what `ACCTID` resolves to for these accounts would orphan precisely the history the
warning exists to protect. The warning is the whole intervention.

---

## `--dry-run` takes no client, and exits 0 on a lapsed consent

**Decision.** `fetch --dry-run` is a separate orchestration function (`dry_run_enablebanking`)
with **no client parameter and no cache directory**, and it exits 0 whenever `config.toml`
resolves — including for a bank it reports as unlinked or expired.

**Why.** The promise is "this costs nothing", and the only way to keep a promise like that is
structurally: a function that has no client cannot call one, and one that never receives
`cache_dir` cannot populate the 6h cache and make the *next* real fetch quietly serve stale data
it never paid for. A `dry_run=True` flag threaded through `fetch_enablebanking` would have put
that guarantee in the hands of every future edit to a 300-line function. For the same reason the
dry run does not create a `RunLog`: that file is an account of what was spent, and an entry for a
run that sent nothing makes it harder to read a `429`, which is the one job it has.

What it does share is everything that determines the answer — `_known_acctid`,
`_disambiguators`, `ofx_filename`. The disambiguation group is the case that makes this worth
insisting on: it counts every account **in the session**, so a bank with two PLN accounts writes
suffixed filenames even in a month when only one of them has transactions. A dry run that
grouped over "accounts that would produce a file" would predict the un-suffixed name and be
wrong exactly where a prediction earns its keep.

An account whose currency was never captured (a session linked under the v1 state schema) is
reported as unpredictable rather than guessed. The currency is in the filename, and a real fetch
learns it from `/balances` — the one call this command may not make.

**One such account suppresses its whole bank's prediction**, rather than being dropped from the
grouping. This was caught by checking predictions against files real fetches had written: a bank
with two PLN accounts writes suffixed filenames, and when one of them had no recorded currency,
dropping it shrank the group to one and predicted the *un-suffixed* name — a name the fetch would
never write. Nor is the error only ever a missing suffix: an account joining a group can flip the
whole group from IBAN tails to `eb-<hash8>` digests, so it can change names that were already
being predicted. A prediction that might be wrong is worth less than no prediction, because the
only reason to run this is to be able to trust the answer without spending a request to check.

The exit code follows from what the command is for. A lapsed consent is not this command's
failure; it is a finding, printed with the `link` command to fix it. Reserving a non-zero exit for
the genuinely global errors — missing credentials, malformed `config.toml`/date, a window running
backwards, `--bank` naming a bank that is not configured — keeps `--dry-run` usable as the
config-debugging step it exists to be, where "the configuration is fine, one bank needs
re-linking" is a success.

**Whatever aborts the fetch must abort the dry run, from the same code.** A reversed window
(`--from 2026-07-31 --to 2026-07-01`) was accepted by both for a while: the dry run predicted
`wise_business_USD_2026_07_31-2026_07_01.ofx` and exited 0, faithfully, because the fetch would
have sent that window to the ASPSP and spent a counted request per account to be told no. The
prediction was correct and the answer was useless — a dry run that says "this is fine" about a
window no fetch can satisfy has spent the user's next request for them. So `require_ordered_window`
is one function called by both, not a check written twice; that they cannot disagree is the point,
and it is the property to preserve if a third entry point ever takes a window. The CLI calls it
too, ahead of the public-IP lookup and the run log, so the reversed window is not reported as a
problem with rate-limit mode. Equal dates are a one-day window and stay valid.

---

## A ledger-key collapse is reported as a pair of counts, never by redefining `covered_accounts`

**Decision.** `tools/evidence`'s `CoverageReconciliation` reports `distinct_coverage_keys` and
`accounts_sharing_a_key` *beside* `covered_accounts`, which keeps its meaning: live accounts whose
resolved key finds a ledger entry. ([#39](https://github.com/skolima/gnucash-ofx/issues/39))

**Why.** Accounts sharing an account number resolve to one `ACCTID`, so N accounts collapse onto
fewer than N ledger keys — and `covered_accounts` cannot see it, because every lookup genuinely
succeeds; three of them just succeed against another account's entry. Measured on the real `state/`
2026-08-13: one connection read `live=5, covered=5, requests_unmatched=0` — perfectly healthy —
over a ledger holding two entries for five accounts (see the Revolut section of
[enable-banking.md](enable-banking.md#revolut-currency-pockets-share-one-master-iban-so-a-naive-acctid-collapses)).
The cost of that silence is that fetching one account advances `covered_through` for its sharers,
so the gap warning built to notice an unfetched account is the component being lied to.

**Why not redefine `covered_accounts` to count ledger entries.** Because it is not wrong, and the
redefinition would trade one silence for another: a ledger holding a stale key from a previous
link would then read as under-covered when it is not. The pair is honest in a way either number
alone is not.

**Both key counts are a lower bound.** Resolution runs with the fetch-time hashes absent — those
come from a live `GET /sessions`, and the evidence tool never touches the network (the same limit
`would_resolve_to_bare_uid` carries;
[`adr-evidence-tool.md`](adr-evidence-tool.md) decision 3). An account with no stored identity
keys on its own `uid`, unique by construction, so it reports as keying alone even where a shared
fetch-time hash would collapse it. `accounts_sharing_a_key == 0` means "no collapse visible from
stored state", never "no collapse" — pinned by test.

**The rule that forbids the collapse has since landed** — the uniform hash policy
([adr-revolut-onboarding.md](adr-revolut-onboarding.md) decision 1, shipped in
[#49](https://github.com/skolima/gnucash-ofx/issues/49)) — and it binds this tool as much as the
fetch: all three `tools/evidence` resolvers go through `_stored_acctids`, the fix's own
connection-wide resolution, or the census mis-measures the fix with its own instrument
(invariant-guard, 2026-08-14: on the old per-account rule, a post-#49 Revolut fetch would have
read as five orphaned hash-keyed coverage entries beside the stale 2-for-5 ones reported live). A
collapse reported here is therefore a real defect again, not the known Revolut shape.

**Cost to reverse:** low in data terms — no identity field moves, nothing re-imports. The cost is
the silence coming back: the one failure mode the gap warning cannot see about itself returns to
needing a scratch script to find.

---

## A census field keyed on wire strings is gated on digits, not length — and not on an allow-list

**Decision.** `StateCensus.identification_scheme_kinds` is the first census field whose dict
*keys* come off the wire (`scheme_name`). `_SCHEME_TOKEN` admits letters and underscore only;
anything failing it is counted under `<non-conforming>`, never emitted. This is the argument
behind AGENTS.md invariant "c". ([#39](https://github.com/skolima/gnucash-ofx/issues/39))

**Why a gate at all.** [`adr-evidence-tool.md`](adr-evidence-tool.md) decision 2's guarantee is
that a raw identifier is *structurally* unable to reach a census's output, and this repo is
public. A bare counter over an ASPSP-supplied string rests that guarantee on the ASPSP instead: a
bank that put something account-sized in `scheme_name` would put it in the census's output.

**Why digits, not length.** Every account-identifier form carries digits — IBAN, BBAN, masked
PAN, proprietary id — so excluding digits excludes the whole class. Length does not: a Norwegian
IBAN is 15 characters and a Belgian one 16, both letter-initial, so the first draft's
length-and-charset gate waved both through while its own test (a 28-character Polish IBAN)
passed. Same correction the machine-reference rule above already made once: key on a property of
the class, not on a generic shape.

**Why not an allow-list of the names seen so far.** `PLKNR` is in this repo's own session fixture
and appears in no ISO 20022 code list, so an allow-list would have bucketed a real bank's real
scheme — and reporting an unknown bank's vocabulary is the field's entire purpose. Both directions
are pinned as citation "c" in `tests/test_invariants.py`.

**The claim to distrust:** the gate holds only while no ASPSP puts an all-letters value in
`scheme_name` that is an identifier rather than a code. None is known and one is hard to
construct, but that is a claim about the world, not about the code.

---

## The run log masks session ids too — and the ids already on disk are scrubbed, not deleted

**Decision.** `redact_uid_path` masks the id segment of `/sessions/{id}` exactly as it already
masked `/accounts/{uid}` (head-4/tail-4, ≤8 characters pass unmasked), and `scrub_log(state_dir)`
re-runs that redaction over the existing `state/fetch-log.jsonl` in place, atomically, once per
`fetch`, reporting the changed-line count to stderr. Decision 1 of
[`adr-input-hardening.md`](adr-input-hardening.md)
([#40](https://github.com/skolima/gnucash-ofx/issues/40)); the invariant is in
[AGENTS.md](../AGENTS.md#invariants).

**Why a session id is a different class of secret from an account uid.** The uid *identifies* an
account; the session id *opens* it — `SECURITY.md` classifies it access-granting. That is what makes
the log's pasteable contract load-bearing rather than cosmetic: the module docstring promises the
file is safe to paste and `diagnose.py` steers users to share it when a rate limit bites, so every
`GET /sessions/{id}` was writing a live credential into the one file the tool asks users to hand
out. The test suite pinned the leak as correct (`redact_uid_path("/sessions/abc")` returning its
input unchanged), which is how it survived the review that shipped the log.

**Why remediation, not only prevention.** The ADR reasoned the leak was latent because no fetch log
existed yet. That measurement was wrong — the log's first line predates the ADR by three days — so
the ids were already on disk and extending the regex would have fixed only the *next* request. The
transferable half is about the measurement, not the regex: a design resting on an absence of
evidence ("that file does not exist yet") is one unchecked directory listing away from being a
design for a problem that has already happened.

**Why the file is rewritten in place, rather than deleted or rotated.** The log is the only account
of what the rate-limit allowance was spent on — the cache records successes only — and two ADRs are
parked on it accumulating: [`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md)'s one
open question, whether two connections to one institution share an allowance, which says in terms
that it cannot be answered by thinking harder and only an observed refusal will settle; and
[`adr-input-hardening.md`](adr-input-hardening.md)'s open question 2, whether a real `Retry-After`
ever exceeds the clamp, which waits on the first logged `429`.
Destroying lines to fix a leak would trade one irreplaceable thing for another:
masking a 36-character id costs nothing, while a request already spent cannot be bought back at any
price and a `429` that never recurs cannot be re-observed.

**Why the scrub runs from `fetch`, and not from `RunLog.__init__` or `load_requests`.** Scrubbing in
the constructor would be structurally unforgettable, which this repo usually prefers, but a
constructor cannot report a count — and the count is the actionable half, because only the user can
decide whether an id they have already pasted means re-linking and rotating. `load_requests` would
have covered the read path too, but `tools/evidence/` imports it, and that package's read-only
contract is a property of the import graph rather than of the grep that checks it
([`adr-evidence-tool.md`](adr-evidence-tool.md) decision 8): a write behind a census function breaks
the property, not merely the check. The accepted limit is worth stating plainly — a user who never
fetches again keeps the leak until they do, and `--dry-run` does not scrub because it writes nothing
under `state/` at all. One behaviour did get worse: two concurrent `fetch` runs used to interleave
harmlessly through `O_APPEND`, and a whole-file rewrite makes that lossy. Concurrent fetches already
double-spend the allowance, so this is pre-existing user error — but it is now a lossy one.

**Idempotency is load-bearing, not incidental.** The scrub re-runs on every `fetch`, so a mask that
was not a fixed point would eat four more characters per run until two accounts of one bank stopped
being tellable apart — which is the property the head/tail shape exists to provide, and which
`tools/evidence`'s run-log↔ledger join depends on from the outside: it takes the `uid` from session
state, then reuses `redact_uid_path` on a synthetic path to derive the masked form it matches against
the log ([`adr-evidence-tool.md`](adr-evidence-tool.md) decision 6 — the join keys on state rather
than on the log's redaction, but it still has to reproduce the mask's shape exactly). Keeping the
head and
the tail makes re-masking yield itself; verified exhaustively over identifier lengths 1–59 for both
path kinds (2026-08-13) and pinned by test.

**Never `str.splitlines()` on a file holding ASPSP-controlled text.** This is the most transferable
finding in the change and it is a general rule, not a bug note: `json.dumps(ensure_ascii=False)`
does not escape U+2028, U+2029 or NEL; `str.splitlines()` treats all three as line boundaries; and
three fields on every request line — `api_code` and `error_name`, straight from the bank's error
body, and the allow-listed header values — come straight from the server. So a crafted error code tore one record
into two and the rejoin wrote the tear back permanently, and the lines at risk were exactly the 4xx
and 429s the log exists to explain. Measured on a two-record case (2026-08-13): the `splitlines()`
algorithm turned 2 parseable records into 1 parseable plus 2 unparseable halves and lost the
`400`/`ASPSP_ERROR` line outright; `split("\n")` with `"\n".join` round-trips byte-exactly. The rule
binds readers as much as the writer — a reader that disagrees with the writer about where a line
ends cannot check the writer.

**And `Path.read_text()` is the same bug wearing a different hat**, found by verifying the shipped
scrub against the real file rather than by reading the code again. Universal-newline mode translates
every terminator to `"\n"` *before* any split runs, so it hides a bare `\r` from the split above —
covering three of the four line boundaries and not the fourth — and it re-flavours the file's
terminators to whichever platform is running, which is a live concern because `state/` is synced
between machines: a CRLF log scrubbed on Linux would come back rewritten end to end, every byte
after the first change different for no reason. That case is live rather than theoretical because
`RunLog._append` writes in default text mode, so every line appended on Windows is CRLF already. So
the scrub decodes from `read_bytes()`, keeps each line's `\r` with the line, and writes with
`newline=""` — the same reason `OfxWriter` needs it. Measured 2026-08-13: through `read_text()` the
foreign record split into two, through `read_bytes().decode()` it stays one. **The rule binds the
reader too, and for a sharper reason than tidiness**: `load_requests` is the instrument that proved
the scrub non-destructive by comparing its `(runs, requests, skipped)` triple either side of a
rewrite, so a reader that disagreed with the writer about where a line ends would report a loss the
scrub did not cause — mis-measuring the fix with the fix's own tool. It reads as bytes too.

**The trade this makes, stated rather than glossed.** A bare CR *inside* a line now leaves that line
unparseable and therefore preserved whole, which is the win. A bare CR *between* records is the same
byte and cannot be told apart without parsing, so such a region reads as one unparseable line and is
left alone — an identifier in it stays unmasked, on that fetch and every later one, and because
nothing changed the CLI prints no warning either. Under the old `read_text()` that region would have
been split and masked. Preserving a request that was paid for is worth more than masking an id that
only a foreign writer could have put there in that shape — `json.dumps` escapes CR and LF, so
`_append` cannot produce it — but it is a trade, not a free win, and the next person to touch this
should know which way it was made.

**Measured 2026-08-13**, by executing the shipped functions over a *copy* of the real
`state/fetch-log.jsonl`, the original verified byte-identical by hash afterwards: 31 lines carried a
raw session id, 9 distinct ids, 7 of them live at the time. After one pass, no `path` field holds a
UUID-shaped value — the ones that remain are `headers.x-request-id`, Enable Banking's own request
id, deliberately allow-listed. 307 lines before and after, nothing dropped; zero records differing
in any key other than `path`, key order included; `load_requests` returned an identical
`(28, 279, 0)` runs/requests/skipped triple; a second pass changed 0 lines and left the file
byte-identical; the pre-existing account masking stayed untouched at 248 lines; and
`tools/evidence/coverage_reconciliation.py` returned a dataclass identical to the one computed from
the real state dir, for all 7 banks. A structural census of the whole log found only three path
shapes in it, so the extended alternation covers every id-bearing path the file actually contains.

**Accepted warts.** The function is still called `redact_uid_path` while masking session ids too —
renaming it would ripple into `tools/evidence/`, two test modules and two ADRs that cite it, so the
docstring carries the truth and the rename stays out of a security fix. And the inherited
"≤8 characters pass unmasked" rule now applies to an access-granting identifier: a hypothetical
9-character session id would mask to a form revealing 8 of its 9 characters. All 9 real session ids
measured are 36-char UUIDs, and changing the rule would move the account-mask shape
`coverage_reconciliation` depends on — so the mask stays calibrated for UUIDs, which is worth
knowing before trusting it on a shorter id.

**Cost to reverse:** low. The redaction is additive; reverting re-opens the leak but loses nothing,
because lines already masked stay masked. What a revert cannot do is un-mask history — the real ids
are gone, which is the point.

---

## `ACCTTYPE` comes from the stored `cash_account_type`, through one function that refuses what it cannot map

**Decision.** Shipped as one piece in [#43](https://github.com/skolima/gnucash-ofx/issues/43),
implementing [`adr-accttype-mapping.md`](adr-accttype-mapping.md) decisions 1–5:

1. `ofxout.accttype_for()` is the single mapping site: `CACC` → `CHECKING`, `SVGS` → `SAVINGS`,
   absence and `OTHR` are the explicit `CHECKING` fallback, and `CARD`/`CASH`/`LOAN` — or any
   value outside Enable Banking's documented six-value enum — raise `UncarryableAccountType`.
   Nothing maps to `MONEYMRKT` or `CREDITLINE`: no Enable Banking value honestly is either.
2. A refusal is account-scoped (`BankFailure`, `scope="account"`) and lands **before the
   account's first request**: the stored type is already local, so an account whose statement
   cannot honestly be written spends none of the bank's daily allowance learning transactions no
   file will carry. Siblings still ship, under unchanged disambiguated names — the refused
   account still counts in the grouping, because filenames follow the shape of the connection.
3. Fallback use is one informational line per bank in `status` — never `needs_attention`, never
   at fetch time, where it would repeat on three banks on every fetch until they re-link, about a
   condition no fetch can change.
4. The raw bank value threads state → `Account.cash_account_type` → `build_statement`; fetch and
   `--dry-run` refuse through the same function, so the prediction cannot disagree with the run.
   Shipped stronger than the ADR wrote it: in `_planned_files` the refusal check precedes the
   currency check, so a currencyless refused account reports the refusal and does **not**
   suppress its bank — the fetch refuses before `/balances` could teach it a currency, so it can
   never join a disambiguation group mid-run.
5. The `_CombinedOfxWriter` invariant comment is reworded from "`BANKMSGSRSV1`/`CHECKING`" to
   bank-message-set terms: the refusal is now what keeps the single message-set block correct.

**Why.** Every OFX file this project had ever written said `ACCTTYPE=CHECKING` and nothing chose
it — `ofxstatement`'s `Statement.__init__` default, correct only because every observed
`cash_account_type` is `CACC` (see
[enable-banking.md](enable-banking.md#account-classification-cash_account_type-and-product)). A
savings or card account linked tomorrow would have imported as a checking account, silently, in
every file. `CARD` in particular is not a field change: a card statement belongs in
`CREDITCARDMSGSRSV1`, a writer subclass that does not exist yet — and a guessed mapping is the
silent, permanent version of exactly the error the refusal makes loud.

**Why the fallback rather than refusing on absence.** 8 of 24 accounts carry no stored type — all
in the three v1-schema state files, a condition no fetch can fix, since only a re-link observes
the bank's answer — so refusing would fail three banks on every run for months. `OTHR` joins the
fallback because it is the bank explicitly answering "not otherwise specified": the same
information content as absence, and the one genuinely close call — read option H in the ADR
before reopening it.

**Accepted costs.** A `CARD`/`CASH`/`LOAN` account, if one is ever linked, is a standing
account-scoped exit-1 until the card writer exists; the message says what to do. The refusal and
`SAVINGS` branches ship on synthetic fixtures only — no local account sends anything but `CACC`.

**Cost to reverse: low.** libofx's `gen_account_id()` excludes `ACCTTYPE` and GnuCash matches on
that id alone, so no re-mapping — including backing this out entirely — can orphan an imported
account or re-import a transaction (confirmed from both sources 2026-08-13; the ADR's §4 keeps
the trail), and GnuCash creates `SAVINGS` and `CHECKING` as the same `ACCT_TYPE_BANK` anyway.
Reversal is deleting one field and one function and restoring the comment.

---

## A server never picks how long we wait, and one account's bad data stops at that account

**Decision.** Decisions 2, 3, 5 and 6 of [`adr-input-hardening.md`](adr-input-hardening.md),
shipped as one piece in [#44](https://github.com/skolima/gnucash-ofx/issues/44); the invariants are
in [AGENTS.md](../AGENTS.md#invariants):

1. `_retry_delay` refuses a non-finite `Retry-After` and clamps a numeric one to
   `_MAX_BACKOFF_SECONDS`.
2. The per-account guard is a named `_BAD_DATA_ERRORS = (ValueError, ArithmeticError, TypeError,
   AttributeError)`, and `finite_decimal` rejects NaN and infinities at the mapper, for amounts and
   balances alike. Both balance-reading sites — `_currency_from_balances` and `extract_balance` —
   came inside the guard, *degrading* with a `FetchWarning` rather than failing.
3. `PageBudget` bounds pagination per account-window and `_MAX_RESPONSE_BYTES` bounds a response
   body before anything parses it; exceeding either raises `ResponseLimitExceeded`.
4. `actions/checkout` and `astral-sh/setup-uv` are pinned to dereferenced commit SHAs.

One property ties the four together: a hostile or broken ASPSP costs at most one account its file,
never a sibling's and never the run.

**Why the clamp reuses the retry ladder's own ceiling instead of a second constant.** The header
path returned whatever the server said, while the 60s cap applied only to the backoff ladder:
`Retry-After: 999999999` parked the run inside `time.sleep` for roughly 31 years, and
`Retry-After: inf` passed the bare `>= 0` check and raised `OverflowError` from inside it. Reusing
`_MAX_BACKOFF_SECONDS` leaves exactly one answer to "how long can a single retry ever wait". A
separate, higher cap for the header was rejected because **the number would be invented**: no
`Retry-After` value has ever been observed on this machine, and the one limit measured to need hours
— `ASPSP_RATE_LIMIT_EXCEEDED` — is never retried at all, so the clamp's worst case is a handful of
too-early attempts under `_MAX_RETRIES`, all visible in the run log. `retry-after` is in that log's
header allow-list, so the first genuine value will arrive as evidence rather than as a guess; see
[enable-banking.md](enable-banking.md#rate-limits) for what has and has not been observed.

**Why `ValueError` was not the containment the guard's own comment already promised.** The comment
said one account's bad data must not cost the siblings their files. `Decimal("garbage")` raises
`decimal.InvalidOperation`, which is an `ArithmeticError`, and a wrong-typed `transaction_amount` —
the string `"100"` where an object belongs — raises `AttributeError` from the `.get` chain. Either
one walked past that `except` and aborted the whole multi-bank run. The widened set is deliberately
**not** bare `Exception`, so a programming error still crashes instead of being filed as a bank's
bad data. The honest trade, because it is a real one: `AttributeError` around `map_transactions` is
broad enough to report one of *our* mapper bugs as the bank's bad data. The line was still drawn
there — such a failure is loud, account-scoped, cannot write a wrong file and cannot advance
coverage, whereas narrowing it leaves open the exact escape route the ADR measured.

**Why a non-finite amount is refused while an absent figure is accepted.** This asymmetry is the
load-bearing part. `NaN` and `Infinity` parse cleanly and do their damage later: `NaN` compares
False against everything, so `build_statement`'s `amount >= 0` silently calls a credit a debit, and
`Infinity` serializes into `<TRNAMT>Infinity</TRNAMT>`. Rejected rather than clamped or zeroed,
because an invented amount in a financial file is worse than a missing file. An *unreadable* figure
is a different thing: a `/balances` body that cannot be read degrades to the currency recorded at
link time, and an unreadable closing balance degrades to `end_balance=None`. `None` is the shape
every closed window already uses — `build_statement` then fills `LEDGERBAL` from the statement's own
running total — so failing there would discard a fully mapped statement over a figure the file does
not require, *and*, because a failure correctly advances no coverage, would fail identically on
every later run. At a bank serving ~90 days the window ages out before that resolves. Reject an
invented number; accept an absent one.

**Why exceeding a bound raises rather than returning what it has.** A short answer that looked
complete would advance the coverage ledger over transactions nobody ever wrote — the failure mode
[`adr-coverage-ledger-and-warnings.md`](adr-coverage-ledger-and-warnings.md) names as worse than
silence. So both caps fail that account loudly and advance nothing, and the next run re-fetches the
window.

**Why the page budget is an object threaded through an account's spans, and the two ways that was
got wrong.** One account-window can be fetched as several request spans, because a gap in the middle
of the cache splits it and each span's months are persisted as they land. The first fix created the
budget per call, which handed every span a fresh allowance and made the real bound `cap x spans`.
The second charged *transactions* against a *page* budget, which is worse than the bug it replaced:
a high-volume account would have exhausted its allowance on the first span and then failed the next
one spuriously, on real data rather than hostile data. It now counts requests, charged before each
is sent, and a test fails if a later edit hands each span its own allowance again.

**Why the size check sits ahead of everything that parses the body — and why the request is still
observed first.** `_observe` reads an error body to extract its `api_code`, the 429 branch reads it
again, and `raise_for_status()` means a check placed after it would never see an error response at
all. So the bound is applied before any of them, and `_observe` is told not to parse. The request is
still recorded, with its body left unread: it was spent either way, and the run log is the account
of what was spent (diagnostics must never cost a fetch, and nor may they hide one).

**Why the same exception is account-scoped in one place and bank-scoped in another.** For an
account's data it fails that account: nothing was *refused* — every one of those requests returned
200 — so a sibling account may well be fine, unlike a per-ASPSP 429 that makes a sibling's request
certain to fail. An oversized `GET /sessions/{id}` has no account to blame and happens before the
account loop, so `fetch_bank` turns it into a `BankError`, the bank-scoped type already handled.
Left bare it escaped every handler: raised on the first bank of a run it killed every later bank
before they were attempted, replaced the stdout file list with a traceback, and under `--combine`
discarded every statement collected so far. `cli.main` has to report it too, because `_request` raises it for *every* endpoint
— `link` and `aspsps` discovery included, where no fetch handler exists.

**The message never carries the value or the path.** A rejected amount is described by length and
character class, a rejected identifier by length and code point, and the cap message names the
`operation` rather than the request path. Three separate reasons: `InvalidOperation`
fires on merely non-canonical *real* money (`"1 234,56"`, `"100 PLN"`), `Decimal` accepts a NaN
*payload* (`NaN0000123456789`) so echoing the spelling would place attacker-chosen digits in that
summary, and the path would carry a full
account uid or a live session id — the identifier [#40](https://github.com/skolima/gnucash-ofx/issues/40)
spent a PR masking out of the run log, arriving through a new channel three days later. All of these
strings reach `BankFailure.payload` and the stderr summary users are asked to paste.

**Why the CI pins are commits, and the trap in producing one.** Both actions were on mutable major
tags; a retargeted tag runs attacker code in CI, and `permissions: contents: read` with no secrets
bounds that blast radius without closing it — such a build can still lie about test results.
`astral-sh/setup-uv`'s `v7` is an **annotated** tag, so the obvious `.object.sha` is the tag object
and does not resolve as an action ref; the pin is the commit it dereferences to. Resolved
2026-08-13: both pins are the commit `v7` already pointed at, verified against `v7.0.1` and
`v7.6.0`, so CI runs exactly what it ran before. Dependabot's existing `github-actions` group keeps
them current, so pinning costs no maintenance.

**One thing shipped here that no ADR decision covers.** `pair_conversions` divides two booked
amounts and quantizes, so a finite-but-absurd magnitude (`1e30 / 1`) raises `InvalidOperation` — the
class decision 3 widened the guard for — and it runs *after* every per-account guard, where
`finite_decimal` cannot reach it because that bounds finiteness, not magnitude. The pairing now
degrades to unpaired legs with a `FetchWarning`: the annotation is an enrichment, so losing it costs
a memo line, where aborting cost every bank in the run. Recorded here rather than left as scope
drift — the pairing's own section above still holds (it changes description text only), and this is
the one path by which it could ever have cost a file.

**Cost to reverse: low, per decision.** The clamp and both caps are named constants with
definition-site labels; each reversal or re-tune is a one-line change, and the fetch log is
accumulating the evidence that would justify one. The guards restore today's crash-the-run behaviour
if reverted and nothing else. No identity field changes value in either direction, so no account
orphans and no transaction re-imports. Reverting a SHA pin to a tag is one line per action.

---

## Controls never reach the file, and an identifier is validated rather than repaired

**Decision.** Decision 4 of [`adr-input-hardening.md`](adr-input-hardening.md), shipped on its own
in [#45](https://github.com/skolima/gnucash-ofx/issues/45) because it is the only one of the six
that changes what reaches the OFX file, and so had to clear the **Windows** libofx conformance pass
that Linux CI structurally cannot perform. `to_ascii` drops C0 controls and DEL; a control that is a
*separator* becomes a space; `validate_raw_fields` checks the fields that bypass folding, at the
mapping step, and never repairs one. The invariant is in [AGENTS.md](../AGENTS.md#invariants).

**Why stripped rather than rejected.** The `ord(c) < 128` keep-filter passed every C0 code point and
DEL, and one `\x02` in a memo aborted the writer's `minidom` step with an uncaught `ExpatError` —
measured on the real path, it produces *no file at all*, and since the write loop has no per-account
guard it takes the whole run with it. Zero control characters occur across every string in the local
cache — measured 2026-08-13 over all 244 entries, C1 range included, and over the 12× smaller corpus
before it — so stripping breaks no real data, and failing a run over one byte the file could never carry
fails proportionality. Memo text is *display* data, where silent repair is cheap; the identity
fields below get the opposite treatment for exactly that reason.

**Why a separator becomes a space instead of vanishing.** A word boundary is information: `ACME`,
tab, `Invoice` must stay two tokens for GnuCash's matcher rather than collapse into `ACMEInvoice`,
and libofx silently merges tokens across a newline it deletes. The set is explicit —
`\t\n\v\f\r`, NEL, U+2028, U+2029 — and deliberately **not** `str.isspace()`, which also counts
U+001C–001F as whitespace and would make the rule unpredictable. The last three are not ASCII, so
the old `< 128` filter dropped them silently, and they are exactly the separators decision 1
established that ASPSPs really do put in strings. A source CRLF therefore folds to *two* spaces:
deterministic, and GnuCash tokenizes on whitespace runs.

**Why the raw-passthrough fields are validated and never repaired.** Measured against `ofxdump`
0.10.5 from the **Windows** GnuCash build, 2026-08-13: libofx does not *refuse* a non-ASCII `FITID`,
it returns it **mutated** (`TX-Ü-001` → `TX-\x1ce-001`), and an empty `ACCTID` comes back as `'>'`.
So the alternative to validating is not a crash, it is silent identity corruption — a mutated
`FITID` re-imports the same transaction as new, a mutated `ACCTID` orphans the account and every
transaction already under it. The rule is printable ASCII, not a tight alphanumeric whitelist:
`entry_reference` is the majority `FITID` source and carries `- . _ / |` at three of the six banks
measured, and all 95 printable code points round-trip through libofx byte-identically, so the check
is not stricter than the parser it protects. A rejected identifier is described by length and
offending code point, never quoted — an `ACCTID` *is* an account number, and the message reaches the
stderr summary users are asked to paste.

**Why validation sits at the mapping step and not in the writer.** The write loop has no
per-account guard and coverage has already advanced by the time it runs, so a rejection there would
abort the run *and* leave the ledger claiming a day whose file was never written. At the mapping step
it is one account's failure, before anything is recorded. A test pins the placement: moving the call
into the writer makes the `ValueError` escape the unguarded loop, and the test errors rather than
passing.

**The enumeration of raw-passthrough fields was wrong three times across two reviews, and that is
the transferable finding.** It went three → four → five:

| Field | Provenance | Treatment |
|---|---|---|
| `ACCTID` | ASPSP / derived | validated, account fails |
| `FITID` | ASPSP | validated, account fails |
| `CURDEF` | ASPSP | validated as `[A-Z]{3}`, account fails |
| `BANKID` | **config** | validated, account fails |
| counterparty number in `BANKACCTTO` | ASPSP | gated and **omitted**, account survives |

`BANKACCTTO`'s counterparty number was the fourth, found by running the real writer over hostile
text rather than by reading the code again: a control character there reproduced the same
`ExpatError` *after* `_persist_coverage` had run for every mapped account, so under `--combine` one
crafted counterparty number cost **every** bank in the run its file while every bank's coverage
advanced — and at a ~90-day ASPSP those days then age out unrecoverable. It is omitted rather than
failed because libofx parses nothing out of `BANKACCTTO` (it reports the aggregate as unsupported in
its own output) while the folded `MEMO` copy, the one that actually routes accounts, is untouched.
`BANKID` was the fifth, and its provenance is *config* — `bank_id_for` only strips, upper-cases and
truncates — which makes a typo a **likelier** source than a hostile server rather than a rarer one.
The lesson is not the list: it is that the
enumeration is a thing to re-derive against the writer, never to trust from a previous claim. A
count stated as complete ("three raw-passthrough fields") is what a future contributor reads and
stops checking — [AGENTS.md](../AGENTS.md#invariants)'s own bullet carried a stale count through two
of the three revisions, which is why it now tells the reader to re-derive it instead.

**`CHECKNUM`'s "dropped, not truncated" guarantee had to move back to the raw reference.**
`compose_check_number` folds before it gates, and now that folding *deletes* controls, `R\x027`
would become the valid-looking `R7` — a cut identifier in the register's Num column, precisely what
that rule exists to refuse. Not reachable through Enable Banking today (`_OWN_REFERENCE` fullmatches
digit and UUID shapes), but the guarantee had drifted out of the gate whose own comment claims it
and into another module's regex. The gate now decides on the raw reference, and on control
characters only: `REF-Ä12` → `REF-A12` is a deliberate, information-preserving transliteration and
must keep working.

**The accepted cost, written where the outage would be diagnosed.** `CURDEF`'s `[A-Z]{3}` is the one
rule libofx does not force — it parses `pln` happily, exit 0 — so it rests on ISO 4217 being a
closed contract rather than on the parser. An ASPSP sending a lower-case code would have its
accounts fail on every run instead of being upper-cased. Not normalized, because upper-casing at the
`currency_or_none` boundary would desynchronize the mapper's currency comparison, which checks the
account's currency against each transaction's — a wider change than this decision warrants for a
case no bank has been observed producing.

**Verified twice**, before and after the review changes, because three of them alter what reaches the
file: 26 fixtures through `ofxdump` 0.10.5 (Windows GnuCash build), all exit 0, no `\r\r\n`, no bare
LF or CR, and no byte below `0x20` or above `0x7E` in any body. The hostile counterparty number
produces a clean file where `HEAD~1` still raises `ExpatError` on the same input; all three new
separators come back as a single space with both tokens intact; a control-bearing reference yields
neither `CHECKNUM` nor `REFNUM` (tags absent, not empty). No folding regression: eight fixtures
generated from this branch and from `HEAD~1` are byte-identical apart from `DTSERVER`. The caps hold
in both directions, including the expansion case the conformance pass added — 250 × `ß` folding to
500 characters, with `MEMO` still delivered at exactly 390.

**Cost to reverse: low.** Reverting restores today's crash-the-run behaviour and nothing else. No
identity field changes value in either direction — that is the whole point of validating rather than
repairing — so nothing orphans and no transaction re-imports, whichever way the change is made.

---

## Requests open a transaction-date margin; the cache merges, files by booking month, and stops discarding what it holds

**Decision.** Decisions 1–5 of
[`adr-transaction-date-window-margin.md`](adr-transaction-date-window-margin.md), shipped as one
piece in [#47](https://github.com/skolima/gnucash-ofx/issues/47), closing
[#35](https://github.com/skolima/gnucash-ofx/issues/35); the invariants are in
[AGENTS.md](../AGENTS.md#invariants):

1. Every planned span's **wire** request opens `TRANSACTION_DATE_MARGIN` (7 days, a policy value
   defined beside `LATE_BOOKING_MARGIN`) earlier than the span it answers for.
2. Months stay bucketed by booking date, and **every returned entry is filed into its booking
   month** — including months outside the requested span, under an empty claim that can never
   satisfy `covers()`.
3. `save_cached_month` **merges**: authoritative inside the incoming claim, never lossy outside
   it, keyed on `_transaction_id` — the identity `FITID` rests on.
4. Claims keep deriving from the planned, **un-widened** span — never the widened wire request,
   never the response's extent.
5. A booking−transaction lag past the margin raises a `FetchWarning` (`kind="coverage"`,
   day-counts only in the message) — the margin's named revisit trigger.

**Why.** The live probe (`probe-txn-date-window`, 2026-08-13, Alior, 6 requests, both controls
identical) settled that Alior filters the requested window by `transaction_date` — the purchase
date — while this tool files, claims and resumes by booking date, so a purchase made before
`date_from` but booked inside the window was invisible to the exact-window request. Three
individually-correct mechanisms then certified the deficient result complete forever (`covers()`,
the settled-month TTL, the ledger's requested-window advance), and it fired on the *default*
resume path the morning the ADR was written. Independently, the old overwrite in
`save_cached_month` destroyed a fuller month chunk twice in the two days after the issue was
filed, and the old `months_in(span)` narrowing threw away returned entries booked outside the
requested months. The local census (2026-08-13, every datable cached entry) put the lag at 0–3
days, median 0, never negative — hence 7 = max observed + slack, one calendar week, and hence no
forward margin on `date_to`. At Alior/Alior Kantor/Erste, serving ~90 days of history, each
certified-complete deficient day drifts toward being unrecoverable at any price. The ADR carries
the full measurements, the rejected options (bucket by the filter field, record what came back,
keep the overwrite) and the open questions.

**Where the implementation refined the ADR**, found by `invariant-guard` and review rather than
invented here:

- **The wire floor applies to explicit `--from` too, and the wire is capped at `claim_from`.**
  Decision 1 widened below an explicit `--from` unconditionally while decision 6's repair command
  puts an explicit `--from` at the horizon, where the widened request would `400`. Resolved by
  reading decision 1's "exactly as it does for a resolved window" as operative: the 89-day floor
  bounds every window's wire, the margin is absorbed at the horizon, and the *claim* still honors
  an explicit `--from` exactly as typed. The cap at `claim_from` closes the other edge: a
  defaulted `--from` against an explicit `--to` older than the clamp resolves below the floor,
  where `max(widened, floor)` alone would invert the request and let an empty answer claim days
  no request asked about.
- **A merge that retains days only the old fetch answered keeps the old `fetched_at`** — a
  crosser-only save, or a narrow re-claim of a fuller chunk, must not make a stale moving-tail
  claim read as fresh inside the 6-hour TTL.
- **A poisoned chunk degrades the merge to the incoming set** for that one chunk: keying an
  id-less entry hashes its amount fields, so a wrong-typed entry already on disk (cached before
  the mapper's guard rejects its account) raises `_BAD_DATA_ERRORS` on every later save touching
  its chunk — outside every guard, that crash was the whole run's. `_BAD_DATA_ERRORS` moved to
  `models.py` so `cache.py` shares the one named set.
- **The statement's `FITID` dedup takes the later copy's content at the first copy's position.**
  The wire margin deliberately re-asks a served month's tail, so a freshly amended entry
  duplicates its cached copy; first-copy-wins would ship the stale details under a `FITID`
  GnuCash then refuses to correct on any later import.

**Request cost, so nobody re-derives it fearfully:** the typical resumed fetch stays one span —
zero extra requests; a clamped cold fetch cannot widen — zero; only a wide-but-unclamped window
(over ~83 days at a deep-history bank) can cost at most one extra request per account per fetch.
`CHUNK_VERSION` deliberately stayed 2: a bump discards every existing chunk — a full re-buy at
the horizon banks — while an older reader meeting a `null` claim gets a miss, never a wrong
answer.

**Cost to reverse.** Decision 1 is trivial in code — one constant, one subtraction — and
expensive in silence: removing it re-opens a hole the system then certifies closed three ways,
and every day fetched narrow while it is off ages into permanent loss at the horizon banks; any
reversal must ship with a `--refresh` of the affected tail. Decision 3 is low: no schema change,
so chunks written under the merge read fine under a reverted overwrite — the cost is the measured
narrow-over-wide loss returning. Reversing decision 4 *toward* response-derived claims is the
invasive redesign the ADR's option C describes (chunk format and ledger meaning both move) and
should reopen the ADR, not patch past it. No identity field moved in either direction, so nothing
orphans and nothing re-imports.

---

## Issue citations were renumbered once at export, in a single simultaneous pass

**Decision.** The export renumbering ([#56](https://github.com/skolima/gnucash-ofx/issues/56))
remapped every issue and PR citation in the tracked tree — 317 replacements across 35 files — from
the private repo's numbers to the 55 stub issues on the public `skolima/gnucash-ofx` tracker,
which took over the name when this repo was renamed. `/pull/N` URLs became `/issues/N`: every stub
is an issue, and GitHub redirects either path by number. The standing convention this created is
in [AGENTS.md](../AGENTS.md)'s Conventions; the old→new table lives on the private repo's
export-tracking issue, deliberately not here.

**Why it could not be left alone.** After the rename, every old-number URL resolved to the *wrong
item* on the fresh public repo — old `#19`'s URL landed on stub `#19`, an unrelated issue. Worse
than a 404, because nothing looks broken and no link checker flags it.

**Why one simultaneous regex pass, not a sequence of replaces.** The old and new number ranges
overlap at 19, 22, 23, 24 and 55, so a sequential replace re-maps its own output: an early step
turns old `#76` into `#19`, and a later `#19→#1` step corrupts it. One alternation applied
everywhere at once, consuming a markdown link's text together with its URL so a single match
renumbers both halves and nothing rescans generated text. The pass warned on every 2–3-digit `#N`
it saw and did not map — zero warnings was the completeness check — and lookbehind excluded
upstream references (`libofx#60`) and HTML entities (`&#220;`), verified untouched in the diff.

**The miss worth one line.** A word-boundary `#N` match does not match a part-letter citation:
`#56c` kept its old number while its 17 siblings moved, and would have auto-linked to an unrelated
issue the moment the public tracker reached that count. Caught by invariant review, fixed in the
same change. The next mechanical pass over citations must match part-letter suffixes too.

**Cost to reverse.** Nobody should want to: the private numbers are unreachable from the public
repo. What outlives the pass is the hazard it worked around — because the ranges overlap, a
pre-export number pasted into the tree links somewhere plausible rather than nowhere, which is why
the AGENTS.md convention says translated, never pasted.

---

## Test account numbers carry provenance: one documented example, everything else the recipe

**Decision.** Every full account number in the tree states where it came from, in the comment
where it is defined ([#57](https://github.com/skolima/gnucash-ofx/issues/57)). Two categories
exist: recipe synthetics ([AGENTS.md](../AGENTS.md)'s Data section — unassigned `99999999` bank
code, checksum-valid), and one published documentation example, `EXAMPLE_IBAN` in
`tests/test_coverage.py`, verified by exact-string web search (2026-08-22) against two sources:
the UNFCCC banking-form instructions' per-country IBAN-examples table and tcllib's `iban.test`.
The two values that verified against nothing were replaced with recipe synthetics. The standing
rules live in AGENTS.md's Data section and leak-check's brief, each pointing at the other; this
entry is the why.

**Why a documented-example category, not nines everywhere.** A value that verifies against
published documentation is *stronger* provenance than a fresh nines value: a stranger auditing
the public repo can re-run the search and confirm it was never anyone's account, instead of
taking a "synthetic" label on faith. But only with the sources and the verification date cited
where the value is defined — an unexplained value carrying a real bank code (`11402004` is
mBank's) is indistinguishable from a leak, which was the entire problem being fixed.

**Why the label cites two sources and stops.** The overclaim failure mode: the comment
deliberately does not claim the value is the SWIFT IBAN Registry's own Poland example — the
registry's row is likely a different value. A future verifier who checks the registry, finds a
mismatch, and had been told "this is the registry example" would rightly distrust the whole
label. A provenance label is worth exactly what was verified; cite that and no more.

**Measured, 2026-08-22.** The discarded `PL61109000010000000123456789` fails mod-97 (checked
with an independent mod-97 implementation, not the recipe that generates the synthetics): it
looks like a hand-edited derivative of the registry's Poland example and was never an assignable
number. It survived unnoticed because nothing in the pipeline checksums a value that already
carries its `PL` prefix — `normalize_account_number()` runs mod-97 only to decide whether a bare
26-digit NRB earns the prefix — so green tests never vouched for a test IBAN's validity. In the
same pass, gitleaks over a `git archive HEAD` extraction reported zero findings, the
deliberately fake PEM block in `tests/test_cli.py` included: no gitleaks allowlist exists, and
none is needed until a finding appears.

**Cost to reverse.** Re-sanitizing `EXAMPLE_IBAN` into a nines value keeps every test green — no
assertion reads its digits — and silently destroys the provenance work. That green-tests
sweep is exactly what the AGENTS.md exception and the leak-check carve-out were written to stop;
the label and those two gate documents are the only things defending the value.

---

## Not done, and why

- **Per-transaction details endpoint.** Would cost one API call per transaction against a strict
  daily limit, and the banks that lack counterparty data also return `transaction_id: null`, so it
  is uncallable exactly where it would help.
- **Direct bank PSD2 APIs.** Require a licensed TPP and an eIDAS QWAC/QSEAL certificate. Avoiding
  that is the entire reason for using an aggregator in Restricted Mode.
- **OFX 2.x (XML) output.** Tested, and it does not dodge the SGML character-set restriction —
  see [#4](https://github.com/skolima/gnucash-ofx/issues/4) for the six-variant matrix. libofx
  has no XML parse path: it detects `<?xml`, skips the iconv conversion, and hands the file to
  the same OpenSP parser with the same `opensp.dcl` and the same `ofx160.dtd` (`ofx201.dtd` is
  shipped but never referenced). A pure-ASCII OFX 2.0 file imports cleanly, so the format itself
  is accepted; the format makes no difference to the characters on any platform — on Windows both
  1.02 and 2.x lose them and the register shows the same string either way, on Linux both keep
  them. The sharpest evidence is the numeric-character-reference variant, a pure-ASCII file where
  nothing can go wrong at the decoding stage and OpenSP still reports
  `"220" is not a character number in the document character set` — everywhere, not just Windows.
  Patching `opensp.dcl` to admit 128-255 would not help either, since `SP_ENCODING=ms-dos` is
  compiled into libofx and the bytes would survive as CP437 mojibake. Only an upstream fix moves
  this, which is what [#1](https://github.com/skolima/gnucash-ofx/issues/1) tracks.

  The cost was previously recorded here as "writing our own OFX writer". That was an
  overestimate: `ofxstatement` builds the body with `ElementTree`, so it is already well-formed
  XML with every element closed, and only the nine-line `OFXHEADER:100 …` preamble is
  1.x-specific — which `ofxtools.header.OFXHeaderV2` already renders. Moot given the result, but
  it should not stand as a reason not to have tested.
