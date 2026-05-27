"""Tests for long-poll wakeup on GET /v1/messages/{id}/answer.

Phase 2: answers are delivered via the real ``/telegram/webhook/{secret}``
endpoint by feeding it Telegram-shaped ``callback_query`` updates.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from relay_server.callback_data import encode as encode_cb

from tests.conftest import post_callback_query  # type: ignore[attr-defined]


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
