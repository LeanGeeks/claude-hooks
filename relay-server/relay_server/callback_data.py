"""Encoding helpers for Telegram inline button ``callback_data``.

Telegram limits ``callback_data`` to 64 bytes. We pack the server-side
``message_id`` and the option index inside the message's keyboard into a tiny
ASCII string of the form ``m:{message_id}:o:{option_idx}``. Both numbers are
non-negative decimal integers; the longest realistic value (64-bit message_id +
two-digit option index) is well under the 64-byte ceiling.

Keeping the format human-readable simplifies log spelunking and avoids the
need for any opaque encoding/signing — the data is not security-sensitive
(authorization happens via chat membership, not button payloads).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallbackData:
    message_id: int
    option_idx: int

    def encode(self) -> str:
        return f"m:{self.message_id}:o:{self.option_idx}"


def encode(message_id: int, option_idx: int) -> str:
    return CallbackData(message_id=message_id, option_idx=option_idx).encode()


def decode(raw: str) -> CallbackData | None:
    """Parse a callback_data string. Returns None if it doesn't look like ours.

    Strict on shape so we can distinguish our own callbacks from anything a
    future feature (or a misbehaving client) puts in a button payload.
    """
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 4 or parts[0] != "m" or parts[2] != "o":
        return None
    try:
        message_id = int(parts[1])
        option_idx = int(parts[3])
    except ValueError:
        return None
    if message_id < 0 or option_idx < 0:
        return None
    return CallbackData(message_id=message_id, option_idx=option_idx)
