"""Tests for re-answerable question groups (AskUserQuestion fan-out).

A group is a set of relay messages sharing a ``group_id``. Until every member
has an answer, each stays editable: button taps record a *provisional* choice
and re-render the keyboard with the selection highlighted; a custom text reply
is reflected in the body. Once all members are answered, the relay finalizes the
whole group — strips every keyboard and bakes each choice into the message text.

Covers:
- A tap keeps the message open and highlights the chosen button.
- Re-tapping a different option updates the provisional choice (change of mind).
- The group finalizes (all terminal, keyboards stripped, answers baked) only
  once every member is answered.
- A custom text reply is recorded provisionally and reflected in the body.
- A single-member group finalizes on the first answer.
"""

from __future__ import annotations

import httpx
import pytest

from relay_server.callback_data import encode as encode_cb

from tests.conftest import TEST_WEBHOOK_SECRET  # type: ignore[attr-defined]


# ---- Helpers ---------------------------------------------------------------


async def _create_grouped(
    client: httpx.AsyncClient,
    token: str,
    *,
    group_id: str,
    group_total: int,
    options: list[tuple[str, str]],
    text: str = "Pick one",
) -> int:
    """Create one grouped question message; return its relay message_id."""
    body = {
        "kind": "question",
        "text": text,
        "keyboard": [[{"label": label, "value": value}] for label, value in options],
        "reply_required": True,
        "ttl_sec": 60,
        "group_id": group_id,
        "group_total": group_total,
    }
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


async def _tap(
    client: httpx.AsyncClient,
    *,
    relay_msg_id: int,
    option_idx: int,
    chat_id: int,
    user_id: int,
    update_id: int,
) -> httpx.Response:
    return await client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": update_id,
            "callback_query": {
                "id": f"cbq-{update_id}",
                "from": {"id": user_id},
                "data": encode_cb(relay_msg_id, option_idx),
                "message": {"message_id": 1000, "chat": {"id": chat_id}},
            },
        },
    )


async def _reply(
    client: httpx.AsyncClient,
    *,
    reply_to_tg_id: int,
    text: str,
    chat_id: int,
    user_id: int,
    update_id: int,
) -> httpx.Response:
    return await client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}",
        json={
            "update_id": update_id,
            "message": {
                "message_id": 9000 + update_id,
                "chat": {"id": chat_id},
                "from": {"id": user_id},
                "text": text,
                "reply_to_message_id": reply_to_tg_id,
            },
        },
    )


async def _answer(client: httpx.AsyncClient, token: str, msg_id: int) -> httpx.Response:
    return await client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )


def _edits_for(backend, tg_id: int):
    return [
        c
        for c in backend.calls
        if c.method == "edit_message" and c.kwargs["telegram_message_id"] == tg_id
    ]


# ---- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_tap_keeps_open_and_highlights(
    app_client: httpx.AsyncClient, seeded: dict[str, object], backend
) -> None:
    token = seeded["token"]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]
    user_id = int(seeded["bound_user_id"])  # type: ignore[arg-type]

    m1 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("Yes", "qa0"), ("No", "qa1")],
    )
    await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("A", "qa0"), ("B", "qa1")],
    )

    r = await _tap(
        app_client, relay_msg_id=m1, option_idx=0,
        chat_id=chat_id, user_id=user_id, update_id=10,
    )
    assert r.status_code == 200

    # Still open — the group is not complete (m2 unanswered).
    assert (await _answer(app_client, token, m1)).status_code == 204

    # The first message (tg id 1000) was re-rendered with the chosen button
    # marked and the keyboard kept live (callback routing preserved).
    edits = _edits_for(backend, 1000)
    assert edits, "expected a highlight edit on the tapped message"
    kb = edits[-1].kwargs["keyboard"]
    assert kb is not None
    assert kb[0][0]["label"].startswith("✅ ")
    assert edits[-1].kwargs["relay_message_id"] == m1
    assert any(c.method == "answer_callback_query" for c in backend.calls)


@pytest.mark.asyncio
async def test_change_of_mind_updates_provisional_choice(
    app_client: httpx.AsyncClient, seeded: dict[str, object], backend
) -> None:
    token = seeded["token"]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]
    user_id = int(seeded["bound_user_id"])  # type: ignore[arg-type]

    m1 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("Yes", "qa0"), ("No", "qa1")],
    )
    m2 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("A", "qa0"), ("B", "qa1")],
    )

    # Tap "Yes", change mind to "No", both before answering m2.
    await _tap(app_client, relay_msg_id=m1, option_idx=0,
               chat_id=chat_id, user_id=user_id, update_id=10)
    await _tap(app_client, relay_msg_id=m1, option_idx=1,
               chat_id=chat_id, user_id=user_id, update_id=11)

    # Highlight always rebuilds from the pristine keyboard: only "No" is marked.
    kb = _edits_for(backend, 1000)[-1].kwargs["keyboard"]
    assert kb[0][0]["label"] == "Yes"  # row 0 unmarked
    assert kb[1][0]["label"] == "✅ No"

    # Finalize the group by answering m2.
    await _tap(app_client, relay_msg_id=m2, option_idx=0,
               chat_id=chat_id, user_id=user_id, update_id=12)

    a1 = await _answer(app_client, token, m1)
    assert a1.status_code == 200
    assert a1.json()["answer"]["value"] == "qa1"  # the changed choice wins


@pytest.mark.asyncio
async def test_group_finalizes_only_when_all_answered(
    app_client: httpx.AsyncClient, seeded: dict[str, object], backend
) -> None:
    token = seeded["token"]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]
    user_id = int(seeded["bound_user_id"])  # type: ignore[arg-type]

    m1 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("Yes", "qa0"), ("No", "qa1")], text="Q1",
    )
    m2 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("A", "qa0"), ("B", "qa1")], text="Q2",
    )

    await _tap(app_client, relay_msg_id=m1, option_idx=1,
               chat_id=chat_id, user_id=user_id, update_id=10)
    # m1 answered, m2 not → neither is terminal yet.
    assert (await _answer(app_client, token, m1)).status_code == 204
    assert (await _answer(app_client, token, m2)).status_code == 204

    await _tap(app_client, relay_msg_id=m2, option_idx=0,
               chat_id=chat_id, user_id=user_id, update_id=11)

    # Both now terminal with their chosen answers.
    a1 = await _answer(app_client, token, m1)
    a2 = await _answer(app_client, token, m2)
    assert a1.json()["answer"]["value"] == "qa1"
    assert a2.json()["answer"]["value"] == "qa0"

    # Finalization stripped both keyboards (keyboard=None) and baked the choice
    # into each body (✅ marker present).
    for tg_id in (1000, 1001):
        final = [e for e in _edits_for(backend, tg_id) if e.kwargs["keyboard"] is None]
        assert final, f"expected a keyboard-stripping finalize edit on {tg_id}"
        assert "✅" in final[-1].kwargs["text"]


@pytest.mark.asyncio
async def test_custom_reply_is_reflected_then_finalizes(
    app_client: httpx.AsyncClient, seeded: dict[str, object], backend
) -> None:
    token = seeded["token"]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]
    user_id = int(seeded["bound_user_id"])  # type: ignore[arg-type]

    m1 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("Yes", "qa0"), ("No", "qa1")],
    )
    m2 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("A", "qa0"), ("B", "qa1")],
    )

    # Custom text reply to m1 (tg id 1000): provisional, body reflects it,
    # keyboard kept (un-highlighted) so the user can still switch to a button.
    await _reply(app_client, reply_to_tg_id=1000, text="my own answer",
                 chat_id=chat_id, user_id=user_id, update_id=20)
    assert (await _answer(app_client, token, m1)).status_code == 204
    reflect = _edits_for(backend, 1000)[-1]
    assert "✍️" in reflect.kwargs["text"] and "my own answer" in reflect.kwargs["text"]
    assert reflect.kwargs["keyboard"] is not None
    assert all(not b["label"].startswith("✅") for row in reflect.kwargs["keyboard"] for b in row)

    # Answer m2 → group finalizes; m1's recorded answer is the custom text.
    await _tap(app_client, relay_msg_id=m2, option_idx=1,
               chat_id=chat_id, user_id=user_id, update_id=21)
    a1 = (await _answer(app_client, token, m1)).json()
    assert a1["answer"]["via"] == "reply"
    assert a1["answer"]["text"] == "my own answer"


@pytest.mark.asyncio
async def test_baked_answer_text_is_html_escaped(
    app_client: httpx.AsyncClient, seeded: dict[str, object], backend
) -> None:
    """Baked option labels / replies must be HTML-escaped: outbound edits use
    parse_mode=HTML, so an unescaped ``<``/``&`` would make Telegram reject the
    finalize edit and leave the keyboard un-stripped."""
    token = seeded["token"]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]
    user_id = int(seeded["bound_user_id"])  # type: ignore[arg-type]

    # Option label with HTML-special chars on the finalizing (button) message.
    m1 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("<b>A & B</b>", "qa0"), ("No", "qa1")],
    )
    m2 = await _create_grouped(
        app_client, token, group_id="g1", group_total=2,
        options=[("X", "qa0"), ("Y", "qa1")],
    )

    # Reply to m2 with special chars, then finalize via a button on m1.
    await _reply(app_client, reply_to_tg_id=1001, text="a < b & c",
                 chat_id=chat_id, user_id=user_id, update_id=30)
    await _tap(app_client, relay_msg_id=m1, option_idx=0,
               chat_id=chat_id, user_id=user_id, update_id=31)

    final_m1 = [e for e in _edits_for(backend, 1000) if e.kwargs["keyboard"] is None][-1]
    final_m2 = [e for e in _edits_for(backend, 1001) if e.kwargs["keyboard"] is None][-1]
    assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in final_m1.kwargs["text"]
    assert "<b>" not in final_m1.kwargs["text"].split("✅")[-1]
    assert "a &lt; b &amp; c" in final_m2.kwargs["text"]


@pytest.mark.asyncio
async def test_single_member_group_finalizes_immediately(
    app_client: httpx.AsyncClient, seeded: dict[str, object], backend
) -> None:
    token = seeded["token"]
    chat_id = int(seeded["chat_id"])  # type: ignore[arg-type]
    user_id = int(seeded["bound_user_id"])  # type: ignore[arg-type]

    m1 = await _create_grouped(
        app_client, token, group_id="solo", group_total=1,
        options=[("Yes", "qa0"), ("No", "qa1")],
    )
    await _tap(app_client, relay_msg_id=m1, option_idx=0,
               chat_id=chat_id, user_id=user_id, update_id=10)

    a1 = await _answer(app_client, token, m1)
    assert a1.status_code == 200
    assert a1.json()["answer"]["value"] == "qa0"
    final = [e for e in _edits_for(backend, 1000) if e.kwargs["keyboard"] is None]
    assert final, "single-member group should finalize on first answer"
