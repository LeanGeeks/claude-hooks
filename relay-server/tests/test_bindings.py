"""Tests for Phase 3: binding codes API + /bind webhook handler + relay-client CLI.

Coverage:
- POST /v1/bindings/request happy path
- POST /v1/bindings/request idempotent replay
- POST /v1/bindings/request idempotency: in-flight 409
- GET /v1/bindings/{code} → pending
- GET /v1/bindings/{code} → bound
- GET /v1/bindings/{code} → expired (410)
- GET /v1/bindings/{code} → cross-installation 404
- /bind webhook: unknown code → reply "Unknown bind code."
- /bind webhook: expired code → reply "has expired"
- /bind webhook: already-consumed → reply "already used"
- /bind webhook: valid → consumes, updates installations, sends confirmation
- /bind webhook: rebinding overwrites previous chat_id
- /bind webhook: concurrent duplicate — only one confirmation sent (TOCTOU fix)
- relay-client CLI: subcommands exist; bind command calls the correct endpoints
- relay-client CLI: 5xx transient retry during bind polling
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
from relay_server.db import connect, connect as db_connect, init_schema
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


# ---------------------------------------------------------------------------
# #1/#3 request_binding idempotency — in-flight 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_binding_idem_in_flight_409(
    db_path: str, seeded: dict
) -> None:
    """If the idempotency sentinel is pending (response_json IS NULL) a second
    concurrent caller must receive 409 idempotency_key_in_flight rather than
    silently generating a second code."""
    key = str(uuid.uuid4())
    token = seeded["token"]
    installation_id = seeded["installation_id"]

    # The endpoint stores _canonical_body_hash({}) as the request_hash for
    # this no-body endpoint.  Use the same value so the hash-mismatch guard
    # doesn't fire before we reach the in-flight 409.
    import hashlib
    import json as _json
    empty_hash = hashlib.sha256(
        _json.dumps({}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    # Manually insert a pending sentinel (response_json = NULL).
    conn = db_connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO idempotency_keys"
            "(installation_id, key, request_hash, response_json, created_at)"
            " VALUES (?, ?, ?, NULL, datetime('now'))",
            (installation_id, key, empty_hash),
        )
    conn.close()

    backend = FakeTelegramBackend()
    app = create_app(backend=backend, config=make_test_config(db_path))
    transport = httpx.ASGITransport(app=app)
    async with _run_lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/v1/bindings/request",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": key,
                },
            )
    assert r.status_code == 409
    assert r.json()["detail"] == "idempotency_key_in_flight"


# ---------------------------------------------------------------------------
# #2 /bind consume TOCTOU — concurrent deliveries, only one confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_webhook_concurrent_consume_sends_one_reply(
    app_client: httpx.AsyncClient,
    seeded: dict,
    backend: FakeTelegramBackend,
    db_path: str,
) -> None:
    """Two concurrent /bind webhook deliveries for the same code must result in
    exactly one 'Bound' confirmation reply sent to Telegram."""
    unbound_token = seeded["unbound_token"]
    r = await app_client.post(
        "/v1/bindings/request",
        headers={
            "Authorization": f"Bearer {unbound_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 200
    code = r.json()["code"]
    chat_id = 55555

    # Fire two webhook deliveries for the same code concurrently.
    import asyncio as _asyncio

    async def _send_bind(uid: int) -> httpx.Response:
        return await _bind_msg(
            app_client,
            code=code,
            chat_id=chat_id,
            from_user_id=uid,
            update_id=300 + uid,
        )

    results = await _asyncio.gather(_send_bind(10), _send_bind(11))

    # Both HTTP responses must be 200 (webhook must always ack).
    assert all(r.status_code == 200 for r in results)

    # Exactly one 'Bound' confirmation reply must have been sent.
    bound_replies = [
        c
        for c in backend.calls
        if c.method == "send_text"
        and "Bound" in c.kwargs.get("text", "")
        and c.kwargs.get("chat_id") == chat_id
    ]
    assert len(bound_replies) == 1, (
        f"Expected exactly 1 Bound reply, got {len(bound_replies)}: "
        + str([c.kwargs for c in bound_replies])
    )

    # The binding_codes row must be consumed exactly once.
    conn = db_connect(db_path)
    rows = conn.execute(
        "SELECT consumed_at FROM binding_codes WHERE code = ?", (code,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["consumed_at"] is not None


# ---------------------------------------------------------------------------
# #4 CLI bind polling — 5xx transient retry
# ---------------------------------------------------------------------------


def test_cli_bind_poll_retries_on_5xx(tmp_path) -> None:
    """The bind command must retry on 5xx and succeed on eventual 200."""
    import relay_server.client_cli as cli_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'server_url = "http://fake-relay"\ninstallation_token = "rly_test"\n'
    )

    call_counts: dict[str, int] = {"post": 0, "get": 0}

    class _FakeResp:
        def __init__(self, status: int, body: dict | None = None):
            self.status_code = status
            self._body = body or {}

        def json(self):
            return self._body

        @property
        def text(self):
            return str(self._body)

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, path, **kw):
            call_counts["post"] += 1
            return _FakeResp(200, {"code": "BIND-ABCD-EFGH", "expires_at": "2099-01-01T00:00:00+00:00"})

        def get(self, path, **kw):
            call_counts["get"] += 1
            # First two calls return 503; third returns bound.
            if call_counts["get"] <= 2:
                return _FakeResp(503)
            return _FakeResp(200, {"state": "bound", "chat_id": 99, "telegram_user_id": 7})

    original_make_client = cli_mod._make_client
    original_sleep = cli_mod.time.sleep

    cli_mod._make_client = lambda cfg: _FakeClient()
    cli_mod.time.sleep = lambda _: None  # skip all sleeps

    try:
        runner = CliRunner()
        result = runner.invoke(
            relay_client_cli,
            ["bind", "--config-path", str(cfg_path)],
        )
    finally:
        cli_mod._make_client = original_make_client
        cli_mod.time.sleep = original_sleep

    assert result.exit_code == 0, result.output
    assert "Bound" in result.output
    # The first two 503s were retried and the third call succeeded.
    assert call_counts["get"] == 3


def test_cli_bind_poll_exits_after_max_5xx_retries(tmp_path) -> None:
    """After _MAX_TRANSIENT_RETRIES consecutive 5xx, bind must exit non-zero."""
    import relay_server.client_cli as cli_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'server_url = "http://fake-relay"\ninstallation_token = "rly_test"\n'
    )

    class _FakeResp:
        def __init__(self, status: int):
            self.status_code = status
            self.text = "server error"

        def json(self):
            return {}

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, path, **kw):
            return _FakeResp(200)

        # Need json() for post response too.
        def _post_resp(self):
            class R:
                status_code = 200
                text = ""
                def json(self):
                    return {"code": "BIND-ABCD-EFGH", "expires_at": "2099-01-01T00:00:00"}
            return R()

        def __init__(self):
            pass

        def post(self, path, **kw):  # noqa: F811
            r = object.__new__(_FakeResp)
            r.status_code = 200
            r.text = ""
            r._body = {"code": "BIND-ABCD-EFGH", "expires_at": "2099-01-01T00:00:00"}
            r.json = lambda: r._body
            return r

        def get(self, path, **kw):
            return _FakeResp(500)

    original_make_client = cli_mod._make_client
    original_sleep = cli_mod.time.sleep

    cli_mod._make_client = lambda cfg: _FakeClient()
    cli_mod.time.sleep = lambda _: None

    try:
        runner = CliRunner()
        result = runner.invoke(
            relay_client_cli,
            ["bind", "--config-path", str(cfg_path)],
        )
    finally:
        cli_mod._make_client = original_make_client
        cli_mod.time.sleep = original_sleep

    assert result.exit_code != 0
