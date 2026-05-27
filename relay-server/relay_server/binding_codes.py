"""Utilities for generating and validating short, user-typable binding codes.

Format: ``BIND-XXXX-XXXX`` where each group is 4 characters drawn from a
confusion-free alphabet (avoids 0/O/I/1 which look similar in many fonts).
"""

from __future__ import annotations

import re
import secrets

# Characters that are visually unambiguous.
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_GROUP_LEN = 4
_NUM_GROUPS = 2
_CODE_RE = re.compile(
    r"^BIND-[" + _ALPHABET + r"]{4}-[" + _ALPHABET + r"]{4}$"
)


def generate_code() -> str:
    """Return a fresh ``BIND-XXXX-XXXX`` code."""
    groups = [
        "".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN))
        for _ in range(_NUM_GROUPS)
    ]
    return "BIND-" + "-".join(groups)


def normalise_code(raw: str) -> str | None:
    """Upper-case + strip whitespace; return None if the result isn't valid."""
    candidate = raw.strip().upper()
    # Accept both ``BIND-XXXX-XXXX`` and bare ``XXXX-XXXX`` input.
    if re.match(r"^[" + _ALPHABET + r"]{4}-[" + _ALPHABET + r"]{4}$", candidate):
        candidate = "BIND-" + candidate
    if _CODE_RE.match(candidate):
        return candidate
    return None
