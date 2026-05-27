"""Tests for message lifecycle: PATCH validation, cancel keyboard strip,
cross-installation isolation, and the internal endpoint gating."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from relay_server.app import create_app
from relay_server.db import connect, init_schema
from relay_server.telegram_backend import FakeTelegramBackend
from relay_server.tokens import generate_token, hash_token

from tests.conftest import _run_lifespan  # type: ignore[attr-defined]


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


# ---- Cross-installation isolation (#14) ------------------------------------


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
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("RELAY_ENABLE_INTERNAL_ENDPOINTS", "1")
    app = create_app(db_path=db_path, backend=backend)
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

    # A creates a message; B must not see/touch it.
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


# ---- Internal endpoint gating (#3) ----------------------------------------


@asynccontextmanager
async def _client_with_env(
    db_path: str, backend: FakeTelegramBackend, monkeypatch_value: str | None
):
    import os

    saved = os.environ.get("RELAY_ENABLE_INTERNAL_ENDPOINTS")
    if monkeypatch_value is None:
        os.environ.pop("RELAY_ENABLE_INTERNAL_ENDPOINTS", None)
    else:
        os.environ["RELAY_ENABLE_INTERNAL_ENDPOINTS"] = monkeypatch_value
    try:
        app = create_app(db_path=db_path, backend=backend)
        transport = httpx.ASGITransport(app=app)
        async with _run_lifespan(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as ac:
                yield ac
    finally:
        if saved is None:
            os.environ.pop("RELAY_ENABLE_INTERNAL_ENDPOINTS", None)
        else:
            os.environ["RELAY_ENABLE_INTERNAL_ENDPOINTS"] = saved


@pytest.mark.asyncio
async def test_internal_endpoint_disabled_by_default(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "gated.db")
    conn = connect(db)
    init_schema(conn)
    tok = generate_token()
    with conn:
        conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id, created_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            ("x", hash_token(tok), 1),
        )
    conn.close()

    backend = FakeTelegramBackend()
    async with _client_with_env(db, backend, None) as client:
        msg_id = await _create(client, tok)
        r = await client.post(
            f"/v1/_internal/record_answer/{msg_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"value": "x"},
        )
        # The route is not registered at all -> 404 from the framework.
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_internal_endpoint_enabled_when_env_set(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "gated_on.db")
    conn = connect(db)
    init_schema(conn)
    tok = generate_token()
    with conn:
        conn.execute(
            "INSERT INTO installations(label, token_hash, telegram_chat_id, created_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            ("x", hash_token(tok), 1),
        )
    conn.close()

    backend = FakeTelegramBackend()
    async with _client_with_env(db, backend, "1") as client:
        msg_id = await _create(client, tok)
        r = await client.post(
            f"/v1/_internal/record_answer/{msg_id}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"value": "x"},
        )
        assert r.status_code == 200
