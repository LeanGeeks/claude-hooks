"""Tests for the Phase 5 reaper background task.

We test the reaper by calling ``reaper_tick`` directly (inline tick pattern)
rather than spinning up the full loop with sleeps — this keeps tests fast and
deterministic.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from relay_server.db import connect, init_schema, run_in_thread
from relay_server.reaper import reaper_tick
from relay_server.render import TAG, TAG_LINE
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
    payload: dict | None = None,
) -> int:
    if expires_at is None:
        expires_at = _utcnow() - timedelta(seconds=1)  # expired by default
    with conn:
        cur = conn.execute(
            "INSERT INTO messages("
            "installation_id, telegram_chat_id, telegram_message_id,"
            " kind, payload_json, state, created_at, expires_at)"
            " VALUES (?, ?, ?, 'question', ?, ?, datetime('now'), ?)",
            (
                installation_id,
                chat_id,
                tg_message_id,
                json.dumps(payload) if payload is not None else "{}",
                state,
                _iso(expires_at),
            ),
        )
    return int(cur.lastrowid)


def _question_payload(text: str) -> dict:
    """A payload shaped like an open permission prompt (buttons → tagged)."""
    return {
        "kind": "question",
        "text": text,
        "keyboard": [[{"label": "Yes", "value": "y"}]],
        "reply_required": False,
    }


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


# ---- Expiry drops the #unanswered tag (19-03) ------------------------------
#
# This reverses task 05's "Option A" (keyboard-only strip, no text edit): an
# expired prompt that keeps its tag would leave Telegram's hashtag search — the
# pending-work index — listing work nobody is waiting on (brd §4.3).


@pytest.mark.asyncio
async def test_expiry_rewrites_the_body_without_the_tag(tmp_path: Path) -> None:
    """Text edited, tag gone, keyboard stripped, waiter woken — in one tick."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn, chat_id=55)
    mid = _insert_message(
        conn, iid, state="open",
        expires_at=_utcnow() - timedelta(seconds=10),
        chat_id=55,
        tg_message_id=9001,
        payload=_question_payload("Allow the tool call?"),
    )

    backend = FakeTelegramBackend()
    waiters = WaiterRegistry()
    wait_task = asyncio.create_task(waiters.wait(mid, timeout=5.0))

    await reaper_tick(conn, backend, waiters)

    edits = [c for c in backend.calls if c.method == "edit_message"]
    assert len(edits) == 1
    assert edits[0].kwargs["chat_id"] == 55
    assert edits[0].kwargs["telegram_message_id"] == 9001
    assert edits[0].kwargs["text"] == "Allow the tool call?"
    assert TAG not in edits[0].kwargs["text"]
    assert edits[0].kwargs["keyboard"] is None

    strips = [c for c in backend.calls if c.method == "edit_reply_markup"]
    assert len(strips) == 1
    assert strips[0].kwargs["keyboard"] is None

    assert _get_message_state(conn, mid) == "expired"
    assert await wait_task is True


@pytest.mark.asyncio
async def test_expiry_strips_a_tag_a_client_typed_itself(tmp_path: Path) -> None:
    """Even a body whose stored text somehow carries the tag renders clean."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _insert_message(
        conn, iid, state="open",
        payload=_question_payload("legacy body" + TAG_LINE),
    )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry())

    edits = [c for c in backend.calls if c.method == "edit_message"]
    assert edits[0].kwargs["text"] == "legacy body"


@pytest.mark.asyncio
async def test_expiry_without_a_stored_body_skips_the_text_edit(
    tmp_path: Path,
) -> None:
    """An empty ``editMessageText`` is a guaranteed Telegram rejection, so rows
    with no readable body fall back to the keyboard strip alone."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(conn, iid, state="open")  # payload_json == '{}'

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry())

    assert not any(c.method == "edit_message" for c in backend.calls)
    assert any(c.method == "edit_reply_markup" for c in backend.calls)
    assert _get_message_state(conn, mid) == "expired"


@pytest.mark.asyncio
async def test_not_modified_on_the_text_edit_does_not_stop_the_tick(
    tmp_path: Path,
) -> None:
    """An already-untagged message being expired is the common correct case.
    The transition completes, the keyboard is still stripped, and the rest of
    the tick (further rows, the idempotency purge) still runs."""

    class NotModifiedBackend(FakeTelegramBackend):
        async def edit_message(self, **kwargs):
            await super().edit_message(**kwargs)
            raise TelegramApiError(
                400, "Bad Request: message is not modified"
            )

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    first = _insert_message(
        conn, iid, state="open", tg_message_id=3001,
        payload=_question_payload("one"),
    )
    second = _insert_message(
        conn, iid, state="open", tg_message_id=3002,
        payload=_question_payload("two"),
    )
    _insert_idem_key(conn, iid, "old-key", age_hours=25)

    backend = NotModifiedBackend()
    await reaper_tick(conn, backend, WaiterRegistry())  # must not raise

    assert _get_message_state(conn, first) == "expired"
    assert _get_message_state(conn, second) == "expired"
    assert len([c for c in backend.calls if c.method == "edit_message"]) == 2
    assert len([c for c in backend.calls if c.method == "edit_reply_markup"]) == 2
    assert _count_idem_keys(conn) == 0


@pytest.mark.asyncio
async def test_text_edit_failure_does_not_prevent_expiry(tmp_path: Path) -> None:
    """The text edit is best-effort: a hard Telegram failure must not block the
    state transition, the keyboard strip, or the waiter wake-up."""

    class BrokenBackend(FakeTelegramBackend):
        async def edit_message(self, **kwargs):
            raise TelegramApiError(400, "Bad Request: message to edit not found")

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    mid = _insert_message(
        conn, iid, state="open", payload=_question_payload("body"),
    )

    backend = BrokenBackend()
    waiters = WaiterRegistry()
    wait_task = asyncio.create_task(waiters.wait(mid, timeout=5.0))

    await reaper_tick(conn, backend, waiters)  # must not raise

    assert _get_message_state(conn, mid) == "expired"
    assert any(c.method == "edit_reply_markup" for c in backend.calls)
    assert await wait_task is True


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
