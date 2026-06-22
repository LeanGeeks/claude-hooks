"""Synchronous client library for the Telegram relay server.

Hooks in ``.claude/hooks/`` are plain synchronous Python scripts launched per
invocation by Claude Code; forcing them to spin up an asyncio loop just to talk
HTTP would add latency and complexity for no benefit. The relay's public API is
plain HTTP, so a thin ``httpx.Client`` wrapper is the right shape.

Config file: ``~/.config/claude-tg-relay/config.toml``.
"""

from __future__ import annotations

import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "claude-tg-relay" / "config.toml"

# Transient errors retry budget. Three attempts smooths over the occasional
# upstream blip without blocking the hook for minutes when the relay is down.
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 0.5


# ---- Exceptions ------------------------------------------------------------


class RelayError(Exception):
    """Base class for relay client errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotBoundError(RelayError):
    """Server returned 409 not_bound — installation has no chat binding."""


class IdempotencyMismatchError(RelayError):
    """Server returned 422 — idempotency key reused with a different body."""


class BindingExpiredError(RelayError):
    """Server returned 410 — binding code or resource expired."""


# ---- Result types ----------------------------------------------------------


@dataclass(frozen=True)
class MessageHandle:
    """Server-issued identifiers for a relay-tracked message."""

    message_id: int
    telegram_message_id: int


@dataclass(frozen=True)
class Answer:
    """Terminal state returned from ``wait_for_answer``.

    ``state`` is one of ``answered``, ``expired``, ``cancelled``. When
    ``answered``, ``answer`` carries the server's recorded payload — typically
    ``{option_idx, label, value, via}`` for button taps or ``{text, via}`` for
    free-text replies.
    """

    state: str
    answer: dict[str, Any] | None = None


# ---- Client ----------------------------------------------------------------


class RelayClient:
    """Synchronous HTTP client for the relay server.

    Use ``RelayClient.from_config()`` to load credentials from the standard
    config path; the constructor takes explicit values when callers know them.
    """

    def __init__(
        self,
        server_url: str,
        installation_token: str,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 35.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        if not installation_token:
            raise ValueError("installation_token is required")
        self._server_url = server_url.rstrip("/")
        self._token = installation_token
        timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._client = client or httpx.Client(
            base_url=self._server_url,
            headers={"Authorization": f"Bearer {installation_token}"},
            timeout=timeout,
        )
        self._owns_client = client is None

    @classmethod
    def from_config(
        cls,
        config_path: Path | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> "RelayClient":
        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        if not path.exists():
            raise RelayError(f"relay config not found: {path}")
        with path.open("rb") as f:
            data = tomllib.load(f)
        server_url = data.get("server_url", "")
        token = data.get("installation_token", "")
        if not server_url or not token:
            raise RelayError(
                f"relay config missing server_url or installation_token: {path}"
            )
        return cls(server_url, token, client=client)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "RelayClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- HTTP plumbing -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        """Send a request with bounded retries on transient failures.

        Retries cover connection errors and 5xx responses. 4xx responses are
        terminal — callers map them to typed exceptions.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._client.request(
                    method,
                    path,
                    json=json_body,
                    headers=headers,
                    params=params,
                    timeout=timeout,
                )
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.TimeoutException,
            ) as exc:
                # Treat all transport-layer failures (including timeouts and
                # connection errors) as transient — a network blip during a
                # long-poll should not permanently fall back to the terminal.
                # Three retries with exponential backoff give us ~3.5 s of
                # headroom before giving up.
                last_exc = exc
                if attempt >= _MAX_RETRIES:
                    raise RelayError(f"transport error: {exc}") from exc
                time.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
                continue

            if 500 <= resp.status_code < 600 and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
                continue
            return resp

        # Unreachable; loop either returns or raises.
        raise RelayError(  # pragma: no cover
            f"unreachable: retries exhausted ({last_exc})"
        )

    def _raise_for_error(self, resp: httpx.Response) -> None:
        """Map server error responses to typed exceptions."""
        if resp.status_code < 400:
            return

        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("error") or "")
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]

        sc = resp.status_code
        if sc == 409 and "not_bound" in detail:
            raise NotBoundError(detail or "not_bound", status_code=sc)
        if sc == 422 and "idempotency_key_reused" in detail:
            raise IdempotencyMismatchError(detail, status_code=sc)
        if sc == 410:
            raise BindingExpiredError(detail or "expired", status_code=sc)
        raise RelayError(
            f"relay HTTP {sc}: {detail or resp.text[:200]}", status_code=sc
        )

    # ---- Messages ------------------------------------------------------

    def send_message(
        self,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        *,
        kind: str = "notification",
        reply_required: bool = False,
        ttl_sec: int = 300,
        idempotency_key: str | None = None,
        group_id: str | None = None,
        group_total: int | None = None,
        multi_select: bool = False,
    ) -> MessageHandle:
        body: dict[str, Any] = {
            "kind": kind,
            "text": text,
            "reply_required": reply_required,
            "ttl_sec": ttl_sec,
        }
        if keyboard is not None:
            body["keyboard"] = keyboard
        # Re-answerable group (AskUserQuestion fan-out): the server keeps these
        # messages editable until every sibling sharing ``group_id`` is answered.
        if group_id is not None:
            body["group_id"] = group_id
            body["group_total"] = group_total
        # Multi-select fan-out member: taps toggle options and the message stays
        # live until the Submit button is tapped (see relay _handle_grouped_button).
        if multi_select:
            body["multi_select"] = True

        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        resp = self._request("POST", "/v1/messages", json_body=body, headers=headers)
        self._raise_for_error(resp)
        data = resp.json()
        return MessageHandle(
            message_id=int(data["message_id"]),
            telegram_message_id=int(data["telegram_message_id"]),
        )

    def edit_message(self, message_id: int, text: str | None = None) -> None:
        if text is None:
            raise ValueError("edit_message requires text (keyboard edit unsupported)")
        resp = self._request(
            "PATCH",
            f"/v1/messages/{message_id}",
            json_body={"text": text},
        )
        self._raise_for_error(resp)

    def delete_message(self, message_id: int) -> None:
        resp = self._request("DELETE", f"/v1/messages/{message_id}")
        self._raise_for_error(resp)

    def cancel_message(self, message_id: int) -> None:
        resp = self._request("POST", f"/v1/messages/{message_id}/cancel")
        self._raise_for_error(resp)

    def wait_for_answer(
        self,
        message_id: int,
        timeout: float = 300.0,
        long_poll_chunk: int = 30,
    ) -> Answer | None:
        """Long-poll the answer endpoint until the message reaches a terminal
        state or ``timeout`` seconds elapse.

        Returns ``None`` only on overall timeout (the client gave up). When the
        server reports ``expired`` or ``cancelled``, an ``Answer`` with that
        state is returned — callers distinguish the two by inspecting
        ``answer.state``.
        """
        deadline = time.monotonic() + timeout
        # Make sure each chunk has a hard upper bound that fits in our read
        # timeout budget; HTTP layer reads add ~5s headroom on top.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            wait = max(1, min(long_poll_chunk, int(remaining)))
            resp = self._request(
                "GET",
                f"/v1/messages/{message_id}/answer",
                headers=None,
                json_body=None,
                # CRITICAL: pass ``wait`` as a query param so the server parks
                # on the waiter for that long. Without it the server defaults
                # to wait=0, returns 204 immediately, and this loop spins at
                # ~1000+ req/s — which starves the hook and trips Claude Code's
                # hook timeout before a human can answer.
                params={"wait": wait},
                # Add slack: the server may take up to ``wait`` seconds; the
                # client must give it that plus network jitter.
                timeout=httpx.Timeout(wait + 10.0, connect=5.0),
            )
            if resp.status_code == 204:
                continue
            self._raise_for_error(resp)
            data = resp.json()
            state = data.get("state", "")
            if state in ("answered", "expired", "cancelled"):
                return Answer(state=state, answer=data.get("answer"))
            # Any other shape: keep polling rather than fail noisily — server
            # may grow new transient states in the future.
            if remaining <= 0:
                return None
