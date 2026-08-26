"""Tests for epic 23-02: per-message nudge schedules and server-side escalation.

Coverage per the task's Tests section:
- Ladder parsing including the '*' position error
- Active-time arithmetic across a window boundary for a repeating rung
- Override-vs-chat-config precedence including nudge_enabled = 0
- Escalation fires-once, unknown-token refusal, answer attribution from the
  escalated copy
- Full reaper tick with all four passes and rows in each state
- Regression floor per invariant 9 (existing messages NULL for all three columns,
  byte-identical relay calls)
- v4->v5 forward migration: new columns appear, existing rows keep NULL defaults

Uses FakeTelegramBackend, no network, no real bot.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from relay_server.app import create_app
from relay_server.availability import (
    Window,
    advance_active,
    parse_nudge_schedule_with_repeat,
)
from relay_server.callback_data import encode as encode_cb
from relay_server.config import RelayConfig
from relay_server.db import connect, get_schema_version, init_schema
from relay_server.reaper import reaper_tick
from relay_server.telegram_backend import FakeTelegramBackend
from relay_server.tokens import generate_token, hash_token
from relay_server.waiters import WaiterRegistry

from tests.conftest import (  # type: ignore[attr-defined]
    TEST_WEBHOOK_SECRET,
    _run_lifespan,
    make_test_config,
)

UTC = timezone.utc
_update_id_seq = itertools.count(50000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    return conn


def _insert_installation(
    conn: sqlite3.Connection, *, chat_id: int = 42, label: str = "inst-a"
) -> tuple[int, str]:
    """Return (installation_id, raw_token) for a bound installation."""
    token = generate_token()
    with conn:
        cur = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES (?, ?, ?, 7, datetime('now'))",
            (label, hash_token(token), chat_id),
        )
    return int(cur.lastrowid), token


def _insert_message(
    conn: sqlite3.Connection,
    installation_id: int,
    *,
    state: str = "open",
    chat_id: int = 42,
    tg_message_id: int = 1000,
    payload: dict | None = None,
    expires_at: datetime | None = None,
    next_nudge_at: datetime | None = None,
    nudge_count: int = 0,
    nudge_schedule_override: str | None = None,
    escalate_at: datetime | None = None,
    escalate_to_token_hash: str | None = None,
    parent_message_id: int | None = None,
) -> int:
    if expires_at is None:
        expires_at = datetime(9999, 12, 31, tzinfo=UTC)
    with conn:
        cur = conn.execute(
            "INSERT INTO messages("
            "installation_id, telegram_chat_id, telegram_message_id,"
            " kind, payload_json, state, created_at, expires_at,"
            " nudge_count, next_nudge_at,"
            " nudge_schedule_override, escalate_at, escalate_to_token_hash,"
            " parent_message_id)"
            " VALUES (?, ?, ?, 'question', ?, ?, datetime('now'), ?,"
            " ?, ?, ?, ?, ?, ?)",
            (
                installation_id,
                chat_id,
                tg_message_id,
                json.dumps(
                    payload
                    or {
                        "kind": "question",
                        "text": "test?",
                        "keyboard": [[{"label": "Yes", "value": "yes"}]],
                    }
                ),
                state,
                _iso(expires_at),
                nudge_count,
                _iso(next_nudge_at) if next_nudge_at is not None else None,
                nudge_schedule_override,
                _iso(escalate_at) if escalate_at is not None else None,
                escalate_to_token_hash,
                parent_message_id,
            ),
        )
    return int(cur.lastrowid)


def _enable_nudge(
    conn: sqlite3.Connection, chat_id: int, schedule: str = "15m,45m"
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO recipients(telegram_chat_id, nudge_enabled, nudge_schedule,"
            " updated_at)"
            " VALUES (?, 1, ?, datetime('now'))"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            " nudge_enabled=1, nudge_schedule=?, updated_at=datetime('now')",
            (chat_id, schedule, schedule),
        )


def _nudge_off(conn: sqlite3.Connection, chat_id: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO recipients(telegram_chat_id, nudge_enabled, updated_at)"
            " VALUES (?, 0, datetime('now'))"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            " nudge_enabled=0, updated_at=datetime('now')",
            (chat_id,),
        )


def _get_message(conn: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()


# ===========================================================================
# 1. Ladder parsing
# ===========================================================================

class TestLadderParsing:
    def test_plain_no_repeat(self) -> None:
        schedule, repeats = parse_nudge_schedule_with_repeat("15m,45m,3h", 10)
        assert repeats is False
        assert len(schedule) == 3
        assert schedule[0] == timedelta(minutes=15)
        assert schedule[1] == timedelta(minutes=45)
        assert schedule[2] == timedelta(hours=3)

    def test_repeating_tail(self) -> None:
        schedule, repeats = parse_nudge_schedule_with_repeat("4h,1d,3d,7d*", 10)
        assert repeats is True
        assert len(schedule) == 4
        assert schedule[-1] == timedelta(days=7)

    def test_single_repeating(self) -> None:
        schedule, repeats = parse_nudge_schedule_with_repeat("1h*", 10)
        assert repeats is True
        assert schedule == [timedelta(hours=1)]

    def test_star_on_non_last_rung_raises(self) -> None:
        with pytest.raises(ValueError, match="last rung"):
            parse_nudge_schedule_with_repeat("4h*,24h,168h", 10)

    def test_star_on_middle_rung_raises(self) -> None:
        with pytest.raises(ValueError, match="last rung"):
            parse_nudge_schedule_with_repeat("4h,24h*,168h", 10)

    def test_bad_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="bad duration"):
            parse_nudge_schedule_with_repeat("4h,xyz*", 10)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_nudge_schedule_with_repeat("", 10)

    def test_cap_respected(self) -> None:
        with pytest.raises(ValueError, match="cap"):
            parse_nudge_schedule_with_repeat("1h,2h,3h,4h", 3)

    def test_cap_after_stripping_star(self) -> None:
        schedule, repeats = parse_nudge_schedule_with_repeat("1h,2h,3h*", 3)
        assert repeats is True
        assert len(schedule) == 3


# ===========================================================================
# 2. Active-time arithmetic for repeating rung
# ===========================================================================

class TestRepeatingTailArithmetic:
    def test_repeating_fires_in_next_window(self) -> None:
        """advance_active obeys windows even for a repeating rung."""
        # Mon-Fri 09:00-17:00; now is Monday 14:00 (2h left in window).
        # 4h active from Mon 14:00 => uses Mon 14:00-17:00 (3h) + Tue 09:00-10:00 (1h)
        # => lands Tuesday 10:00 (one extra hour past 09:00).
        weekday_windows = [
            Window(weekday=d, start_minute=9 * 60, end_minute=17 * 60)
            for d in range(5)  # Mon-Fri
        ]
        now = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)  # Monday 14:00 UTC
        result = advance_active(now, timedelta(hours=4), "UTC", weekday_windows)
        assert result is not None
        assert result.weekday() == 1  # Tuesday
        assert result.hour == 10

    def test_repeating_rung_past_cap_uses_last_interval(self) -> None:
        from relay_server.reaper import _compute_next_nudge_due
        schedule = [timedelta(hours=1), timedelta(days=1)]
        now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        result = _compute_next_nudge_due(now, schedule, True, 2, "UTC", None)
        assert result is not None
        assert result == now + timedelta(days=1)

    def test_non_repeating_past_cap_returns_none(self) -> None:
        from relay_server.reaper import _compute_next_nudge_due
        schedule = [timedelta(hours=1), timedelta(days=1)]
        now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        result = _compute_next_nudge_due(now, schedule, False, 2, "UTC", None)
        assert result is None


# ===========================================================================
# 3. Nudge pass: override-vs-chat-config and nudge_enabled=0 precedence
# ===========================================================================

def _make_cfg(tmp_path: Path, schedule: str = "15m,45m") -> RelayConfig:
    return RelayConfig(
        db_path=str(tmp_path / "test.db"),
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
        nudge_default_schedule=schedule,
        nudge_max=10,
    )


@pytest.mark.asyncio
async def test_override_nudges_with_nudge_enabled_off(tmp_path: Path) -> None:
    """Message with override schedule nudges even when chat nudges=0."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()
    cfg = _make_cfg(tmp_path)

    inst_id, _ = _insert_installation(conn, chat_id=99)
    _nudge_off(conn, 99)

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id, chat_id=99, tg_message_id=5001,
        nudge_schedule_override="30m,2h*",
        next_nudge_at=now - timedelta(seconds=1),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=now)

    assert len(backend.replies) == 1
    row = _get_message(conn, msg_id)
    assert row["nudge_count"] == 1
    assert row["next_nudge_at"] is not None


@pytest.mark.asyncio
async def test_no_override_obeys_nudge_enabled_off(tmp_path: Path) -> None:
    """Message without override respects nudge_enabled=0."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()
    cfg = _make_cfg(tmp_path)

    inst_id, _ = _insert_installation(conn, chat_id=100)
    _nudge_off(conn, 100)

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id, chat_id=100, tg_message_id=5002,
        next_nudge_at=now - timedelta(seconds=1),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=now)

    assert len(backend.replies) == 0
    assert _get_message(conn, msg_id)["next_nudge_at"] is None


@pytest.mark.asyncio
async def test_override_uses_own_schedule_not_chat(tmp_path: Path) -> None:
    """next_due uses the override schedule, not the chat's ladder."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()
    cfg = _make_cfg(tmp_path)

    inst_id, _ = _insert_installation(conn, chat_id=101)
    _enable_nudge(conn, 101, "15m,45m")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id, chat_id=101, tg_message_id=5003,
        nudge_schedule_override="2h,8h",
        next_nudge_at=now - timedelta(seconds=1),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=now)

    assert len(backend.replies) == 1
    row = _get_message(conn, msg_id)
    next_due = datetime.fromisoformat(row["next_nudge_at"])
    expected = now + timedelta(hours=8)
    assert abs((next_due - expected).total_seconds()) < 60


@pytest.mark.asyncio
async def test_repeating_tail_fires_after_schedule_exhausted(tmp_path: Path) -> None:
    """Repeating tail keeps nudge alive past the finite schedule."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()
    cfg = _make_cfg(tmp_path, "15m")

    inst_id, _ = _insert_installation(conn, chat_id=102)
    _enable_nudge(conn, 102, "15m")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id, chat_id=102, tg_message_id=5004,
        nudge_schedule_override="1h,2h*",
        nudge_count=3,
        next_nudge_at=now - timedelta(seconds=1),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=now)

    assert len(backend.replies) == 1
    row = _get_message(conn, msg_id)
    assert row["nudge_count"] == 4
    assert row["next_nudge_at"] is not None


@pytest.mark.asyncio
async def test_non_repeating_cap_stops_nudging(tmp_path: Path) -> None:
    """Without '*', exhausting the schedule sets next_nudge_at to NULL."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()
    cfg = _make_cfg(tmp_path, "15m")

    inst_id, _ = _insert_installation(conn, chat_id=103)
    _enable_nudge(conn, 103, "15m")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id, chat_id=103, tg_message_id=5005,
        nudge_schedule_override="1h,2h",
        nudge_count=2,
        next_nudge_at=now - timedelta(seconds=1),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=now)

    assert len(backend.replies) == 0
    assert _get_message(conn, msg_id)["next_nudge_at"] is None


# ===========================================================================
# 4. Escalation pass
# ===========================================================================

@pytest.mark.asyncio
async def test_escalation_fires_once(tmp_path: Path) -> None:
    """escalate_at due -> escalation fires and escalate_at is cleared."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=200, label="inst-a")
    _, token_b = _insert_installation(conn, chat_id=201, label="inst-b")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id_a, chat_id=200, tg_message_id=6001,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    assert len(backend.sent) == 1
    assert backend.sent[0].chat_id == 201

    row = _get_message(conn, msg_id)
    assert row["escalate_at"] is None

    children = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (msg_id,)
    ).fetchall()
    assert len(children) == 1
    child = children[0]
    assert child["state"] == "open"
    assert child["installation_id"] == inst_id_a
    assert child["telegram_chat_id"] == 201


@pytest.mark.asyncio
async def test_escalation_fires_only_once_on_multiple_ticks(tmp_path: Path) -> None:
    """escalate_at cleared after first fire; second tick sends nothing new."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=202, label="inst-a2")
    _, token_b = _insert_installation(conn, chat_id=203, label="inst-b2")

    now = _utcnow()
    _insert_message(
        conn, inst_id_a, chat_id=202, tg_message_id=6002,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)
    first_sends = len(backend.sent)

    await reaper_tick(conn, backend, WaiterRegistry(), now=now + timedelta(seconds=31))
    assert len(backend.sent) == first_sends == 1


@pytest.mark.asyncio
async def test_unknown_escalation_token_at_send_time(tmp_path: Path) -> None:
    """POST /v1/messages with an unbound escalation token -> 422."""
    db_path = str(tmp_path / "app.db")
    cfg = make_test_config(db_path)
    db_conn = connect(db_path)
    init_schema(db_conn)
    _, token_a = _insert_installation(db_conn, chat_id=210)
    backend = FakeTelegramBackend()

    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token_a}"},
                json={
                    "kind": "question",
                    "text": "urgent?",
                    "ttl_sec": 60,
                    "keyboard": [[{"label": "Yes", "value": "yes"}]],
                    "escalate_after_sec": 3600,
                    "escalate_to_token": "rly_nonexistent_token_000000000000000000000000",
                },
            )
            assert r.status_code == 422
            assert "escalation_token_not_bound" in r.text


@pytest.mark.asyncio
async def test_escalation_requires_both_fields(tmp_path: Path) -> None:
    """escalate_to_token without escalate_after_sec -> 422 and vice versa."""
    db_path = str(tmp_path / "app2.db")
    cfg = make_test_config(db_path)
    db_conn = connect(db_path)
    init_schema(db_conn)
    _, token_a = _insert_installation(db_conn, chat_id=211)
    backend = FakeTelegramBackend()

    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            base = {
                "kind": "question",
                "text": "q?",
                "ttl_sec": 60,
                "keyboard": [[{"label": "Y", "value": "y"}]],
            }
            r = await client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token_a}"},
                json={**base, "escalate_to_token": "rly_abc"},
            )
            assert r.status_code == 422

            r2 = await client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token_a}"},
                json={**base, "escalate_after_sec": 3600},
            )
            assert r2.status_code == 422


@pytest.mark.asyncio
async def test_answer_from_escalated_copy_attributes_to_original(
    tmp_path: Path,
) -> None:
    """Answering the escalated copy marks the original as answered."""
    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    init_schema(conn)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=220, label="inst-a3")
    _, token_b = _insert_installation(conn, chat_id=221, label="inst-b3")

    now = _utcnow()
    parent_id = _insert_message(
        conn, inst_id_a, chat_id=220, tg_message_id=7001,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    children = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (parent_id,)
    ).fetchall()
    assert len(children) == 1
    child = children[0]
    child_id = int(child["id"])
    child_tg_msg_id = int(child["telegram_message_id"])

    cfg = RelayConfig(
        db_path=db_path,
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
    )
    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            uid = next(_update_id_seq)
            r = await client.post(
                f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
                json={
                    "update_id": uid,
                    "callback_query": {
                        "id": f"cbq-{uid}",
                        "from": {"id": 7},
                        "data": encode_cb(child_id, 0),
                        "message": {
                            "message_id": child_tg_msg_id,
                            "chat": {"id": 221},
                        },
                    },
                },
            )
            assert r.status_code == 200

    assert _get_message(conn, parent_id)["state"] == "answered"
    assert _get_message(conn, child_id)["state"] == "cancelled"


@pytest.mark.asyncio
async def test_duplicate_not_in_target_installation_feed(tmp_path: Path) -> None:
    """Escalated copy has installation_id=original, never appears in B's feed."""
    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    init_schema(conn)
    backend = FakeTelegramBackend()

    inst_id_a, token_a = _insert_installation(conn, chat_id=230, label="inst-a4")
    inst_id_b, token_b = _insert_installation(conn, chat_id=231, label="inst-b4")

    hash_b = conn.execute(
        "SELECT token_hash FROM installations WHERE id=?", (inst_id_b,)
    ).fetchone()["token_hash"]

    now = _utcnow()
    parent_id = _insert_message(
        conn, inst_id_a, chat_id=230, tg_message_id=8001,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_b,
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    child = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (parent_id,)
    ).fetchone()
    assert child is not None
    assert child["installation_id"] == inst_id_a
    assert child["installation_id"] != inst_id_b

    # Force-answer the child so the feed has an answered row to query.
    with conn:
        conn.execute(
            "UPDATE messages SET state='answered',"
            " answer_json=? WHERE id=?",
            (
                json.dumps({"value": "yes", "answered_at": now.isoformat()}),
                int(child["id"]),
            ),
        )

    # Verify GET /v1/answers as installation B returns nothing — the child
    # carries installation_id = inst_a, not inst_b.
    cfg = RelayConfig(
        db_path=db_path,
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
    )
    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r = await client.get(
                "/v1/answers",
                headers={"Authorization": f"Bearer {token_b}"},
                params={"after": 0},
            )
            # 204 = no content; 200 with [] = empty list; neither should surface
            # the child row whose installation_id belongs to installation A.
            assert r.status_code in (200, 204)
            if r.status_code == 200:
                assert r.json() == []


@pytest.mark.asyncio
async def test_answering_original_cancels_escalated_copy(tmp_path: Path) -> None:
    """Answering the original cancels the escalated copy."""
    db_path = str(tmp_path / "test.db")
    conn = connect(db_path)
    init_schema(conn)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=240, label="inst-a5")
    _, token_b = _insert_installation(conn, chat_id=241, label="inst-b5")

    now = _utcnow()
    parent_id = _insert_message(
        conn, inst_id_a, chat_id=240, tg_message_id=9001,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    child = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (parent_id,)
    ).fetchone()
    assert child is not None
    child_id = int(child["id"])

    cfg = RelayConfig(
        db_path=db_path,
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
    )
    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            uid = next(_update_id_seq)
            r = await client.post(
                f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
                json={
                    "update_id": uid,
                    "callback_query": {
                        "id": f"cbq-{uid}",
                        "from": {"id": 7},
                        "data": encode_cb(parent_id, 0),
                        "message": {
                            "message_id": 9001,
                            "chat": {"id": 240},
                        },
                    },
                },
            )
            assert r.status_code == 200

    assert _get_message(conn, parent_id)["state"] == "answered"
    assert _get_message(conn, child_id)["state"] == "cancelled"


# ===========================================================================
# 5. Full reaper tick with all four passes
# ===========================================================================

@pytest.mark.asyncio
async def test_full_reaper_tick_all_passes(tmp_path: Path) -> None:
    """One tick exercises expiry, cleanup, nudge, and escalation passes."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()
    cfg = RelayConfig(
        db_path=str(tmp_path / "test.db"),
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
        nudge_default_schedule="15m,45m",
        nudge_max=10,
    )

    # Row 1: expired
    inst_id, _ = _insert_installation(conn, chat_id=300)
    expired_id = _insert_message(
        conn, inst_id, chat_id=300, tg_message_id=301,
        expires_at=_utcnow() - timedelta(hours=2),
    )

    # Row 2: nudge due
    inst_id2, _ = _insert_installation(conn, chat_id=301, label="inst-nudge")
    _enable_nudge(conn, 301, "15m")
    now = _utcnow()
    nudge_id = _insert_message(
        conn, inst_id2, chat_id=301, tg_message_id=302,
        nudge_schedule_override="30m*",
        next_nudge_at=now - timedelta(seconds=1),
    )

    # Row 3: escalation due
    inst_id3, _ = _insert_installation(conn, chat_id=302, label="inst-esc")
    _, token_b = _insert_installation(conn, chat_id=303, label="inst-esc-b")
    esc_id = _insert_message(
        conn, inst_id3, chat_id=302, tg_message_id=303,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    # Row 4: open, no action
    open_id = _insert_message(
        conn, inst_id, chat_id=300, tg_message_id=304,
        expires_at=_utcnow() + timedelta(hours=1),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), cfg, now=now)

    assert _get_message(conn, expired_id)["state"] == "expired"
    assert len(backend.replies) >= 1
    assert _get_message(conn, nudge_id)["nudge_count"] == 1
    assert len(conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (esc_id,)
    ).fetchall()) == 1
    assert _get_message(conn, open_id)["state"] == "open"


# ===========================================================================
# 6. Regression floor -- invariant 9
# ===========================================================================

@pytest.mark.asyncio
async def test_regression_floor_null_columns(tmp_path: Path) -> None:
    """Existing rows have NULL for all three new columns."""
    conn = _setup_db(tmp_path)
    inst_id, _ = _insert_installation(conn, chat_id=400)
    msg_id = _insert_message(conn, inst_id, chat_id=400, tg_message_id=401)
    row = _get_message(conn, msg_id)
    assert row["nudge_schedule_override"] is None
    assert row["escalate_at"] is None
    assert row["escalate_to_token_hash"] is None
    assert row["parent_message_id"] is None


@pytest.mark.asyncio
async def test_regression_floor_api_no_new_columns(tmp_path: Path) -> None:
    """POST /v1/messages without new fields: all three columns remain NULL."""
    db_path = str(tmp_path / "app.db")
    cfg = make_test_config(db_path)
    db_conn = connect(db_path)
    init_schema(db_conn)
    _, token = _insert_installation(db_conn, chat_id=401)
    backend = FakeTelegramBackend()

    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "kind": "question",
                    "text": "hello?",
                    "ttl_sec": 60,
                    "keyboard": [[{"label": "Yes", "value": "yes"}]],
                },
            )
            assert r.status_code == 200
            msg_id = r.json()["message_id"]

    row = _get_message(db_conn, msg_id)
    assert row["nudge_schedule_override"] is None
    assert row["escalate_at"] is None
    assert row["escalate_to_token_hash"] is None
    assert row["parent_message_id"] is None


# ===========================================================================
# 7. Migration: v4 -> v5
# ===========================================================================

def _build_v4_db(path: Path) -> None:
    """Write a v4 schema DB at path."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (4);

        CREATE TABLE installations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            telegram_chat_id INTEGER,
            bound_user_id INTEGER,
            created_at TIMESTAMP NOT NULL,
            last_seen_at TIMESTAMP,
            revoked_at TIMESTAMP
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id INTEGER NOT NULL REFERENCES installations(id),
            telegram_chat_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            answer_json TEXT,
            created_at TIMESTAMP NOT NULL,
            answered_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            nudge_count INTEGER NOT NULL DEFAULT 0,
            next_nudge_at TIMESTAMP,
            nudge_tg_message_id INTEGER,
            render_dirty INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS messages_state_expiry ON messages(state, expires_at);
        CREATE INDEX IF NOT EXISTS messages_nudge_due ON messages(state, next_nudge_at);
        CREATE INDEX IF NOT EXISTS messages_render_dirty ON messages(render_dirty) WHERE render_dirty = 1;
        CREATE INDEX IF NOT EXISTS messages_answer_feed ON messages(telegram_chat_id, state, id);

        CREATE TABLE recipients (
            telegram_chat_id INTEGER PRIMARY KEY,
            tz TEXT,
            windows_json TEXT,
            nudge_enabled INTEGER NOT NULL DEFAULT 0,
            nudge_schedule TEXT,
            updated_at TIMESTAMP NOT NULL
        );

        CREATE TABLE binding_codes (
            code TEXT PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installations(id),
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP,
            bound_chat_id INTEGER,
            bound_user_id INTEGER
        );

        CREATE TABLE idempotency_keys (
            key TEXT NOT NULL,
            installation_id INTEGER NOT NULL,
            request_hash TEXT,
            response_json TEXT,
            created_at TIMESTAMP NOT NULL,
            PRIMARY KEY (installation_id, key)
        );

        INSERT INTO installations(label, token_hash, telegram_chat_id, bound_user_id, created_at)
            VALUES ('test', 'testhash', 42, 7, datetime('now'));
        INSERT INTO messages(installation_id, telegram_chat_id, telegram_message_id,
            kind, payload_json, state, created_at, expires_at)
            VALUES (1, 42, 1000, 'question', '{}', 'open', datetime('now'),
            '9999-12-31T00:00:00+00:00');
    """)
    conn.close()


def test_v4_migrates_to_v5_cleanly(tmp_path: Path) -> None:
    """v4 DB migrates to v5; new columns added; existing row stays NULL."""
    db_path = tmp_path / "v4.db"
    _build_v4_db(db_path)

    conn = connect(str(db_path))
    init_schema(conn)

    assert get_schema_version(conn) == 5

    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for col in ("nudge_schedule_override", "escalate_at", "escalate_to_token_hash",
                "parent_message_id"):
        assert col in cols, f"missing column: {col}"

    idx_names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "messages_escalation_due" in idx_names

    row = conn.execute("SELECT * FROM messages WHERE id=1").fetchone()
    assert row is not None
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM messages WHERE id=1").fetchone()
    assert row["nudge_schedule_override"] is None
    assert row["escalate_at"] is None
    assert row["escalate_to_token_hash"] is None
    assert row["parent_message_id"] is None
    conn.close()


def test_v4_v5_schema_columns_match(tmp_path: Path) -> None:
    """Fresh v5 schema and migrated v4->v5 expose identical message columns."""
    fresh = connect(str(tmp_path / "fresh.db"))
    init_schema(fresh)
    fresh_cols = {r[1] for r in fresh.execute("PRAGMA table_info(messages)").fetchall()}

    v4_path = tmp_path / "v4b.db"
    _build_v4_db(v4_path)
    migrated = connect(str(v4_path))
    init_schema(migrated)
    migrated_cols = {r[1] for r in migrated.execute("PRAGMA table_info(messages)").fetchall()}

    assert fresh_cols == migrated_cols


# ===========================================================================
# 8. Per-message nudge schedule via the API
# ===========================================================================

@pytest.mark.asyncio
async def test_api_nudge_schedule_override_seeded(tmp_path: Path) -> None:
    """Message with nudge_schedule gets next_nudge_at seeded even if chat nudges=0."""
    db_path = str(tmp_path / "app.db")
    cfg = RelayConfig(
        db_path=db_path,
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
        nudge_default_schedule="15m",
        nudge_max=10,
    )
    db_conn = connect(db_path)
    init_schema(db_conn)
    _, token = _insert_installation(db_conn, chat_id=500)
    _nudge_off(db_conn, 500)
    backend = FakeTelegramBackend()

    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "kind": "question",
                    "text": "nudge me?",
                    "ttl_sec": 60,
                    "keyboard": [[{"label": "Yes", "value": "yes"}]],
                    "nudge_schedule": "30m,2h*",
                },
            )
            assert r.status_code == 200
            msg_id = r.json()["message_id"]

    row = _get_message(db_conn, msg_id)
    assert row["nudge_schedule_override"] == "30m,2h*"
    assert row["next_nudge_at"] is not None


@pytest.mark.asyncio
async def test_api_invalid_nudge_schedule_rejected(tmp_path: Path) -> None:
    """Invalid nudge_schedule (star not on last rung) in POST -> 422."""
    db_path = str(tmp_path / "app.db")
    cfg = make_test_config(db_path)
    db_conn = connect(db_path)
    init_schema(db_conn)
    _, token = _insert_installation(db_conn, chat_id=501)
    backend = FakeTelegramBackend()

    app = create_app(backend=backend, config=cfg)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "kind": "question",
                    "text": "bad nudge",
                    "ttl_sec": 60,
                    "keyboard": [[{"label": "Y", "value": "y"}]],
                    "nudge_schedule": "4h*,24h",
                },
            )
            assert r.status_code == 422
            assert "nudge_schedule" in r.text


# ===========================================================================
# 9. Expiry → child cancellation (Issue 3B)
# ===========================================================================

@pytest.mark.asyncio
async def test_expiry_cancels_escalated_child(tmp_path: Path) -> None:
    """When a parent expires, its open escalated child is cancelled."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=600, label="inst-a-exp")
    _, token_b = _insert_installation(conn, chat_id=601, label="inst-b-exp")

    now = _utcnow()
    # Parent has already expired.
    parent_id = _insert_message(
        conn, inst_id_a, chat_id=600, tg_message_id=6100,
        escalate_to_token_hash=hash_token(token_b),
        expires_at=now - timedelta(seconds=1),
    )
    # Pre-insert a child to simulate a prior successful escalation.
    child_id = _insert_message(
        conn, inst_id_a, chat_id=601, tg_message_id=6101,
        parent_message_id=parent_id,
        expires_at=datetime(9999, 12, 31, tzinfo=UTC),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    assert _get_message(conn, parent_id)["state"] == "expired"
    assert _get_message(conn, child_id)["state"] == "cancelled"


@pytest.mark.asyncio
async def test_expiry_cancels_unsent_child(tmp_path: Path) -> None:
    """A child with tg_msg_id=0 (send failed) is also cancelled when parent expires."""
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=602, label="inst-a-exp2")
    _, token_b = _insert_installation(conn, chat_id=603, label="inst-b-exp2")

    now = _utcnow()
    parent_id = _insert_message(
        conn, inst_id_a, chat_id=602, tg_message_id=6200,
        escalate_to_token_hash=hash_token(token_b),
        expires_at=now - timedelta(seconds=1),
    )
    # Child with tg_msg_id=0: a previously failed send left it dangling.
    child_id = _insert_message(
        conn, inst_id_a, chat_id=603, tg_message_id=0,
        parent_message_id=parent_id,
        expires_at=datetime(9999, 12, 31, tzinfo=UTC),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    assert _get_message(conn, parent_id)["state"] == "expired"
    assert _get_message(conn, child_id)["state"] == "cancelled"
    # No edit_message attempted for the unsent child (tg_msg_id=0).
    edit_calls = [c for c in backend.calls if c.method == "edit_message"
                  and c.kwargs.get("telegram_message_id") == 0]
    assert edit_calls == [], "Should not attempt edit for unsent child"


# ===========================================================================
# 10. Idempotent escalation (Issue 3A)
# ===========================================================================

@pytest.mark.asyncio
async def test_escalation_send_failure_leaves_escalate_at_set(tmp_path: Path) -> None:
    """A transient send failure leaves escalate_at set so the next tick retries."""
    conn = _setup_db(tmp_path)

    class FailingBackend(FakeTelegramBackend):
        async def send_message(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("Telegram unavailable")

    backend = FailingBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=700, label="inst-a-fail")
    _, token_b = _insert_installation(conn, chat_id=701, label="inst-b-fail")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id_a, chat_id=700, tg_message_id=7000,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    # escalate_at must still be set — not silently dropped on failure.
    assert _get_message(conn, msg_id)["escalate_at"] is not None

    # A placeholder child row should exist (inserted before the failed send).
    children = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (msg_id,)
    ).fetchall()
    assert len(children) == 1
    assert int(children[0]["telegram_message_id"]) == 0


@pytest.mark.asyncio
async def test_escalation_retry_on_second_tick_succeeds(tmp_path: Path) -> None:
    """After a failed send, the next tick retries and clears escalate_at exactly once."""
    conn = _setup_db(tmp_path)

    attempt = {"n": 0}

    class FirstFailBackend(FakeTelegramBackend):
        async def send_message(self, **kwargs):  # type: ignore[override]
            if attempt["n"] == 0:
                attempt["n"] += 1
                raise RuntimeError("first attempt fails")
            return await super().send_message(**kwargs)

    backend = FirstFailBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=710, label="inst-a-retry")
    _, token_b = _insert_installation(conn, chat_id=711, label="inst-b-retry")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id_a, chat_id=710, tg_message_id=7100,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    # First tick — send fails; escalate_at stays set.
    await reaper_tick(conn, backend, WaiterRegistry(), now=now)
    assert _get_message(conn, msg_id)["escalate_at"] is not None

    first_children = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (msg_id,)
    ).fetchall()
    assert len(first_children) == 1
    child_id_first = int(first_children[0]["id"])
    assert int(first_children[0]["telegram_message_id"]) == 0

    # Second tick — send succeeds.
    await reaper_tick(
        conn, backend, WaiterRegistry(), now=now + timedelta(seconds=31)
    )

    assert _get_message(conn, msg_id)["escalate_at"] is None

    children = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (msg_id,)
    ).fetchall()
    # Exactly one child row — no duplicate insertion.
    assert len(children) == 1
    assert int(children[0]["id"]) == child_id_first
    assert int(children[0]["telegram_message_id"]) != 0

    # Exactly one Telegram message sent (the successful retry).
    assert len(backend.sent) == 1


@pytest.mark.asyncio
async def test_escalation_no_second_send_after_crash_simulation(
    tmp_path: Path,
) -> None:
    """Simulated crash: child with real tg_msg_id exists but escalate_at not yet cleared.

    The next tick detects tg_msg_id != 0 on the existing child and only clears
    escalate_at — no second send is issued.
    """
    conn = _setup_db(tmp_path)
    backend = FakeTelegramBackend()

    inst_id_a, _ = _insert_installation(conn, chat_id=720, label="inst-a-crash")
    _, token_b = _insert_installation(conn, chat_id=721, label="inst-b-crash")

    now = _utcnow()
    msg_id = _insert_message(
        conn, inst_id_a, chat_id=720, tg_message_id=7200,
        escalate_at=now - timedelta(seconds=1),
        escalate_to_token_hash=hash_token(token_b),
    )

    # Simulate: the send already happened (child has a real tg_msg_id) but
    # escalate_at was NOT cleared yet (crash between send and DB commit).
    child_id = _insert_message(
        conn, inst_id_a, chat_id=721, tg_message_id=9999,
        parent_message_id=msg_id,
        expires_at=datetime(9999, 12, 31, tzinfo=UTC),
    )

    # Tick should detect the existing sent child and only clear escalate_at.
    await reaper_tick(conn, backend, WaiterRegistry(), now=now)

    assert _get_message(conn, msg_id)["escalate_at"] is None
    assert len(backend.sent) == 0  # No second send.

    children = conn.execute(
        "SELECT * FROM messages WHERE parent_message_id = ?", (msg_id,)
    ).fetchall()
    assert len(children) == 1  # Still exactly one child.
    assert int(children[0]["id"]) == child_id


# ===========================================================================
# 11. parse_duration day support (Issue 1 verification)
# ===========================================================================

class TestParseDurationDaySupport:
    def test_spec_default_ladder_parseable(self) -> None:
        """The epic's documented default '4h,1d,3d,7d*' parses without error."""
        schedule, repeats = parse_nudge_schedule_with_repeat("4h,1d,3d,7d*", 10)
        assert repeats is True
        assert len(schedule) == 4
        assert schedule[0] == timedelta(hours=4)
        assert schedule[1] == timedelta(days=1)
        assert schedule[2] == timedelta(days=3)
        assert schedule[3] == timedelta(days=7)

    def test_day_only(self) -> None:
        from relay_server.availability import parse_duration
        td = parse_duration("1d")
        assert td == timedelta(days=1)

    def test_day_and_hours(self) -> None:
        from relay_server.availability import parse_duration
        td = parse_duration("2d12h")
        assert td == timedelta(days=2, hours=12)

    def test_day_hours_minutes(self) -> None:
        from relay_server.availability import parse_duration
        td = parse_duration("1d6h30m")
        assert td == timedelta(days=1, hours=6, minutes=30)

    def test_existing_formats_unchanged(self) -> None:
        """Previously valid forms (h, m, hm) still parse correctly."""
        from relay_server.availability import parse_duration
        assert parse_duration("15m") == timedelta(minutes=15)
        assert parse_duration("3h") == timedelta(hours=3)
        assert parse_duration("2h30m") == timedelta(hours=2, minutes=30)

    def test_existing_invalid_still_invalid(self) -> None:
        from relay_server.availability import parse_duration
        assert parse_duration("xyz") is None
        assert parse_duration("") is None
        assert parse_duration("0d") is None

    def test_week_three_nudge_still_fires(self) -> None:
        """A repeating 7d tail means the message still nudges in week three."""
        from relay_server.reaper import _compute_next_nudge_due
        schedule = [timedelta(hours=4), timedelta(days=1),
                    timedelta(days=3), timedelta(days=7)]
        now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        # nudge_count=15 (well past the 4-rung schedule) — repeating tail fires.
        result = _compute_next_nudge_due(now, schedule, True, 15, "UTC", None)
        assert result == now + timedelta(days=7)
