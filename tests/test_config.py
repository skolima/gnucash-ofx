"""TDD for config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from gnucash_ofx.config import ConfigError, load_config

SAMPLE = """
[output]
dir = "./out"

[state]
dir = "./st"

[banks.alior]
source = "enablebanking"
aspsp = "Alior Bank"
country = "PL"

[banks.wise_personal]
source = "wise"
token_env = "WISE_TOKEN_PERSONAL"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_reads_dirs_and_banks(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, SAMPLE), load_env_file=False)

    assert cfg.output_dir == Path("./out")
    assert cfg.state_dir == Path("./st")
    assert set(cfg.banks) == {"alior", "wise_personal"}

    alior = cfg.banks["alior"]
    assert alior.source == "enablebanking"
    assert alior.options == {"aspsp": "Alior Bank", "country": "PL"}

    wise = cfg.banks["wise_personal"]
    assert wise.source == "wise"
    assert wise.options == {"token_env": "WISE_TOKEN_PERSONAL"}


def test_load_config_defaults_dirs(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[banks.x]\nsource = 'wise'\n"), load_env_file=False)
    assert cfg.output_dir == Path("./output")
    assert cfg.state_dir == Path("./state")


def test_load_config_rejects_bank_without_source(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="source"):
        load_config(_write(tmp_path, "[banks.x]\naspsp = 'Alior Bank'\n"), load_env_file=False)


def test_load_config_rejects_unsafe_bank_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid bank key"):
        load_config(_write(tmp_path, "[banks.'../x']\nsource = 'wise'\n"), load_env_file=False)


def test_load_config_rejects_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(_write(tmp_path, "[output\ndir = './out'\n"), load_env_file=False)


def test_load_config_loads_env_next_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gnucash_ofx.config import get_secret

    monkeypatch.delenv("GNUCASH_OFX_TEST_SECRET", raising=False)
    (tmp_path / ".env").write_text("GNUCASH_OFX_TEST_SECRET=from-env\n", encoding="utf-8")
    load_config(_write(tmp_path, "[banks.x]\nsource = 'wise'\n"), load_env_file=True)
    assert get_secret("GNUCASH_OFX_TEST_SECRET") == "from-env"
