"""The manifest of `AGENTS.md` invariants that have a machine check, and the two that live here.

Most of the invariants named by issue #18 already had a test — see `docs/adr-invariant-tests.md`
for the measurement. This file does not relocate or duplicate those nine; it cites them in the
`MANIFEST` below, checks the citations still resolve to something real, and holds the two
candidates that had no test anywhere: `dry_run_enablebanking`'s signature, and the rule that a
global precondition belongs in every entry point, not just the ones its author remembered.

Deliberately left unpinned, for this batch: nothing. Every candidate issue #18 named, plus the
precondition rule, is assertable without reimplementing the function it pins — see
`docs/adr-invariant-tests.md` decision 6. This line exists so that fact is stated, not left for a
reader to infer from an empty section.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gnucash_ofx import cli, run
from gnucash_ofx.config import AppConfig, BankConfig
from gnucash_ofx.run import dry_run_enablebanking, fetch_enablebanking
from gnucash_ofx.sources.enablebanking import EnableBankingClient
from gnucash_ofx.state import LinkedAccount, SessionState, save_session

# --------------------------------------------------------------------------- the manifest


@dataclass(frozen=True)
class Citation:
    # issue #18's numbering ("1".."10"), or a letter for an invariant written down after it: "b"
    # the precondition rule, "c" the census scheme-name gate, "d" evidence's use of
    # SessionState.raw, "e" the fetch log's rewrite pass and record contract (#37 decision 1),
    # "f" the ACCTTYPE
    # mapping and its refusal (#22), "g" the bounds on what a server can dictate and the
    # per-account bad-data guard (#37 decisions 2/3/5), "h" the control-character strip and
    # the raw-passthrough field validation (#37 decision 4), "i" the direction-named
    # counterparty side (#33), "j" the transaction-date margin, the booking-month filing,
    # the cache merge and its lag tripwire (#35), "k" the shared-IBAN uniform hash policy and
    # its IBAN-and-warn floor (#32), "l" the batch-splitting rules — never split a day, the
    # no-filename-marker deduction, final-batch-only live balance (#14), "m" the Revolut EXCHANGE
    # deal key: read-only off the FITID field, and no key at all without a reference (#50)
    candidate: str
    bullet: str  # quoted (possibly abridged) from AGENTS.md's Invariants section
    module: str  # the test module the citation points at
    test_name: str


MANIFEST: tuple[Citation, ...] = (
    Citation(
        "1",
        'Write OFX with newline="".',
        "test_ofxout",
        "test_written_file_has_clean_crlf_line_endings",
    ),
    Citation(
        "2",
        "NAME is capped at 96 characters.",
        "test_ofxout",
        "test_compose_name_truncates_at_a_word_boundary",
    ),
    Citation(
        "2",
        "MEMO [is capped] at 390.",
        "test_ofxout",
        "test_compose_memo_caps_at_the_libofx_buffer",
    ),
    Citation(
        "2",
        "CHECKNUM is capped at 12, and an over-long value is dropped, not truncated.",
        "test_ofxout",
        "test_compose_check_number_drops_an_over_long_reference",
    ),
    Citation(
        "3",
        "LEDGERBAL carries the bank's balance only when the window ends today or later.",
        "test_run",
        "test_a_closed_window_spends_no_balance_call",
    ),
    Citation(
        "3",
        "...and the tag is present either way.",
        "test_run",
        "test_a_live_window_still_carries_the_banks_ledger_balance",
    ),
    Citation(
        "4",
        "fetch stdout is the file list; everything else is stderr.",
        "test_cli",
        "test_stdout_is_the_written_paths_and_nothing_else",
    ),
    Citation(
        "5",
        "ASPSP_RATE_LIMIT_EXCEEDED is never retried.",
        "test_enablebanking_client",
        "test_the_daily_cap_is_not_retried",
    ),
    Citation(
        "5",
        "Match on error, never message.",
        "test_enablebanking_client",
        "test_api_error_code_reads_error_not_message",
    ),
    Citation(
        "5",
        "Other 429s keep the ladder.",
        "test_enablebanking_client",
        "test_retries_on_429_then_succeeds",
    ),
    Citation(
        "6",
        "A settled month does not expire.",
        "test_cache",
        "test_a_settled_month_does_not_expire",
    ),
    Citation(
        "6",
        "...only the moving tail expires.",
        "test_cache",
        "test_a_stale_tail_is_refetched_but_a_settled_month_is_not",
    ),
    Citation(
        "8",
        "The disambiguation group counts every account in the session, so a failing account "
        "cannot silently rename its same-currency sibling.",
        "test_run",
        "test_surviving_sibling_keeps_the_filename_it_has_in_a_full_run",
    ),
    Citation(
        "8",
        "One account with no link-time currency suppresses its whole bank's prediction — it may "
        "be a same-currency sibling.",
        "test_run",
        "test_v1_state_cannot_reserve_the_group_slot",
    ),
    Citation(
        "9",
        "RunLog swallows every OSError.",
        "test_runlog",
        "test_a_broken_log_never_breaks_a_fetch",
    ),
    Citation(
        "10",
        "Signed amounts: credits positive, debits negative.",
        "test_enablebanking_mapper",
        "test_debit_is_negative_credit_is_positive",
    ),
    # docs/adr-ofx-batch-splitting.md, added with #14. Decision 4 removes the batch filename
    # marker entirely on the strength of a deduction — days are never split (decision 3), so an
    # account's batch date ranges are disjoint and strictly increasing, so two batches can never
    # compute one filename. Both halves are cited: break the first and the second stops holding,
    # silently, with the second batch overwriting the first.
    Citation(
        "l",
        "A booking date is never split across two batches; a day larger than the cap stays whole.",
        "test_batching",
        "test_only_an_oversized_day_may_exceed_the_cap",
    ),
    Citation(
        "l",
        "Batch date ranges are disjoint and strictly increasing, so ofx_filename needs no batch "
        "marker.",
        "test_batching",
        "test_batch_date_ranges_never_collide",
    ),
    Citation(
        "l",
        "LEDGERBAL: only the final batch of a window reaching today carries the live balance.",
        "test_batching",
        "test_only_the_final_batch_carries_the_live_balance",
    ),
    Citation(
        "7",
        "dry_run_enablebanking takes no client and no cache dir.",
        "test_invariants",
        "pinned here",
    ),
    Citation(
        "b",
        "Any new global precondition belongs in both entry points for the same reason.",
        "test_invariants",
        "pinned here",
    ),
    Citation(
        "c",
        "A census field whose keys come off the wire is gated to a vocabulary and bucketed, "
        "never counted as received.",
        "test_tools_state_census",
        "test_state_census_never_emits_a_scheme_name_shaped_like_an_identifier",
    ),
    Citation(
        "c",
        "The gate excludes digits, not length. Do not relax it to an allow-list of the names "
        "seen so far either.",
        "test_tools_state_census",
        "test_state_census_admits_an_unfamiliar_all_letters_scheme_name",
    ),
    Citation(
        "d",
        "tools/evidence/ may traverse SessionState.raw, but only for structural facts.",
        "test_tools_state_census",
        "test_state_census_reports_all_account_ids_lengths_from_the_raw_body",
    ),
    Citation(
        "e",
        "redact_uid_path must stay idempotent - scrub_log re-runs it over the whole log on "
        "every fetch, and a mask that did not fix-point would eat four characters per run.",
        "test_runlog",
        "test_redaction_is_idempotent",
    ),
    Citation(
        "e",
        "A log line is split on newline, never str.splitlines(): ASPSP-controlled text can "
        "carry U+2028, and the scrub would write the tear back permanently.",
        "test_runlog",
        "test_scrub_keeps_a_record_whose_text_holds_a_unicode_line_separator",
    ),
    Citation(
        "e",
        "Read the fetch log with read_bytes().decode(), never read_text(): universal-newline "
        "mode normalises a bare CR to a newline before the split can see it.",
        "test_runlog",
        "test_scrub_keeps_a_line_holding_a_bare_carriage_return",
    ),
    Citation(
        "e",
        "The scrub's rewrite carries the terminators it read, so it writes with newline='' for "
        "the same reason OfxWriter does.",
        "test_runlog",
        "test_scrub_preserves_crlf_terminators_byte_for_byte",
    ),
    Citation(
        "e",
        "A field added to a request record is optional forever - old lines are paid-for "
        "evidence, so readers take it with .get and never treat absence as malformed.",
        "test_runlog",
        "test_a_record_written_before_error_name_existed_reads_back_as_none",
    ),
    Citation(
        "f",
        "ACCTTYPE comes from the stored cash_account_type through accttype_for() and nowhere "
        "else; absence and OTHR are the explicit CHECKING fallback.",
        "test_ofxout",
        "test_accttype_for_maps_the_carryable_vocabulary",
    ),
    Citation(
        "f",
        "CARD/CASH/LOAN and any undocumented value refuse the account, before its first request "
        "is spent - siblings still ship and still keep their disambiguated names.",
        "test_run",
        "test_fetch_bank_refuses_an_uncarryable_account_type_before_spending_a_request",
    ),
    Citation(
        "f",
        "The refusal applies in --dry-run through the same function, so the prediction cannot "
        "disagree with the run.",
        "test_run",
        "test_dry_run_predicts_the_refusal_and_keeps_the_siblings_names",
    ),
    Citation(
        "g",
        "not get to pick how long the run sleeps.",
        "test_enablebanking_client",
        "test_a_huge_retry_after_is_clamped_to_the_backoff_ceiling",
    ),
    Citation(
        "g",
        "A non-finite Retry-After is refused: inf passed a bare >= 0 check and raised "
        "OverflowError inside time.sleep.",
        "test_enablebanking_client",
        "test_a_non_finite_retry_after_falls_back_to_backoff",
    ),
    Citation(
        "g",
        "A local bound exceeded raises rather than truncating - a short answer that looked "
        "complete would advance the coverage ledger over transactions nobody wrote.",
        "test_enablebanking_client",
        "test_pagination_stops_at_the_page_cap_instead_of_following_forever",
    ),
    Citation(
        "g",
        "A rejected amount is described by shape, never quoted: InvalidOperation fires on "
        "non-canonical real money and the message reaches the summary users paste.",
        "test_enablebanking_client",
        "test_a_rejected_amount_is_never_quoted_in_the_error",
    ),
    Citation(
        "h",
        "to_ascii drops C0 controls and DEL - one in a memo aborted the writer's minidom step "
        "with an uncaught ExpatError, which is the whole run and not the account.",
        "test_ofxout",
        "test_to_ascii_strips_control_characters",
    ),
    Citation(
        "h",
        "The raw-passthrough fields are validated as printable ASCII, never against a tight "
        "alphanumeric whitelist: entry_reference legitimately carries - . _ / | .",
        "test_ofxout",
        "test_validate_raw_fields_accepts_every_real_fitid_alphabet",
    ),
    Citation(
        "h",
        "A raw-passthrough field is validated and never repaired: on Windows libofx returns a "
        "non-ASCII FITID mutated rather than refused, which re-imports as a new transaction.",
        "test_ofxout",
        "test_validate_raw_fields_rejects_what_the_file_cannot_carry",
    ),
    Citation(
        "i",
        "The counterparty is read from the side the direction names (creditor on DBIT, debtor "
        'on CRDT), never "whichever side is populated".',
        "test_enablebanking_mapper",
        "test_n26_own_account_never_becomes_the_counterparty",
    ),
    Citation(
        "j",
        "save_cached_month merges: [...] never lossy outside [the incoming claim].",
        "test_cache",
        "test_a_narrow_save_no_longer_discards_what_a_wide_one_stored",
    ),
    Citation(
        "j",
        "save_cached_month merges: authoritative inside the incoming claim [- a bank-side "
        "withdrawal or amendment still lands].",
        "test_cache",
        "test_the_incoming_set_is_authoritative_inside_its_claim",
    ),
    Citation(
        "j",
        "The merge is keyed on _transaction_id - the identity FITID rests on - and the incoming "
        "copy wins on any key collision.",
        "test_cache",
        "test_the_incoming_copy_wins_on_a_key_collision_outside_its_claim",
    ),
    Citation(
        "j",
        "Disjoint claims keep the wider one and never claim the gap between them.",
        "test_cache",
        "test_disjoint_claims_keep_the_wider_and_never_claim_the_gap",
    ),
    Citation(
        "j",
        "An empty claim (null/null) holds data and never satisfies covers().",
        "test_cache",
        "test_an_empty_claim_holds_data_but_never_covers",
    ),
    Citation(
        "j",
        "Claims keep deriving from the un-widened span - claiming the margin days would repeat "
        "#35 one level down.",
        "test_run",
        "test_request_spans_claims_never_widen_past_the_requested_window",
    ),
    Citation(
        "j",
        "A merge that retains days only the old fetch answered keeps the old fetched_at.",
        "test_cache",
        "test_a_save_retaining_old_claim_days_keeps_the_old_timestamp",
    ),
    Citation(
        "j",
        "Every returned entry is filed into its booking month, including months outside the "
        "requested span.",
        "test_run",
        "test_a_returned_entry_booked_outside_the_span_is_cached_not_discarded",
    ),
    Citation(
        "j",
        "The booking-lag FetchWarning is the margin's named revisit trigger - kind coverage, "
        "never a failure.",
        "test_run",
        "test_a_booking_lag_past_the_margin_raises_a_warning",
    ),
    Citation(
        "j",
        "The statement's FITID dedup takes the later copy's content [...].",
        "test_run",
        "test_a_freshly_amended_copy_beats_the_cached_one_in_the_statement",
    ),
    Citation(
        "j",
        "The statement's FITID dedup takes [...] the first copy's position.",
        "test_run",
        "test_the_deduplicated_copy_keeps_the_first_copys_position",
    ),
    Citation(
        "j",
        "Every wire request opens TRANSACTION_DATE_MARGIN earlier than the span it answers for "
        "[and the margin counts against the 90-day request cap].",
        "test_run",
        "test_request_spans_pack_whole_months_up_to_the_bank_limit",
    ),
    Citation(
        "j",
        "The widened wire is floored at the 89-day clamp [- a cold fetch at the clamp cannot "
        "widen, and does not split for a margin it cannot open].",
        "test_run",
        "test_request_spans_floor_absorbs_the_margin_at_the_lookback_clamp",
    ),
    Citation(
        "j",
        "The widened wire is floored at the 89-day clamp for every window, explicit --from "
        "included.",
        "test_run",
        "test_the_floor_partially_absorbs_the_margin_near_the_horizon",
    ),
    Citation(
        "j",
        "[The wire is] capped at claim_from, so a floor above the whole window cannot invert "
        "the request.",
        "test_run",
        "test_the_wire_never_rises_above_the_claim_when_the_floor_sits_above_the_window",
    ),
    Citation(
        "j",
        "Under _BAD_DATA_ERRORS the merge degrades to the incoming set for that one chunk - the "
        "account's problem, never the run's.",
        "test_cache",
        "test_a_poisoned_chunk_degrades_the_merge_instead_of_crashing_the_run",
    ),
    Citation(
        "k",
        "A connection in which any IBAN is shared resolves every account's ACCTID through "
        "identification_hash - the unique-IBAN sibling included.",
        "test_run",
        "test_shared_iban_connection_resolves_every_pocket_through_the_hash",
    ),
    Citation(
        "k",
        "An account with no stored hash keeps its IBAN; it never falls to the re-link-volatile "
        "uid.",
        "test_run",
        "test_shared_iban_floor_keeps_the_iban_never_the_uid",
    ),
    Citation(
        "k",
        "An account with no stored hash keeps its IBAN and warns (_warn_shared_iban, "
        "symptom-keyed like the stale-identity warning).",
        "test_run",
        "test_fetch_bank_warns_when_the_shared_iban_floor_is_taken",
    ),
    Citation(
        "k",
        "Only the stored hash counts in this branch - a fetch-time rescue would make fetch and "
        "status diverge about the same account.",
        "test_run",
        "test_shared_iban_floor_ignores_the_fetch_time_hash",
    ),
    Citation(
        "k",
        "False for all 19 pre-existing accounts, so no configured bank's ACCTID moves.",
        "test_run",
        "test_unshared_connection_resolution_is_unchanged",
    ),
    # docs/adr-revolut-exchange-pairing.md, added with #53. The reference-less half is the part a
    # tidy-up breaks silently: `raw.get("entry_reference") or ""` reads naturally and hands
    # pair_conversions one false cluster holding every reference-less EXCHANGE row.
    Citation(
        "m",
        "A Revolut EXCHANGE leg's deal key is revolut:<entry_reference> - the same field FITID "
        "falls through to - so extraction is read-only.",
        "test_enablebanking_mapper",
        "test_revolut_exchange_legs_share_a_namespaced_key",
    ),
    Citation(
        "m",
        "A reference-less EXCHANGE row gets no key: an empty key would falsely cluster every "
        "such row.",
        "test_enablebanking_mapper",
        "test_an_exchange_row_without_an_entry_reference_gets_no_key",
    ),
    Citation(
        "m",
        "_validate's one-booking-date check stays strict on purpose - a cross-date deal, if "
        "Revolut ever books one, is refused into unannotated text, never mis-paired.",
        "test_conversions",
        "test_different_dates_are_left_untouched",
    ),
)


@pytest.mark.parametrize(
    "citation",
    [c for c in MANIFEST if c.test_name != "pinned here"],
    ids=lambda c: f"{c.candidate}:{c.module}.{c.test_name}",
)
def test_manifest_citation_still_exists(citation: Citation) -> None:
    """Proves the citation still points at something real — not that the invariant still holds.

    Without this, a rename or deletion of a cited test elsewhere would silently orphan its
    manifest row, and the manifest would keep claiming a pin that no longer resolves. It does not
    check the cited test's docstring against `AGENTS.md`'s current wording — see
    `docs/adr-invariant-tests.md` decision 3 for why that gap is an accepted cost.
    """
    module = importlib.import_module(citation.module)
    assert callable(getattr(module, citation.test_name))


# --------------------------------------------------------------------------- candidate 7


def test_dry_run_takes_no_client_and_no_cache_dir() -> None:
    """AGENTS.md: "dry_run_enablebanking takes no client and no cache dir" — not a flag threaded
    through fetch_enablebanking, a fact about the function's own parameter list.
    """
    params = inspect.signature(dry_run_enablebanking).parameters
    assert "client" not in params
    assert "cache_dir" not in params


# --------------------------------------------------------------------------- rule (b)

# One tuple to extend when a second global precondition is added, so there is exactly one place
# to remember rather than three call sites. See docs/adr-invariant-tests.md decision 5 and its
# open question 1 on whether this needs to escalate to a registry in run.py/cli.py someday.
# name -> the `cli.py` callable that must reach it. Both orchestration entry points call every
# precondition as one of their first statements, so only the CLI's own call site varies: a window
# is parsed before the public-IP lookup, a batch size is settled when the flags are resolved
# against config.toml. Add a row when a precondition is added, not a whole test.
PRECONDITIONS: dict[str, Callable[[], object]] = {
    "require_ordered_window": lambda: cli._parse_window(
        argparse.Namespace(date_from="2026-06-01", date_to="2026-06-30")
    ),
    "require_valid_batch_size": lambda: cli._resolve_packaging(
        AppConfig(output_dir=Path("out"), state_dir=Path("state"), banks={}),
        combine=None,
        batch_size=10,
        no_batch_size=False,
    ),
}


@pytest.fixture(scope="module")
def private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.mark.parametrize("name", sorted(PRECONDITIONS))
def test_precondition_reached_from_all_three_entry_points(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_key_pem: bytes
) -> None:
    """AGENTS.md: "Any new global precondition belongs in both entry points for the same reason"
    — and the CLI is the third. Reached, not merely present: each entry point is invoked for
    real, with a spy substituted for the named precondition, on a window valid enough that
    nothing else refuses it first.

    Patching both `run.<name>` and `cli.<name>` is load-bearing, not defensive style: `cli.py`
    imports the function by name (`from gnucash_ofx.run import ..., require_ordered_window, ...`),
    which binds a *separate* module-level name in `cli.py`, resolved to the function object at
    import time. A spy on `run.<name>` alone would never observe a call made through `cli.py`'s
    own bound name — exactly the CLI-only omission this test exists to catch. See
    `docs/adr-invariant-tests.md`'s Measured section and decision 5.

    This does not replace the existing reversed-window tests in test_run.py/test_cli.py — those
    prove the precondition rejects a bad window correctly; this proves it is reached at all from
    every entry point, which is the property that generalizes to a second precondition nobody has
    written a per-site test for yet.
    """
    calls = {"run": 0, "cli": 0}
    monkeypatch.setattr(run, name, lambda *a, **k: calls.__setitem__("run", calls["run"] + 1))
    monkeypatch.setattr(cli, name, lambda *a, **k: calls.__setitem__("cli", calls["cli"] + 1))

    config = AppConfig(
        output_dir=tmp_path / "out",
        state_dir=tmp_path,
        banks={
            "alior": BankConfig("alior", "enablebanking", {"aspsp": "Alior Bank", "country": "PL"})
        },
        cache_dir=tmp_path / "cache",
    )
    client = EnableBankingClient(
        application_id="app",
        private_key=private_key_pem,
        http=httpx.Client(
            base_url="https://api.enablebanking.com",
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        ),
    )

    # fetch_enablebanking: the precondition is its first statement, ahead of everything that
    # would otherwise need a working bank behind `client`. What happens after is irrelevant here.
    with contextlib.suppress(Exception):
        fetch_enablebanking(config, client, date_from=date(2026, 6, 1), date_to=date(2026, 6, 30))
    assert calls["run"] == 1

    # dry_run_enablebanking: same ordering, no client to begin with.
    save_session(
        tmp_path,
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=date(2099, 1, 1),
            accounts=(
                LinkedAccount(uid="acc-a", iban="PL00000000000000000000001", currency="PLN"),
            ),
        ),
    )
    with contextlib.suppress(Exception):
        dry_run_enablebanking(config, date_from=date(2026, 6, 1), date_to=date(2026, 6, 30))
    assert calls["run"] == 2

    # The CLI reaches each precondition from its own call site — see PRECONDITIONS — and does so
    # ahead of the public-IP lookup and the run log that _cmd_fetch opens.
    with contextlib.suppress(Exception):
        PRECONDITIONS[name]()
    assert calls["cli"] == 1
