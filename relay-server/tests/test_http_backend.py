"""Unit tests for HttpTelegramBackend using httpx.MockTransport.

These don't touch the network — we route every request through a callback
that asserts URL/body shape and returns canned Telegram-shaped JSON.
"""

from __future__ import annotations

import json

import httpx
import pytest

from relay_server.telegram_backend import (
    HttpTelegramBackend,
    TelegramApiError,
    TelegramForbidden,
)

BOT_TOKEN = "TEST:TOKEN"


def _make_backend(handler) -> HttpTelegramBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return HttpTelegramBackend(BOT_TOKEN, client=client)


@pytest.mark.asyncio
async def test_send_message_happy_path() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 555}}
        )

    backend = _make_backend(handler)
    try:
        tg_id = await backend.send_message(
            chat_id=42,
            text="hello",
            keyboard=[[{"label": "Yes", "value": "y"}]],
            reply_required=False,
            message_id=7,
        )
    finally:
        await backend.aclose()
    assert tg_id == 555
    assert captured["url"].endswith(f"/bot{BOT_TOKEN}/sendMessage")
    assert captured["body"]["chat_id"] == 42
    assert captured["body"]["text"] == "hello"
    assert captured["body"]["parse_mode"] == "HTML"
    markup = captured["body"]["reply_markup"]
    assert "inline_keyboard" in markup
    btn = markup["inline_keyboard"][0][0]
    assert btn["text"] == "Yes"
    # callback_data encodes the message_id + option index.
    assert btn["callback_data"] == "m:7:o:0"


@pytest.mark.asyncio
async def test_send_message_force_reply_when_no_keyboard() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}
        )

    backend = _make_backend(handler)
    try:
        await backend.send_message(
            chat_id=1,
            text="?",
            keyboard=None,
            reply_required=True,
            message_id=2,
        )
    finally:
        await backend.aclose()
    assert captured["body"]["reply_markup"] == {
        "force_reply": True,
        "selective": False,
    }


@pytest.mark.asyncio
async def test_forbidden_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            },
        )

    backend = _make_backend(handler)
    try:
        with pytest.raises(TelegramForbidden) as exc:
            await backend.send_message(
                chat_id=1,
                text="x",
                keyboard=None,
                reply_required=False,
                message_id=1,
            )
        assert "blocked" in exc.value.description.lower()
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_400_response_raises_api_error_with_description() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            },
        )

    backend = _make_backend(handler)
    try:
        with pytest.raises(TelegramApiError) as exc:
            await backend.send_message(
                chat_id=1,
                text="x",
                keyboard=None,
                reply_required=False,
                message_id=1,
            )
        assert exc.value.status_code == 400
        assert exc.value.description == "Bad Request: chat not found"
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_set_webhook_payload_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    backend = _make_backend(handler)
    try:
        await backend.set_webhook("https://example.test/telegram/webhook/sek")
    finally:
        await backend.aclose()
    assert captured["url"].endswith(f"/bot{BOT_TOKEN}/setWebhook")
    assert captured["body"]["url"] == "https://example.test/telegram/webhook/sek"
    assert captured["body"]["allowed_updates"] == ["message", "callback_query"]
