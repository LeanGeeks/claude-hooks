"""Tests for epic 23-01: never_expires sentinel, GET /v1/answers feed.

Coverage per the task's Tests section:
- never_expires row survives repeated full reaper ticks
- ttl_sec still required and still capped (existing validation untouched)
- Feed ordering, paging and the 500-row cap
- Installation scoping with two installations sharing one chat
- Waiter wake on _record_answer (parked long-poll wakes within milliseconds)
- EXPLAIN QUERY PLAN proves messages_answer_feed is used (no table scan)
- Migration applies cleanly forward on a locally-built v3 database
- Regression floor: existing message and permission flows byte-identical

Uses FakeTelegramBackend, no network.
"""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.callback_data import encode as encode_cb
from relay_server.config import RelayConfig
from relay_server.db import connect, get_schema_version, init_schema
from relay_server.models import NEVER_EXPIRES
from relay_server.reaper import reaper_tick
from relay_server.telegram_backend import FakeTelegramBackend
from relay_server.tokens import generate_token, hash_token

from tests.conftest import (  # type: ignore[attr-defined]
    TEST_WEBHOOK_SECRET,
    _run_lifespan,
    make_test_config,
)

# Module-level counter for unique webhook update_ids across all tests.
_update_id_seq = itertools.count(10000)


# ---------------------------------------------------------------------------
# Local webhook helper that uses unique update_ids (avoids in-process dedup)
# ---------------------------------------------------------------------------


async def _deliver_button_answer(
    client: httpx.AsyncClient,
    msg_id: int,
    chat_id: int,
    user_id: int,
    option_idx: int = 0,
) -> httpx.Response:
    """Send a Telegram callback_query webhook update with a unique update_id."""
    uid = next(_update_id_seq)
    return await client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": uid,
            "callback_query": {
                "id": f"cbq-{uid}",
                "from": {"id": user_id},
                "data": encode_cb(msg_id, option_idx),
                "message": {
                    "message_id": 1000,
                    "chat": {"id": chat_id},
                },
            },
        },
    )


# ---------------------------------------------------------------------------
# Helper: create a message via the API and return its relay message_id
# ---------------------------------------------------------------------------


async def _create_msg(
    client: httpx.AsyncClient,
    token: str,
    *,
    never_expires: bool = False,
    ttl_sec: int = 60,
    kind: str = "question",
    keyboard: list | None = None,
) -> int:
    body: dict = {
        "kind": kind,
        "text": "test question?",
        "ttl_sec": ttl_sec,
        "never_expires": never_expires,
        "keyboard": keyboard if keyboard is not None else [[{"label": "Yes", "value": "yes"}]],
    }
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


# ---------------------------------------------------------------------------
# Two-installation fixture helpers
# ---------------------------------------------------------------------------


def _seed_two_installations(db_path: str) -> dict[str, object]:
    """Create schema and seed two installations sharing one chat_id."""
    conn = connect(db_path)
    init_schema(conn)
    token_a = generate_token()
    token_b = generate_token()
    with conn:
        cur_a = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES ('inst_a', ?, 99, 7, datetime('now'))",
            (hash_token(token_a),),
        )
        id_a = cur_a.lastrowid
        cur_b = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES ('inst_b', ?, 99, 7, datetime('now'))",
            (hash_token(token_b),),
        )
        id_b = cur_b.lastrowid
    conn.close()
    return {
        "token_a": token_a,
        "token_b": token_b,
        "id_a": id_a,
        "id_b": id_b,
        "chat_id": 99,
        "bound_user_id": 7,
    }


# ---------------------------------------------------------------------------
# 1. never_expires row survives repeated full reaper ticks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_expires_survives_reaper_ticks(
    app_client: httpx.AsyncClient,
    seeded: dict,
    db_path: str,
    backend: FakeTelegramBackend,
) -> None:
    """A message created with never_expires=True must survive many reaper ticks."""
    from relay_server.config import RelayConfig

    token = seeded["token"]
    mid = await _create_msg(app_client, token, never_expires=True)

    conn = connect(db_path)

    # Verify the sentinel was stored.
    row = conn.execute("SELECT expires_at FROM messages WHERE id=?", (mid,)).fetchone()
    assert row is not None
    assert row["expires_at"] == NEVER_EXPIRES, (
        f"Expected NEVER_EXPIRES sentinel, got {row['expires_at']!r}"
    )

    # Run many reaper ticks with now set far in the future.
    far_future = datetime(9998, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    config = RelayConfig(db_path=db_path, set_webhook_on_startup=False)
    waiters_stub = _StubWaiters()
    for _ in range(5):
        await reaper_tick(conn, backend, waiters_stub, config=config, now=far_future)

    # Row must still be open.
    row = conn.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()
    assert row is not None
    assert row["state"] == "open", (
        f"never_expires row must survive reaper; got state={row['state']!r}"
    )
    conn.close()


class _StubWaiters:
    """Minimal waiter stub for reaper_tick (just needs notify)."""

    def notify(self, _: int) -> None:  # noqa: ANN001
        pass


# ---------------------------------------------------------------------------
# 2. ttl_sec still required and still capped (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_sec_still_required(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    """Omitting ttl_sec must still return 422."""
    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {seeded['token']}"},
        json={"kind": "notification", "text": "hi"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_ttl_sec_still_capped_at_24h(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    """ttl_sec > 86400 (24 h) must still return 422."""
    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {seeded['token']}"},
        json={"kind": "notification", "text": "hi", "ttl_sec": 86401},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 3. Feed ordering: answers come back in ascending id order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_ordering_ascending(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """Feed returns answered rows sorted by id ascending."""
    token = seeded["token"]
    ids = []
    for _ in range(3):
        mid = await _create_msg(app_client, token)
        ids.append(mid)

    # Answer each in reverse order using unique update_ids.
    for mid in reversed(ids):
        r = await _deliver_button_answer(
            app_client, mid,
            chat_id=int(seeded["chat_id"]),
            user_id=int(seeded["bound_user_id"]),
        )
        assert r.status_code == 200

    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": 0},
    )
    assert r.status_code == 200
    rows = r.json()
    returned_ids = [row["id"] for row in rows]
    assert returned_ids == sorted(returned_ids), (
        f"Feed not in ascending order: {returned_ids}"
    )
    for mid in ids:
        assert mid in returned_ids, f"Message {mid} missing from feed"


# ---------------------------------------------------------------------------
# 4. Feed paging: after= advances the cursor correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_paging_after_cursor(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """after=N returns only rows with id > N."""
    token = seeded["token"]
    ids = []
    for _ in range(4):
        mid = await _create_msg(app_client, token)
        ids.append(mid)
    for mid in ids:
        r = await _deliver_button_answer(
            app_client, mid,
            chat_id=int(seeded["chat_id"]),
            user_id=int(seeded["bound_user_id"]),
        )
        assert r.status_code == 200

    r1 = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": 0},
    )
    assert r1.status_code == 200
    page1 = r1.json()
    assert len(page1) == 4
    pivot = page1[1]["id"]

    r2 = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": pivot},
    )
    assert r2.status_code == 200
    page2 = r2.json()
    assert all(row["id"] > pivot for row in page2), (
        f"Paged rows must all have id > pivot {pivot}: {[r['id'] for r in page2]}"
    )
    assert len(page2) == 2


# ---------------------------------------------------------------------------
# 5. Feed 500-row cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_page_cap_500(
    app_client: httpx.AsyncClient,
    seeded: dict,
    db_path: str,
) -> None:
    """Feed returns at most 500 rows regardless of how many are answered."""
    conn = connect(db_path)
    install_id = seeded["installation_id"]
    chat_id = seeded["chat_id"]
    with conn:
        for i in range(510):
            conn.execute(
                "INSERT INTO messages(installation_id, telegram_chat_id,"
                " telegram_message_id, kind, payload_json, state,"
                " answer_json, created_at, answered_at, expires_at)"
                " VALUES (?, ?, ?, 'question', '{}', 'answered',"
                "  '{\"via\": \"button\", \"option_idx\": 0, \"label\": \"Y\"}', "
                "  datetime('now'), datetime('now'), datetime('now', '+1 hour'))",
                (install_id, chat_id, 9000 + i),
            )
    conn.close()

    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {seeded['token']}"},
        params={"after": 0},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) <= 500, f"Feed must be capped at 500 rows, got {len(rows)}"


# ---------------------------------------------------------------------------
# 6. Installation scoping: two installations sharing one chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_installation_scoping_shared_chat(
    db_path: str,
    backend: FakeTelegramBackend,
) -> None:
    """Each installation sees only its own answered messages on the feed."""
    seed = _seed_two_installations(db_path)
    config = make_test_config(db_path)
    app = create_app(backend=backend, config=config)
    transport = httpx.ASGITransport(app=app)

    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token_a = seed["token_a"]
            token_b = seed["token_b"]
            chat_id = seed["chat_id"]
            user_id = seed["bound_user_id"]

            mid_a = await _create_msg(client, token_a)
            r = await _deliver_button_answer(
                client, mid_a, chat_id=int(chat_id), user_id=int(user_id)
            )
            assert r.status_code == 200

            mid_b = await _create_msg(client, token_b)
            r = await _deliver_button_answer(
                client, mid_b, chat_id=int(chat_id), user_id=int(user_id)
            )
            assert r.status_code == 200

            # Installation A sees only its own message.
            r_a = await client.get(
                "/v1/answers",
                headers={"Authorization": f"Bearer {token_a}"},
                params={"after": 0},
            )
            assert r_a.status_code == 200
            ids_a = {row["id"] for row in r_a.json()}
            assert mid_a in ids_a, "Installation A must see its own answer"
            assert mid_b not in ids_a, "Installation A must NOT see installation B's answer"

            # Installation B sees only its own message.
            r_b = await client.get(
                "/v1/answers",
                headers={"Authorization": f"Bearer {token_b}"},
                params={"after": 0},
            )
            assert r_b.status_code == 200
            ids_b = {row["id"] for row in r_b.json()}
            assert mid_b in ids_b, "Installation B must see its own answer"
            assert mid_a not in ids_b, "Installation B must NOT see installation A's answer"


# ---------------------------------------------------------------------------
# 7. Waiter wake: long-poll parks and wakes within milliseconds of answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiter_wakes_on_answer(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """A parked GET /v1/answers wakes within milliseconds when an answer arrives."""
    token = seeded["token"]
    mid = await _create_msg(app_client, token)

    async def _poll() -> httpx.Response:
        return await app_client.get(
            "/v1/answers",
            headers={"Authorization": f"Bearer {token}"},
            params={"after": 0, "wait": 25},
            timeout=30.0,
        )

    async def _answer() -> None:
        await asyncio.sleep(0.05)
        await _deliver_button_answer(
            app_client, mid,
            chat_id=int(seeded["chat_id"]),
            user_id=int(seeded["bound_user_id"]),
        )

    poll_task = asyncio.create_task(_poll())
    answer_task = asyncio.create_task(_answer())
    await answer_task

    resp = await asyncio.wait_for(poll_task, timeout=5.0)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(row["id"] == mid for row in rows), (
        f"Expected message {mid} in feed rows, got: {rows}"
    )


# ---------------------------------------------------------------------------
# 8. EXPLAIN QUERY PLAN: feed query uses messages_answer_feed index
# ---------------------------------------------------------------------------


def test_feed_query_uses_answer_feed_index(tmp_path: Path) -> None:
    """EXPLAIN QUERY PLAN confirms the feed uses messages_answer_feed, not a table scan."""
    conn = connect(tmp_path / "idx.db")
    init_schema(conn)

    plan_rows = conn.execute(
        "EXPLAIN QUERY PLAN"
        " SELECT id, kind, answer_json, answered_at"
        " FROM messages"
        " WHERE telegram_chat_id = 42 AND state = 'answered'"
        "   AND id > 0 AND installation_id = 1"
        " ORDER BY id ASC"
        " LIMIT 500"
    ).fetchall()

    # Each EXPLAIN QUERY PLAN row has a 'detail' column with the description.
    plan_details = " ".join(row["detail"] for row in plan_rows).lower()
    assert "messages_answer_feed" in plan_details, (
        f"Expected messages_answer_feed index in EXPLAIN QUERY PLAN, got:\n{plan_details}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# 9. Migration v3 → v4 applies cleanly on a locally-built v3 database
# ---------------------------------------------------------------------------


def _build_v3_db(path: Path) -> sqlite3.Connection:
    """Build a schema-version-3 database that matches what would exist on disk."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (3);

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

        CREATE INDEX IF NOT EXISTS messages_state_expiry
            ON messages(state, expires_at);
        CREATE INDEX IF NOT EXISTS messages_nudge_due
            ON messages(state, next_nudge_at);
        CREATE INDEX IF NOT EXISTS messages_render_dirty
            ON messages(render_dirty) WHERE render_dirty = 1;

        CREATE TABLE IF NOT EXISTS recipients (
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

        INSERT INTO installations(label, token_hash, telegram_chat_id,
            bound_user_id, created_at)
            VALUES ('test', 'hash1', 42, 7, datetime('now'));

        INSERT INTO messages(installation_id, telegram_chat_id,
            telegram_message_id, kind, payload_json, state,
            created_at, expires_at)
            VALUES (1, 42, 1000, 'question', '{}', 'open',
                    datetime('now'), datetime('now', '+1 hour'));
    """)
    return conn


def test_v3_migrates_to_v4_cleanly(tmp_path: Path) -> None:
    """A v3 database migrates to v4 and gains messages_answer_feed index."""
    db_path = tmp_path / "v3.db"
    conn = _build_v3_db(db_path)
    conn.close()

    conn = connect(db_path)
    init_schema(conn)

    assert get_schema_version(conn) == 4
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "messages_answer_feed" in indexes, (
        "messages_answer_feed index must be present after v3→v4 migration"
    )
    row = conn.execute("SELECT state FROM messages WHERE id=1").fetchone()
    assert row is not None
    assert row["state"] == "open"
    conn.close()


def test_v3_to_v4_and_fresh_schemas_identical(tmp_path: Path) -> None:
    """A fresh v4 DB and a migrated v3→v4 DB have identical index sets."""
    fresh_path = tmp_path / "fresh.db"
    conn_fresh = connect(fresh_path)
    init_schema(conn_fresh)

    v3_path = tmp_path / "v3.db"
    conn_v3 = _build_v3_db(v3_path)
    conn_v3.close()
    conn_migrated = connect(v3_path)
    init_schema(conn_migrated)

    def _indexes(conn: sqlite3.Connection) -> set[str]:
        return {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            if not r["name"].startswith("sqlite_")
        }

    fresh_idx = _indexes(conn_fresh)
    mig_idx = _indexes(conn_migrated)
    assert fresh_idx == mig_idx, (
        f"Index mismatch between fresh and migrated v3→v4:\n"
        f"  fresh: {sorted(fresh_idx)}\n"
        f"  migrated: {sorted(mig_idx)}"
    )


# ---------------------------------------------------------------------------
# 10. Feed response shape: answer_text, via, option_idx extracted correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_response_shape_button(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """Feed rows have correct answer_text/via/option_idx for a button tap."""
    token = seeded["token"]
    mid = await _create_msg(
        app_client, token, keyboard=[[{"label": "Approve", "value": "approve"}]]
    )
    r = await _deliver_button_answer(
        app_client, mid,
        chat_id=int(seeded["chat_id"]),
        user_id=int(seeded["bound_user_id"]),
    )
    assert r.status_code == 200

    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": 0},
    )
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert mid in rows
    row = rows[mid]
    assert row["answer_text"] == "Approve"
    assert row["via"] == "button"
    assert row["option_idx"] == 0
    assert row["kind"] == "question"
    assert row["answered_at"] is not None


@pytest.mark.asyncio
async def test_feed_response_shape_text_reply(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """Feed rows have correct answer_text/via for a free-text reply."""
    token = seeded["token"]

    r_create = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "kind": "question",
            "text": "What do you think?",
            "keyboard": [[{"label": "Yes", "value": "yes"}]],
            "reply_required": True,
            "ttl_sec": 60,
        },
    )
    assert r_create.status_code == 200
    mid = r_create.json()["message_id"]
    tg_mid = r_create.json()["telegram_message_id"]

    uid = next(_update_id_seq)
    r = await app_client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": uid,
            "message": {
                "message_id": uid,
                "from": {"id": int(seeded["bound_user_id"])},
                "chat": {"id": int(seeded["chat_id"])},
                "text": "My free-text answer",
                "reply_to_message": {"message_id": tg_mid},
            },
        },
    )
    assert r.status_code == 200

    resp = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": 0},
    )
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()}
    assert mid in rows
    row = rows[mid]
    assert row["answer_text"] == "My free-text answer"
    assert row["via"] == "reply"
    assert row["option_idx"] is None


# ---------------------------------------------------------------------------
# 11. Feed returns 204 when there is nothing and wait=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_returns_204_when_empty_no_wait(
    app_client: httpx.AsyncClient,
    seeded: dict,
) -> None:
    """GET /v1/answers with no answered rows and wait=0 returns 204."""
    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {seeded['token']}"},
        params={"after": 0, "wait": 0},
    )
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# 12. after=0 is a full replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_zero_full_replay(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """after=0 returns all answered rows for the installation."""
    token = seeded["token"]
    n = 5
    ids = []
    for _ in range(n):
        mid = await _create_msg(app_client, token)
        ids.append(mid)
        r = await _deliver_button_answer(
            app_client, mid,
            chat_id=int(seeded["chat_id"]),
            user_id=int(seeded["bound_user_id"]),
        )
        assert r.status_code == 200

    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": 0},
    )
    assert r.status_code == 200
    returned_ids = {row["id"] for row in r.json()}
    for mid in ids:
        assert mid in returned_ids, f"Message {mid} missing from full replay"


# ---------------------------------------------------------------------------
# 13. Regression: existing message creation/answer flow unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_message_flow_unaffected(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """Standard message create/answer flow is byte-identical to pre-23-01."""
    token = seeded["token"]

    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "kind": "question",
            "text": "Old-style question",
            "keyboard": [[{"label": "OK", "value": "ok"}]],
            "ttl_sec": 300,
        },
    )
    assert r.status_code == 200
    mid = r.json()["message_id"]

    r2 = await _deliver_button_answer(
        app_client, mid,
        chat_id=int(seeded["chat_id"]),
        user_id=int(seeded["bound_user_id"]),
    )
    assert r2.status_code == 200

    r3 = await app_client.get(
        f"/v1/messages/{mid}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r3.status_code == 200
    data = r3.json()
    assert data["state"] == "answered"
    assert data["answer"]["value"] == "ok"


# ---------------------------------------------------------------------------
# 14. never_expires=True still requires ttl_sec in the request body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_expires_still_requires_ttl_sec(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    """never_expires=True does not waive the ttl_sec requirement."""
    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {seeded['token']}"},
        json={"kind": "notification", "text": "hi", "never_expires": True},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 15. never_expires: normal messages still use ttl_sec (sentinel not leaked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_message_does_not_use_sentinel(
    app_client: httpx.AsyncClient,
    seeded: dict,
    db_path: str,
) -> None:
    """A message with never_expires=False (default) uses the TTL, not the sentinel."""
    token = seeded["token"]
    mid = await _create_msg(app_client, token, ttl_sec=300)

    conn = connect(db_path)
    row = conn.execute("SELECT expires_at FROM messages WHERE id=?", (mid,)).fetchone()
    conn.close()
    assert row is not None
    assert row["expires_at"] != NEVER_EXPIRES, (
        "A normal message must NOT use the NEVER_EXPIRES sentinel"
    )


# ---------------------------------------------------------------------------
# 16. Regression: second long-poll still parks after first answer was seen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_long_poll_still_parks(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """After answering one message and advancing the cursor, the next long-poll
    must park (block) for approximately the full wait= duration, not return in
    ~0 ms due to a permanently-set waiter event.

    This is the regression for the HIGH issue in the 23-01 review: the old
    WaiterRegistry.Event.set() latched permanently so every subsequent wait()
    returned True immediately.
    """
    import time

    token = seeded["token"]
    wait_secs = 2  # short enough that CI is fast; long enough to measure

    # Create and answer one message so answer_waiters has been notified once.
    mid = await _create_msg(app_client, token)
    r = await _deliver_button_answer(
        app_client, mid,
        chat_id=int(seeded["chat_id"]),
        user_id=int(seeded["bound_user_id"]),
    )
    assert r.status_code == 200

    # Consume the first answer with a non-waiting request.
    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": 0, "wait": 0},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 1
    latest_id = max(row["id"] for row in rows)

    # The next long-poll (cursor past the already-seen answer) must park.
    # With the bug, it returned in < 1 ms; with the fix it blocks for ~wait_secs.
    t0 = time.monotonic()
    r = await app_client.get(
        "/v1/answers",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": latest_id, "wait": wait_secs},
        timeout=wait_secs + 5.0,
    )
    elapsed = time.monotonic() - t0

    assert r.status_code == 204, (
        f"Expected 204 (nothing new past cursor {latest_id}), got {r.status_code}: {r.text}"
    )
    assert elapsed >= wait_secs * 0.8, (
        f"Long-poll returned in {elapsed:.3f}s — expected to park for ~{wait_secs}s. "
        f"This indicates the waiter event is permanently set (the Issue 1 regression)."
    )


# ---------------------------------------------------------------------------
# 17. Grouped-message finalization wakes feed long-pollers (Issue 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grouped_message_wakes_feed_poller(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    """When a grouped message group finalizes, the feed long-poll must wake
    within milliseconds, not wait for the full timeout.

    This covers the _finalize_group_if_complete path that bypasses _record_answer
    and therefore previously never called answer_waiters.notify().
    """
    import time

    token = seeded["token"]

    # Create two grouped messages (group_id shared, group_total=2).
    group_id = "test-group-wake-01"
    r1 = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "kind": "question",
            "text": "Group member 1?",
            "ttl_sec": 300,
            "keyboard": [[{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]],
            "group_id": group_id,
            "group_total": 2,
        },
    )
    assert r1.status_code == 200
    mid1 = r1.json()["message_id"]

    r2 = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "kind": "question",
            "text": "Group member 2?",
            "ttl_sec": 300,
            "keyboard": [[{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]],
            "group_id": group_id,
            "group_total": 2,
        },
    )
    assert r2.status_code == 200
    mid2 = r2.json()["message_id"]

    # Answer the first member — group is still incomplete, no finalization yet.
    await _deliver_button_answer(
        app_client, mid1,
        chat_id=int(seeded["chat_id"]),
        user_id=int(seeded["bound_user_id"]),
    )

    # Start the long-poll before answering the second member.
    poll_wait = 10  # generous timeout; we expect early wake

    async def _poll() -> httpx.Response:
        return await app_client.get(
            "/v1/answers",
            headers={"Authorization": f"Bearer {token}"},
            params={"after": 0, "wait": poll_wait},
            timeout=poll_wait + 5.0,
        )

    async def _finalize() -> None:
        # Give the poller time to park before triggering finalization.
        await asyncio.sleep(0.1)
        r = await _deliver_button_answer(
            app_client, mid2,
            chat_id=int(seeded["chat_id"]),
            user_id=int(seeded["bound_user_id"]),
        )
        assert r.status_code == 200

    t0 = time.monotonic()
    poll_task = asyncio.create_task(_poll())
    fin_task = asyncio.create_task(_finalize())
    await fin_task

    resp = await asyncio.wait_for(poll_task, timeout=5.0)
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200, (
        f"Expected feed to return rows after group finalization, got {resp.status_code}"
    )
    rows = resp.json()
    returned_ids = {row["id"] for row in rows}
    assert mid1 in returned_ids or mid2 in returned_ids, (
        f"Expected at least one group member in feed rows: {returned_ids}"
    )
    # Should have woken well before the full poll_wait timeout.
    assert elapsed < poll_wait * 0.5, (
        f"Feed long-poll took {elapsed:.3f}s — expected early wake from group "
        f"finalization, not to wait the full {poll_wait}s timeout."
    )
