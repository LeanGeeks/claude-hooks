"""Round-trip tests for the callback_data encoder/decoder."""

from __future__ import annotations

import pytest

from relay_server.callback_data import CallbackData, decode, encode


@pytest.mark.parametrize(
    "message_id, option_idx",
    [(1, 0), (42, 3), (10**9, 99), (0, 0)],
)
def test_roundtrip(message_id: int, option_idx: int) -> None:
    raw = encode(message_id, option_idx)
    decoded = decode(raw)
    assert decoded == CallbackData(message_id=message_id, option_idx=option_idx)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "garbage",
        "m:1:o",
        "m:1:x:0",
        "x:1:o:0",
        "m:-1:o:0",
        "m:1:o:-2",
        "m:abc:o:0",
        "m:1:o:abc",
    ],
)
def test_decode_rejects_malformed(bad: str) -> None:
    assert decode(bad) is None


def test_format_under_64_bytes() -> None:
    # 64-bit message_id range + 3-digit option index still fits with margin.
    raw = encode(2**63 - 1, 999)
    assert len(raw.encode("ascii")) < 64
