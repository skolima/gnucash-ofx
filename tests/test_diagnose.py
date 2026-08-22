"""TDD for the error-explanation lookup (pure, no network)."""

from __future__ import annotations

from gnucash_ofx.diagnose import diagnose


def test_rate_limit_by_code() -> None:
    finding = diagnose(429, "ASPSP_RATE_LIMIT_EXCEEDED", "transactions")
    assert finding is not None
    assert "per bank" in finding.explanation
    # Points at the run log rather than at EB_PSU_IP: the address is detected now, and a run that
    # could not determine one refuses instead of quietly taking the ~4/day background allowance.
    assert finding.action is not None and "psu_mode" in finding.action


def test_rate_limit_by_status_alone() -> None:
    """A 429 with no parseable body still means the same thing."""
    assert diagnose(429, None) is not None


def test_window_refused() -> None:
    finding = diagnose(400, "ASPSP_ERROR", "transactions")
    assert finding is not None
    assert "90 days" in finding.explanation
    assert finding.action is not None and "--from" in finding.action


def test_window_refusal_does_not_claim_to_explain_balances() -> None:
    """The date-range reading only makes sense for the call that carries a date range."""
    assert diagnose(400, "ASPSP_ERROR", "balances") is None


def test_psu_headers() -> None:
    finding = diagnose(400, "PSU_HEADER_NOT_PROVIDED", "transactions")
    assert finding is not None
    assert finding.action is not None and "EB_PSU_IP" in finding.action


def test_unknown_codes_get_no_invented_explanation() -> None:
    assert diagnose(500, "SOMETHING_NEW", "transactions") is None
    assert diagnose(403, None, "transactions") is None
    assert diagnose(None, None, None) is None


def test_explanations_never_name_institutions() -> None:
    """Bank keys are user-chosen and the ASPSP set differs per deployment."""
    named = ("alior", "erste", "millennium", "wise", "santander")
    for status, code in ((429, None), (400, "ASPSP_ERROR"), (400, "PSU_HEADER_NOT_PROVIDED")):
        finding = diagnose(status, code, "transactions")
        assert finding is not None
        text = f"{finding.explanation} {finding.action or ''}".lower()
        assert not any(name in text for name in named)


def test_explanations_are_ascii() -> None:
    """Console output must survive a cp1252 terminal.

    These strings are printed on the failure path, which is the worst possible place to raise
    UnicodeEncodeError. The project already folds OFX text to ASCII for a related reason.
    """
    for status, code in ((429, None), (400, "ASPSP_ERROR"), (400, "PSU_HEADER_NOT_PROVIDED")):
        finding = diagnose(status, code, "transactions")
        assert finding is not None
        f"{finding.explanation} {finding.action}".encode("ascii")
