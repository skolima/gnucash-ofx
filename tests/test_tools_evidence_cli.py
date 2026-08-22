"""Tests for tools/evidence/cli.py, the human-facing entry point."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from tools.evidence.cli import main

from gnucash_ofx.cache import save_cached_month
from gnucash_ofx.state import LinkedAccount, SessionState, save_session


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    save_session(
        directory,
        SessionState(
            bank="alior",
            session_id="s1",
            valid_until=date(2026, 10, 27),
            accounts=(
                LinkedAccount(uid="u1", iban="PL00000000000000000000005387", currency="PLN"),
            ),
        ),
    )
    return directory


def test_prints_a_report_covering_every_census(
    tmp_path: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_dir = tmp_path / "cache"
    save_cached_month(cache_dir, "u1", date(2026, 7, 1), date(2026, 7, 1), date(2026, 7, 31), [])
    config_path = tmp_path / "config.toml"
    config_path.write_text('[banks.alior]\nsource = "enablebanking"\n', encoding="utf-8")

    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--cache-dir",
            str(cache_dir),
            "--config",
            str(config_path),
            "--today",
            "2026-08-09",
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "state census" in out
    assert "alior" in out
    assert "cache census" in out
    assert "coverage reconciliation" in out
    assert "filename prediction" in out
    # No account number, uid, or path may ever reach stdout.
    assert "PL00000000000000000000005387" not in out
    assert "u1" not in out


def test_runs_without_a_config_toml(
    tmp_path: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--config",
            str(tmp_path / "does-not-exist.toml"),
        ]
    )
    assert exit_code == 0
    assert "no config.toml" in capsys.readouterr().out
