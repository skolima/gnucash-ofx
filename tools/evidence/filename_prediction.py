"""What ``ofx_filename``/``_disambiguators`` predict for the sessions currently linked.

Calls the same functions ``run.py``'s own ``--dry-run`` composes (``docs/adr-evidence-tool.md``
decision 7), not the public ``dry_run_enablebanking`` wrapper: that wrapper requires Enable Banking
credentials for a check that never contacts the network, and its ``PlannedFile.path`` bakes the
account's disambiguator into a string this tool must never emit - see the ``local-evidence`` agent's
rule against emitting filenames from ``output/``, which carry an account prefix the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from gnucash_ofx.config import BankConfig
from gnucash_ofx.models import Account
from gnucash_ofx.ofxout import ofx_filename
from gnucash_ofx.run import _disambiguators, _stored_acctids
from gnucash_ofx.state import StateError, load_session

# Any valid one-day window works: only whether a name *can* be formed is reported, never the name.
_PROBE_PERIOD = (date(2000, 1, 1), date(2000, 1, 1))


@dataclass(frozen=True, slots=True)
class FilenamePrediction:
    bank: str
    predictable_count: int
    unpredictable_count: int  # unknown currency at link time, or an unformattable name
    disambiguator_kind: dict[str, int] = field(default_factory=dict)


def filename_prediction(
    state_dir: Path, bank: str, bank_config: BankConfig
) -> FilenamePrediction | None:
    """Predict what a real fetch would name, from ``state/<bank>.json`` alone.

    Mirrors :func:`gnucash_ofx.run._planned_files`: one account of unknown currency (a session
    linked under the pre-``accounts`` state schema) suppresses the whole bank's prediction, because
    it may share a currency group with an account that could otherwise be named, and joining that
    group can change every member's disambiguator kind.
    """
    try:
        session = load_session(state_dir, bank)
    except StateError:
        return None
    if session is None:
        return None

    total = len(session.accounts)
    if any(not stored.currency for stored in session.accounts):
        return FilenamePrediction(bank=bank, predictable_count=0, unpredictable_count=total)

    # Resolved connection-wide (docs/adr-revolut-onboarding.md decision 1): a shared-IBAN
    # connection's disambiguation groups are built from the hash-derived ACCTIDs the fetch will
    # actually use, or this prediction would drift from the real filenames the moment such a
    # connection gained same-currency siblings.
    accounts = [
        Account(
            bank_key=bank,
            account_id=acct_id,
            currency=stored.currency or "",
            bank_id=bank_config.options.get("bankid"),
        )
        for stored, acct_id in zip(session.accounts, _stored_acctids(session.accounts), strict=True)
    ]
    disambiguators = _disambiguators(accounts, siblings=[])

    kind_counts: dict[str, int] = {}
    predictable = 0
    period_start, period_end = _PROBE_PERIOD
    for account in accounts:
        disambiguator = disambiguators.get(account.account_id)
        try:
            ofx_filename(account, period_start, period_end, disambiguator=disambiguator)
        except ValueError:
            continue
        predictable += 1
        if disambiguator is not None:
            kind = "digest" if disambiguator.startswith("eb-") else "iban_tail"
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
    return FilenamePrediction(
        bank=bank,
        predictable_count=predictable,
        unpredictable_count=total - predictable,
        disambiguator_kind=kind_counts,
    )
