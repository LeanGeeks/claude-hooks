"""Tests for message lifecycle: PATCH validation, cancel keyboard strip,
and cross-installation isolation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.db import connect, init_schema
from relay_server.render import (
    TAG,
    TAG_LINE,
    TELEGRAM_TEXT_LIMIT,
    _html_safe_prefix,
    render_body,
)
from relay_server.telegram_backend import FakeTelegramBackend, TelegramApiError
from relay_server.tokens import generate_token, hash_token

from tests.conftest import (  # type: ignore[attr-defined]
    _run_lifespan,
    make_test_config,
)


def _msg_body(text: str = "?") -> dict:
    return {
        "kind": "question",
        "text": text,
        "keyboard": [[{"label": "A", "value": "a"}]],
        "ttl_sec": 60,
    }


async def _create(client: httpx.AsyncClient, token: str, text: str = "?") -> int:
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=_msg_body(text),
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


def _last_text(backend: FakeTelegramBackend) -> str:
    """The most recent body Telegram was asked to display."""
    for call in reversed(backend.calls):
        if call.method == "edit_message" and call.kwargs.get("text") is not None:
            return call.kwargs["text"]
        if call.method == "send_message":
            return call.kwargs["text"]
    raise AssertionError("nothing was ever sent to Telegram")


@pytest.mark.asyncio
async def test_patch_keyboard_replacement_rejected(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    """Phase 2 explicitly refuses keyboard replacement via PATCH because the
    edit path can't reconstruct correct encoded callback_data without the
    original message_id (see #7 in the review)."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)
    r = await app_client.patch(
        f"/v1/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"keyboard": [[{"label": "X", "value": "x"}]]},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "keyboard_replace_not_supported"


@pytest.mark.asyncio
async def test_patch_with_no_fields_returns_400(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    token = seeded["token"]
    msg_id = await _create(app_client, token)
    r = await app_client.patch(
        f"/v1/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "at_least_one_field_required"


@pytest.mark.asyncio
async def test_cancel_uses_edit_reply_markup(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    token = seeded["token"]
    msg_id = await _create(app_client, token)
    r = await app_client.post(
        f"/v1/messages/{msg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    # The cancel path must strip the keyboard via the dedicated method, not
    # an ambiguous edit_message(text=None, keyboard=None).
    erm = [c for c in backend.calls if c.method == "edit_reply_markup"]
    assert len(erm) == 1
    assert erm[0].kwargs["keyboard"] is None
    # Since 19-03 it also rewrites the body — that is how the tag comes off —
    # but always with a text, never as a keyboard edit in disguise.
    edits = [c for c in backend.calls if c.method == "edit_message"]
    assert len(edits) == 1
    assert edits[0].kwargs["text"] == "?"
    assert edits[0].kwargs["keyboard"] is None


# ---- Idempotent finalize (15-07) ------------------------------------------
#
# The client finalizes a message by PATCHing the body and then cancelling.
# ``editMessageText`` with no ``reply_markup`` already drops the keyboard, so
# the cancel that follows asks Telegram to remove a keyboard that is no longer
# there — answered with 400 "message is not modified". That is the *happy* path
# of every finalize, and it used to surface as an HTTP 500 the caller logged and
# ignored (three of them in 15-07's live run, all on runs that otherwise passed).


def _not_modified() -> TelegramApiError:
    return TelegramApiError(
        400,
        "Bad Request: message is not modified: specified new message content"
        " and reply markup are exactly the same as a current content and reply"
        " markup of the message",
    )


@pytest.mark.asyncio
async def test_cancel_is_idempotent_when_the_keyboard_is_already_gone(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Cancel's contract is *this message has no keyboard and is closed*, not
    *change something*. Already being in that state is success."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)

    async def _raise(**_kwargs):
        raise _not_modified()

    backend.edit_reply_markup = _raise  # type: ignore[method-assign]

    r = await app_client.post(
        f"/v1/messages/{msg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_cancel_reports_a_real_telegram_failure_as_502(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Only "not modified" is benign. Anything else is an upstream fault and
    must say so with a gateway status, not an anonymous 500."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)

    async def _raise(**_kwargs):
        raise TelegramApiError(400, "Bad Request: message to edit not found")

    backend.edit_reply_markup = _raise  # type: ignore[method-assign]

    r = await app_client.post(
        f"/v1/messages/{msg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "telegram_error"


@pytest.mark.asyncio
async def test_cancel_still_closes_the_message_when_telegram_balks(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """The state flip precedes the Bot API call, so a message whose keyboard was
    already stripped is still closed to further answers — the long-poller that
    is parked on it must be woken rather than left to time out."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)

    async def _raise(**_kwargs):
        raise _not_modified()

    backend.edit_reply_markup = _raise  # type: ignore[method-assign]

    await app_client.post(
        f"/v1/messages/{msg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
        params={"wait": 5},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_patch_is_idempotent_when_the_body_already_reads_that_way(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    token = seeded["token"]
    msg_id = await _create(app_client, token)

    async def _raise(**_kwargs):
        raise _not_modified()

    backend.edit_message = _raise  # type: ignore[method-assign]

    r = await app_client.patch(
        f"/v1/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "same"},
    )
    assert r.status_code == 200, r.text


# ---- The finalize-then-cancel regression (19-03) ---------------------------
#
# ``finalize_message`` in the hook PATCHes the baked answer text and *then*
# cancels (a cancelled message can still be edited). Cancel now authors text, so
# it re-renders the body — and the only body it can reach is ``payload_json``.
# If PATCH does not write its text back there, cancel rewrites the message with
# the *create-time* body and silently wipes the ✍️ answer the hook just baked in.
# That failure is invisible to any test that only asserts on the tag.


@pytest.mark.asyncio
async def test_cancel_preserves_a_patched_finalized_body(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    token = seeded["token"]
    msg_id = await _create(app_client, token, text="original body")

    finalized = "original body\n\n✍️ some answer"
    r = await app_client.patch(
        f"/v1/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": finalized},
    )
    assert r.status_code == 200, r.text

    r = await app_client.post(
        f"/v1/messages/{msg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text

    final = _last_text(backend)
    assert "✍️ some answer" in final, (
        "cancel wiped the answer the client baked in — it re-rendered from a"
        f" stale payload. Telegram was left showing: {final!r}"
    )
    assert "#unanswered" not in final


@pytest.mark.asyncio
async def test_cancel_survives_not_modified_on_its_text_edit(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Cancelling a message that never carried the tag makes the text edit a
    no-op, which Telegram reports as a 400. The transition still completes and
    the keyboard is still stripped — no 502."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)

    async def _raise(**_kwargs):
        raise _not_modified()

    backend.edit_message = _raise  # type: ignore[method-assign]

    r = await app_client.post(
        f"/v1/messages/{msg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert any(c.method == "edit_reply_markup" for c in backend.calls)
    r = await app_client.get(
        f"/v1/messages/{msg_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_of_an_already_terminal_message_leaves_the_body_alone(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """A second cancel must not re-render: the body another path baked in (a
    group finalization's ``✅`` line, say) is text the payload does not carry,
    and rewriting from the payload would erase it."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)
    for _ in range(2):
        r = await app_client.post(
            f"/v1/messages/{msg_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
    edits = [c for c in backend.calls if c.method == "edit_message"]
    assert len(edits) == 1, "only the cancel that flipped the state may rewrite"


# ---- The #unanswered tag (19-03) ------------------------------------------


def _stored_text(db_path: str, msg_id: int) -> str:
    """The canonical body as persisted in ``payload_json`` — always untagged."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        assert row is not None
        return json.loads(row["payload_json"])["text"]
    finally:
        conn.close()


async def _create_notification(
    client: httpx.AsyncClient, token: str, *, text: str, reply_required: bool
) -> int:
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "kind": "notification",
            "text": text,
            "reply_required": reply_required,
            "ttl_sec": 60,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


@pytest.mark.asyncio
async def test_open_permission_message_is_tagged(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """An open message awaiting a human carries the trailing tag — that is what
    makes Telegram's hashtag search the pending-work index."""
    token = seeded["token"]
    r = await app_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "kind": "permission",
            "text": "Allow rm -rf /tmp/x?",
            "keyboard": [[{"label": "Allow", "value": "allow"}]],
            "ttl_sec": 60,
        },
    )
    assert r.status_code == 200, r.text
    sent = backend.sent[-1].text
    assert sent == "Allow rm -rf /tmp/x?" + TAG_LINE
    assert sent.endswith(TAG)


@pytest.mark.asyncio
async def test_informational_notification_is_not_tagged(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    token = seeded["token"]
    await _create_notification(
        app_client, token, text="build finished", reply_required=False
    )
    assert backend.sent[-1].text == "build finished"


@pytest.mark.asyncio
async def test_idle_notification_with_reply_required_is_not_tagged(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """Invariant 7 / brd §4.1, and the reason the predicate keys on ``kind``.

    ``notification_hook`` sends idle-session messages with
    ``reply_required=True`` whenever reply-from-Telegram is on. A predicate
    keyed on ``reply_required`` alone would sweep every idle session into the
    tag — and, once the nudge engine lands, ping about it at 09:00.
    ``#unanswered`` means *an agent is blocked on you*.
    """
    token = seeded["token"]
    await _create_notification(
        app_client, token, text="session went idle", reply_required=True
    )
    assert backend.sent[-1].text == "session went idle"
    assert TAG not in backend.sent[-1].text


@pytest.mark.asyncio
async def test_patch_twice_with_the_same_text_yields_one_tag(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Rendering is idempotent by construction: the body is stripped before the
    tag is appended, so a retried PATCH cannot double it."""
    token = seeded["token"]
    msg_id = await _create(app_client, token)
    for _ in range(2):
        r = await app_client.patch(
            f"/v1/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "updated body"},
        )
        assert r.status_code == 200, r.text
    final = _last_text(backend)
    assert final.count(TAG) == 1
    assert final == "updated body" + TAG_LINE
    # ...and the payload stays canonical: the visible body minus the tag.
    assert _stored_text(db_path, msg_id) == "updated body"


@pytest.mark.asyncio
async def test_client_supplied_tag_is_stripped_on_ingest(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """The tag never enters ``payload_json`` from any direction — including a
    client that echoes our own rendered text back at us."""
    token = seeded["token"]
    msg_id = await _create(app_client, token, text="please review" + TAG_LINE)
    assert backend.sent[-1].text == "please review" + TAG_LINE
    assert backend.sent[-1].text.count(TAG) == 1
    assert _stored_text(db_path, msg_id) == "please review"

    r = await app_client.patch(
        f"/v1/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "second look" + TAG_LINE},
    )
    assert r.status_code == 200, r.text
    assert _last_text(backend).count(TAG) == 1
    assert _stored_text(db_path, msg_id) == "second look"


# ---- Length guard (brd §2.6) ----------------------------------------------


@pytest.mark.asyncio
async def test_long_body_is_trimmed_rather_than_sent_oversize(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """A 4090-character body plus the tag would exceed Telegram's 4096 cap. The
    guard trims at a line boundary; the *stored* body keeps every character."""
    token = seeded["token"]
    body = "x" * 4000 + "\n" + "y" * 89
    assert len(body) == 4090
    msg_id = await _create(app_client, token, text=body)

    sent = backend.sent[-1].text
    assert len(sent) <= TELEGRAM_TEXT_LIMIT
    assert sent.endswith(TAG_LINE)
    assert sent.startswith("x" * 4000)
    assert "y" not in sent  # trimmed at the newline, not mid-run
    assert _stored_text(db_path, msg_id) == body  # canonical body intact


@pytest.mark.asyncio
async def test_untrimmable_long_body_drops_the_tag(
    app_client: httpx.AsyncClient,
    seeded: dict[str, object],
    backend: FakeTelegramBackend,
) -> None:
    """With no safe boundary to cut at, dropping the tag beats sending a body
    Telegram would reject outright."""
    token = seeded["token"]
    body = "x" * 4090
    await _create(app_client, token, text=body)
    sent = backend.sent[-1].text
    assert sent == body
    assert TAG not in sent


def test_trim_never_splits_an_entity_or_a_tag() -> None:
    """The guard's notion of a safe cut: not inside a tag, not inside an entity,
    and not between an opening tag and its close (all three make Telegram reject
    the edit with "can't parse entities")."""
    assert _html_safe_prefix("plain text")
    assert _html_safe_prefix("<pre>code</pre>\nmore")
    assert _html_safe_prefix("a &lt; b")
    assert not _html_safe_prefix("<b")
    assert not _html_safe_prefix("a &l")
    assert not _html_safe_prefix("<pre>code\nmore")


def test_render_body_is_pure_and_state_driven() -> None:
    """One function owns the append; no call site decides for itself."""
    payload = {"kind": "question", "text": "Q", "reply_required": True}
    assert render_body(payload, "open") == "Q" + TAG_LINE
    for state in ("answered", "cancelled", "expired"):
        assert render_body(payload, state) == "Q"
    # A group member with neither buttons nor reply_required still awaits a human.
    assert render_body(
        {"kind": "question", "text": "Q", "group_id": "g"}, "open"
    ) == "Q" + TAG_LINE
    # Nothing to await → no tag, whatever the state.
    assert render_body({"kind": "question", "text": "Q"}, "open") == "Q"


# ---- Cross-installation isolation -----------------------------------------


@pytest.fixture
def two_installations(db_path: str) -> dict[str, object]:
    conn = connect(db_path)
    init_schema(conn)
    tok_a = generate_token()
    tok_b = generate_token()
    with conn:
        a = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id, created_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            ("a", hash_token(tok_a), 100),
        ).lastrowid
        b = conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id, created_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            ("b", hash_token(tok_b), 200),
        ).lastrowid
    conn.close()
    return {"token_a": tok_a, "token_b": tok_b, "id_a": a, "id_b": b}


@pytest_asyncio.fixture
async def isolation_client(
    db_path: str,
    backend: FakeTelegramBackend,
    two_installations: dict[str, object],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_cross_installation_isolation_404(
    isolation_client: httpx.AsyncClient, two_installations: dict[str, object]
) -> None:
    tok_a = two_installations["token_a"]
    tok_b = two_installations["token_b"]

    msg = await _create(isolation_client, tok_a)
    auth_b = {"Authorization": f"Bearer {tok_b}"}

    r = await isolation_client.get(f"/v1/messages/{msg}/answer", headers=auth_b)
    assert r.status_code == 404

    r = await isolation_client.patch(
        f"/v1/messages/{msg}", headers=auth_b, json={"text": "evil"}
    )
    assert r.status_code == 404

    r = await isolation_client.delete(f"/v1/messages/{msg}", headers=auth_b)
    assert r.status_code == 404
