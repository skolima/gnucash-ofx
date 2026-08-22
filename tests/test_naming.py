"""Tests for the safe path-component validator."""

from __future__ import annotations

import pytest

from gnucash_ofx.naming import safe_component


@pytest.mark.parametrize("value", ["alior", "wise_personal", "PLN", "EUR-1", "a1"])
def test_accepts_safe_values(value: str) -> None:
    assert safe_component(value, "thing") == value


@pytest.mark.parametrize("value", ["../etc", "a/b", "a\\b", "..", "", "with space", "a.b"])
def test_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid thing"):
        safe_component(value, "thing")
