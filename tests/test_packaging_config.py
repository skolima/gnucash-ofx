"""`[fetch]` in config.toml, and how CLI flags resolve against it.

docs/adr-ofx-batch-splitting.md decision 9. Two flags that are *preferences* rather than per-run
choices, so they belong in the config file — with the consequence that turning one off for a
single run needs a real off-switch, and that a typo in the config has to be an error rather than
a setting that quietly does nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gnucash_ofx.cli import _packaging_line, _resolve_packaging
from gnucash_ofx.config import AppConfig, ConfigError, load_config

_BANK = """
[banks.alior]
source = "enablebanking"
aspsp = "Alior Bank"
country = "PL"
"""


def _config(tmp_path: Path, fetch_section: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(_BANK + fetch_section, encoding="utf-8")
    return load_config(path, load_env_file=False)


# --------------------------------------------------------------------------- parsing [fetch]


def test_absent_section_keeps_todays_behaviour(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert (config.combine, config.batch_size) == (False, None)


def test_section_is_read(tmp_path: Path) -> None:
    config = _config(tmp_path, "\n[fetch]\ncombine = true\nbatch_size = 120\n")
    assert (config.combine, config.batch_size) == (True, 120)


def test_unknown_option_is_reported_not_ignored(tmp_path: Path) -> None:
    """A typo'd key must fail at startup, not silently do nothing for weeks."""
    with pytest.raises(ConfigError, match="unknown option"):
        _config(tmp_path, "\n[fetch]\nbatchsize = 120\n")


def test_refresh_is_not_a_config_option(tmp_path: Path) -> None:
    """`--refresh` spends rate-limit allowance and must stay a deliberate per-run act."""
    with pytest.raises(ConfigError, match="unknown option"):
        _config(tmp_path, "\n[fetch]\nrefresh = true\n")


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ("\n[fetch]\nbatch_size = 0\n", "1 or more"),
        ("\n[fetch]\nbatch_size = -5\n", "1 or more"),
        ('\n[fetch]\nbatch_size = "120"\n', "whole number"),
        # bool is an int subclass, so `batch_size = true` would otherwise batch at 1.
        ("\n[fetch]\nbatch_size = true\n", "whole number"),
        ('\n[fetch]\ncombine = "yes"\n', "true or false"),
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, section: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        _config(tmp_path, section)


# --------------------------------------------------------------------------- precedence


def _resolve(config: AppConfig, **kwargs: object) -> tuple[bool, int | None]:
    args: dict[str, object] = {"combine": None, "batch_size": None, "no_batch_size": False}
    args.update(kwargs)
    return _resolve_packaging(config, **args)  # type: ignore[arg-type]


CONFIGURED = AppConfig(
    output_dir=Path("out"),
    state_dir=Path("state"),
    banks={},
    combine=True,
    batch_size=120,
)
BARE = AppConfig(output_dir=Path("out"), state_dir=Path("state"), banks={})


def test_config_supplies_the_default_when_no_flag_is_given() -> None:
    assert _resolve(CONFIGURED) == (True, 120)


def test_built_in_default_is_unchanged_by_this_feature() -> None:
    assert _resolve(BARE) == (False, None)


def test_cli_overrides_config() -> None:
    assert _resolve(BARE, combine=True, batch_size=25) == (True, 25)
    assert _resolve(CONFIGURED, batch_size=25) == (True, 25)


def test_off_switches_beat_a_configured_setting() -> None:
    """The reason `--combine` is BooleanOptionalAction and `--no-batch-size` exists at all."""
    assert _resolve(CONFIGURED, combine=False) == (False, 120)
    assert _resolve(CONFIGURED, no_batch_size=True) == (True, None)
    assert _resolve(CONFIGURED, combine=False, no_batch_size=True) == (False, None)


def test_contradictory_batch_flags_are_an_error() -> None:
    from gnucash_ofx.run import RunError

    with pytest.raises(RunError, match="cannot both be given"):
        _resolve(CONFIGURED, batch_size=25, no_batch_size=True)


@pytest.mark.parametrize("size", [0, -1])
def test_non_positive_batch_size_is_an_error_not_a_magic_off_switch(size: int) -> None:
    """`--batch-size 0` is invalid input, not a documented way to disable batching."""
    from gnucash_ofx.run import RunError

    with pytest.raises(RunError, match="1 or more"):
        _resolve(BARE, batch_size=size)


# --------------------------------------------------------------------------- the stderr line


def test_a_default_run_says_nothing_about_packaging() -> None:
    """Config-set packaging is invisible in the typed command, but a default run is not news."""
    assert _packaging_line(False, None) is None


def test_non_default_packaging_is_announced() -> None:
    assert "one file per account" in str(_packaging_line(False, 120))
    assert "120" in str(_packaging_line(False, 120))
    assert "every account's statement" in str(_packaging_line(True, None))
    combined_and_batched = str(_packaging_line(True, 120))
    assert "every account's statement" in combined_and_batched
    assert "120" in combined_and_batched
