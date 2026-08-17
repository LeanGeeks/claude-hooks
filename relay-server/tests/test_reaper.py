"""Tests for the Phase 5 reaper background task.

We test the reaper by calling ``reaper_tick`` directly (inline tick pattern)
rather than spinning up the full loop with sleeps — this keeps tests fast and
deterministic.

Epic 19-04 added the nudge pass and the cleanup sweep. Their tests share the
same inline-tick pattern; the ones that must observe a *real* terminal
transition (an ungrouped button tap or plain-text reply, a cancel, a group
finalize) drive the actual webhook/HTTP surface through the ``nudge_app``
fixture and then tick by hand, because the whole point of those cases is what
the transition leaves behind.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.callback_data import encode as encode_cb
from relay_server.config import RelayConfig
from relay_server.db import connect, init_schema, run_in_thread
from relay_server.reaper import reaper_tick
from relay_server.render import TAG, TAG_LINE
from relay_server.telegram_backend import (
    FakeTelegramBackend,
    TelegramApiError,
    TelegramForbidden,
)
from relay_server.tokens import generate_token, hash_token
from relay_server.waiters import WaiterRegistry

from tests.conftest import (  # type: ignore[attr-defined]
    TEST_WEBHOOK_SECRET,
    _run_lifespan,
)


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
# Nudge-engine helpers (19-04)
# ---------------------------------------------------------------------------

def _cfg(schedule: str = "15m,45m,3h", nudge_max: int = 3) -> RelayConfig:
    """A config carrying only the nudge knobs the pass reads.

    Tests construct ``RelayConfig`` (as conftest does); production reads
    ``app.state.config``. The reaper never invents these values.
    """
    return RelayConfig(nudge_default_schedule=schedule, nudge_max=nudge_max)


def _set_recipient(
    conn,
    chat_id: int,
    *,
    tz: str | None = None,
    windows: str | None = None,
    nudge_enabled: bool = True,
    schedule: str | None = None,
) -> None:
    """Upsert a ``recipients`` row. ``windows`` is a canonical *spec string*."""
    with conn:
        conn.execute(
            "INSERT INTO recipients(telegram_chat_id, tz, windows_json,"
            " nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "  tz = excluded.tz, windows_json = excluded.windows_json,"
            "  nudge_enabled = excluded.nudge_enabled,"
            "  nudge_schedule = excluded.nudge_schedule,"
            "  updated_at = excluded.updated_at",
            (
                chat_id,
                tz,
                windows,
                1 if nudge_enabled else 0,
                schedule,
                _iso(_utcnow()),
            ),
        )


def _insert_open_row(
    conn,
    installation_id: int,
    *,
    now: datetime | None = None,
    chat_id: int = 42,
    tg_message_id: int = 1000,
    text: str = "Allow the tool call?",
    kind: str = "question",
    keyboard: bool = True,
    reply_required: bool = False,
    group_id: str | None = None,
    group_total: int | None = None,
    state: str = "open",
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    next_nudge_at: datetime | None = None,
    nudge_count: int = 0,
    nudge_tg_message_id: int | None = None,
    render_dirty: int = 0,
) -> int:
    """Insert a row shaped like a live prompt, with the nudge columns settable.

    Unlike ``_insert_message`` this defaults to a *future* ``expires_at``: the
    nudge pass is only interesting on rows the expiry pass would leave alone.
    ``created_at`` is written in the same ISO form the app uses so the pass's
    oldest-first ordering is meaningful.
    """
    base = now or _utcnow()
    payload: dict = {
        "kind": kind,
        "text": text,
        "keyboard": [[{"label": "Yes", "value": "y"}]] if keyboard else None,
        "reply_required": reply_required,
    }
    if group_id is not None:
        payload["group_id"] = group_id
        payload["group_total"] = group_total
    with conn:
        cur = conn.execute(
            "INSERT INTO messages("
            "installation_id, telegram_chat_id, telegram_message_id, kind,"
            " payload_json, state, created_at, expires_at, nudge_count,"
            " next_nudge_at, nudge_tg_message_id, render_dirty)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                installation_id,
                chat_id,
                tg_message_id,
                kind,
                json.dumps(payload),
                state,
                _iso(created_at or base),
                _iso(expires_at or (base + timedelta(hours=12))),
                nudge_count,
                _iso(next_nudge_at) if next_nudge_at is not None else None,
                nudge_tg_message_id,
                render_dirty,
            ),
        )
    return int(cur.lastrowid)


def _row(conn, message_id: int):
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None, f"message {message_id} not found"
    return row


def _replies(backend: FakeTelegramBackend) -> list:
    return [c for c in backend.calls if c.method == "send_reply"]


def _deletes(backend: FakeTelegramBackend) -> list:
    return [c for c in backend.calls if c.method == "delete_message"]


def _edits(backend: FakeTelegramBackend) -> list:
    return [c for c in backend.calls if c.method == "edit_message"]


# ---------------------------------------------------------------------------
# App-driven fixture: for the transitions that must be *real* (an ungrouped
# button tap, a plain-text reply, a cancel, a group finalize).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def nudge_app(tmp_path: Path):
    db_file = str(tmp_path / "relay_nudge.db")
    conn = connect(db_file)
    init_schema(conn)
    token = generate_token()
    with conn:
        cur = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))",
            ("test", hash_token(token), 42, 7),
        )
        installation_id = int(cur.lastrowid)
    conn.close()

    backend = FakeTelegramBackend()
    config = RelayConfig(
        db_path=db_file,
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
        nudge_default_schedule="15m,45m,3h",
        nudge_max=3,
    )
    app = create_app(backend=backend, config=config)
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield SimpleNamespace(
                app=app,
                client=client,
                backend=backend,
                config=config,
                db=app.state.db,
                waiters=app.state.waiters,
                token=token,
                installation_id=installation_id,
                chat_id=42,
                user_id=7,
                auth={"Authorization": f"Bearer {token}"},
            )


async def _create(
    ctx,
    *,
    text: str = "Allow the tool call?",
    kind: str = "question",
    keyboard: bool = True,
    reply_required: bool = False,
    ttl_sec: int = 3600,
    group_id: str | None = None,
    group_total: int | None = None,
) -> int:
    body: dict = {
        "kind": kind,
        "text": text,
        "ttl_sec": ttl_sec,
        "reply_required": reply_required,
    }
    if keyboard:
        body["keyboard"] = [[{"label": "Yes", "value": "y"}]]
    if group_id is not None:
        body["group_id"] = group_id
        body["group_total"] = group_total
    r = await ctx.client.post("/v1/messages", headers=ctx.auth, json=body)
    assert r.status_code == 200, r.text
    return int(r.json()["message_id"])


async def _tap_button(ctx, message_id: int, *, option_idx: int = 0) -> None:
    r = await ctx.client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": 900 + message_id,
            "callback_query": {
                "id": f"cbq-{message_id}",
                "from": {"id": ctx.user_id},
                "data": encode_cb(message_id, option_idx),
                "message": {
                    "message_id": 1000,
                    "chat": {"id": ctx.chat_id},
                },
            },
        },
    )
    assert r.status_code == 200, r.text


async def _plain_reply(
    ctx, *, tg_message_id: int, text: str = "yes please", update_id: int = 950
) -> None:
    r = await ctx.client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": update_id,
            "message": {
                "message_id": 5000 + update_id,
                "chat": {"id": ctx.chat_id},
                "from": {"id": ctx.user_id},
                "text": text,
                "reply_to_message_id": tg_message_id,
            },
        },
    )
    assert r.status_code == 200, r.text


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


# ===========================================================================
# 19-04 — the nudge engine
# ===========================================================================
#
# Invariant 4 first: with nudges off the new pass must touch nothing at all.


@pytest.mark.asyncio
async def test_nudges_off_never_selects_queries_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nudges off (the default) → no query, no send, no column write.

    ``load_recipient`` is booby-trapped: reaching it would mean the pass got
    past its ``next_nudge_at IS NOT NULL`` gate, which is the whole of
    invariant 4. Asserted, not assumed.
    """
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    now = _utcnow()
    # No recipients row at all — that *is* "nudges off" (db.load_recipient
    # returns defaults), so next_nudge_at is never seeded.
    mid = _insert_open_row(conn, iid, now=now, tg_message_id=7000)

    def _boom(*args, **kwargs):
        raise AssertionError(
            "the nudge pass read a recipient although nothing was due"
        )

    monkeypatch.setattr("relay_server.reaper.load_recipient", _boom)

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    assert backend.calls == []
    row = _row(conn, mid)
    assert row["state"] == "open"
    assert row["next_nudge_at"] is None
    assert row["nudge_count"] == 0
    assert row["nudge_tg_message_id"] is None
    assert row["render_dirty"] == 0


@pytest.mark.asyncio
async def test_idle_notification_is_never_seeded_or_nudged(nudge_app) -> None:
    """An idle notification with ``reply_required=True`` and nudges **on** is
    never seeded and never nudged, past every ladder interval (invariant 7).

    ``notification_hook`` sets ``reply_required`` whenever reply-from-Telegram
    is on, so a predicate keyed on that alone would ping about a finished
    session at 09:00. The eligibility predicate is 19-03's ``awaits_human``.
    """
    ctx = nudge_app
    _set_recipient(ctx.db, ctx.chat_id, nudge_enabled=True)

    mid = await _create(
        ctx, kind="notification", keyboard=False, reply_required=True,
        text="session went idle",
    )
    assert _row(ctx.db, mid)["next_nudge_at"] is None

    now = _utcnow()
    for offset in (timedelta(minutes=16), timedelta(hours=1), timedelta(hours=4),
                   timedelta(days=1)):
        await reaper_tick(
            ctx.db, ctx.backend, ctx.waiters, ctx.config, now=now + offset
        )
        assert _replies(ctx.backend) == []
        assert _row(ctx.db, mid)["next_nudge_at"] is None


@pytest.mark.asyncio
async def test_open_prompt_is_seeded_when_nudges_are_on(nudge_app) -> None:
    """The mirror of the case above: a prompt awaiting a human *is* seeded, and
    with no windows configured the first rung is plain wall-clock addition."""
    ctx = nudge_app
    _set_recipient(ctx.db, ctx.chat_id, nudge_enabled=True, schedule="15m")

    before = _utcnow()
    mid = await _create(ctx, text="Allow the tool call?")
    seeded = _row(ctx.db, mid)["next_nudge_at"]
    assert seeded is not None
    due = datetime.fromisoformat(seeded)
    assert before + timedelta(minutes=14) < due < before + timedelta(minutes=16)


@pytest.mark.asyncio
async def test_due_row_sends_one_reply_at_the_right_target(tmp_path: Path) -> None:
    """On, active, due → exactly one reply-send quoting the target message."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    mid = _insert_open_row(
        conn, iid, now=now, tg_message_id=7001,
        text="Allow <b>rm -rf</b> to run?",
        next_nudge_at=now - timedelta(seconds=1),
    )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    replies = _replies(backend)
    assert len(replies) == 1
    kw = replies[0].kwargs
    assert kw["chat_id"] == 42
    assert kw["reply_to_message_id"] == 7001
    # Short, not a copy of the prompt, and with the HTML markup dropped so a
    # half-open tag cannot make Telegram reject the whole nudge.
    assert kw["text"] == "⏳ still waiting — Allow rm -rf to run?"
    assert "<b>" not in kw["text"]
    assert TAG not in kw["text"]  # it speaks for one row only

    # The reply-shaped send records target and text on the fake as well.
    assert backend.replies[0].reply_to_message_id == 7001
    assert backend.replies[0].text == kw["text"]

    row = _row(conn, mid)
    assert row["nudge_count"] == 1
    assert row["nudge_tg_message_id"] == backend.replies[0].message_id
    # Next rung is 45m, measured from now.
    assert datetime.fromisoformat(row["next_nudge_at"]) == now + timedelta(minutes=45)
    # No keyboard on a nudge — the buttons live on the original.
    assert all(c.method != "send_message" for c in backend.calls)


@pytest.mark.asyncio
async def test_group_of_four_produces_one_nudge_at_the_first_member(
    tmp_path: Path,
) -> None:
    """An AskUserQuestion spanning four messages nudges once, never per question."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    ids = [
        _insert_open_row(
            conn, iid, now=now, tg_message_id=8000 + i,
            text=f"Question {i}", group_id="grp-1", group_total=4,
            created_at=now - timedelta(seconds=10 - i),
            next_nudge_at=now - timedelta(seconds=1),
        )
        for i in range(4)
    ]

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    replies = _replies(backend)
    assert len(replies) == 1
    assert replies[0].kwargs["reply_to_message_id"] == 8000  # the first member
    # One target, so no "+N more" clause.
    assert "more" not in replies[0].kwargs["text"]

    first, *rest = ids
    assert _row(conn, first)["nudge_count"] == 1
    assert _row(conn, first)["nudge_tg_message_id"] is not None
    for mid in rest:
        # Folded rows own nothing (invariant 6); their due time is merely pushed.
        row = _row(conn, mid)
        assert row["nudge_count"] == 0
        assert row["nudge_tg_message_id"] is None
        assert datetime.fromisoformat(row["next_nudge_at"]) == now + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_four_rows_in_one_chat_produce_one_nudge_with_plus_three_more(
    tmp_path: Path,
) -> None:
    """Four sessions going idle in one tick produce one nudge, not four."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    ids = [
        _insert_open_row(
            conn, iid, now=now, tg_message_id=8100 + i, text=f"Prompt {i}",
            created_at=now - timedelta(minutes=10 - i),
            next_nudge_at=now - timedelta(seconds=1),
        )
        for i in range(4)
    ]

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    replies = _replies(backend)
    assert len(replies) == 1
    assert replies[0].kwargs["reply_to_message_id"] == 8100  # oldest
    assert replies[0].kwargs["text"] == (
        f"⏳ still waiting — Prompt 0\n\n+3 more {TAG}"
    )
    assert _row(conn, ids[0])["nudge_count"] == 1
    for mid in ids[1:]:
        assert _row(conn, mid)["nudge_count"] == 0
        assert _row(conn, mid)["nudge_tg_message_id"] is None


@pytest.mark.asyncio
async def test_four_chats_produce_four_nudges(tmp_path: Path) -> None:
    """Coalescing is per chat: four chats' worth of due rows → one nudge each."""
    conn = _setup_db(tmp_path)
    now = _utcnow()
    chats = [101, 102, 103, 104]
    for idx, chat in enumerate(chats):
        iid = _insert_installation(conn, chat_id=chat, label=f"i{chat}")
        _set_recipient(conn, chat, nudge_enabled=True)
        _insert_open_row(
            conn, iid, now=now, chat_id=chat, tg_message_id=8200 + idx,
            text=f"chat {chat}", created_at=now - timedelta(minutes=idx),
            next_nudge_at=now - timedelta(seconds=1),
        )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    replies = _replies(backend)
    assert len(replies) == 4
    assert {c.kwargs["chat_id"] for c in replies} == set(chats)
    for c in replies:
        assert "more" not in c.kwargs["text"]


@pytest.mark.asyncio
async def test_inactive_hours_push_due_times_and_then_fire(tmp_path: Path) -> None:
    """Outside the window: nothing sent, due times pushed to the next window
    start, and the nudge then fires there."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    berlin = ZoneInfo("Europe/Berlin")
    # Monday 2026-08-17, 22:30 local — well outside 09:00-19:00.
    night = datetime(2026, 8, 17, 22, 30, tzinfo=berlin)
    _set_recipient(
        conn, 42, tz="Europe/Berlin", windows="mon-fri 09:00-19:00",
        nudge_enabled=True,
    )
    mid = _insert_open_row(
        conn, iid, now=night, tg_message_id=7300,
        expires_at=night + timedelta(hours=20),
        next_nudge_at=night - timedelta(minutes=5),
    )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=night)

    assert _replies(backend) == []
    pushed = datetime.fromisoformat(_row(conn, mid)["next_nudge_at"])
    assert pushed == datetime(2026, 8, 18, 9, 0, tzinfo=berlin)
    assert _row(conn, mid)["nudge_count"] == 0

    # ... and the nudge then fires inside the window.
    morning = datetime(2026, 8, 18, 9, 5, tzinfo=berlin)
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=morning)
    replies = _replies(backend)
    assert len(replies) == 1
    assert replies[0].kwargs["reply_to_message_id"] == 7300


@pytest.mark.asyncio
async def test_prompt_raised_at_1850_nudges_next_morning_at_0920(
    nudge_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """brd §7, end to end: raised at 18:50 with a 30 m ladder and a window that
    closes at 19:00 → nudged at 09:20 the next morning, not 19:20, not 09:00.

    The clock the *create* path reads is patched so the seed is the real one the
    relay would write; the tick is then driven at chosen instants.
    """
    ctx = nudge_app
    berlin = ZoneInfo("Europe/Berlin")
    raised = datetime(2026, 8, 17, 18, 50, tzinfo=berlin)  # Monday
    monkeypatch.setattr("relay_server.app._utcnow", lambda: raised)
    _set_recipient(
        ctx.db, ctx.chat_id, tz="Europe/Berlin",
        windows="mon-fri 09:00-19:00", nudge_enabled=True, schedule="30m",
    )

    mid = await _create(ctx, text="Allow the deploy?", ttl_sec=86400)

    seeded = datetime.fromisoformat(_row(ctx.db, mid)["next_nudge_at"])
    assert seeded == datetime(2026, 8, 18, 9, 20, tzinfo=berlin)

    # 19:10 the same evening: past the window, nowhere near due.
    await reaper_tick(
        ctx.db, ctx.backend, ctx.waiters, ctx.config,
        now=datetime(2026, 8, 17, 19, 10, tzinfo=berlin),
    )
    assert _replies(ctx.backend) == []

    # 09:25 the next morning: due, inside the window.
    await reaper_tick(
        ctx.db, ctx.backend, ctx.waiters, ctx.config,
        now=datetime(2026, 8, 18, 9, 25, tzinfo=berlin),
    )
    replies = _replies(ctx.backend)
    assert len(replies) == 1
    assert replies[0].kwargs["text"] == "⏳ still waiting — Allow the deploy?"
    # One rung only, so the ladder is now spent.
    row = _row(ctx.db, mid)
    assert row["nudge_count"] == 1
    assert row["next_nudge_at"] is None


@pytest.mark.asyncio
async def test_ladder_three_nudges_then_silence_each_replacing_the_last(
    tmp_path: Path,
) -> None:
    """Three nudges then silence at the cap; each send deletes its predecessor."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True, schedule="15m,45m,3h")
    t0 = _utcnow()
    mid = _insert_open_row(
        conn, iid, now=t0, tg_message_id=7400,
        expires_at=t0 + timedelta(hours=23),
        next_nudge_at=t0 - timedelta(seconds=1),
    )
    backend = FakeTelegramBackend()
    cfg = _cfg()

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=t0)
    first_nudge = backend.replies[0].message_id
    assert _deletes(backend) == []
    row = _row(conn, mid)
    assert row["nudge_count"] == 1
    assert datetime.fromisoformat(row["next_nudge_at"]) == t0 + timedelta(minutes=45)

    t1 = t0 + timedelta(minutes=46)
    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=t1)
    assert len(_replies(backend)) == 2
    assert [c.kwargs["telegram_message_id"] for c in _deletes(backend)] == [first_nudge]
    second_nudge = backend.replies[1].message_id
    row = _row(conn, mid)
    assert row["nudge_count"] == 2
    assert row["nudge_tg_message_id"] == second_nudge
    assert datetime.fromisoformat(row["next_nudge_at"]) == t1 + timedelta(hours=3)

    t2 = t1 + timedelta(hours=3, minutes=1)
    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=t2)
    assert len(_replies(backend)) == 3
    assert [c.kwargs["telegram_message_id"] for c in _deletes(backend)] == [
        first_nudge, second_nudge,
    ]
    row = _row(conn, mid)
    assert row["nudge_count"] == 3
    # The cap is reached, so the ladder is retired: no fourth 03:00 ping.
    assert row["next_nudge_at"] is None

    t3 = t2 + timedelta(hours=6)
    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=t3)
    assert len(_replies(backend)) == 3


@pytest.mark.asyncio
async def test_a_capped_row_that_is_somehow_due_is_never_sent(tmp_path: Path) -> None:
    """The cap is checked before the send, not after."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True, schedule="15m,45m,3h")
    now = _utcnow()
    mid = _insert_open_row(
        conn, iid, now=now, tg_message_id=7500, nudge_count=3,
        next_nudge_at=now - timedelta(minutes=1),
    )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    assert _replies(backend) == []
    row = _row(conn, mid)
    assert row["nudge_count"] == 3
    assert row["next_nudge_at"] is None




@pytest.mark.asyncio
async def test_never_active_window_seeds_null_and_never_nudges(nudge_app) -> None:
    """A window that can never accumulate the ladder's active time retires it.

    ``advance_active`` returns None when no solution exists inside its 14-day
    horizon (19-01) — a one-minute weekly window cannot add up to 3 h. The seed
    is then NULL, no nudge is ever sent, and nothing crashes.
    """
    ctx = nudge_app
    _set_recipient(
        ctx.db, ctx.chat_id, tz="UTC", windows="mon 09:00-09:01",
        nudge_enabled=True, schedule="3h",
    )

    mid = await _create(ctx, text="Allow the deploy?", ttl_sec=86400)
    assert _row(ctx.db, mid)["next_nudge_at"] is None

    now = _utcnow()
    for offset in (timedelta(hours=4), timedelta(hours=20)):
        await reaper_tick(
            ctx.db, ctx.backend, ctx.waiters, ctx.config, now=now + offset
        )
    assert _replies(ctx.backend) == []
    assert _row(ctx.db, mid)["next_nudge_at"] is None


# ---- Cleanup: every terminal transition takes the nudge with it ------------


@pytest.mark.asyncio
async def test_expiry_deletes_the_nudge_in_the_tick_that_would_have_nudged(
    tmp_path: Path,
) -> None:
    """A row that is both expired and due loses its nudge and gains none."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    mid = _insert_open_row(
        conn, iid, now=now, tg_message_id=7700, text="Allow?",
        expires_at=now - timedelta(seconds=5),
        next_nudge_at=now - timedelta(minutes=1),
        nudge_tg_message_id=4242,
    )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    assert _get_message_state(conn, mid) == "expired"
    assert [c.kwargs["telegram_message_id"] for c in _deletes(backend)] == [4242]
    assert _replies(backend) == []
    row = _row(conn, mid)
    assert row["nudge_tg_message_id"] is None
    assert row["next_nudge_at"] is None
    assert row["render_dirty"] == 0


@pytest.mark.asyncio
async def test_cancel_deletes_the_nudge_and_retires_the_ladder(nudge_app) -> None:
    """The cancel endpoint — one of the two client-originated transitions."""
    ctx = nudge_app
    _set_recipient(ctx.db, ctx.chat_id, nudge_enabled=True)
    mid = await _create(ctx, text="Allow the deploy?")
    with ctx.db:
        ctx.db.execute(
            "UPDATE messages SET nudge_tg_message_id = 5150 WHERE id = ?", (mid,)
        )

    r = await ctx.client.post(f"/v1/messages/{mid}/cancel", headers=ctx.auth)
    assert r.status_code == 200, r.text

    assert [c.kwargs["telegram_message_id"] for c in _deletes(ctx.backend)] == [5150]
    row = _row(ctx.db, mid)
    assert row["state"] == "cancelled"
    assert row["nudge_tg_message_id"] is None
    assert row["next_nudge_at"] is None
    assert row["render_dirty"] == 0

    # Nothing left for the sweep.
    before = len(ctx.backend.calls)
    await reaper_tick(ctx.db, ctx.backend, ctx.waiters, ctx.config)
    assert len(ctx.backend.calls) == before


@pytest.mark.asyncio
async def test_group_finalize_deletes_the_nudge(nudge_app) -> None:
    """The relay-side group transition deletes the nudge it spoke with."""
    ctx = nudge_app
    _set_recipient(ctx.db, ctx.chat_id, nudge_enabled=True)
    first = await _create(ctx, text="Question A", group_id="g-1", group_total=2)
    second = await _create(ctx, text="Question B", group_id="g-1", group_total=2)
    # The group's single nudge hangs off its first member (brd §5.3/§5.5).
    with ctx.db:
        ctx.db.execute(
            "UPDATE messages SET nudge_tg_message_id = 6161 WHERE id = ?", (first,)
        )

    await _tap_button(ctx, first)
    await _tap_button(ctx, second)  # completes the group → finalize

    assert _row(ctx.db, first)["state"] == "answered"
    assert _row(ctx.db, second)["state"] == "answered"
    assert [c.kwargs["telegram_message_id"] for c in _deletes(ctx.backend)] == [6161]
    for mid in (first, second):
        row = _row(ctx.db, mid)
        assert row["nudge_tg_message_id"] is None
        assert row["next_nudge_at"] is None
        assert row["render_dirty"] == 0


# ---- The _record_answer hole (state.md 2026-08-16) -------------------------
#
# The leak is silent: the row goes 'answered' with a live tag and a live nudge,
# and nothing raises. These two tests are the ones written first.


@pytest.mark.asyncio
async def test_record_answer_hole_button_tap_is_swept(nudge_app) -> None:
    """Ungrouped button tap, then the machine sleeps: no PATCH, no cancel.

    One tick must remove the tag and the nudge.
    """
    ctx = nudge_app
    _set_recipient(ctx.db, ctx.chat_id, nudge_enabled=True)
    mid = await _create(ctx, text="Allow the deploy?")
    tg_id = ctx.backend.sent[0].message_id
    with ctx.db:
        ctx.db.execute(
            "UPDATE messages SET nudge_tg_message_id = 7171 WHERE id = ?", (mid,)
        )

    await _tap_button(ctx, mid)

    # The flip itself reaches Telegram not at all — that is the hole.
    row = _row(ctx.db, mid)
    assert row["state"] == "answered"
    assert row["render_dirty"] == 1
    assert row["next_nudge_at"] is None
    assert _edits(ctx.backend) == []

    await reaper_tick(ctx.db, ctx.backend, ctx.waiters, ctx.config)

    edits = _edits(ctx.backend)
    assert len(edits) == 1
    assert edits[0].kwargs["telegram_message_id"] == tg_id
    assert edits[0].kwargs["text"] == "Allow the deploy?"
    assert TAG not in edits[0].kwargs["text"]
    assert [c.kwargs["telegram_message_id"] for c in _deletes(ctx.backend)] == [7171]
    row = _row(ctx.db, mid)
    assert row["render_dirty"] == 0
    assert row["nudge_tg_message_id"] is None

    # Idempotent: a second tick finds nothing left to do.
    before = len(ctx.backend.calls)
    await reaper_tick(ctx.db, ctx.backend, ctx.waiters, ctx.config)
    assert len(ctx.backend.calls) == before


@pytest.mark.asyncio
async def test_record_answer_hole_plain_text_reply_is_swept(nudge_app) -> None:
    """The same hole through the other door: an ungrouped plain-text reply."""
    ctx = nudge_app
    _set_recipient(ctx.db, ctx.chat_id, nudge_enabled=True)
    mid = await _create(
        ctx, text="What should I name it?", keyboard=False, reply_required=True,
    )
    tg_id = ctx.backend.sent[0].message_id
    with ctx.db:
        ctx.db.execute(
            "UPDATE messages SET nudge_tg_message_id = 7272 WHERE id = ?", (mid,)
        )

    await _plain_reply(ctx, tg_message_id=tg_id, text="call it foo")

    row = _row(ctx.db, mid)
    assert row["state"] == "answered"
    assert row["render_dirty"] == 1
    assert _edits(ctx.backend) == []

    await reaper_tick(ctx.db, ctx.backend, ctx.waiters, ctx.config)

    edits = _edits(ctx.backend)
    assert len(edits) == 1
    assert edits[0].kwargs["text"] == "What should I name it?"
    assert TAG not in edits[0].kwargs["text"]
    assert [c.kwargs["telegram_message_id"] for c in _deletes(ctx.backend)] == [7272]
    row = _row(ctx.db, mid)
    assert row["render_dirty"] == 0
    assert row["nudge_tg_message_id"] is None


@pytest.mark.asyncio
async def test_patch_after_the_flip_costs_the_sweep_no_telegram_call(
    nudge_app,
) -> None:
    """The normal case must not reach the sweep: flip, PATCH, tick → no edit.

    This is what keeps the sweep a net rather than a second cost on the hot
    path — the hook's PATCH lands within a second of the flip and clears the
    flag before the reaper ever looks.
    """
    ctx = nudge_app
    mid = await _create(ctx, text="Allow the deploy?")

    await _tap_button(ctx, mid)
    assert _row(ctx.db, mid)["render_dirty"] == 1

    r = await ctx.client.patch(
        f"/v1/messages/{mid}",
        headers=ctx.auth,
        json={"text": "Allow the deploy?\n\n✅ Yes"},
    )
    assert r.status_code == 200, r.text
    assert _row(ctx.db, mid)["render_dirty"] == 0

    calls_before = len(ctx.backend.calls)
    await reaper_tick(ctx.db, ctx.backend, ctx.waiters, ctx.config)
    assert len(ctx.backend.calls) == calls_before, (
        "the sweep issued a Telegram call although the flag was already clear"
    )


@pytest.mark.asyncio
async def test_untagged_terminal_row_is_never_flagged_nor_swept(nudge_app) -> None:
    """An idle notification carries no tag, so answering it flags nothing."""
    ctx = nudge_app
    mid = await _create(
        ctx, kind="notification", keyboard=False, reply_required=True,
        text="session went idle",
    )
    tg_id = ctx.backend.sent[0].message_id

    await _plain_reply(ctx, tg_message_id=tg_id, text="ok")

    row = _row(ctx.db, mid)
    assert row["state"] == "answered"
    assert row["render_dirty"] == 0, (
        "an untagged row must never be flagged — the sweep would edit a message"
        " that had no tag"
    )

    calls_before = len(ctx.backend.calls)
    await reaper_tick(ctx.db, ctx.backend, ctx.waiters, ctx.config)
    assert len(ctx.backend.calls) == calls_before


# ---- Best-effort: no nudge failure may reach the expiry pass ---------------


@pytest.mark.asyncio
async def test_nudge_send_failure_completes_the_tick_and_expiry_still_runs(
    tmp_path: Path,
) -> None:
    class BrokenSend(FakeTelegramBackend):
        async def send_reply(self, **kwargs):  # type: ignore[override]
            raise TelegramApiError(400, "Bad Request: chat not found")

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    doomed = _insert_open_row(
        conn, iid, now=now, tg_message_id=7800, text="expire me",
        expires_at=now - timedelta(seconds=5),
    )
    due = _insert_open_row(
        conn, iid, now=now, tg_message_id=7801, text="nudge me",
        next_nudge_at=now - timedelta(seconds=1),
    )

    backend = BrokenSend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)  # no raise

    assert _get_message_state(conn, doomed) == "expired"
    assert any(c.method == "edit_reply_markup" for c in backend.calls)
    row = _row(conn, due)
    assert row["state"] == "open"
    assert row["nudge_tg_message_id"] is None
    # The rung is consumed so a chat that keeps failing walks to the cap and
    # falls silent instead of being retried every 30 s forever.
    assert row["nudge_count"] == 1
    assert datetime.fromisoformat(row["next_nudge_at"]) == now + timedelta(minutes=45)
    assert _count_idem_keys(conn) == 0


@pytest.mark.asyncio
async def test_nudge_delete_failure_completes_the_tick_and_expiry_still_runs(
    tmp_path: Path,
) -> None:
    class BrokenDelete(FakeTelegramBackend):
        async def delete_message(self, **kwargs):  # type: ignore[override]
            await super().delete_message(**kwargs)
            raise TelegramApiError(400, "Bad Request: message can't be deleted")

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    doomed = _insert_open_row(
        conn, iid, now=now, tg_message_id=7900, text="expire me",
        expires_at=now - timedelta(seconds=5), nudge_tg_message_id=1111,
    )
    due = _insert_open_row(
        conn, iid, now=now, tg_message_id=7901, text="nudge me",
        next_nudge_at=now - timedelta(seconds=1), nudge_tg_message_id=2222,
    )

    backend = BrokenDelete()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)  # no raise

    assert _get_message_state(conn, doomed) == "expired"
    # Both deletes were attempted and both columns cleared regardless — a stale
    # id must not pin a row into the sweep forever.
    assert {c.kwargs["telegram_message_id"] for c in _deletes(backend)} == {1111, 2222}
    assert _row(conn, doomed)["nudge_tg_message_id"] is None
    # The replacement nudge still went out and is now the row's live one.
    assert len(_replies(backend)) == 1
    assert _row(conn, due)["nudge_tg_message_id"] == backend.replies[0].message_id


@pytest.mark.asyncio
async def test_forbidden_on_a_nudge_send_stops_the_chat_without_unbinding(
    tmp_path: Path,
) -> None:
    """``TelegramForbidden`` is terminal, not transient: stop nudging that chat.

    The reaper has no request to fail, so unlike every path in ``app.py`` it
    does **not** unbind — a background sweep silently disconnecting a machine
    whose user merely archived the chat is the worse failure. It logs loudly and
    clears the due times instead.
    """
    class ForbiddenReply(FakeTelegramBackend):
        async def send_reply(self, **kwargs):  # type: ignore[override]
            raise TelegramForbidden("bot was blocked by the user")

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    doomed = _insert_open_row(
        conn, iid, now=now, tg_message_id=8000, text="expire me",
        expires_at=now - timedelta(seconds=5),
    )
    due_a = _insert_open_row(
        conn, iid, now=now, tg_message_id=8001, text="first",
        created_at=now - timedelta(minutes=2),
        next_nudge_at=now - timedelta(seconds=1),
    )
    due_b = _insert_open_row(
        conn, iid, now=now, tg_message_id=8002, text="second",
        created_at=now - timedelta(minutes=1),
        next_nudge_at=now - timedelta(seconds=1),
    )

    backend = ForbiddenReply()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)  # no raise

    # Expiry is untouched by the forbidden chat.
    assert _get_message_state(conn, doomed) == "expired"
    # Both open rows stop being due.
    assert _row(conn, due_a)["next_nudge_at"] is None
    assert _row(conn, due_b)["next_nudge_at"] is None
    # The binding is deliberately left alone.
    install = conn.execute(
        "SELECT telegram_chat_id FROM installations WHERE id = ?", (iid,)
    ).fetchone()
    assert install["telegram_chat_id"] == 42

    # And the next tick makes no further attempt.
    calls_before = len(backend.calls)
    await reaper_tick(
        conn, backend, WaiterRegistry(), _cfg(), now=now + timedelta(minutes=30)
    )
    assert len(backend.calls) == calls_before


@pytest.mark.asyncio
async def test_nudges_off_in_recipients_clears_a_stray_due_time(
    tmp_path: Path,
) -> None:
    """The recipient row is authoritative: a due row in a nudges-off chat is
    cleared rather than sent (``/nudge off`` already clears them, so this only
    fires if a write was lost)."""
    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=False)
    now = _utcnow()
    mid = _insert_open_row(
        conn, iid, now=now, tg_message_id=8100,
        next_nudge_at=now - timedelta(seconds=1),
    )

    backend = FakeTelegramBackend()
    await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    assert _replies(backend) == []
    assert _row(conn, mid)["next_nudge_at"] is None


@pytest.mark.asyncio
async def test_reaper_skips_and_clears_ineligible_seeded_row(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive re-check: a bad seed on a kind='notification' row is never sent.

    If any future writer sets next_nudge_at on an ineligible row (bypassing
    awaits_human), the reaper must not emit a nudge.  It must:
    - not call send_reply
    - log a WARNING naming the row id
    - clear next_nudge_at so the row does not re-appear every tick
    (brd §4.1, invariant 7 — secondary fix in 19-08).
    """
    import logging

    conn = _setup_db(tmp_path)
    iid = _insert_installation(conn)
    _set_recipient(conn, 42, nudge_enabled=True)
    now = _utcnow()
    # Directly seed a notification row — simulating the bug that the primary
    # fix closes.  next_nudge_at is set by the fixture, bypassing awaits_human.
    mid = _insert_open_row(
        conn, iid, now=now, tg_message_id=8200,
        kind="notification", keyboard=False, reply_required=True,
        next_nudge_at=now - timedelta(seconds=1),
    )

    backend = FakeTelegramBackend()
    with caplog.at_level(logging.WARNING, logger="relay_server.reaper"):
        await reaper_tick(conn, backend, WaiterRegistry(), _cfg(), now=now)

    assert _replies(backend) == [], "notification row must never be nudged"
    row = _row(conn, mid)
    assert row["next_nudge_at"] is None, (
        "ineligible seeded row must have next_nudge_at cleared"
    )
    assert any(
        str(mid) in record.message and record.levelno >= logging.WARNING
        for record in caplog.records
    ), f"expected a WARNING log naming row id {mid}; got: {caplog.records}"
