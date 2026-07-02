"""Tests for the /telegram/webhook/{secret} routing surface.

Covers:
- Wrong-secret returns 404 (does not advertise the endpoint).
- callback_query happy path and chat-mismatch trust check.
- reply_to_message_id matches an open message.
- reply_to that matches no open message does NOT fall through to the fallback.
- Loose reply: attributed only when a single target is open; ambiguous (multiple
  idle sessions) is ignored with a nudge; a question group counts as one target.
- Idle notifications are answerable but sent without a force_reply prompt.
- /bind command is accepted and a no-op (Phase 3 territory).
- update_id deduplication.
- Unhandled handler exceptions still return 2xx (no infinite Telegram retry).
- 403 on edit/delete/cancel auto-unbinds the installation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.callback_data import encode as encode_cb
from relay_server.db import connect
from relay_server.telegram_backend import (
    FakeTelegramBackend,
    TelegramForbidden,
)

from tests.conftest import (  # type: ignore[attr-defined]
    TEST_WEBHOOK_SECRET,
    _run_lifespan,
    make_test_config,
    post_callback_query,
)


# ---- Fake backend variants -------------------------------------------------


class ForbiddenSendBackend(FakeTelegramBackend):
    """send_message always raises TelegramForbidden."""

    async def send_message(self, **kwargs):  # type: ignore[override]
        raise TelegramForbidden("blocked")


class ForbiddenEditBackend(FakeTelegramBackend):
    """All edit/delete/markup calls raise TelegramForbidden."""

    async def edit_message(self, **kwargs):  # type: ignore[override]
        raise TelegramForbidden("blocked")

    async def edit_reply_markup(self, **kwargs):  # type: ignore[override]
        raise TelegramForbidden("blocked")

    async def delete_message(self, **kwargs):  # type: ignore[override]
        raise TelegramForbidden("blocked")


# ---- Helpers ---------------------------------------------------------------


async def _send_update(
    client: httpx.AsyncClient, update: dict, *, secret: str = TEST_WEBHOOK_SECRET
) -> httpx.Response:
    return await client.post(f"/telegram/webhook/{secret}", json=update)


async def _create_message(client: httpx.AsyncClient, token: str) -> int:
    body = {
        "kind": "question",
        "text": "?",
        "keyboard": [[{"label": "A", "value": "a"}]],
        "ttl_sec": 60,
    }
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


async def _create_notification(
    client: httpx.AsyncClient, token: str, text: str = "idle"
) -> int:
    """Create an answerable idle notification (no keyboard, reply_required)."""
    body = {
        "kind": "notification",
        "text": text,
        "reply_required": True,
        "ttl_sec": 60,
    }
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


# ---- Wrong secret ----------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_secret_returns_404(app_client: httpx.AsyncClient) -> None:
    r = await _send_update(app_client, {"update_id": 1}, secret="nope")
    assert r.status_code == 404


# ---- callback_query happy path + chat-mismatch -----------------------------


@pytest.mark.asyncio
async def test_callback_query_happy_path_records_and_acks(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    # Long-poll tests already exercise the waker wakeup; this test asserts the
    # answer is recorded and answerCallbackQuery is invoked.
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    r = await post_callback_query(
        app_client,
        callback_data=encode_cb(msg_id, 0),
        chat_id=int(seeded["chat_id"]),  # type: ignore[arg-type]
        from_user_id=int(seeded["bound_user_id"]),  # type: ignore[arg-type]
    )
    assert r.status_code == 200
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    assert ans.json()["answer"]["via"] == "button"
    assert any(c.method == "answer_callback_query" for c in backend.calls)


@pytest.mark.asyncio
async def test_callback_chat_mismatch_is_rejected(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    bad_chat = int(seeded["chat_id"]) + 9999  # type: ignore[operator]
    r = await post_callback_query(
        app_client,
        callback_data=encode_cb(msg_id, 0),
        chat_id=bad_chat,
        from_user_id=int(seeded["bound_user_id"]),  # type: ignore[arg-type]
    )
    assert r.status_code == 200
    # Answer must still be open — chat-mismatch is rejected silently.
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204
    # Telegram still gets an ack so the spinner stops.
    assert any(c.method == "answer_callback_query" for c in backend.calls)


# ---- Free-text reply via reply_to_message_id -------------------------------


@pytest.mark.asyncio
async def test_reply_to_message_id_records_answer(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    # FakeTelegramBackend assigns telegram_message_id starting at 1000, so the
    # first message sent via the API has tg id 1000.
    update = {
        "update_id": 7,
        "message": {
            "message_id": 5000,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "hello",
            "reply_to_message_id": 1000,
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    body = ans.json()
    assert body["answer"]["via"] == "reply"
    assert body["answer"]["text"] == "hello"


# ---- Fallback: bound user plain text → last open ---------------------------


@pytest.mark.asyncio
async def test_fallback_attribution_for_bound_user(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    update = {
        "update_id": 11,
        "message": {
            "message_id": 6000,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "free form answer",
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    assert ans.json()["answer"]["via"] == "fallback"


@pytest.mark.asyncio
async def test_loose_reply_ambiguous_with_multiple_open_is_ignored(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Two sessions idle at once → a loose (non-threaded) reply is ambiguous and
    must NOT be attributed to either; the user is nudged to use Reply."""
    token = seeded["token"]
    msg_a = await _create_notification(app_client, token, "session A idle")
    msg_b = await _create_notification(app_client, token, "session B idle")
    update = {
        "update_id": 31,
        "message": {
            "message_id": 6100,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "do the thing",
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200
    # Neither message got the answer.
    for mid in (msg_a, msg_b):
        ans = await app_client.get(
            f"/v1/messages/{mid}/answer",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ans.status_code == 204
    # The user was nudged to use Reply.
    assert any(c.method == "send_text" for c in backend.calls)


@pytest.mark.asyncio
async def test_grouped_questions_count_as_single_target(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Sibling messages of one AskUserQuestion group are one logical target, so
    a loose reply is NOT rejected as ambiguous — it's routed to the group
    handler (recorded provisionally, no "use Reply" nudge)."""
    token = seeded["token"]
    body = {
        "kind": "question",
        "text": "?",
        "keyboard": [[{"label": "A", "value": "a"}]],
        "reply_required": True,
        "ttl_sec": 60,
        "group_id": "grp-1",
        "group_total": 2,
    }
    ids = []
    for _ in range(2):
        r = await app_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        assert r.status_code == 200, r.text
        ids.append(r.json()["message_id"])
    update = {
        "update_id": 32,
        "message": {
            "message_id": 6200,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "grouped free text",
        },
    }
    hint_calls_before = sum(1 for c in backend.calls if c.method == "send_text")
    r = await _send_update(app_client, update)
    assert r.status_code == 200
    # Not treated as ambiguous: no "use Reply" nudge was sent...
    hint_calls_after = sum(1 for c in backend.calls if c.method == "send_text")
    assert hint_calls_after == hint_calls_before
    # ...and the loose reply was routed into the group (provisional render edits
    # the message), rather than being dropped.
    assert any(c.method == "edit_message" for c in backend.calls)


@pytest.mark.asyncio
async def test_reply_to_unmatched_does_not_fall_through(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    """A threaded reply whose target is not an open message must NOT drop into
    the loose-reply fallback — otherwise it would mis-thread onto whatever is
    open. The lone open message stays unanswered."""
    token = seeded["token"]
    msg_id = await _create_notification(app_client, token, "the only open msg")
    update = {
        "update_id": 33,
        "message": {
            "message_id": 6300,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "meant for a since-resolved message",
            "reply_to_message_id": 999999,  # no such open message
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204


@pytest.mark.asyncio
async def test_notification_sent_without_force_reply(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Idle notifications must be answerable but WITHOUT a force_reply prompt
    (which would auto-target the newest and mis-thread across sessions).
    Questions keep force_reply."""
    token = seeded["token"]
    await _create_notification(app_client, token, "idle")
    await _create_message(app_client, token)  # a question
    sends = [c for c in backend.calls if c.method == "send_message"]
    by_reqd = {c.kwargs["text"]: c.kwargs for c in sends}
    assert by_reqd["idle"]["force_reply"] is False
    assert by_reqd["idle"]["reply_required"] is True
    assert by_reqd["?"]["force_reply"] is True


@pytest.mark.asyncio
async def test_fallback_ignores_other_users(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    update = {
        "update_id": 12,
        "message": {
            "message_id": 6001,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": 99999},  # not the bound user
            "text": "intruder",
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204


# ---- /bind command is handled by Phase 3 (does NOT record an answer) -------


@pytest.mark.asyncio
async def test_bind_command_does_not_record_answer(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """A /bind update is routed to the bind handler; it must not record an answer
    for any open relay message, regardless of whether the code is valid."""
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    r = await _send_update(
        app_client,
        {
            "update_id": 22,
            "message": {
                "message_id": 7000,
                "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
                "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
                "text": "/bind ABCDEF",
            },
        },
    )
    assert r.status_code == 200
    # The relay message must still be open (no answer recorded).
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204


# ---- update_id dedup -------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_update_id_is_deduped(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    update = {
        "update_id": 4242,
        "message": {
            "message_id": 8000,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "first",
            "reply_to_message_id": 1000,
        },
    }
    r1 = await _send_update(app_client, update)
    assert r1.status_code == 200
    # Second send with the same update_id but a "different" text — must be
    # dropped before any processing; the recorded answer is from the first.
    update2 = dict(update)
    update2["message"] = dict(update["message"])
    update2["message"]["text"] = "second"
    r2 = await _send_update(app_client, update2)
    assert r2.status_code == 200
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    assert ans.json()["answer"]["text"] == "first"


# ---- Handler exception still returns 2xx -----------------------------------


@pytest.mark.asyncio
async def test_handler_exception_still_returns_2xx(
    app_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relay_server.app as app_module

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(app_module, "_handle_update", boom)
    r = await _send_update(
        app_client, {"update_id": 99999, "message": {"text": "x"}}
    )
    assert r.status_code == 200


# ---- 403 auto-unbind on send/edit/delete/cancel ----------------------------


@pytest_asyncio.fixture
async def forbidden_send_client(
    db_path: str, seeded: dict[str, object]
) -> AsyncIterator[tuple[httpx.AsyncClient, ForbiddenSendBackend]]:
    backend = ForbiddenSendBackend()
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            yield ac, backend


@pytest.mark.asyncio
async def test_send_403_auto_unbinds(
    forbidden_send_client: tuple[httpx.AsyncClient, ForbiddenSendBackend],
    seeded: dict[str, object],
    db_path: str,
) -> None:
    client, _backend = forbidden_send_client
    token = seeded["token"]
    body = {"kind": "question", "text": "?", "ttl_sec": 60}
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "not_bound"

    # DB now has telegram_chat_id NULL.
    conn = connect(db_path)
    row = conn.execute(
        "SELECT telegram_chat_id, bound_user_id FROM installations WHERE id = ?",
        (seeded["installation_id"],),
    ).fetchone()
    conn.close()
    assert row["telegram_chat_id"] is None
    # bound_user_id intentionally retained for audit.
    assert row["bound_user_id"] == seeded["bound_user_id"]

    # Second attempt: no chat → still 409 not_bound.
    r2 = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "not_bound"


@pytest_asyncio.fixture
async def forbidden_edit_client(
    db_path: str, seeded: dict[str, object]
) -> AsyncIterator[tuple[httpx.AsyncClient, list]]:
    # Seed an existing message row so we don't need a working send_message
    # to trigger the edit/delete/cancel paths.
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO messages(id, installation_id, telegram_chat_id,"
            " telegram_message_id, kind, payload_json, state, created_at,"
            " expires_at)"
            " VALUES (1, ?, ?, 1001, 'question', ?, 'open', datetime('now'),"
            " datetime('now', '+1 hour'))",
            (
                seeded["installation_id"],
                seeded["chat_id"],
                '{"kind":"question","text":"?","ttl_sec":60}',
            ),
        )
    conn.close()

    backend = ForbiddenEditBackend()
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            yield ac, backend


@pytest.mark.asyncio
async def test_patch_403_auto_unbinds(
    forbidden_edit_client, seeded: dict[str, object], db_path: str
) -> None:
    client, _ = forbidden_edit_client
    token = seeded["token"]
    r = await client.patch(
        "/v1/messages/1",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "new"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "not_bound"
    conn = connect(db_path)
    row = conn.execute(
        "SELECT telegram_chat_id FROM installations WHERE id = ?",
        (seeded["installation_id"],),
    ).fetchone()
    conn.close()
    assert row["telegram_chat_id"] is None


@pytest.mark.asyncio
async def test_delete_403_auto_unbinds(
    forbidden_edit_client, seeded: dict[str, object], db_path: str
) -> None:
    client, _ = forbidden_edit_client
    token = seeded["token"]
    r = await client.delete(
        "/v1/messages/1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "not_bound"
    conn = connect(db_path)
    row = conn.execute(
        "SELECT telegram_chat_id FROM installations WHERE id = ?",
        (seeded["installation_id"],),
    ).fetchone()
    conn.close()
    assert row["telegram_chat_id"] is None


@pytest.mark.asyncio
async def test_cancel_403_auto_unbinds(
    forbidden_edit_client, seeded: dict[str, object], db_path: str
) -> None:
    client, _ = forbidden_edit_client
    token = seeded["token"]
    r = await client.post(
        "/v1/messages/1/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "not_bound"
    conn = connect(db_path)
    row = conn.execute(
        "SELECT telegram_chat_id FROM installations WHERE id = ?",
        (seeded["installation_id"],),
    ).fetchone()
    conn.close()
    assert row["telegram_chat_id"] is None
