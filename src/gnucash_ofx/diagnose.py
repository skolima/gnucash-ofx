"""Turn an Enable Banking error response into a human explanation and a next step.

Pure and table-driven, so it is unit-tested without a client. The table is seeded **only** from
behaviour verified in ``docs/enable-banking.md``: an unrecognised code gets no explanation at
all, because a wrong one is worse than none — the caller still prints the bank's verbatim
response, which is the genuinely useful part when debugging.

Explanations never name institutions. Bank keys are user-chosen and the set of ASPSPs differs per
deployment, so "this bank" is the only thing that is true everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

RATE_LIMIT = "ASPSP_RATE_LIMIT_EXCEEDED"
ASPSP_ERROR = "ASPSP_ERROR"
PSU_HEADER_MISSING = "PSU_HEADER_NOT_PROVIDED"


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """What an error usually means, and what the user can do about it."""

    explanation: str
    action: str | None = None


_RATE_LIMITED = Diagnosis(
    "The bank's daily fetch allowance is spent. The cap is per bank, so the other banks in this "
    "run are unaffected, and retrying sooner does not help - only time does.",
    "Wait about 6 hours. Check psu_mode in state/fetch-log.jsonl first: a run that fell back to "
    "background mode gets only ~4 requests a day.",
)

_WINDOW_REFUSED = Diagnosis(
    "The bank refused the requested date range. Either the window is longer than it accepts, or "
    "it reaches further back than it serves - some banks keep only about 90 days of history.",
    "Re-run this bank with a later --from.",
)

_PSU_HEADERS = Diagnosis(
    "A partial set of PSU headers was sent; banks require all of them or none.",
    "Unset EB_PSU_IP in .env and let the address be detected, or check that the value you set is "
    "a valid IPv4/IPv6 address.",
)


def diagnose(
    status_code: int | None, api_code: str | None, operation: str | None = None
) -> Diagnosis | None:
    """Explain a failure, or ``None`` when we have nothing trustworthy to say about it.

    ``api_code`` is Enable Banking's ``message``/``code`` field. Matching prefers it over the HTTP
    status, since the status alone (a bare 400) carries no meaning.
    """
    code = (api_code or "").strip().upper()
    if code == RATE_LIMIT or status_code == 429:
        return _RATE_LIMITED
    if code == PSU_HEADER_MISSING:
        return _PSU_HEADERS
    if code == ASPSP_ERROR and status_code == 400 and operation == "transactions":
        return _WINDOW_REFUSED
    return None
