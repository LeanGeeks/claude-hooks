"""Installation token generation and hashing."""

from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "rly_"


def generate_token() -> str:
    """Generate a fresh installation token. Returns the plaintext token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of the token. Used for storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
