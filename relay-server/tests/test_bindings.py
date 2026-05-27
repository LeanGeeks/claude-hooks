"""Tests for Phase 3: binding codes API + /bind webhook handler + relay-client CLI.

Coverage:
- POST /v1/bindings/request happy path
- POST /v1/bindings/request idempotent replay
- GET /v1/bindings/{code} → pending
- GET /v1/bindings/{code} → bound
- GET /v1/bindings/{code} → expired (410)
- GET /v1/bindings/{code} → cross-installation 404
- /bind webhook: unknown code → reply "Unknown bind code."
- /bind webhook: expired code → reply "has expired"
- /bind webhook: already-consumed → reply "already used"
- /bind webhook: valid → consumes, updates installations, sends confirmation
- /bind webhook: rebinding overwrites previous chat_id
- relay-client CLI: subcommands exist; bind command calls the correct endpoints
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from click.testing import CliRunner

from relay_server.app import create_app
from relay_server.binding_codes import generate_code, normalise_code
from relay_server.client_cli import cli as relay_client_cli
from relay_server.db import connect, init_schema
from relay_server.telegram_backend import FakeTelegramBackend
from relay_server.tokens import generate_token, hash_token

from tests.conftest import (  # type: ignore[attr-defined]
    TEST_WEBHOOK_SECRET,
    _run_lifespan,
    make_test_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem(key: str | None = None) -> dict[str, str]:
    return {"Idempotency-Key": key or str(uuid.uuid4())}


async def _webhook(client: httpx.AsyncClient, update: dict) -> httpx.Response:
    return await client.post(
        f"/telegram/webhook/{TEST_WEBHOOK_SECRET}", json=update
    )


async def _bind_msg(
    client: httpx.AsyncClient,
    *,
    code: str,
    chat_id: int,
    from_user_id: int = 99,
    update_id: int = 100,
) -> httpx.Response:
    return await _webhook(
        client,
        {
            "update_id": update_id,
            "message": {
                "message_id": 9000 + update_id,
                "chat": {"id": chat_id},
                "from": {"id": from_user_id},
                "text": f"/bind {code}",
            },
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/bindings/request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_binding_returns_code(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(seeded["token"]), **_idem()},
    )
    assert r.status_code == 200
    body = r.json()
    assert "code" in body
    assert body["code"].startswith("BIND-")
    assert "expires_at" in body


@pytest.mark.asyncio
async def test_request_binding_idempotent_replay(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    key = str(uuid.uuid4())
    headers = {**_auth(seeded["token"]), "Idempotency-Key": key}
    r1 = await app_client.post("/v1/bindings/request", headers=headers)
    r2 = await app_client.post("/v1/bindings/request", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["code"] == r2.json()["code"]


@pytest.mark.asyncio
async def test_request_binding_works_when_unbound(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    """An unbound installation can still request a binding code."""
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(seeded["unbound_token"]), **_idem()},
    )
    assert r.status_code == 200
    assert r.json()["code"].startswith("BIND-")


# ---------------------------------------------------------------------------
# GET /v1/bindings/{code}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_binding_pending(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(seeded["token"]), **_idem()},
    )
    code = r.json()["code"]

    poll = await app_client.get(
        f"/v1/bindings/{code}", headers=_auth(seeded["token"])
    )
    assert poll.status_code == 200
    assert poll.json()["state"] == "pending"


@pytest.mark.asyncio
async def test_get_binding_bound(
    app_client: httpx.AsyncClient, seeded: dict, db_path: str
) -> None:
    # Request a code.
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(seeded["token"]), **_idem()},
    )
    code = r.json()["code"]
    chat_id = seeded["chat_id"]

    # Simulate the /bind command arriving via webhook.
    bind_r = await _bind_msg(
        app_client, code=code, chat_id=int(chat_id), from_user_id=55
    )
    assert bind_r.status_code == 200

    # Now poll — should be bound.
    poll = await app_client.get(
        f"/v1/bindings/{code}", headers=_auth(seeded["token"])
    )
    assert poll.status_code == 200
    data = poll.json()
    assert data["state"] == "bound"
    assert data["chat_id"] == int(chat_id)
    assert data["telegram_user_id"] == 55


@pytest.mark.asyncio
async def test_get_binding_expired_returns_410(
    db_path: str, seeded: dict
) -> None:
    """Manually insert an expired, unconsumed code and expect 410."""
    conn = connect(db_path)
    code = generate_code()
    past = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with conn:
        conn.execute(
            "INSERT INTO binding_codes(code, installation_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (code, seeded["installation_id"], past, past),
        )
    conn.close()

    backend = FakeTelegramBackend()
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            poll = await client.get(
                f"/v1/bindings/{code}",
                headers=_auth(seeded["token"]),
            )
            assert poll.status_code == 410
            assert poll.json()["state"] == "expired"


@pytest.mark.asyncio
async def test_get_binding_cross_installation_returns_404(
    app_client: httpx.AsyncClient, seeded: dict
) -> None:
    """A code belonging to installation A must not be visible to installation B."""
    # Request code for the bound installation (A).
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(seeded["token"]), **_idem()},
    )
    code = r.json()["code"]

    # Try to read it as the unbound installation (B).
    poll = await app_client.get(
        f"/v1/bindings/{code}", headers=_auth(seeded["unbound_token"])
    )
    assert poll.status_code == 404


# ---------------------------------------------------------------------------
# /bind webhook handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_webhook_unknown_code(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    before = len(backend.calls)
    r = await _bind_msg(
        app_client,
        code="BIND-2222-3333",
        chat_id=int(seeded["chat_id"]),
        update_id=200,
    )
    assert r.status_code == 200
    new_calls = [c for c in backend.calls[before:] if c.method == "send_text"]
    assert any("Unknown" in c.kwargs.get("text", "") for c in new_calls)


@pytest.mark.asyncio
async def test_bind_webhook_expired_code(
    db_path: str, seeded: dict
) -> None:
    conn = connect(db_path)
    code = generate_code()
    past = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with conn:
        conn.execute(
            "INSERT INTO binding_codes(code, installation_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (code, seeded["installation_id"], past, past),
        )
    conn.close()

    backend = FakeTelegramBackend()
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            r = await _bind_msg(
                client,
                code=code,
                chat_id=int(seeded["chat_id"]),
                update_id=210,
            )
            assert r.status_code == 200
            send_texts = [
                c for c in backend.calls if c.method == "send_text"
            ]
            assert any("expired" in c.kwargs.get("text", "") for c in send_texts)


@pytest.mark.asyncio
async def test_bind_webhook_already_consumed(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
) -> None:
    # Get a code and consume it.
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(seeded["token"]), **_idem()},
    )
    code = r.json()["code"]
    chat_id = int(seeded["chat_id"])
    await _bind_msg(app_client, code=code, chat_id=chat_id, update_id=220)

    # Bind again with same code.
    before = len(backend.calls)
    r2 = await _bind_msg(app_client, code=code, chat_id=chat_id, update_id=221)
    assert r2.status_code == 200
    new_texts = [
        c for c in backend.calls[before:] if c.method == "send_text"
    ]
    assert any("already used" in c.kwargs.get("text", "") for c in new_texts)


@pytest.mark.asyncio
async def test_bind_webhook_valid_consumes_and_updates(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    # Request a code for the *unbound* installation to prove chat_id gets set.
    unbound_token = seeded["unbound_token"]
    unbound_id = seeded["unbound_id"]
    r = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(unbound_token), **_idem()},
    )
    code = r.json()["code"]
    new_chat_id = 12345
    from_uid = 77

    bind_r = await _bind_msg(
        app_client,
        code=code,
        chat_id=new_chat_id,
        from_user_id=from_uid,
        update_id=230,
    )
    assert bind_r.status_code == 200

    # Confirm the reply text contains the installation label.
    send_texts = [c for c in backend.calls if c.method == "send_text"]
    assert any(
        "Bound" in c.kwargs.get("text", "") for c in send_texts
    ), [c.kwargs for c in send_texts]

    # Confirm DB updated.
    conn = connect(db_path)
    row = conn.execute(
        "SELECT telegram_chat_id, bound_user_id FROM installations WHERE id = ?",
        (unbound_id,),
    ).fetchone()
    code_row = conn.execute(
        "SELECT consumed_at, bound_chat_id, bound_user_id FROM binding_codes"
        " WHERE code = ?",
        (code,),
    ).fetchone()
    conn.close()

    assert row["telegram_chat_id"] == new_chat_id
    assert row["bound_user_id"] == from_uid
    assert code_row["consumed_at"] is not None
    assert code_row["bound_chat_id"] == new_chat_id
    assert code_row["bound_user_id"] == from_uid


@pytest.mark.asyncio
async def test_bind_webhook_rebinding_overwrites_chat(
    app_client: httpx.AsyncClient,
    seeded: dict,
    db_path: str,
) -> None:
    """Rebinding an already-bound installation overwrites the chat_id."""
    token = seeded["token"]
    original_chat = int(seeded["chat_id"])
    new_chat = 99999

    # First binding code → use in original_chat (already set by seeded fixture,
    # but let's do it explicitly via /bind to exercise the code path).
    r1 = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(token), **_idem()},
    )
    code1 = r1.json()["code"]
    await _bind_msg(
        app_client, code=code1, chat_id=original_chat, from_user_id=7, update_id=240
    )

    # Second binding code → bind to new_chat.
    r2 = await app_client.post(
        "/v1/bindings/request",
        headers={**_auth(token), **_idem()},
    )
    code2 = r2.json()["code"]
    await _bind_msg(
        app_client, code=code2, chat_id=new_chat, from_user_id=8, update_id=241
    )

    conn = connect(db_path)
    row = conn.execute(
        "SELECT telegram_chat_id FROM installations WHERE id = ?",
        (seeded["installation_id"],),
    ).fetchone()
    conn.close()
    assert row["telegram_chat_id"] == new_chat


# ---------------------------------------------------------------------------
# relay-client CLI (unit + light integration)
# ---------------------------------------------------------------------------


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(relay_client_cli, ["--help"])
    assert result.exit_code == 0


def test_cli_subcommands_exist() -> None:
    runner = CliRunner()
    for sub in ["bind", "whoami", "config"]:
        r = runner.invoke(relay_client_cli, [sub, "--help"])
        assert r.exit_code == 0, f"{sub} --help failed: {r.output}"


def test_cli_config_init(tmp_path) -> None:
    cfg_path = str(tmp_path / "config.toml")
    runner = CliRunner()
    r = runner.invoke(
        relay_client_cli,
        [
            "config",
            "init",
            "--server-url",
            "https://relay.example.com",
            "--token",
            "rly_test",
            "--config-path",
            cfg_path,
        ],
    )
    assert r.exit_code == 0, r.output
    content = (tmp_path / "config.toml").read_text()
    assert "relay.example.com" in content
    assert "rly_test" in content


def test_cli_whoami_uses_correct_endpoint(tmp_path) -> None:
    """whoami reads config file and hits /v1/installations/me."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'server_url = "http://fake-relay"\n'
        'installation_token = "rly_testtoken"\n'
    )

    import relay_server.client_cli as cli_mod

    captured: dict = {}

    original = cli_mod._make_client

    def mock_make_client(cfg: dict):
        captured["cfg"] = cfg

        class _FakeResp:
            status_code = 200

            def json(self):
                return {
                    "id": 1,
                    "label": "test-install",
                    "chat_bound": True,
                    "last_seen_at": "2026-05-27T00:00:00+00:00",
                }

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, path, **kw):
                captured["path"] = path
                return _FakeResp()

        return _FakeClient()

    cli_mod._make_client = mock_make_client
    try:
        runner = CliRunner()
        r = runner.invoke(
            relay_client_cli,
            ["whoami", "--config-path", str(cfg_path)],
        )
        assert r.exit_code == 0, r.output
        assert "test-install" in r.output
        assert captured.get("path") == "/v1/installations/me"
        assert captured["cfg"]["installation_token"] == "rly_testtoken"
    finally:
        cli_mod._make_client = original


# ---------------------------------------------------------------------------
# binding_codes utility tests
# ---------------------------------------------------------------------------


def test_generate_code_format() -> None:
    for _ in range(20):
        code = generate_code()
        assert code.startswith("BIND-")
        parts = code.split("-")
        assert len(parts) == 3
        assert all(len(p) == 4 for p in parts[1:])


def test_normalise_code_accepts_full_code() -> None:
    code = generate_code()
    assert normalise_code(code) == code


def test_normalise_code_case_insensitive() -> None:
    code = generate_code()
    assert normalise_code(code.lower()) == code


def test_normalise_code_rejects_garbage() -> None:
    assert normalise_code("not-a-code") is None
    assert normalise_code("BIND-000-0000") is None  # contains 0 which is excluded
