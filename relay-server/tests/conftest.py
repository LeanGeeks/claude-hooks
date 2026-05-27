"""Shared fixtures for relay-server tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.config import RelayConfig
from relay_server.db import connect, init_schema
from relay_server.telegram_backend import FakeTelegramBackend
from relay_server.tokens import generate_token, hash_token

TEST_WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "relay.db")


@pytest.fixture
def backend() -> FakeTelegramBackend:
    return FakeTelegramBackend()


@pytest.fixture
def seeded(db_path: str) -> dict[str, object]:
    """Create the schema and seed installations: bound, revoked, unbound."""
    conn = connect(db_path)
    init_schema(conn)
    token = generate_token()
    revoked_token = generate_token()
    unbound_token = generate_token()
    with conn:
        cur = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id,"
            " bound_user_id, created_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))",
            ("test", hash_token(token), 42, 7),
        )
        installation_id = cur.lastrowid
        conn.execute(
            "INSERT INTO installations(label, token_hash, created_at, revoked_at)"
            " VALUES (?, ?, datetime('now'), datetime('now'))",
            ("revoked", hash_token(revoked_token)),
        )
        cur2 = conn.execute(
            "INSERT INTO installations(label, token_hash, created_at)"
            " VALUES (?, ?, datetime('now'))",
            ("unbound", hash_token(unbound_token)),
        )
        unbound_id = cur2.lastrowid
    conn.close()
    return {
        "token": token,
        "installation_id": installation_id,
        "chat_id": 42,
        "bound_user_id": 7,
        "revoked_token": revoked_token,
        "unbound_token": unbound_token,
        "unbound_id": unbound_id,
    }


def make_test_config(db_path: str) -> RelayConfig:
    return RelayConfig(
        db_path=db_path,
        webhook_secret=TEST_WEBHOOK_SECRET,
        set_webhook_on_startup=False,
    )


async def post_callback_query(
    client: httpx.AsyncClient,
    *,
    callback_data: str,
    chat_id: int,
    callback_query_id: str = "cbq-1",
    from_user_id: int = 7,
    message_id: int = 1000,
) -> httpx.Response:
    """Send a Telegram-shaped callback_query update through the webhook."""
    return await client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": 1,
            "callback_query": {
                "id": callback_query_id,
                "from": {"id": from_user_id},
                "data": callback_data,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": chat_id},
                },
            },
        },
    )


@asynccontextmanager
async def _run_lifespan(app):
    """Manually drive ASGI lifespan startup/shutdown for tests."""
    scope = {"type": "lifespan"}
    send_queue: asyncio.Queue = asyncio.Queue()
    recv_queue: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await recv_queue.get()

    async def send(msg):
        await send_queue.put(msg)

    task = asyncio.create_task(app(scope, receive, send))
    await recv_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await recv_queue.put({"type": "lifespan.shutdown"})
        try:
            msg = await asyncio.wait_for(send_queue.get(), timeout=5.0)
            assert msg["type"] == "lifespan.shutdown.complete", msg
        finally:
            await task


@pytest_asyncio.fixture
async def app_client(
    db_path: str,
    backend: FakeTelegramBackend,
    seeded: dict[str, object],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            yield ac
