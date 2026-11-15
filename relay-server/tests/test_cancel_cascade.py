"""Epic 46-01: cancel reaches the escalated copy, carries an optional final
text, and per-message ladders have their own cap (``nudge_message_max``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from relay_server.app import create_app
from relay_server.config import RelayConfig, load_config
from relay_server.db import connect
from relay_server.reaper import _row_nudge_schedule
from relay_server.render import TAG, TAG_LINE, TELEGRAM_TEXT_LIMIT
from relay_server.telegram_backend import FakeTelegramBackend

from tests.conftest import _run_lifespan, make_test_config  # type: ignore[attr-defined]


def _auth(seeded: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['token']}"}


def _body(text: str = "Q?", **extra: object) -> dict:
    return {
        "kind": "question",
        "text": text,
        "keyboard": [[{"label": "A", "value": "a"}]],
        "ttl_sec": 600,
        **extra,
    }


async def _create(client, seeded, text="Q?", **extra) -> int:
    r = await client.post("/v1/messages", headers=_auth(seeded), json=_body(text, **extra))
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


def _add_child(db_path, seeded, parent_id, *, tg_id=5000, chat_id=77,
               text="Q?", nudge_tg=None) -> int:
    conn = connect(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO messages(installation_id, telegram_chat_id,"
            " telegram_message_id, kind, payload_json, state, created_at,"
            " expires_at, parent_message_id, next_nudge_at, nudge_tg_message_id)"
            " VALUES (?, ?, ?, 'question', ?, 'open', datetime('now'),"
            " datetime('now', '+1 hour'), ?, '2999-01-01T00:00:00+00:00', ?)",
            (
                seeded["installation_id"], chat_id, tg_id,
                json.dumps({"kind": "question", "text": text,
                            "keyboard": [[{"label": "A", "value": "a"}]]}),
                parent_id, nudge_tg,
            ),
        )
    cid = int(cur.lastrowid)
    conn.close()
    return cid


def _row(db_path, mid):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    finally:
        conn.close()


def _cancel_calls(backend, tg_id=None):
    return [
        (c.method, c.kwargs.get("telegram_message_id"), c.kwargs.get("text"))
        for c in backend.calls
        if c.method in ("edit_message", "edit_reply_markup")
        and (tg_id is None or c.kwargs.get("telegram_message_id") == tg_id)
    ]


# ---- cascade ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_cascades_to_open_escalated_child(
    app_client, seeded, backend, db_path
) -> None:
    pid = await _create(app_client, seeded)
    cid = _add_child(db_path, seeded, pid, tg_id=5000)
    r = await app_client.post(f"/v1/messages/{pid}/cancel", headers=_auth(seeded))
    assert r.status_code == 200
    assert _row(db_path, pid)["state"] == "cancelled"
    child = _row(db_path, cid)
    assert child["state"] == "cancelled"
    assert child["next_nudge_at"] is None
    calls = _cancel_calls(backend, 5000)
    assert ("edit_message", 5000, "Q?") in calls  # untagged body, no keyboard
    assert any(c[0] == "edit_message" for c in calls)
    edit = [c for c in backend.calls
            if c.method == "edit_message" and c.kwargs["telegram_message_id"] == 5000]
    assert edit[0].kwargs["keyboard"] is None
    assert TAG not in edit[0].kwargs["text"]


@pytest.mark.asyncio
async def test_repeated_cancel_still_cancels_child(
    app_client, seeded, backend, db_path
) -> None:
    pid = await _create(app_client, seeded)
    r = await app_client.post(f"/v1/messages/{pid}/cancel", headers=_auth(seeded))
    assert r.status_code == 200
    # An escalated copy that survived a cancel from a relay without the cascade.
    cid = _add_child(db_path, seeded, pid, tg_id=5001)
    r = await app_client.post(f"/v1/messages/{pid}/cancel", headers=_auth(seeded))
    assert r.status_code == 200
    assert _row(db_path, cid)["state"] == "cancelled"
    assert any(c[1] == 5001 for c in _cancel_calls(backend))


@pytest.mark.asyncio
async def test_child_without_telegram_id_cancelled_without_edit(
    app_client, seeded, backend, db_path
) -> None:
    pid = await _create(app_client, seeded)
    cid = _add_child(db_path, seeded, pid, tg_id=0)
    r = await app_client.post(f"/v1/messages/{pid}/cancel", headers=_auth(seeded))
    assert r.status_code == 200
    assert _row(db_path, cid)["state"] == "cancelled"
    assert not [c for c in _cancel_calls(backend) if c[1] == 0]


@pytest.mark.asyncio
async def test_child_nudge_is_retired(app_client, seeded, backend, db_path) -> None:
    pid = await _create(app_client, seeded)
    _add_child(db_path, seeded, pid, tg_id=5002, nudge_tg=6002)
    await app_client.post(f"/v1/messages/{pid}/cancel", headers=_auth(seeded))
    assert any(
        c.method == "delete_message" and c.kwargs.get("telegram_message_id") == 6002
        for c in backend.calls
    )


# ---- final_text ------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_text_on_open_row_shown_on_parent_and_child(
    app_client, seeded, backend, db_path
) -> None:
    pid = await _create(app_client, seeded)
    cid = _add_child(db_path, seeded, pid, tg_id=5003)
    end = f"<b>Withdrawn</b> - why{TAG_LINE}"
    r = await app_client.post(
        f"/v1/messages/{pid}/cancel", headers=_auth(seeded), json={"final_text": end}
    )
    assert r.status_code == 200
    clean = "<b>Withdrawn</b> - why"
    assert json.loads(_row(db_path, pid)["payload_json"])["text"] == clean
    assert json.loads(_row(db_path, cid)["payload_json"])["text"] == clean
    texts = [c.kwargs["text"] for c in backend.calls if c.method == "edit_message"]
    assert texts.count(clean) == 2


@pytest.mark.asyncio
async def test_final_text_on_answered_row_leaves_text_untouched(
    app_client, seeded, backend, db_path
) -> None:
    pid = await _create(app_client, seeded, text="orig")
    conn = connect(db_path)
    with conn:
        conn.execute("UPDATE messages SET state='answered' WHERE id=?", (pid,))
    conn.close()
    r = await app_client.post(
        f"/v1/messages/{pid}/cancel", headers=_auth(seeded),
        json={"final_text": "late"},
    )
    assert r.status_code == 200
    row = _row(db_path, pid)
    assert row["state"] == "answered"
    assert json.loads(row["payload_json"])["text"] == "orig"
    # …and nothing displayed it either: no edit of any kind carries the text.
    assert not [
        c for c in backend.calls
        if c.method == "edit_message" and "late" in (c.kwargs.get("text") or "")
    ]


@pytest.mark.asyncio
async def test_final_text_too_long_is_422(app_client, seeded, db_path) -> None:
    pid = await _create(app_client, seeded)
    r = await app_client.post(
        f"/v1/messages/{pid}/cancel", headers=_auth(seeded),
        json={"final_text": "x" * (TELEGRAM_TEXT_LIMIT + 1)},
    )
    assert r.status_code == 422
    assert _row(db_path, pid)["state"] == "open"
    r = await app_client.post(
        f"/v1/messages/{pid}/cancel", headers=_auth(seeded),
        json={"final_text": "x" * TELEGRAM_TEXT_LIMIT},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"json": {}}, {"json": {"final_text": None}}])
async def test_no_final_text_traffic_identical_to_legacy(
    seeded, db_path, kwargs
) -> None:
    async def run(**kw):
        be = FakeTelegramBackend()
        app = create_app(backend=be, config=make_test_config(db_path))
        async with _run_lifespan(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as c:
                mid = await _create(c, seeded)
                r = await c.post(f"/v1/messages/{mid}/cancel", headers=_auth(seeded), **kw)
                assert r.status_code == 200
        return [(c.method, c.kwargs.get("text"), c.kwargs.get("keyboard"))
                for c in be.calls if c.method != "send_message"]

    traffic = await run(**kwargs)
    assert traffic == await run()
    # Pinned literally, not just against itself: what a body-less cancel of a
    # childless message sent to Telegram before this task — one text edit that
    # drops the #unanswered tag and the keyboard, then the keyboard strip.
    assert [method for method, _text, _kb in traffic] == ["edit_message", "edit_reply_markup"]
    assert all(kb is None for _method, _text, kb in traffic)
    assert "#unanswered" not in (traffic[0][1] or "")


# ---- nudge_message_max -----------------------------------------------------


@pytest.mark.asyncio
async def test_four_rung_ladder_accepted_seeded_and_repeating(
    app_client, seeded, db_path
) -> None:
    mid = await _create(app_client, seeded, nudge_schedule="4h,1d,3d,7d*")
    row = _row(db_path, mid)
    assert row["nudge_schedule_override"] == "4h,1d,3d,7d*"
    # Seeded from the message's own first rung (4h), not from the chat ladder
    # (15m) that a rejected override would have fallen back to.  The recipient
    # in this fixture has no availability windows, so active time is wall time.
    assert row["next_nudge_at"] is not None
    seeded_at = datetime.fromisoformat(row["next_nudge_at"])
    created_at = datetime.fromisoformat(row["created_at"])
    if seeded_at.tzinfo is None:
        seeded_at = seeded_at.replace(tzinfo=timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    assert timedelta(hours=3, minutes=55) < seeded_at - created_at < timedelta(hours=4, minutes=5)
    sched, repeats = _row_nudge_schedule(row, None, RelayConfig())  # type: ignore[arg-type]
    assert repeats is True
    assert sched[-1] == timedelta(days=7) and len(sched) == 4


@pytest.mark.asyncio
async def test_nine_rung_ladder_is_422_naming_cap(app_client, seeded) -> None:
    r = await app_client.post(
        "/v1/messages", headers=_auth(seeded),
        json=_body(nudge_schedule=",".join(["1h"] * 9)),
    )
    assert r.status_code == 422
    assert "nudge_message_max=8" in r.json()["detail"]


@pytest.mark.asyncio
async def test_per_chat_nudge_cap_is_still_nudge_max(
    app_client, seeded, backend
) -> None:
    r = await app_client.post(
        f"/telegram/webhook/{make_test_config('x').webhook_secret}",
        json={"update_id": 7001, "message": {
            "message_id": 8001, "chat": {"id": seeded["chat_id"]},
            "from": {"id": seeded["bound_user_id"]}, "text": "/nudge 1h,2h,3h,4h"}},
    )
    assert r.status_code == 200
    texts = [c.kwargs.get("text", "") for c in backend.calls if c.method == "send_text"]
    assert any("server cap is 3" in t for t in texts)


def test_env_sets_both_caps_as_ints() -> None:
    cfg = load_config(
        config_path="/nonexistent.toml",
        env={"RELAY_NUDGE_MAX": "5", "RELAY_NUDGE_MESSAGE_MAX": "12"},
    )
    assert cfg.nudge_max == 5 and cfg.nudge_message_max == 12
    assert load_config(config_path="/nonexistent.toml", env={}).nudge_message_max == 8
