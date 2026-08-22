# Enable Banking: observed API behaviour

Things this project learned the hard way, mostly by hitting them in production. The official
docs do not state most of this. Everything here was verified against live connections unless
marked otherwise; where a behaviour changed over time, the date is noted.

Access is **Restricted Mode** (free, own accounts only, no commercial contract), which is why
some endpoints below are unavailable.

## Endpoints

| Endpoint | Behaviour worth knowing |
|---|---|
| `GET /aspsps` | Enable Banking's own catalog, **not** an ASPSP call — it does not consume the bank's rate-limit allowance. Fields: `name, country, logo, psu_types, auth_methods, maximum_consent_validity, beta`, and — added since this table was first written — `bic` on some entries; see [BIC](#bic). `required_psu_headers` appears on a small minority. |
| `POST /sessions` | The **only** place the full `AccountResource` appears. Per account: `uid`, `account_id` (`iban` / `other`), `all_account_ids`, `account_servicer`, `currency`, `identification_hash` + `identification_hashes`, `name`, `product`, `usage`, `cash_account_type`, `details`, `credit_limit`, `legal_age`, `postal_address`, `psu_status`. **No `accounts_data` array** — that is a `GET /sessions` thing; here the hash sits on the account object. Discard any of it at link time and you cannot get it back without re-linking, so `link` persists every field it names *and* the whole response body into `state/<bank>.json`. |
| `GET /sessions/{id}` | `accounts` is a list of **bare UID strings**, not objects. A separate `accounts_data` array carries `uid` + `identification_hash` + `identification_hashes` — and nothing else. No IBAN, no BIC, no currency. |
| `GET /accounts/{uid}` | **404 in Restricted Mode.** Account currency has to be inferred from `/balances` instead — or, when the ASPSP returns no usable balance, read from what `link` captured. |
| `GET /accounts/{uid}/balances` | Reliable. The practical source of an account's currency. |
| `GET /accounts/{uid}/transactions` | See the quirks below — the date parameters are advisory at several banks. |
| `GET /accounts/{uid}/transactions/{transaction_id}` | Requires a non-null `transaction_id`. Passing `entry_reference` instead returns `422 TRANSACTION_DOES_NOT_EXIST`. |

### Account identity across sessions

**Account UIDs are regenerated on every re-link.** Anything derived from a UID is unstable across
consent cycles, which for us would mean GnuCash seeing brand-new accounts every ~180 days.

Stable identifiers, in the order this project prefers them:

1. `account_id.iban` — captured at link time into `state/<bank>.json`.
2. `account_id.other.identification` — several ASPSPs leave `iban` null and put the number here.
3. `identification_hash` — Enable Banking documents it as stable "for matching accounts between
   multiple sessions". Digested to a short `eb-<16hex>` token. Captured at link time, with
   `accounts_data` as the fallback for sessions linked before that was stored.
4. `uid` — last resort only.

Everything `POST /sessions` reports per account is persisted at link time, plus the raw body — see
[decisions.md](decisions.md#everything-the-link-response-carries-is-captured) for why, and what is
deliberately dropped from it.

Measured 2026-08-13, via `tools/evidence`'s census over this machine's `state/` (stored data only,
zero API requests):

- The `scheme_name` vocabulary observed across all seven linked connections is `IBAN`, `BBAN`,
  `CPAN`, `PLKNR`. `PLKNR` appears in no ISO 20022 external code list — a real bank uses a name no
  allow-list would contain, which is why the census gates the field on shape (letters only)
  instead; see [decisions.md](decisions.md#a-census-field-keyed-on-wire-strings-is-gated-on-digits-not-length--and-not-on-an-allow-list).
- **Three of the seven connections store no raw `POST /sessions` body at all** (linked under the
  v1 state schema, before the body was captured). Anything read from the raw body — such as
  `all_account_ids` — therefore has "accounts with a raw entry", not the account count, as its
  honest denominator.

### Account classification: `cash_account_type` and `product`

Measured 2026-08-13 — a census over this machine's `state/` (all 24 parsed account records across
the seven connections, plus the 16 raw `POST /sessions` account bodies the v2 schema keeps) and
Enable Banking's API reference, fetched the same day:

- **`cash_account_type` is a six-value enum**: `CACC`, `CARD`, `CASH`, `LOAN`, `OTHR`, `SVGS` —
  Enable Banking's own vocabulary, **not** the full ISO 20022 `ExternalCashAccountType1Code`
  list. `TRAN` cannot arrive.
- **Observed vocabulary: {`CACC`} ×16.** The other 8 of the 24 accounts carry none — all in the
  three v1-schema state files (erste, millennium, wise_personal), which predate the field.
  Absence means "never persisted", not "bank omitted it", and only a re-link can fill it:
  `POST /sessions` is the only endpoint that returns the field.
- **`product` is absent everywhere** — all 24 parsed records and all 16 raw session bodies — so
  it is not an alternative signal for an account's type.

`ofxout.accttype_for()` is the single mapping onto OFX `ACCTTYPE`, refusing what the bank message
set cannot carry — see [`adr-accttype-mapping.md`](adr-accttype-mapping.md) and
[decisions.md](decisions.md#accttype-comes-from-the-stored-cash_account_type-through-one-function-that-refuses-what-it-cannot-map).

## Error envelope

Errors come back as a JSON object with four keys, and **the machine-readable code is `error`**,
not `message`:

```json
{"code": 400, "message": "Error interacting with ASPSP", "error": "ASPSP_ERROR", "detail": null}
{"code": 429, "message": "Too many requests", "error": "ASPSP_RATE_LIMIT_EXCEEDED",
 "detail": {"message": "Too many requests", "error_name": "RateLimitException", "error_data": {}}}
{"code": 400, "message": "Error interacting with ASPSP", "error": "ASPSP_ERROR",
 "detail": {"message": "Service unavailable", "error_name": "HttpException", "error_data": {}}}
```

- `code` repeats the HTTP status; it carries no extra information.
- `message` is prose for a human and is **not** stable enough to match on — the same
  `ASPSP_ERROR` reads as "Error interacting with ASPSP".
- `error` is the token worth branching on (`ASPSP_ERROR`, `ASPSP_RATE_LIMIT_EXCEEDED`).
- `detail` is **not a reliable ASPSP/platform discriminator**: the first example is an
  `ASPSP_ERROR` with `detail` null, the third a live `ASPSP_ERROR` carrying the platform shape
  (N26 `/balances`, 2026-08-19) — three observations total, so no claim here about which shape is
  typical. `error_name: "HttpException"` / `"Service unavailable"` there means the ASPSP leg was
  unavailable — whether N26 itself or Enable Banking's connector to it, the response cannot say —
  and the failure was transient: the same call had returned 200 two hours earlier that day, it is
  the only N26 non-200 ever logged, and the 400 carried no `retry-after` header (so the
  never-observed claim under [Rate limits](#rate-limits) still holds). That shape reads as
  **retry later**, not a window or consent problem. Nothing threads `detail` into `diagnose()` —
  one observation per `error_name` is a vocabulary, not a contract — but `state/fetch-log.jsonl`
  **persists `detail.error_name`** (re-weighed the same day this section first said it should not:
  the field's shape is attested twice, on the verified 429 and this 400, and "re-weigh if it
  recurs" is unactionable from a log in which a recurrence is only another bare 400).
  `error_name` is a token only — the `message`/`detail.message` prose stays out, or unbounded
  server text lands in a file meant to be pasteable — and it is ASPSP-controlled text under the
  same split-on-`"\n"` and redaction rules the scrub invariant already names for `api_code`. Lines
  written before 2026-08-19 lack the key; read it with `.get`.

All three verified against live 400 and 429 responses, August 2026. Match on `error`; anything
reading `message` gets prose and silently fails to recognise the code.

## Rate limits

- PSD2 caps **background** fetches at roughly **4 per day** per ASPSP. Exceeding it returns
  `429 ASPSP_RATE_LIMIT_EXCEEDED`, and the documented recovery is to **wait ~6 hours**.
- A fetch counts as **online** — much higher limits — when PSU headers are present
  (`Psu-Ip-Address`, `Psu-User-Agent`). Either send *all* required headers or *none*; a partial
  set returns `PSU_HEADER_NOT_PROVIDED`. Required headers per bank are in `required_psu_headers`.
- Retrying with backoff does **not** beat the daily cap. Once exhausted it stays exhausted; only
  time helps. This is why successful fetches are cached (default 6h).
- **There is no quota counter to read.** Measured 2026-08-09 over `GET /application` and
  `GET /aspsps`: the responses carry no `X-RateLimit-*`, no `RateLimit-*`, and no `Retry-After` —
  nothing but Google Front End plumbing and an `x-request-id`. So the allowance cannot be
  inspected, only spent, and the *scope* of a limit (per connection? per ASPSP? per PSU?) can be
  learned only by observing which requests get refused. That is what `state/fetch-log.jsonl`
  exists for — see below.
- **No `Retry-After` value has ever been observed here, on any response.** Measured 2026-08-13 by
  reading every request line in `state/fetch-log.jsonl` — the whole log, 279 requests at the time,
  going back to 2026-08-09: zero `429`s, zero retries (every line is a first attempt — and `attempt`
  is 0-based), and the one non-200 was a `400 ASPSP_ERROR` carrying no `retry-after`. So any claim
  about typical values is invention, including a "generous" local cap for the header. The header is
  in the run log's allow-list, so **the first one will be recorded** and turns a re-tune into an
  evidence-backed one-line change. Until then a numeric value is clamped to the backoff ladder's own
  ceiling (60s) and a non-finite one is refused —
  [`adr-input-hardening.md`](adr-input-hardening.md) decision 2.
- **Online mode used to depend on a lookup that could fail silently.** PSU headers need an IP, and
  a single lookup service decided the whole run's rate-limit mode: if it was down, `fetch` sent no
  PSU headers and ran at ~4/day, with one easy-to-miss warning on stderr. The bank with the most
  accounts then tripped the cap first — which looks exactly like a shared allowance and is not.
  Now: several services are tried in turn, each answer validated as an address before it is
  accepted, and a run that still cannot determine one **stops** instead of quietly taking the
  smaller allowance (`--allow-background` overrides). Every run records `psu_mode` regardless.
- **`EB_PSU_IP` is a fallback, not an override.** The *detected* address wins, because the header
  is supposed to carry where the user is actually connecting from and a configured value on a
  dynamic address is stale within a day — pinning it would replace a correct value with a
  confidently wrong one. A disagreement between the two is one warning line, and the configured
  value is used only when every lookup has failed.

### The run log

`state/fetch-log.jsonl` is a record of every request a `fetch` sent: bank key, the
`(aspsp, country, psu_type)` rate-limit domain, method, path (account UID and session id redacted),
status, the `error` code, the `detail.error_name` token on non-200s (lines before 2026-08-19 lack
the key), attempt number, elapsed, and any quota-ish response headers. One `run`
record per invocation carries the window and `psu_mode`. Append-only apart from `scrub_log`, the
one in-place redaction pass each `fetch` runs over it — `GET /sessions/{id}` was logged unredacted
until [`adr-input-hardening.md`](adr-input-hardening.md) decision 1, and a regex fix alone would
have left the already-written ids on disk.

Two traps for anything that reads this file: `attempt` is **0-based**, so a first-attempt filter
written as `attempt == 1` matches nothing; and lines must be split on `"\n"` rather than
`str.splitlines()`, because `api_code`, `error_name` and the kept header values are ASPSP-controlled
text and a U+2028 in any of them would tear one record into two.

It exists because the cache records **successes only**, which makes a 429, a 400 and a bank that
was never requested indistinguishable after the fact — see
[`adr-aspsp-rate-limit-domain.md`](adr-aspsp-rate-limit-domain.md) §7, where that is what stopped a
reconstruction of past runs from settling whether two connections to one institution share an
allowance. It costs no API requests. The attempt number is the field that separates a 429 which
recovered under backoff (transient, or a per-second limit) from one that never does (the daily cap).

It holds no amounts, counterparties or account numbers, and is gitignored with the rest of
`state/`. Reading it: `alior_kantor` showing `status: 429` on its **first** request, with no
successful request of its own before it, is the shared-allowance signature.

### Local caps, and what they rest on

Two bounds are enforced client-side, so a server answering forever or answering enormously cannot
hold or exhaust a run ([`adr-input-hardening.md`](adr-input-hardening.md) decision 5). Both are
**policy picks with a measured floor and no measured ceiling**, and hitting one is a reason to
investigate the ASPSP before re-tuning the constant:

| Cap | Value | What it is measured against |
|---|---|---|
| `_MAX_PAGES_PER_ACCOUNT` | 100 requests per account-window | provable pagination has never exceeded 2 pages; no account-window has ever needed more than 4 requests |
| `_MAX_RESPONSE_BYTES` | 10 MB per response body | the largest whole-window response ever observed here is well under 1 MB |

Measured 2026-08-13 over every request line of `state/fetch-log.jsonl` and all 244 cache entries.
Both therefore carry better than 25x headroom over everything ever seen. Exceeding either raises
`ResponseLimitExceeded` and fails **that account** — never a silent short answer, which would
advance the coverage ledger over transactions nobody wrote. The failure names the `operation`
(`transactions`, `balances`, `session`) rather than the request path, so read that to tell which
endpoint misbehaved; an oversized `GET /sessions/{id}` fails the whole **bank**, since there is no
account to attribute it to. The body check runs before `.json()`, so
it bounds the parse and everything downstream, not the transfer: httpx has already buffered and
decompressed by the time the response object exists. Re-tune against a fresh runlog census, never
against a guess, and never by raising one to make a bank work.

## Transaction data quirks

- **Date filters are ignored by some ASPSPs.** Millennium returns full history regardless of
  `date_from`/`date_to`, so results must be filtered client-side.
- **The date window filters on `transaction_date` (purchase date), not `booking_date`, at Alior.**
  Probed 2026-08-13 (`probe-txn-date-window`, 6 requests, one card account — the [probe
  ledger](probes.md) has the full design): a window opening 2026-08-07 returned every entry both
  booked and transacted on that boundary, omitted every entry booked on or after it but transacted
  a day or two before it, and its minimum `transaction_date` landed on `date_from` exactly; two
  identical control windows sandwiching the test ruled out mutation. Consequence: a request must open earlier than
  the earliest *purchase* date it needs, not the earliest booking date — an entry booked in-window
  is invisible if bought before `date_from`. Note the observability limits: `transaction_date` is
  populated only by `alior`/`alior_kantor`, and only in responses fetched since ~July 2026 (the
  same months fetched 2026-06-30 have it null throughout); Revolut populates `value_date` but
  always with zero lag; Erste, Millennium and Wise populate neither extra date — the filter basis
  there is unobservable, not disproven. See
  [`adr-transaction-date-window-margin.md`](adr-transaction-date-window-margin.md) for the
  measured lag range and the request-margin design this forced. Since
  [#47](https://github.com/skolima/gnucash-ofx/issues/47) every wire request opens **up to**
  `TRANSACTION_DATE_MARGIN` (7 days, `cache.py`) earlier than the span it answers for — less
  where the 89-day floor absorbs it (a cold fetch at the clamp opens zero days early), and never
  above the span's own start. A fetch-log window opening up to a week before the reported
  resolved window is that margin at work, not a bug.
- **Windows longer than ~90 days are rejected** by several banks (`400 ASPSP_ERROR`), hence the
  90-day request chunking.
- **History reaches back only ~90 days** at Alior, Alior Kantor and Erste — any window starting
  earlier fails with `400 ASPSP_ERROR`, and `strategy=longest` does *not* help (the ASPSP itself
  refuses). Millennium and Wise serve much longer history. Consequence: a full-year export is
  simply not possible for those three; they start ~90 days back.
- **`date_from` at exactly today − 90 is served**; today − 120 is refused. Measured 2026-08-09 in
  online mode with single-day windows, one account per bank: Alior `200` at −30 (control), `200` at
  −90, `400 ASPSP_ERROR` at −120; Erste `200` at −30 and `200` at −90. So the 90 is the **last day
  served**, not the first day refused, and a default window may reach it. The exact edge between 90
  and 120 was left unmeasured on purpose — bisecting it costs counted requests at the ASPSP with
  the tightest allowance, to learn something no code needs. Alior Kantor was not probed: same
  institution, plausibly the same allowance.
- **`continuation_key` pagination is shallow — and a request count is not a page count.** Measured
  2026-08-13 across every request line ever logged and all 244 cache entries: every page chain
  provably due to pagination stayed within **2 pages**, and no account-window has ever needed more
  than **4 requests**, even under the worst reading of the early log lines (they predate the `window`
  field, so a multi-span window and pagination cannot always be told apart there). The two differ
  because one account-window can be fetched as several request spans — a gap in the middle of the
  cache splits it — so counting requests per window over-counts pages, never under-counts them. The
  loop's exit condition is the server's own key, which is why it is bounded locally; see the caps
  above.
- **`strategy`** (`default` / `longest`) controls history *depth*, not detail level.
  `transaction_status` only filters BOOK/PENDING.
- **`transaction_id` is null** when the ASPSP exposes no extra detail, which makes the
  transaction-details endpoint uncallable. `entry_reference` is not a substitute.
- **Counterparty account numbers** arrive inconsistently: sometimes `creditor_account.iban`,
  sometimes `other.identification`, and for Polish banks often as a bare 26-digit domestic NRB
  rather than an IBAN. Always read them via `account_identifier()`.
- **The counterparty account is frequently absent entirely**, and when it is, *nothing else in the
  payload carries it* — not `*_agent`, not `*_account_additional_identification`, not the
  remittance text. There is no fallback to write; the transaction simply has no IBAN.
  Verified by scanning every field of 268 live transactions for an IBAN-shaped string: the mapper
  extracted every one that was present. See the Wise row below for the worst case.
- **`merchant_category_code` is always null.** The field is in the schema and present on every
  transaction, from every ASPSP here — and populated on none of them, card payments included.
  A full field inventory over the local cache found it empty in 100% of occurrences. So there is no
  MCC to map to a category name, and nothing to feed the matcher from it. Do not re-derive this by
  reading the schema and assuming the field is live; it reads as available and is not.
- **`bank_transaction_code.code` is the only category-ish field that carries anything**, and it is
  a *mechanism*, not a merchant category: the observed values are `CARD`, `TRANSFER`, `CONVERSION`,
  `DEPOSIT`, `MONEY_ADDED`, `ACCRUAL_CHARGE` and `UNKNOWN`, and it is absent on more than half of
  transactions (Wise populates it; the Polish banks largely do not). It says how money moved, never
  what was bought — every purchase is `CARD` whatever the shop. `bank_transaction_code.description`
  and `.sub_code` are present in the schema and never populated either.
- **`remittance_information` is a list, and the split between its elements is real structure —
  but not the structure it looks like.** Wise sends its reference as an element of its own, in a
  uniform `["<reference>", "<prose>"]` pair: `TRANSFER-<10 digits>` on transfers,
  `CARD-<10 digits>` on card payments, `BALANCE_CASHBACK-<uuid>` on cashback. It is tempting to
  read that as "element 0 is the machine part": it is not. In the large majority of two-element
  arrays element 0 is ordinary prose. So `_split_reference()` matches a whole element against a
  known prefix and never a substring or a position. No other ASPSP here sends anything comparable.
- **Fee rows repeat the parent's id, in both slots.** A fee arrives as
  `["FEE-CARD-<id>", "Wise Charges for: CARD-<id>"]` with no counterparty name, where the id is the
  *charged* transaction's, not the fee's. The element is dropped and the prose kept, so the link
  survives in the one place it reads as a sentence.
- **Not every `<PREFIX>-<digits>` is a payment reference.** `BALANCE-<digits>` recurs across
  transactions — it identifies the balance — and `ACCRUAL_CHECKOUT-invoice-<digits>` names
  something a reader uses. The prefix set is explicit for exactly this reason.
- **The reference is already inside `entry_reference`** (and so inside the `FITID`) for every Wise
  transaction observed, which is why moving it to `CHECKNUM` loses nothing from the file — only
  from the fields GnuCash tokenizes.
- **Per-transaction fees arrive as their own entries, booked on their parent's date** — a bank's
  own CSV/statement export may merge the same fee into a column on the parent row, so the API side
  runs finer than an export suggests, with more entries per window and longer same-day runs.
  Measured 2026-08-12 at a fintech-style institution by joining a historical export archive
  against `cache/` — both already on disk, zero requests. Where a bank-style institution could be
  compared month-for-month the two sides agreed entry for entry, and card authorisations, pending
  vs booked, conversion legs and internal transfers showed no count difference anywhere
  ([adr-ofx-batch-splitting.md](adr-ofx-batch-splitting.md) §11).
- **The own-account side is sometimes populated instead of the counterparty side.** Wise card
  transactions (`bank_transaction_code.code == "CARD"`, `DBIT`) leave `creditor_account` null but
  fill `debtor_account` with the *card's* last four digits under
  `other.identification` + `scheme_name: "CPAN"`. Millennium fills both sides on every
  transaction. Reading the side chosen by `credit_debit_indicator` — as `_counterparty_iban()`
  does — is what keeps our own account number out of the memo.

## Per-institution notes

| ASPSP | Notes |
|---|---|
| Alior Bank | `beta`. Counterparty fields (`creditor`/`debtor`, `*_account`) and `transaction_id` were **all null until Enable Banking fixed the integration in August 2026** — after which the fix applied retroactively to historical transactions. Counterparty accounts come as bare NRBs under `other.identification`. **Filters the requested window by `transaction_date`, not `booking_date`** — see the quirk above. History ~90 days. |
| Alior Kantor | Same institution and bank code (2490) as Alior; a separate connection. History ~90 days. |
| Bank Millennium | Ignores server-side date filters. Leaves `creditor_account.iban` null, putting the number under `other.identification` with `scheme_name: "BBAN"`. Full history available. |
| N26 | Not `beta`. Catalog `bic` `NTSBDEBBXXX`; consent 180 days. Everything below measured 2026-08-13 over **one account and a very small same-day SEPA sample** — every "always" means "in every transaction observed", nothing more. `transaction_id` **null** with `entry_reference` a **bare UUID**, so the FITID is that UUID and the transaction-details endpoint is uncallable. **Both** `creditor_account.iban` and `debtor_account.iban` populated on every transaction, own side included — Millennium's both-sides pattern but under `iban`, never `other.identification` — so direction-based reading is what keeps our own IBAN out of the memo. Only the counterparty's party object carries a `.name`; the own-side object is null. `bank_transaction_code` fully populated with **ISO 20022 tokens** (`PMNT`/`ICDT`/`ESCT`) — `description` carries the ISO domain token, not prose. Remittance is a single prose element; no machine reference to lift. `value_date == booking_date`, `transaction_date` null. Balances report **only `XPCD`** ("Expected balance") — no `CLBD` to prefer. Link-time `currency` was a real ISO code (no `XXX`), `cash_account_type` `CACC`, `product` populated, `account_servicer.bic_fi` populated. History horizon unmeasured (the account is newer than any window that could probe it). One transient `400 ASPSP_ERROR` with a platform-shaped `detail` ("Service unavailable") observed on `/balances`, 2026-08-19 — see [Error envelope](#error-envelope). |
| Erste Bank Polska | `beta`. Accounts carry bank code 1090 (formerly Santander Bank Polska). BIC `WBKPPLPP` **confirmed 2026-08-14 on the bank's own site** (erste.pl FAQ: "Kod SWIFT (BIC code) Erste Bank Polska jest następujący: WBKPPLPP") — the ex-Santander BIC was kept after the acquisition, not reissued; the catalog entry carries no `bic` at all, so the bank's own statement is the source ([#51](https://github.com/skolima/gnucash-ofx/issues/51)). History ~90 days. |
| Revolut | Not `beta`; catalog `bic` `REVOLT21` under both PL and LT; consent 180 days. **One account per currency pocket, four of five sharing one master LT IBAN** (the PLN pocket also carries its own PL one; one pocket a BBAN under `other`) — so an IBAN-derived `ACCTID` names a group, and the **whole connection resolves through `identification_hash`** instead ([adr-revolut-onboarding.md](adr-revolut-onboarding.md) decision 1, the uniform policy; hashes measured distinct per pocket). `account_servicer` **null on all five** — the whole object — so no `bic_fi` is ever coming; the `bankid` is the catalog value, typed by hand. Everything below measured 2026-08-12 over one connection's first fetch. `transaction_id` **null on every transaction**, so `FITID` is `entry_reference` — a 36-char lowercase UUID, populated on all, **byte-identical on both legs of a currency conversion** (the deal key since [#53](https://github.com/skolima/gnucash-ofx/issues/53) — `revolut:<entry_reference>`, see the pairing section below; a conversion is always two transactions in two different pockets, never one). `bank_transaction_code` `EXCHANGE` / `TOPUP` / `TRANSFER` observed — vocabulary **not closed**; `sub_code` null. Counterparty presence **tracks transaction type, not direction**: every non-conversion row carries a party name and an account under `iban`, every conversion leg carries neither — contrast Wise, asymmetric by direction. Remittance is 1–2 prose elements with **no machine references** (the machine reference is the `entry_reference` field, and at 36 characters it could never reach `CHECKNUM` anyway). `booking_date == value_date` (date-only) on 100%; `exchange_rate` a present-but-null key; `instructed_amount` absent entirely; `status` `BOOK` throughout, no `PDNG` returned. **Registering PL surfaces all five pockets despite the LT-majority IBANs** — the prefix says where an account was issued, not which catalog entry a consent belongs under. A 2026-05-15 → 2026-08-12 window was accepted (not the Alior/Erste refusal shape at that depth); deeper delivery is unmeasurable until the account accumulates history. |
| Wise (personal / business) | Not `beta`. One account per currency balance, **serviced by different institutions per currency** (`BE..` Wise Europe, `GB..TRWI..` Wise UK, `PL..1020....` a local partner) — so no single BIC describes the connection, and `account_servicer` is **null** on every account, so no `bic_fi` is ever captured. `product` is null too; `currency`, `name`, `usage`, `cash_account_type` and `identification_hash` are all populated. Some balances have no IBAN at all. Full history available. **No counterparty account on outgoing payments** — see below. |

All four Polish banks are flagged `beta: true` by Enable Banking; Wise, N26 and Revolut are not.

### Wise: counterparty IBANs are mostly unavailable

Wise is the worst offender for missing counterparty accounts, and the pattern is asymmetric.
Measured over the wise_business EUR balance, 2026-01-01 → 2026-08-08:

| Direction | Code | Counterparty account present |
|---|---|---|
| `DBIT` (money out) | `TRANSFER` | **0 of 20** |
| `CRDT` (money in) | `DEPOSIT` | 7 of 8 |
| `CRDT` (money in) | `UNKNOWN` (Wise cashback) | 0 of 8 — cashback has no counterparty |

- **Outgoing payments never carry one.** `creditor_account` and `creditor_agent` are both null on
  every outgoing transfer; only `creditor.name` is populated. So a recurring payee like a monthly
  supplier invoice will never get an IBAN in its memo, no matter how many times it recurs. In
  GnuCash the payee name in `NAME` is the only matching signal available for these.
- **Incoming payments usually do, but not always** — which is what makes it look intermittent.
  The same payer appeared 7 times: 6 with `debtor_account.iban`, once without. The odd one out
  also differed in ways we do not control (name casing, and a bare invoice number as the
  reference instead of the payer's usual reference format), which points at the payment being
  sent over a different rail. The IBAN travels with the payment, so whether it arrives is the
  *payer's* choice of rail, not a property of this connection.

This is data absence upstream, not a mapping gap: the fields simply are not in the response.
Cross-checking against Wise's own API would need `WISE_TOKEN_*` in `.env` (currently unset).

### Revolut: currency pockets share one master IBAN, so a naive `ACCTID` collapses

What the collapse cost, and the fix: with the pockets collapsed onto one `BANKID`+`ACCTID`, the
two legs of a currency conversion — which share one byte-identical `FITID` by design — landed
under one account identity, and GnuCash's per-account `FITID` de-duplication **silently dropped
the second leg** in the default per-pocket import (measured on GnuCash 5.16, 2026-08-13 — the
gate record is in [adr-revolut-onboarding.md](adr-revolut-onboarding.md) open question 1). The
accepted fix (decision 1, the uniform policy, option M): once any IBAN is shared within a
connection, **every** account of it resolves through `identification_hash` — an account with no
hash keeps its IBAN and warns, never falling to the re-link-volatile `uid`.

Measured 2026-08-13, via `tools/evidence`'s census and reconciliation over this machine's `state/`
(stored data only, zero API requests):

- The connection's **5 live accounts resolve to 2 distinct coverage keys, with 4 accounts sharing
  a key** — the pockets carry one master IBAN, plus one pocket with its own. Every one of the five
  ledger lookups still succeeds (`covered_accounts=5`), so fetching one pocket advances
  `covered_through` for its sharers; see
  [decisions.md](decisions.md#a-ledger-key-collapse-is-reported-as-a-pair-of-counts-never-by-redefining-covered_accounts).
  The other six connections report no sharing at all (`distinct_keys == live_accounts`) — the
  collapse is specific to this bank's shape, not a general condition.
- **2 distinct IBANs, but 5 distinct `identification_hash` values across the 5 accounts** — the
  hash can separate the pockets where the IBAN cannot, which is what decision 1's uniform
  `identification_hash` resolution falls through to.
- `all_account_ids` cannot do that job: the 5 pockets hold **8 entries between them with only 3
  distinct identifier values**, and two of the doubled pockets carry `scheme_name: "IBAN"` twice —
  an identifier can repeat under more than one entry, so entry count is not identifier count.

**Observed at the 2026-08-14 re-link (first observation; the tripwire stays):** the hash's
stability across re-links was documented by Enable Banking but never observed here until the
connection was re-linked on 2026-08-14 — under the uniform policy all five pockets ride on it.
Compared locally (SHA-256 digests of hashes and the resolved `ACCTID` set, before and after, via
`state.load_session` + `run._stored_acctids`): **5 of 5 hashes unchanged, 0 `ACCTID`s moved; 0 of
5 uids survived, as expected; the shared-IBAN structure persists (one identifier on 4 of 5); new
consent to 2027-02-10.** The design bet decision 1 made without evidence is confirmed for one
re-link cycle on one ASPSP — it says nothing about a Revolut-side re-KYC, an account re-issue, or
Enable Banking changing its hashing input, so **repeat the comparison at each future re-link**
rather than retiring it on one data point. Compare **locally**, publish only counts — the digests
and identifiers are account data and never reach this public repo
([adr-revolut-onboarding.md](adr-revolut-onboarding.md), decision 1's first caveat).

### Revolut: `EXCHANGE` legs pair on `entry_reference`

The deal key is `revolut:<entry_reference>`, the third row in `_conversion_key`'s table
([adr-revolut-exchange-pairing.md](adr-revolut-exchange-pairing.md) decision 1, shipped in
[#53](https://github.com/skolima/gnucash-ofx/issues/53)). What is measured about the bank, dated:

- **`entry_reference` is not uid-shaped (2026-08-19).** Month 2026-08 was fetched under both uid
  generations (old uids 2026-08-14, current uids 2026-08-18); comparing the same cached month
  under both, the per-pocket `entry_reference` sets were **byte-equal on 5 of 5 pockets, zero
  asymmetric values**. One re-link cycle on one ASPSP — the same caveat and repeat-at-each-re-link
  tripwire as the hash-stability observation above.
- **Post-fix verification over the real cache (2026-08-19, owner's machine, shipped pipeline, no
  network):** every `EXCHANGE` leg was annotated, forming the expected two-leg deals; each derived
  rate reproduces the two booked amounts exactly; `FITID`s byte-unchanged against pre-fix output.
  Both legs of a deal deliberately share one `FITID` across their two files — per-account
  uniqueness is the scope GnuCash de-duplicates on (see
  [decisions.md](decisions.md#gnucashs-fitid-de-duplication-is-per-account-and-across-import-runs-not-within-one)),
  and the pockets resolve to distinct `ACCTID`s.
- **Settled-empty months live only under dead uids (2026-08-19, structural, cache census).** The
  post-relink re-fetch opened its window at 2026-08-01 and did not re-buy 2026-05..07, so those
  months' claimed-but-empty chunks exist only under the pre-relink uids and the current session's
  cache coverage starts 2026-08-01. A later backfill of that span is a re-buy, inside the ~90-day
  horizon or not at all.

## Currency

- **`POST /sessions` can report ISO 4217's reserved `"XXX"` ("no currency") as an account's
  `currency`, for a real account with real transactions.** Measured 2026-08-10: a re-link of
  `alior` and `alior_kantor` returned `"XXX"` for every account in both connections (5 + 3
  accounts). It is a non-empty string, so code that only checked "is a currency stored" treated it
  as known and skipped the `/balances` discovery fallback — see
  [`decisions.md`](decisions.md#xxx-is-normalized-to-no-currency-at-the-boundary-never-trusted-downstream)
  and [`adr-xxx-currency-placeholder.md`](adr-xxx-currency-placeholder.md) for the fix. `/balances`
  itself was not observed to return `"XXX"` for these accounts — the cache held real ISO currencies
  from the session generation immediately before the re-link — so the placeholder is a `POST
  /sessions` (and therefore link-time, persisted-to-state) phenomenon here, not (yet) observed from
  the balances endpoint. Among the seven banks linked locally (N26's 2026-08-13 link included —
  it reported a real ISO code), only the two Alior connections showed it.

## Consent

- `maximum_consent_validity` is **180 days** (15552000s) for every bank used here, so `link`
  requests the advertised maximum rather than a fixed 90 days.
- A small margin is subtracted from the request: asking for the exact maximum can trip the
  ASPSP's own bound if the local clock runs slightly ahead of theirs.

## BIC

Enable Banking's position (their FAQ) is that **BICs are not reliable ASPSP identifiers** —
"some ASPSPs use multiple BICs", and the values they publish are "informational only". The only
per-account BIC is `account_servicer.bic_fi`, and it is optional.

The catalog **now publishes a `bic`** — it did not when this file was written — on 18 of the 33 PL
entries. It changes nothing, and the deployment here is a neat illustration of why. Alior Bank,
Bank Millennium and Erste Bank Polska have **no** `bic` in the catalog, so it could not fill in
`BANKID` for them anyway. Wise does: `TRWIBEBB`, Wise Europe's — which is the **wrong** value for
the GB and PL balances of the same connection, whose accounts are serviced by different
institutions per currency. Auto-filling `BANKID` from it would put one institution's BIC on
accounts belonging to three, which is precisely the drift the explicit `bankid` option exists to
prevent.

This is why the OFX `BANKID` is configured explicitly per bank (`bankid` in `config.toml`)
rather than auto-filled from `bic_fi` — see [decisions.md](decisions.md#bankid-identity).
