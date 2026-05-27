"""Tests for bearer-token auth middleware."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_missing_token_rejected(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/v1/installations/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing_bearer_token"


@pytest.mark.asyncio
async def test_invalid_token_rejected(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get(
        "/v1/installations/me",
        headers={"Authorization": "Bearer rly_does_not_exist"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_token"


@pytest.mark.asyncio
async def test_revoked_token_rejected(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    r = await app_client.get(
        "/v1/installations/me",
        headers={"Authorization": f"Bearer {seeded['revoked_token']}"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "revoked_token"


@pytest.mark.asyncio
async def test_valid_token_accepted(
    app_client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    r = await app_client.get(
        "/v1/installations/me",
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "test"
    assert body["chat_bound"] is True


@pytest.mark.asyncio
async def test_malformed_header(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get(
        "/v1/installations/me",
        headers={"Authorization": "rly_no_bearer_prefix"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_endpoint_unauthenticated(app_client: httpx.AsyncClient) -> None:
    """GET /health must return 200 {"status": "ok"} without auth."""
    r = await app_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
