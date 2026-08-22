"""Census of ``cache/``: chunk counts, and which account digests belong to no current session.

Reports *counts*, never "coverage" - see ``docs/adr-coverage-ledger-and-warnings.md`` §1 and
``docs/adr-evidence-tool.md`` decision 4: a month chunk records only the last window that touched
it, so it cannot honestly stand for what has been fetched. :mod:`coverage_reconciliation` is where
coverage-per-account actually lives, read from the ledger instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from gnucash_ofx.cache import _uid_digest

_MONTH_CHUNK = re.compile(r"^m-([0-9a-f]{16})-\d{4}-\d{2}\.json$")
_BALANCE_CHUNK = re.compile(r"^bal-([0-9a-f]{16})\.json$")


@dataclass(frozen=True, slots=True)
class CacheCensus:
    """Chunk counts. Month chunks are the canonical "stranded" denominator - see decision 9."""

    month_chunks_total: int
    month_chunks_stranded: int
    balance_chunks_total: int
    balance_chunks_stranded: int
    distinct_account_digests: int
    stranded_account_digests: int


def _matching_digests(cache_dir: Path, glob: str, pattern: re.Pattern[str]) -> list[str]:
    if not cache_dir.is_dir():
        return []
    digests = []
    for path in cache_dir.glob(glob):
        match = pattern.match(path.name)
        if match:
            digests.append(match.group(1))
    return digests


def cache_census(cache_dir: Path, live_uids: Iterable[str]) -> CacheCensus:
    """Count cache chunks and how many belong to no currently-linked account.

    ``live_uids`` is every account ``uid`` from every currently-linked session, across every bank -
    a chunk whose filename digest matches none of them is stranded, cross-referenced with the same
    :func:`gnucash_ofx.cache._uid_digest` the cache itself uses to name a chunk.
    """
    live_digests = {_uid_digest(uid) for uid in live_uids}
    month_digests = _matching_digests(cache_dir, "m-*.json", _MONTH_CHUNK)
    balance_digests = _matching_digests(cache_dir, "bal-*.json", _BALANCE_CHUNK)
    all_digests = set(month_digests) | set(balance_digests)
    stranded = all_digests - live_digests
    return CacheCensus(
        month_chunks_total=len(month_digests),
        month_chunks_stranded=sum(1 for d in month_digests if d in stranded),
        balance_chunks_total=len(balance_digests),
        balance_chunks_stranded=sum(1 for d in balance_digests if d in stranded),
        distinct_account_digests=len(all_digests),
        stranded_account_digests=len(stranded),
    )
