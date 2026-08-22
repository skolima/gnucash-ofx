"""Load ``config.toml`` (non-secret) plus secrets from the environment/``.env``."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from gnucash_ofx.naming import safe_component

DEFAULT_OUTPUT_DIR = Path("./output")
DEFAULT_STATE_DIR = Path("./state")
DEFAULT_CACHE_DIR = Path("./cache")


class ConfigError(Exception):
    """Raised when ``config.toml`` is malformed."""


@dataclass(frozen=True, slots=True)
class BankConfig:
    """One configured bank: its source and the source-specific options."""

    key: str
    source: str
    options: dict[str, str]

    @property
    def rate_limit_domain(self) -> str:
        """The connections that plausibly draw on one PSD2 rate-limit allowance.

        PSD2 allowances are per PSU per ASPSP, so two connections to the same institution under
        the same kind of login share one — which is why ``alior`` and ``alior_kantor`` exhaust
        each other while ``wise_personal`` and ``wise_business``, two different logins at one
        ASPSP, should not. Deliberately narrow: over-grouping would fail a bank that could have
        succeeded.

        Currently a **label only**, recorded in the run log so the shared-allowance premise can be
        confirmed from evidence rather than asserted. It gains behaviour if
        ``docs/adr-aspsp-rate-limit-domain.md`` is accepted.
        """
        return "|".join(
            (
                self.options.get("aspsp", self.key),
                self.options.get("country", "?"),
                self.options.get("psu_type", "personal"),
            )
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    output_dir: Path
    state_dir: Path
    banks: dict[str, BankConfig]
    cache_dir: Path = DEFAULT_CACHE_DIR
    # `[fetch]` — packaging preferences, not fetching behaviour: neither changes a single API
    # request (docs/adr-ofx-batch-splitting.md decision 9). They live in the config because they
    # are preferences rather than per-run choices — a user who wants combined output wants it
    # every run, and *forgetting* the flag silently changes the output shape, which is worse than
    # the friction of retyping it. A CLI flag still overrides whatever is set here.
    combine: bool = False
    batch_size: int | None = None


def load_env(config_path: Path) -> None:
    """Load a ``.env`` sitting next to ``config.toml`` into the environment.

    Secrets are never stored in the TOML, so commands that need only credentials (e.g.
    ``aspsps``) can call this without requiring a ``config.toml`` to exist.
    """
    load_dotenv(config_path.resolve().parent / ".env")


def load_config(path: Path, *, load_env_file: bool = True) -> AppConfig:
    """Parse ``config.toml`` into an :class:`AppConfig`.

    Secrets are never stored in the TOML; with ``load_env_file`` a ``.env`` next to
    ``config.toml`` is loaded into the environment so :func:`get_secret` can read tokens/keys.
    """
    if load_env_file:
        load_env(path)

    with path.open("rb") as handle:
        try:
            raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    output_dir = Path(raw.get("output", {}).get("dir", DEFAULT_OUTPUT_DIR))
    state_dir = Path(raw.get("state", {}).get("dir", DEFAULT_STATE_DIR))
    cache_dir = Path(raw.get("cache", {}).get("dir", DEFAULT_CACHE_DIR))

    banks: dict[str, BankConfig] = {}
    for key, section in raw.get("banks", {}).items():
        try:
            safe_component(key, "bank key")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        options = {k: str(v) for k, v in section.items()}
        source = options.pop("source", None)
        if not source:
            raise ConfigError(f"bank '{key}' is missing required 'source'")
        banks[key] = BankConfig(key=key, source=source, options=options)

    combine, batch_size = _fetch_defaults(raw.get("fetch", {}))

    return AppConfig(
        output_dir=output_dir,
        state_dir=state_dir,
        banks=banks,
        cache_dir=cache_dir,
        combine=combine,
        batch_size=batch_size,
    )


# `[fetch]`'s whole surface. Deliberately closed: an unrecognised key here is reported rather than
# ignored, so a typo'd `batch_size` is a startup error instead of a setting that silently does
# nothing for weeks. `--refresh` must never join this table - it spends rate-limit allowance and
# has to stay a deliberate per-run act, not something a config file can turn on permanently
# (docs/adr-ofx-batch-splitting.md decision 9).
_FETCH_KEYS = ("combine", "batch_size")


def _fetch_defaults(section: object) -> tuple[bool, int | None]:
    """Parse ``[fetch]`` into ``(combine, batch_size)``, defaulting to today's behaviour."""
    if not isinstance(section, dict):
        raise ConfigError("[fetch] must be a table")
    unknown = sorted(key for key in section if key not in _FETCH_KEYS)
    if unknown:
        known = ", ".join(_FETCH_KEYS)
        raise ConfigError(
            f"[fetch] has unknown option(s): {', '.join(unknown)} (known options: {known})"
        )

    combine = section.get("combine", False)
    if not isinstance(combine, bool):
        raise ConfigError("[fetch] combine must be true or false")

    batch_size = section.get("batch_size")
    if batch_size is None:
        return combine, None
    # bool is an int subclass, and `batch_size = true` is a mistake worth naming rather than
    # silently batching at 1.
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ConfigError("[fetch] batch_size must be a whole number")
    if batch_size < 1:
        raise ConfigError(f"[fetch] batch_size must be 1 or more (got {batch_size})")
    return combine, batch_size


def get_secret(name: str) -> str | None:
    """Read a secret from the environment (populated from ``.env`` by :func:`load_config`)."""
    return os.environ.get(name)
