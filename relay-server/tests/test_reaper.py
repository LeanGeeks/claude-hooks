"""Tests for the Phase 5 reaper background task.

We test the reaper by calling ``reaper_tick`` directly (inline tick pattern)
rather than spinning up the full loop with sleeps — this keeps tests fast and
deterministic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from relay_server.db import connect, init_schema, run_in_thread
from relay_server.reaper import reaper_tick
from relay_server.telegram_backend import FakeTelegramBackend, TelegramApiError
from relay_server.tokens import generate_token, hash_token
from relay_server.waiters import WaiterRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _setup_db(tmp_path: Path):
    """Return an open, schema-initialised connection."""
    conn = connect(str(tmp_path / "reaper_test.db"))
    init_schema(conn)
    return conn


def _insert_installation(conn, *, chat_id: int = 42, label: str = "test") -> int:
    token = generate_token()
    with conn:
        cur = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))",
            (label, hash_token(token), chat_id, 7),
        )
    return int(cur.lastrowid)


def _insert_message(
    conn,
    installation_id: int,
    *,
    state: str = "open",
    expires_at: datetime | None = None,
    chat_id: int = 42,
    tg_message_id: int = 1000,
) -> int:
    if expires_at is None:
        expires_at = _utcnow() - timedelta(seconds=1)  # expired by default
    with conn:
        cur = conn.execute(
            "INSERT INTO messages("
            "installation_id, telegram_chat_id, telegram_message_id,"
            " kind, payload_json, state, created_at, expires_at)"
            " VALUES (?, ?, ?, 'question', '{}', ?, datetime('now'), ?)",
            (installation_id, chat_id, tg_message_id, state, _iso(expires_at)),
        )
    return int(cur.lastrowid)


def _insert_idem_key(conn, installation_id: int, key: str, *, age_hours: float) -> None:
    created_at = _utcnow() - timedelta(hours=age_hours)
    with conn:
        conn.execute(
            "INSERT INTO idempotency_keys(key, installation_id, request_hash,"
            " response_json, created_at) VALUES (?, ?, NULL, '{}', ?)",
            (key, installation_id, _iso(created_at)),
        )


def _get_message_state(conn, message_id: int) -> str:
    row = conn.execute(
        "SELECT state FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None, f"message {message_id} not found"
    return row["state"]


def _count_idem_keys(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_message_is_transitioned(tmp_path: Path) -> None:
    """A single expired-open message → state becomes 'expired'."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(conn, iid, state="open", expires_at=_utcnow() - timedelta(minutes=5))

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    assert _get_message_state(conn, mid) == "expired"


@pytest.mark.asyncio
async def test_expired_message_strips_keyboard(tmp_path: Path) -> None:
    """edit_reply_markup(keyboard=None) is called for the expired message."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn, chat_id=55)
    mid = _insert_message(
        conn, iid, state="open",
        expires_at=_utcnow() - timedelta(seconds=10),
        chat_id=55,
        tg_message_id=9001,
    )

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    edit_calls = [c for c in backend.calls if c.method == "edit_reply_markup"]
    assert len(edit_calls) == 1
    kw = edit_calls[0].kwargs
    assert kw["chat_id"] == 55
    assert kw["telegram_message_id"] == 9001
    assert kw["keyboard"] is None


@pytest.mark.asyncio
async def test_only_expired_message_reaped_not_fresh(tmp_path: Path) -> None:
    """A non-expired open message is left alone; only the expired one is reaped."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)

    expired_mid = _insert_message(
        conn, iid, state="open",
        expires_at=_utcnow() - timedelta(seconds=5),
        tg_message_id=1001,
    )
    fresh_mid = _insert_message(
        conn, iid, state="open",
        expires_at=_utcnow() + timedelta(minutes=10),
        tg_message_id=1002,
    )

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    assert _get_message_state(conn, expired_mid) == "expired"
    assert _get_message_state(conn, fresh_mid) == "open"

    edit_calls = [c for c in backend.calls if c.method == "edit_reply_markup"]
    assert len(edit_calls) == 1


@pytest.mark.asyncio
async def test_backend_error_does_not_prevent_state_transition(tmp_path: Path) -> None:
    """A Telegram error during keyboard strip must not prevent state=expired."""

    class ErrorBackend(FakeTelegramBackend):
        async def edit_reply_markup(self, *, chat_id, telegram_message_id, keyboard):
            await super().edit_reply_markup(
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                keyboard=keyboard,
            )
            raise TelegramApiError(400, "message to edit not found")

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(conn, iid, state="open", expires_at=_utcnow() - timedelta(seconds=1))

    backend = ErrorBackend()
    waiters = WaiterRegistry()

    # Must not raise despite backend error.
    await reaper_tick(conn, backend, waiters)

    # DB transition still happened.
    assert _get_message_state(conn, mid) == "expired"


@pytest.mark.asyncio
async def test_no_work_no_errors_no_backend_calls(tmp_path: Path) -> None:
    """Reaper with nothing to do: no backend calls, no exceptions."""
    conn = _setup_db(tmp_path)
    # No messages, no idempotency keys.

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)  # must not raise

    assert backend.calls == []


@pytest.mark.asyncio
async def test_waiter_is_notified_on_expiry(tmp_path: Path) -> None:
    """A long-poll waiter parked on an expiring message wakes up."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(conn, iid, state="open", expires_at=_utcnow() - timedelta(seconds=1))

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    # Park a waiter before the tick.
    wait_task = asyncio.create_task(waiters.wait(mid, timeout=5.0))

    await reaper_tick(conn, backend, waiters)

    notified = await wait_task
    assert notified is True


@pytest.mark.asyncio
async def test_waiter_registry_event_evicted_after_expiry(tmp_path: Path) -> None:
    """After expiry the WaiterRegistry event is cleared (memory hygiene)."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(conn, iid, state="open", expires_at=_utcnow() - timedelta(seconds=1))

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    # The event should have been evicted from the internal dict.
    # We verify by checking that the defaultdict has no entry for mid.
    # (WaiterRegistry.clear removes from self._events.)
    assert mid not in waiters._events


@pytest.mark.asyncio
async def test_idempotency_keys_older_than_24h_are_deleted(tmp_path: Path) -> None:
    """Keys older than 24 h get purged; newer ones are retained."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)

    _insert_idem_key(conn, iid, "old-key", age_hours=25)   # should be deleted
    _insert_idem_key(conn, iid, "fresh-key", age_hours=1)  # should remain

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    remaining = conn.execute(
        "SELECT key FROM idempotency_keys"
    ).fetchall()
    keys = {r["key"] for r in remaining}
    assert "old-key" not in keys
    assert "fresh-key" in keys


@pytest.mark.asyncio
async def test_idempotency_keys_exactly_at_boundary(tmp_path: Path) -> None:
    """A key just under 24 h old (23 h 59 m) is not deleted."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)

    _insert_idem_key(conn, iid, "borderline-key", age_hours=23.9)

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    count = _count_idem_keys(conn)
    assert count == 1


@pytest.mark.asyncio
async def test_already_answered_message_not_re_expired(tmp_path: Path) -> None:
    """A message already in 'answered' state is not touched even if past expires_at."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(
        conn, iid, state="answered",
        expires_at=_utcnow() - timedelta(seconds=5),
    )

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    # State must remain 'answered' — the reaper only targets state='open'.
    assert _get_message_state(conn, mid) == "answered"
    edit_calls = [c for c in backend.calls if c.method == "edit_reply_markup"]
    assert edit_calls == []


@pytest.mark.asyncio
async def test_multiple_expired_messages_all_reaped(tmp_path: Path) -> None:
    """Multiple expired messages are all transitioned in one tick."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)

    mids = [
        _insert_message(
            conn, iid, state="open",
            expires_at=_utcnow() - timedelta(seconds=i + 1),
            tg_message_id=2000 + i,
        )
        for i in range(3)
    ]

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()

    await reaper_tick(conn, backend, waiters)

    for mid in mids:
        assert _get_message_state(conn, mid) == "expired"

    edit_calls = [c for c in backend.calls if c.method == "edit_reply_markup"]
    assert len(edit_calls) == 3
