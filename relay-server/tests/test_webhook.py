"""Tests for the /telegram/webhook/{secret} routing surface.

Covers:
- Wrong-secret returns 404 (does not advertise the endpoint).
- callback_query happy path and chat-mismatch trust check.
- reply_to_message_id matches an open message.
- reply_to that matches no open message does NOT fall through to the fallback.
- Loose reply: attributed only when a single target is open; ambiguous (multiple
  idle sessions) is ignored with a nudge; a question group counts as one target.
- Idle notifications are answerable but sent without a force_reply prompt.
- Preference commands: /tz, /hours, /nudge, /me (epic 19-02).
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
    # Key on the first line: the question also carries a trailing #unanswered
    # tag line, the notification does not (19-03).
    by_reqd = {c.kwargs["text"].split("\n", 1)[0]: c.kwargs for c in sends}
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


# ---- Preference commands (epic 19-02) ----------------------------------------


async def _send_pref_command(
    client: httpx.AsyncClient,
    chat_id: int,
    from_user_id: int,
    text: str,
    *,
    update_id: int = 9900,
) -> httpx.Response:
    """Send a Telegram message update containing a preference command."""
    return await _send_update(
        client,
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id + 1000,
                "chat": {"id": chat_id},
                "from": {"id": from_user_id},
                "text": text,
            },
        },
    )


@pytest.mark.asyncio
async def test_pref_tz_bound_user_writes_row_and_echoes(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Bound user sending /tz Europe/Berlin writes the recipient row and echoes."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/tz Europe/Berlin",
        update_id=9901,
    )
    assert r.status_code == 200

    conn = connect(db_path)
    row = conn.execute(
        "SELECT tz FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["tz"] == "Europe/Berlin"

    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("Europe/Berlin" in t for t in texts)


@pytest.mark.asyncio
async def test_pref_tz_non_bound_sender_writes_nothing(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Non-bound sender is silently ignored — no row written, no echo."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        99999,  # not bound_user_id
        "/tz Europe/Berlin",
        update_id=9902,
    )
    assert r.status_code == 200
    conn = connect(db_path)
    row = conn.execute(
        "SELECT tz FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is None
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert not texts


@pytest.mark.asyncio
async def test_pref_command_unbound_chat_prompts_bind(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """A preference command sent to an unbound chat replies with a /bind prompt
    and creates no recipient row."""
    unknown_chat_id = 99999
    r = await _send_pref_command(
        app_client,
        unknown_chat_id,
        7,
        "/tz Europe/Berlin",
        update_id=9903,
    )
    assert r.status_code == 200
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("/bind" in t for t in texts)
    conn = connect(db_path)
    row = conn.execute(
        "SELECT tz FROM recipients WHERE telegram_chat_id = ?",
        (unknown_chat_id,),
    ).fetchone()
    conn.close()
    assert row is None


@pytest.mark.asyncio
async def test_pref_command_while_message_open_does_not_record_answer(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """A preference command must NOT be attributed as an answer to an open
    message — the regression this guard exists for."""
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/tz Europe/Berlin",
        update_id=9904,
    )
    assert r.status_code == 200

    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204, (
        f"message was answered by a preference command: {ans.json()}"
    )


@pytest.mark.asyncio
async def test_pref_tz_unknown_timezone_error_no_row(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Unknown timezone sends an error reply and writes no row."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/tz NotARealZone/XYZ",
        update_id=9905,
    )
    assert r.status_code == 200
    conn = connect(db_path)
    row = conn.execute(
        "SELECT tz FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is None
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("Unknown timezone" in t for t in texts)


@pytest.mark.asyncio
async def test_pref_hours_bound_user_writes_row_and_echoes(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Bound user /hours writes windows_json and echoes resolved status."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/hours mon-fri 09:00-19:00",
        update_id=9906,
    )
    assert r.status_code == 200
    conn = connect(db_path)
    row = conn.execute(
        "SELECT windows_json FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert "09:00" in (row["windows_json"] or "")
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    # Echo includes canonical spec AND resolved status with a concrete time.
    # Use case-insensitive check because describe_active_status result is .capitalize()d.
    assert any(
        "09:00" in t and ("active" in t.lower() or "inactive" in t.lower())
        for t in texts
    )


@pytest.mark.asyncio
async def test_pref_hours_bad_spec_no_row_no_partial(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Bad window spec (unknown day): error text sent, no row written."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/hours notaday 09:00-17:00",
        update_id=9908,
    )
    assert r.status_code == 200
    conn = connect(db_path)
    row = conn.execute(
        "SELECT windows_json FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is None
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("Bad window spec" in t or "bad" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_pref_hours_off_clears_windows(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """First set hours, then /hours off — windows_json becomes NULL."""
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/hours mon-fri 09:00-19:00",
        update_id=9910,
    )
    conn = connect(db_path)
    before = conn.execute(
        "SELECT windows_json FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    assert before is not None and before["windows_json"] is not None

    backend.calls.clear()
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/hours off",
        update_id=9911,
    )
    after = conn.execute(
        "SELECT windows_json FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert after is not None and after["windows_json"] is None
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("always available" in t for t in texts)


@pytest.mark.asyncio
async def test_pref_me_reflects_hours_off(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """/me after /hours off reports always-available status."""
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/hours off",
        update_id=9920,
    )
    backend.calls.clear()
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/me",
        update_id=9921,
    )
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert texts, "expected /me to send a reply"
    assert any("always available" in t for t in texts)


@pytest.mark.asyncio
async def test_pref_nudge_on_backfills_open_rows_only_this_chat(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """/nudge on backfills next_nudge_at on open rows in this chat only."""
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    # Seed an open message in a different chat directly in the DB.
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO messages"
        " (installation_id, telegram_chat_id, telegram_message_id,"
        "  kind, payload_json, state, created_at, expires_at)"
        " VALUES (?, 88888, 9999, 'question',"
        "  '{\"kind\":\"question\",\"text\":\"?\",\"ttl_sec\":60}',"
        "  'open', datetime('now'), datetime('now', '+1 hour'))",
        (seeded["installation_id"],),
    )
    conn.commit()

    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge on",
        update_id=9930,
    )
    assert r.status_code == 200

    this_msg = conn.execute(
        "SELECT next_nudge_at FROM messages WHERE id = ?",
        (msg_id,),
    ).fetchone()
    assert this_msg["next_nudge_at"] is not None, (
        "expected next_nudge_at to be backfilled on the open message"
    )

    other_msg = conn.execute(
        "SELECT next_nudge_at FROM messages WHERE telegram_chat_id = 88888"
    ).fetchone()
    assert other_msg is not None
    assert other_msg["next_nudge_at"] is None, "backfill must not touch other chats"
    conn.close()

    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("Nudges on" in t for t in texts)


@pytest.mark.asyncio
async def test_pref_nudge_on_backfill_excludes_notification_rows(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """/nudge on backfill must not seed kind='notification' rows (invariant 7).

    Regression test: the backfill used to UPDATE every open row in the chat,
    including idle-session notification rows.  brd §4.1 and invariant 7 say
    idle notifications are excluded from the ladder — "an idle session left
    overnight must never produce a 09:00 ping".  Only the prompt row
    (kind='question' with a keyboard) should get next_nudge_at.
    """
    token = seeded["token"]
    # Create an idle notification row (kind='notification', reply_required).
    notif_id = await _create_notification(app_client, token, "session went idle")
    # Create an open prompt row that actually awaits a human.
    prompt_id = await _create_message(app_client, token)

    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge on",
        update_id=9931,
    )
    assert r.status_code == 200

    conn = connect(db_path)
    notif_row = conn.execute(
        "SELECT next_nudge_at FROM messages WHERE id = ?", (notif_id,)
    ).fetchone()
    prompt_row = conn.execute(
        "SELECT next_nudge_at FROM messages WHERE id = ?", (prompt_id,)
    ).fetchone()
    conn.close()

    assert notif_row is not None
    assert notif_row["next_nudge_at"] is None, (
        "backfill must not seed kind='notification' rows (invariant 7)"
    )
    assert prompt_row is not None
    assert prompt_row["next_nudge_at"] is not None, (
        "backfill must seed the open prompt row"
    )


@pytest.mark.asyncio
async def test_pref_nudge_on_backfill_excludes_rows_awaiting_nobody(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """/nudge on backfill skips a non-notification row that awaits nobody.

    A row with kind != 'notification' but with no reply_required, no keyboard,
    and no group_id does not block the human — awaits_human returns False for
    it, so it must not be seeded.
    """
    token = seeded["token"]
    installation_id = int(seeded["installation_id"])  # type: ignore[arg-type]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]

    # Insert a 'question' row with none of the eligibility triggers set.
    conn = connect(db_path)
    import json as _json
    payload = {"kind": "question", "text": "no triggers", "keyboard": None,
               "reply_required": False}
    cur = conn.execute(
        "INSERT INTO messages"
        " (installation_id, telegram_chat_id, telegram_message_id,"
        "  kind, payload_json, state, created_at, expires_at)"
        " VALUES (?, ?, 7001, 'question', ?, 'open', datetime('now'),"
        "         datetime('now', '+1 hour'))",
        (installation_id, chat_id, _json.dumps(payload)),
    )
    conn.commit()
    nobody_id = int(cur.lastrowid)
    conn.close()

    r = await _send_pref_command(
        app_client,
        chat_id,
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge on",
        update_id=9932,
    )
    assert r.status_code == 200

    conn = connect(db_path)
    row = conn.execute(
        "SELECT next_nudge_at FROM messages WHERE id = ?", (nobody_id,)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["next_nudge_at"] is None, (
        "backfill must not seed a row that awaits nobody"
    )


@pytest.mark.asyncio
async def test_pref_nudge_bad_schedule_no_row(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """An unparseable nudge schedule returns an error and writes no row."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge bad_schedule",
        update_id=9940,
    )
    assert r.status_code == 200
    conn = connect(db_path)
    row = conn.execute(
        "SELECT nudge_enabled FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is None
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("Bad nudge schedule" in t or "bad" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_pref_nudge_too_many_entries_rejected(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """A schedule exceeding nudge_max is rejected, not truncated."""
    # Default nudge_max is 3; use 4 entries.
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge 15m,30m,1h,2h",
        update_id=9941,
    )
    assert r.status_code == 200
    conn = connect(db_path)
    row = conn.execute(
        "SELECT nudge_enabled FROM recipients WHERE telegram_chat_id = ?",
        (seeded["chat_id"],),
    ).fetchone()
    conn.close()
    assert row is None
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("cap" in t.lower() or "entries" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_installations_me_returns_availability_fields(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """/v1/installations/me returns availability fields after configuring via
    preference commands."""
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/tz Europe/London",
        update_id=9950,
    )
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/hours mon-fri 09:00-17:00",
        update_id=9951,
    )
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge on",
        update_id=9952,
    )

    r = await app_client.get(
        "/v1/installations/me",
        headers={"Authorization": f"Bearer {seeded['token']}"},  # type: ignore[arg-type]
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tz"] == "Europe/London"
    assert body["windows"] is not None
    assert "09:00" in body["windows"]
    assert isinstance(body["active_now"], bool)
    assert body["nudge_enabled"] is True


@pytest.mark.asyncio
async def test_installations_me_unconfigured_chat_returns_nulls(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    """A bound but unconfigured chat returns null tz/windows, bool active_now,
    and nudge_enabled False — no error."""
    r = await app_client.get(
        "/v1/installations/me",
        headers={"Authorization": f"Bearer {seeded['token']}"},  # type: ignore[arg-type]
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chat_bound"] is True
    assert body["tz"] is None
    assert body["windows"] is None
    assert isinstance(body["active_now"], bool)  # unconfigured = always active = True
    assert body["nudge_enabled"] is False


@pytest.mark.asyncio
async def test_pref_command_with_reply_to_does_not_answer_message(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """A preference command sent with reply_to_message_id must NOT be attributed
    as an answer — it is caught before the reply path."""
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    # FakeTelegramBackend assigns telegram_message_id starting at 1000.
    r = await _send_update(
        app_client,
        {
            "update_id": 9960,
            "message": {
                "message_id": 9000,
                "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
                "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
                "text": "/tz UTC",
                "reply_to_message_id": 1000,  # tg_id of the open message
            },
        },
    )
    assert r.status_code == 200

    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204, (
        "/tz sent as a reply was treated as an answer — preference command guard broken"
    )


@pytest.mark.asyncio
async def test_pref_nudge_on_echo_includes_concrete_time(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """/nudge on echo must name a concrete resolved local time (done criterion)."""
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge on",
        update_id=9970,
    )
    assert r.status_code == 200
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    nudge_on_text = next((t for t in texts if "Nudges on" in t), None)
    assert nudge_on_text is not None, "expected a 'Nudges on' echo"
    assert "First nudge:" in nudge_on_text, (
        f"echo must include a concrete resolved time; got: {nudge_on_text!r}"
    )


@pytest.mark.asyncio
async def test_pref_nudge_off_echo_includes_status(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """/nudge off echo must include resolved availability status (done criterion)."""
    # First enable nudges so we can turn them off.
    await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge on",
        update_id=9971,
    )
    backend.calls.clear()
    r = await _send_pref_command(
        app_client,
        int(seeded["chat_id"]),  # type: ignore[arg-type]
        int(seeded["bound_user_id"]),  # type: ignore[arg-type]
        "/nudge off",
        update_id=9972,
    )
    assert r.status_code == 200
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    nudge_off_text = next((t for t in texts if "Nudges off" in t), None)
    assert nudge_off_text is not None, "expected a 'Nudges off' echo"
    # describe_active_status produces "always available", "active now...",
    # "inactive...", or "never active". Match case-insensitively.
    assert any(
        kw in nudge_off_text.lower()
        for kw in ("active", "inactive", "never", "always")
    ), f"echo must include availability status; got: {nudge_off_text!r}"


# ---- Reply-to-nudge routing (epic 19-05) -----------------------------------


@pytest.mark.asyncio
async def test_reply_to_nudge_records_answer_via_nudge_reply(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Reply to a nudge → target message answered, waiter woken, via='nudge_reply'.

    The nudge has no ``messages`` row of its own — it is only recorded as
    ``messages.nudge_tg_message_id`` on the row it serves (brd §5.5, state.md
    invariant 6).  Replying to the nudge's Telegram id must still land as a
    full answer on the target row, with ``via="nudge_reply"`` so logs can show
    whether nudges are actually being answered.
    """
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    nudge_tg_id = 5555

    # Inject a nudge id onto the open row (simulating what the reaper does).
    conn = connect(db_path)
    with conn:
        conn.execute(
            "UPDATE messages SET nudge_tg_message_id = ? WHERE id = ?",
            (nudge_tg_id, msg_id),
        )
    conn.close()

    update = {
        "update_id": 19050,
        "message": {
            "message_id": 19050100,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "nudge answer",
            "reply_to_message_id": nudge_tg_id,
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
    assert body["answer"]["via"] == "nudge_reply"
    assert body["answer"]["text"] == "nudge answer"


@pytest.mark.asyncio
async def test_reply_to_nudge_of_already_answered_message_sends_hint(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Reply to a nudge whose target is already answered → no state change,
    a clear 'already handled' reply, no fall-through to the recency heuristic.

    The nudge may survive past the answer if the reaper's cleanup sweep has not
    yet run (state.md invariant 10).  The handler must detect the terminal state
    and inform the user instead of silently swallowing the reply.
    """
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    nudge_tg_id = 5666

    # Answer the message via button tap so it transitions to 'answered'.
    r = await post_callback_query(
        app_client,
        callback_data=encode_cb(msg_id, 0),
        chat_id=int(seeded["chat_id"]),  # type: ignore[arg-type]
        from_user_id=int(seeded["bound_user_id"]),  # type: ignore[arg-type]
    )
    assert r.status_code == 200

    # Inject a nudge id onto the now-answered row (mimicking a stale nudge the
    # reaper has not yet deleted — the normal scenario between button tap and
    # the reaper's next cleanup tick).
    conn = connect(db_path)
    with conn:
        conn.execute(
            "UPDATE messages SET nudge_tg_message_id = ? WHERE id = ?",
            (nudge_tg_id, msg_id),
        )
    conn.close()

    send_text_before = sum(1 for c in backend.calls if c.method == "send_text")

    update = {
        "update_id": 19051,
        "message": {
            "message_id": 19051100,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "oh wait already handled",
            "reply_to_message_id": nudge_tg_id,
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200

    # Answer unchanged — original button tap is still the recorded answer.
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    assert ans.json()["answer"]["via"] == "button"

    # An informative reply was sent (not silence).
    send_text_after = sum(1 for c in backend.calls if c.method == "send_text")
    assert send_text_after > send_text_before, (
        "expected an 'already handled' hint but no send_text call was made"
    )
    new_texts = [
        c.kwargs.get("text", "")
        for c in backend.calls
        if c.method == "send_text"
    ][send_text_before:]
    assert any("already" in t.lower() or "handled" in t.lower() for t in new_texts), (
        f"hint text must mention 'already' or 'handled'; got: {new_texts}"
    )


@pytest.mark.asyncio
async def test_one_open_prompt_with_nudge_loose_reply_still_unambiguous(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """One open prompt plus its nudge → loose reply still unambiguous and attributed.

    Regression guard: ``_distinct_open_targets`` counts message *rows*, not
    nudge ids.  A nudge stored in ``nudge_tg_message_id`` on a row must not
    make that row count as two targets.  If it did, this chat — which has
    exactly one open prompt — would be treated as ambiguous and the relay would
    refuse plain replies with 'Multiple sessions are waiting'.
    """
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    # Inject a nudge id — a second prompt has NOT been created.
    conn = connect(db_path)
    with conn:
        conn.execute(
            "UPDATE messages SET nudge_tg_message_id = 6660 WHERE id = ?",
            (msg_id,),
        )
    conn.close()

    hint_calls_before = sum(1 for c in backend.calls if c.method == "send_text")

    update = {
        "update_id": 19052,
        "message": {
            "message_id": 19052100,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "loose answer while nudge exists",
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200

    # Not ambiguous — no "Multiple sessions" hint sent.
    hint_calls_after = sum(1 for c in backend.calls if c.method == "send_text")
    assert hint_calls_after == hint_calls_before, (
        "a nudge on a single open prompt must not make loose replies ambiguous"
    )

    # The message was answered by the loose reply.
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    assert ans.json()["answer"]["via"] == "fallback"


@pytest.mark.asyncio
async def test_two_open_prompts_each_with_nudge_loose_reply_still_ambiguous(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Two open prompts each with a nudge → loose reply still refused as ambiguous.

    The regression guard from the other direction: two prompts are two targets
    regardless of whether either has a nudge.  Adding a nudge to each must not
    change the count or the refusal message.
    """
    token = seeded["token"]
    msg_a = await _create_message(app_client, token)
    msg_b = await _create_message(app_client, token)

    conn = connect(db_path)
    with conn:
        conn.execute(
            "UPDATE messages SET nudge_tg_message_id = 7771 WHERE id = ?", (msg_a,)
        )
        conn.execute(
            "UPDATE messages SET nudge_tg_message_id = 7772 WHERE id = ?", (msg_b,)
        )
    conn.close()

    update = {
        "update_id": 19053,
        "message": {
            "message_id": 19053100,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "which one?",
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200

    # Neither message was answered.
    for mid in (msg_a, msg_b):
        ans = await app_client.get(
            f"/v1/messages/{mid}/answer",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ans.status_code == 204, f"message {mid} should still be open"

    # The "Multiple sessions" hint was sent.
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert texts, "expected an ambiguity hint but no send_text call was made"
    assert any(
        "multiple" in t.lower() or "sessions" in t.lower() for t in texts
    ), f"hint must mention multiple sessions; got: {texts}"


@pytest.mark.asyncio
async def test_reply_to_unrelated_bot_message_is_ignored(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Reply to a bot message that is neither a relay prompt nor a nudge
    (e.g. a /me status reply, a 'Nudges on' echo) must not fall through to the
    recency heuristic — the open relay message stays unanswered.

    This is the same invariant as the pre-existing
    ``test_reply_to_unmatched_does_not_fall_through`` test, restated here with
    the 19-05 nudge lookup path explicitly in scope.
    """
    token = seeded["token"]
    msg_id = await _create_message(app_client, token)

    # reply_to points to an arbitrary bot message (id 999888) that is stored in
    # neither telegram_message_id nor nudge_tg_message_id for any row.
    update = {
        "update_id": 19054,
        "message": {
            "message_id": 19054100,
            "chat": {"id": int(seeded["chat_id"])},  # type: ignore[arg-type]
            "from": {"id": int(seeded["bound_user_id"])},  # type: ignore[arg-type]
            "text": "thanks for the status",
            "reply_to_message_id": 999888,
        },
    }
    r = await _send_update(app_client, update)
    assert r.status_code == 200

    # The open relay message was NOT answered.
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 204


@pytest.mark.asyncio
async def test_button_tap_on_original_with_nudge_answers_and_cleanup_deletes_nudge(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Button tap on the original while a nudge exists → answers; 19-04's
    reaper cleanup sweep deletes the nudge on the next tick.

    This verifies that the button-tap path (which goes through
    ``_handle_callback_query`` → ``_record_answer``, brd §2.2's fifth path)
    leaves the row in the state the reaper's cleanup sweep expects, and that
    the sweep correctly deletes the nudge.  The button tap itself makes no
    Telegram call for the nudge — that is the hole the reaper fills.
    """
    from relay_server.db import connect as _connect
    from relay_server.reaper import reaper_tick
    from relay_server.waiters import WaiterRegistry

    token = seeded["token"]
    msg_id = await _create_message(app_client, token)
    nudge_tg_id = 8888

    # Inject a nudge id (simulating a live nudge sent by the reaper before the
    # user answered).
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "UPDATE messages SET nudge_tg_message_id = ? WHERE id = ?",
            (nudge_tg_id, msg_id),
        )

    # Tap the button on the original prompt (not the nudge).
    r = await post_callback_query(
        app_client,
        callback_data=encode_cb(msg_id, 0),
        chat_id=int(seeded["chat_id"]),  # type: ignore[arg-type]
        from_user_id=int(seeded["bound_user_id"]),  # type: ignore[arg-type]
    )
    assert r.status_code == 200

    # Answer is recorded.
    ans = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ans.status_code == 200
    assert ans.json()["answer"]["via"] == "button"

    # Before the reaper: nudge id is still set (the button tap makes no
    # Telegram call for the nudge — that is brd §2.2's fifth-path hole).
    row = conn.execute(
        "SELECT nudge_tg_message_id, state FROM messages WHERE id = ?", (msg_id,)
    ).fetchone()
    assert row["state"] == "answered"
    assert row["nudge_tg_message_id"] == nudge_tg_id

    # Run the reaper cleanup sweep.
    waiters = WaiterRegistry()
    await reaper_tick(conn, backend, waiters, make_test_config(db_path))

    # After the tick: nudge deleted from DB and from Telegram.
    row = conn.execute(
        "SELECT nudge_tg_message_id FROM messages WHERE id = ?", (msg_id,)
    ).fetchone()
    assert row["nudge_tg_message_id"] is None

    delete_calls = [c for c in backend.calls if c.method == "delete_message"]
    assert any(
        c.kwargs.get("telegram_message_id") == nudge_tg_id for c in delete_calls
    ), f"expected delete_message for nudge tg_id {nudge_tg_id}; calls: {delete_calls}"

    conn.close()
