"""Human-facing entry point: print every census as a plain-text report.

Not imported by the ``local-evidence`` agent or any other automation - the four census functions in
this package are the thing meant to be called directly. This exists for a person running
``uv run python -m tools.evidence.cli`` locally. Read-only; never touches the network; the same
sanitization guarantee as every census in this package applies to what it prints.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from tools.evidence.cache_census import cache_census
from tools.evidence.coverage_reconciliation import coverage_reconciliation
from tools.evidence.filename_prediction import filename_prediction
from tools.evidence.state_census import discover_banks, state_census

from gnucash_ofx.config import DEFAULT_CACHE_DIR, DEFAULT_STATE_DIR, ConfigError, load_config
from gnucash_ofx.state import StateError, load_session


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evidence",
        description="Print structural, sanitized censuses of state/, cache/ and the run log. "
        "Read-only; never touches the network.",
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--today", help="Override 'today' for expiry math (YYYY-MM-DD); default is the real date."
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else None
    banks = discover_banks(args.state_dir)

    print(f"state census: {len(banks)} bank(s) with a session file")
    live_uids: list[str] = []
    for bank in banks:
        census = state_census(args.state_dir, bank, today=today)
        if census is None:
            print(f"  {bank}: unreadable or unlinked")
            continue
        print(
            f"  {bank}: schema_version={census.schema_version} accounts={census.account_count} "
            f"with_iban={census.with_iban} "
            f"with_identification_hash={census.with_identification_hash} "
            f"distinct_identification_hashes={census.distinct_identification_hashes} "
            f"with_currency={census.with_currency} "
            f"would_resolve_to_bare_uid={census.would_resolve_to_bare_uid} "
            f"days_until_expiry={census.days_until_expiry}"
        )
        # Identification shape on its own line: the counts above answer "can this be identified",
        # these answer "identified by what", which is the question a new bank actually poses.
        print(
            f"    shape: with_name={census.with_name} with_product={census.with_product} "
            f"with_usage={census.with_usage} "
            f"scheme_kinds={census.identification_scheme_kinds} "
            f"raw_account_entries={census.with_raw_account_entry} "
            f"all_account_ids_lengths={census.all_account_ids_lengths}"
        )
        try:
            session = load_session(args.state_dir, bank)
        except StateError:
            session = None
        if session is not None:
            live_uids.extend(a.uid for a in session.accounts)

    cache = cache_census(args.cache_dir, live_uids)
    print(
        f"\ncache census: month_chunks={cache.month_chunks_total} "
        f"({cache.month_chunks_stranded} stranded), "
        f"balance_chunks={cache.balance_chunks_total} ({cache.balance_chunks_stranded} stranded), "
        f"account_digests={cache.distinct_account_digests} "
        f"({cache.stranded_account_digests} stranded)"
    )

    print("\ncoverage reconciliation:")
    for bank in banks:
        reconciliation = coverage_reconciliation(args.state_dir, bank)
        if reconciliation is None:
            continue
        print(
            f"  {bank}: live_accounts={reconciliation.live_accounts} "
            f"covered_accounts={reconciliation.covered_accounts} "
            f"distinct_coverage_keys={reconciliation.distinct_coverage_keys} "
            f"accounts_sharing_a_key={reconciliation.accounts_sharing_a_key} "
            f"ledger_unreadable={reconciliation.ledger_unreadable} "
            f"ambiguous_truncated_uids={reconciliation.ambiguous_truncated_uids} "
            f"requests_total={reconciliation.requests_total} "
            f"requests_matched={reconciliation.requests_matched_to_live_account} "
            f"requests_unmatched={reconciliation.requests_unmatched}"
        )

    print("\nfilename prediction:")
    try:
        config = load_config(args.config, load_env_file=False)
    except (ConfigError, OSError):
        print("  no config.toml found; skipping (needs bank options such as 'bankid')")
        return 0
    for bank in banks:
        bank_config = config.banks.get(bank)
        if bank_config is None:
            print(f"  {bank}: no config.toml entry, skipped")
            continue
        prediction = filename_prediction(args.state_dir, bank, bank_config)
        if prediction is None:
            continue
        print(
            f"  {bank}: predictable={prediction.predictable_count} "
            f"unpredictable={prediction.unpredictable_count} "
            f"disambiguator_kind={dict(sorted(prediction.disambiguator_kind.items()))}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
