"""Tests for the Idempotency-Key replay middleware on POST /v1/messages."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from relay_server.telegram_backend import FakeTelegramBackend


def _msg_body() -> dict:
    return {
        "kind": "question",
        "text": "pick one",
        "keyboard": [[{"label": "Yes", "value": "y"}, {"label": "No", "value": "n"}]],
        "reply_required": False,
        "ttl_sec": 60,
    }


@pytest.mark.asyncio
async def test_idempotency_replays_response(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    headers = {
        "Authorization": f"Bearer {seeded['token']}",
        "Idempotency-Key": "abc-123",
    }
    r1 = await app_client.post("/v1/messages", headers=headers, json=_msg_body())
    assert r1.status_code == 200
    first = r1.json()

    r2 = await app_client.post("/v1/messages", headers=headers, json=_msg_body())
    assert r2.status_code == 200
    assert r2.json() == first

    # Backend was only invoked once.
    send_calls = [c for c in backend.calls if c.method == "send_message"]
    assert len(send_calls) == 1


@pytest.mark.asyncio
async def test_distinct_keys_create_distinct_messages(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    base_headers = {"Authorization": f"Bearer {seeded['token']}"}
    r1 = await app_client.post(
        "/v1/messages", headers={**base_headers, "Idempotency-Key": "k1"}, json=_msg_body()
    )
    r2 = await app_client.post(
        "/v1/messages", headers={**base_headers, "Idempotency-Key": "k2"}, json=_msg_body()
    )
    assert r1.json()["message_id"] != r2.json()["message_id"]
    assert sum(1 for c in backend.calls if c.method == "send_message") == 2


@pytest.mark.asyncio
async def test_idempotency_same_key_different_body_returns_422(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    headers = {
        "Authorization": f"Bearer {seeded['token']}",
        "Idempotency-Key": "reused",
    }
    r1 = await app_client.post("/v1/messages", headers=headers, json=_msg_body())
    assert r1.status_code == 200

    other = _msg_body()
    other["text"] = "different text"
    r2 = await app_client.post("/v1/messages", headers=headers, json=other)
    assert r2.status_code == 422
    assert r2.json()["detail"] == "idempotency_key_reused_with_different_body"
    # Backend was only called once (the original).
    assert sum(1 for c in backend.calls if c.method == "send_message") == 1


@pytest.mark.asyncio
async def test_idempotency_concurrent_same_key_serializes(
    db_path: str,
    seeded: dict[str, object],
) -> None:
    """Two concurrent POSTs with the same key must result in exactly one
    backend send_message call, and both responses must match."""
    from relay_server.app import create_app
    from relay_server.telegram_backend import FakeTelegramBackend

    # A backend whose send_message blocks until released, so we can hold two
    # in-flight requests at once and exercise the lock + pending-row paths.
    class BlockingBackend(FakeTelegramBackend):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.entered = 0

        async def send_message(self, **kwargs: Any) -> int:  # type: ignore[override]
            self.entered += 1
            await self.release.wait()
            return await super().send_message(**kwargs)

    backend = BlockingBackend()
    app = create_app(db_path=db_path, backend=backend)
    transport = httpx.ASGITransport(app=app)
    from tests.conftest import _run_lifespan  # type: ignore[attr-defined]

    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            headers = {
                "Authorization": f"Bearer {seeded['token']}",
                "Idempotency-Key": "race",
            }
            body = _msg_body()

            async def fire() -> httpx.Response:
                return await ac.post("/v1/messages", headers=headers, json=body)

            t1 = asyncio.create_task(fire())
            # Yield so t1 can claim the pending row and enter the backend.
            await asyncio.sleep(0.05)
            t2 = asyncio.create_task(fire())
            await asyncio.sleep(0.05)
            backend.release.set()
            r1, r2 = await asyncio.gather(t1, t2)

    assert r1.status_code == 200, r1.text
    # The second caller either gets 200 (replayed) or 409 in-flight if it
    # raced before the first finalized; both are correct, but with the
    # in-process Lock we expect 200 with identical body.
    if r2.status_code == 200:
        assert r2.json() == r1.json()
    else:
        assert r2.status_code == 409
    # Critically, the backend was only invoked once.
    assert backend.entered == 1


@pytest.mark.asyncio
async def test_not_bound_returns_409(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {seeded['unbound_token']}"},
        json=_msg_body(),
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "not_bound"
