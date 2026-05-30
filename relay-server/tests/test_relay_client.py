"""Unit tests for the synchronous RelayClient.

Uses ``httpx.MockTransport`` so we can assert on request shape (URL, headers,
body) and return canned responses without spinning up the server.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from relay_server.client import (
    Answer,
    BindingExpiredError,
    IdempotencyMismatchError,
    MessageHandle,
    NotBoundError,
    RelayClient,
    RelayError,
)

SERVER = "http://relay.test"
TOKEN = "rly_test_token"


def _make_client(handler) -> RelayClient:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(
        transport=transport,
        base_url=SERVER,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return RelayClient(SERVER, TOKEN, client=client)


# ---- send_message ----------------------------------------------------------


def test_send_message_posts_body_and_idempotency_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message_id": 10, "telegram_message_id": 999}
        )

    rc = _make_client(handler)
    try:
        handle = rc.send_message(
            text="hi",
            keyboard=[[{"label": "Yes", "value": "y"}]],
            kind="question",
            ttl_sec=120,
            idempotency_key="key-1",
        )
    finally:
        rc.close()

    assert handle == MessageHandle(message_id=10, telegram_message_id=999)
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert captured["headers"]["idempotency-key"] == "key-1"
    assert captured["body"] == {
        "kind": "question",
        "text": "hi",
        "reply_required": False,
        "ttl_sec": 120,
        "keyboard": [[{"label": "Yes", "value": "y"}]],
    }


def test_send_message_generates_idempotency_key_when_absent() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200, json={"message_id": 1, "telegram_message_id": 2}
        )

    rc = _make_client(handler)
    try:
        rc.send_message(text="ping")
    finally:
        rc.close()
    # Auto-generated UUID v4 — must be non-empty.
    assert captured["headers"].get("idempotency-key")


def test_send_message_not_bound_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "not_bound"})

    rc = _make_client(handler)
    try:
        with pytest.raises(NotBoundError):
            rc.send_message(text="x")
    finally:
        rc.close()


def test_send_message_idempotency_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": "idempotency_key_reused_with_different_body"}
        )

    rc = _make_client(handler)
    try:
        with pytest.raises(IdempotencyMismatchError):
            rc.send_message(text="x", idempotency_key="k")
    finally:
        rc.close()


def test_410_maps_to_binding_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"detail": "expired"})

    rc = _make_client(handler)
    try:
        with pytest.raises(BindingExpiredError):
            rc.send_message(text="x")
    finally:
        rc.close()


# ---- edit / delete / cancel ------------------------------------------------


def test_edit_message_sends_patch() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    rc = _make_client(handler)
    try:
        rc.edit_message(42, text="updated")
    finally:
        rc.close()
    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/v1/messages/42")
    assert captured["body"] == {"text": "updated"}


def test_edit_message_without_text_rejected() -> None:
    rc = _make_client(lambda req: httpx.Response(200))
    try:
        with pytest.raises(ValueError):
            rc.edit_message(1)
    finally:
        rc.close()


def test_delete_message_sends_delete() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    rc = _make_client(handler)
    try:
        rc.delete_message(7)
    finally:
        rc.close()
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/v1/messages/7")


def test_cancel_message_posts_cancel() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    rc = _make_client(handler)
    try:
        rc.cancel_message(9)
    finally:
        rc.close()
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/messages/9/cancel")


# ---- wait_for_answer -------------------------------------------------------


def test_wait_for_answer_204_then_answered() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "state": "answered",
                "answer": {"label": "Yes", "value": "y", "option_idx": 0},
            },
        )

    rc = _make_client(handler)
    try:
        answer = rc.wait_for_answer(1, timeout=10, long_poll_chunk=1)
    finally:
        rc.close()
    assert answer == Answer(
        state="answered",
        answer={"label": "Yes", "value": "y", "option_idx": 0},
    )
    assert calls["n"] == 2


def test_wait_for_answer_sends_wait_query_param() -> None:
    """Regression: the long-poll MUST send ``?wait=`` so the server parks on
    the waiter instead of returning 204 immediately. Without it the client
    busy-loops at ~1000+ req/s and starves the calling hook (see the relay
    hook-timeout incident)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["wait"] = request.url.params.get("wait")
        return httpx.Response(
            200, json={"state": "answered", "answer": {"label": "Yes"}}
        )

    rc = _make_client(handler)
    try:
        rc.wait_for_answer(1, timeout=10, long_poll_chunk=7)
    finally:
        rc.close()

    # wait = min(long_poll_chunk, remaining) — bounded by the 7s chunk here.
    assert captured["wait"] is not None, "wait query param was not sent"
    assert 1 <= int(captured["wait"]) <= 7


def test_wait_for_answer_expired_returns_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "expired"})

    rc = _make_client(handler)
    try:
        answer = rc.wait_for_answer(1, timeout=5, long_poll_chunk=1)
    finally:
        rc.close()
    assert answer == Answer(state="expired")


def test_wait_for_answer_overall_timeout_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    rc = _make_client(handler)
    try:
        answer = rc.wait_for_answer(1, timeout=0.0, long_poll_chunk=1)
    finally:
        rc.close()
    assert answer is None


# ---- retries ---------------------------------------------------------------


def test_transient_5xx_retried_then_succeeds(monkeypatch) -> None:
    # Skip the backoff sleeps so the test runs instantly.
    monkeypatch.setattr("relay_server.client.time.sleep", lambda s: None)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200, json={"message_id": 1, "telegram_message_id": 2}
        )

    rc = _make_client(handler)
    try:
        handle = rc.send_message(text="ok")
    finally:
        rc.close()
    assert handle.message_id == 1
    assert calls["n"] == 3


def test_retries_exhausted_raises(monkeypatch) -> None:
    monkeypatch.setattr("relay_server.client.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    rc = _make_client(handler)
    try:
        with pytest.raises(RelayError):
            rc.send_message(text="x")
    finally:
        rc.close()


def test_timeout_exception_retried_then_succeeds(monkeypatch) -> None:
    """A TimeoutException mid-long-poll is treated as transient and retried."""
    monkeypatch.setattr("relay_server.client.time.sleep", lambda s: None)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"message_id": 1, "telegram_message_id": 2})

    transport = httpx.MockTransport(handler)
    inner = httpx.Client(
        transport=transport,
        base_url=SERVER,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    rc = RelayClient(SERVER, TOKEN, client=inner)
    try:
        handle = rc.send_message(text="ok")
    finally:
        rc.close()
    assert handle.message_id == 1
    assert calls["n"] == 3


def test_timeout_exception_exhausted_raises_relay_error(monkeypatch) -> None:
    """When all retries are exhausted on TimeoutException, RelayError is raised."""
    monkeypatch.setattr("relay_server.client.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("always times out", request=request)

    transport = httpx.MockTransport(handler)
    inner = httpx.Client(
        transport=transport,
        base_url=SERVER,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    rc = RelayClient(SERVER, TOKEN, client=inner)
    try:
        with pytest.raises(RelayError, match="transport error"):
            rc.send_message(text="x")
    finally:
        rc.close()


# ---- from_config -----------------------------------------------------------


def test_from_config_reads_toml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'server_url = "http://srv"\n' 'installation_token = "rly_abc"\n'
    )
    rc = RelayClient.from_config(cfg_path)
    try:
        # Smoke-check we wired the credentials through.
        assert rc._server_url == "http://srv"  # noqa: SLF001
        assert rc._token == "rly_abc"  # noqa: SLF001
    finally:
        rc.close()


def test_from_config_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(RelayError):
        RelayClient.from_config(tmp_path / "nope.toml")


def test_from_config_missing_fields_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('server_url = "http://srv"\n')
    with pytest.raises(RelayError):
        RelayClient.from_config(cfg_path)
