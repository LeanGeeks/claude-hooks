"""Tests for long-poll wakeup on GET /v1/messages/{id}/answer.

Phase 2: answers are delivered via the real ``/telegram/webhook/{secret}``
endpoint by feeding it Telegram-shaped ``callback_query`` updates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.callback_data import encode as encode_cb
from relay_server.reaper import reaper_tick
from relay_server.telegram_backend import FakeTelegramBackend

from tests.conftest import (  # type: ignore[attr-defined]
    TEST_WEBHOOK_SECRET,
    make_test_config,
    post_callback_query,
    _run_lifespan,
)


async def _create_message(app_client: httpx.AsyncClient, token: str) -> int:
    body = {
        "kind": "question",
        "text": "?",
        "keyboard": [[{"label": "A", "value": "a"}]],
        "ttl_sec": 60,
    }
    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


async def _deliver_callback(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    msg_id: int,
    option_idx: int = 0,
) -> httpx.Response:
    return await post_callback_query(
        app_client,
        callback_data=encode_cb(msg_id, option_idx),
        chat_id=int(seeded["chat_id"]),  # type: ignore[arg-type]
        from_user_id=int(seeded["bound_user_id"]),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_answer_already_present_returns_immediately(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    rec = await _deliver_callback(app_client, seeded, msg_id)
    assert rec.status_code == 200

    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer?wait=30",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "answered"
    assert body["answer"]["value"] == "a"
    assert body["answer"]["via"] == "button"


@pytest.mark.asyncio
async def test_long_poll_wakeup_on_answer(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    async def deliver_answer_soon() -> None:
        await asyncio.sleep(0.2)
        await _deliver_callback(app_client, seeded, msg_id)

    deliver = asyncio.create_task(deliver_answer_soon())
    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer?wait=5",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    await deliver
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "answered"
    assert body["answer"]["value"] == "a"


@pytest.mark.asyncio
async def test_long_poll_timeout_returns_204(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer?wait=1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_zero_wait_returns_204_when_open(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_cancel_wakes_waiter(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    async def cancel_soon() -> None:
        await asyncio.sleep(0.2)
        await app_client.post(
            f"/v1/messages/{msg_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )

    canceller = asyncio.create_task(cancel_soon())
    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer?wait=5",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    await canceller
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"


# ---------------------------------------------------------------------------
# TOCTOU race: reaper fires between initial DB read and waiters.wait()
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def app_with_client(tmp_path: Path):
    """Yield (starlette_app, httpx_client, token) with lifespan active."""
    from relay_server.db import connect, init_schema
    from relay_server.tokens import generate_token, hash_token

    db_path = str(tmp_path / "race_test.db")
    conn = connect(db_path)
    init_schema(conn)
    token = generate_token()
    with conn:
        conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))",
            ("race", hash_token(token), 99, 7),
        )
    conn.close()

    backend = FakeTelegramBackend()
    config = make_test_config(db_path)
    app = create_app(backend=backend, config=config)
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield app, client, token


@pytest.mark.asyncio
async def test_reaper_race_long_poll_returns_expired_not_204(
    app_with_client,
) -> None:
    """Simulate the TOCTOU race: reaper fires while long-poll is parked.

    The fix in get_answer re-reads the DB when waiters.wait() times out (False)
    and returns the terminal state rather than 204.  We reproduce the race by:
      1. Creating a message with a very short TTL (already expired).
      2. Starting a long-poll (wait=2).
      3. Concurrently running reaper_tick after a short delay — this transitions
         the message to 'expired' and calls notify+clear on the WaiterRegistry.
         Because clear() removes the event, a subsequent waiters.wait() on the
         same id would return False (timeout) after the event is gone.
      4. Asserting the long-poll returns state='expired' (not 204).
    """
    app, client, token = app_with_client

    # Create a message with TTL=1 so it's immediately past expiry.
    body = {
        "kind": "question",
        "text": "expire me",
        "keyboard": [[{"label": "Y", "value": "y"}]],
        "ttl_sec": 1,
    }
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 200, r.text
    msg_id = r.json()["message_id"]

    # Manually set the message to already-expired in the DB to guarantee it
    # is past its TTL when the reaper ticks.
    db: object = app.state.db
    with db:
        db.execute(
            "UPDATE messages SET expires_at = datetime('now', '-5 seconds')"
            " WHERE id = ?",
            (msg_id,),
        )

    async def run_reaper_after_delay() -> None:
        # Give the long-poll time to start and park on waiters.wait().
        await asyncio.sleep(0.3)
        await reaper_tick(db, app.state.backend, app.state.waiters)

    reaper_task = asyncio.create_task(run_reaper_after_delay())

    # Long-poll with wait=2. With the fix, even if waiters.wait() returns False
    # (because the reaper cleared the event), the handler re-reads DB and finds
    # state='expired', returning 200 instead of 204.
    r = await client.get(
        f"/v1/messages/{msg_id}/answer?wait=2",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    await reaper_task

    assert r.status_code == 200, f"expected 200 (expired), got {r.status_code}"
    assert r.json()["state"] == "expired"
