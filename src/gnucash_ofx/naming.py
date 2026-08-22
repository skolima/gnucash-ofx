"""Validation for identifiers that end up in filesystem paths.

Bank keys and currency codes are interpolated into state filenames and OFX output filenames.
Restricting them to a safe character set prevents path traversal (``..``, separators) from
ever escaping the state/output directories.
"""

from __future__ import annotations

import re

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")


def safe_component(value: str, kind: str) -> str:
    """Return ``value`` unchanged if it is a safe path component, else raise ``ValueError``."""
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"invalid {kind} {value!r}: only letters, digits, '_' and '-' are allowed")
    return value
