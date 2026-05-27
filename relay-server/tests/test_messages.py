"""Tests for message lifecycle: PATCH validation, cancel keyboard strip,
and cross-installation isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.db import connect, init_schema
from relay_server.telegram_backend import FakeTelegramBackend
from relay_server.tokens import generate_token, hash_token

from tests.conftest import (  # type: ignore[attr-defined]
    _run_lifespan,
    make_test_config,
)


def _msg_body() -> dict:
    return {
        "kind": "question",
        "text": "?",
        "keyboard": [[{"label": "A", "value": "a"}]],
        "ttl_sec": 60,
    }


async def _create(client: httpx.AsyncClient, token: str) -> int:
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json=_msg_body(),
    )
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


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
    assert not any(c.method == "edit_message" for c in backend.calls)


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
