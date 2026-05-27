"""Telegram backend protocol + implementations.

The Phase 1 ``FakeTelegramBackend`` records calls for tests. The Phase 2
``HttpTelegramBackend`` calls the real Bot API via ``httpx.AsyncClient``.
The ``TelegramBackend`` protocol is the seam.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .callback_data import encode as encode_callback_data

logger = logging.getLogger(__name__)

# Parse mode used for *all* outbound messages. We standardize on HTML because
# the relay surfaces snippets (paths, code) where Telegram's MarkdownV2 escape
# rules would force us to backslash-escape a long list of punctuation; HTML
# only needs ``& < >`` escaping. Clients send plain text; the relay does not
# embed unescaped formatting itself, so a stray ``<`` in user text won't break
# rendering — Telegram falls back to literal display on parse errors and the
# caller has already chosen ``kind`` so we know it's user-facing.
PARSE_MODE = "HTML"


class TelegramForbidden(Exception):
    """Raised when the Bot API returns 403 (bot blocked / kicked from chat).

    The app layer catches this to auto-unbind the installation and surface a
    409 ``not_bound`` to the calling client.
    """

    def __init__(self, description: str = "") -> None:
        super().__init__(description or "telegram_forbidden")
        self.description = description


class TelegramApiError(Exception):
    """Raised on any non-403 Telegram API failure."""

    def __init__(self, status_code: int, description: str) -> None:
        super().__init__(f"telegram_api_error[{status_code}]: {description}")
        self.status_code = status_code
        self.description = description


@dataclass
class SentMessage:
    chat_id: int
    message_id: int
    text: str
    keyboard: list[list[dict[str, Any]]] | None = None
    reply_required: bool = False


class TelegramBackend(Protocol):
    """Minimal surface used by the relay server."""

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, Any]]] | None,
        reply_required: bool,
        message_id: int,
    ) -> int:
        """Send a relay-tracked message; return the Telegram-side message id."""

    async def send_text(self, *, chat_id: int, text: str) -> None:
        """Send a plain text message that requires no relay tracking (e.g. /bind replies)."""

    async def edit_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        text: str | None,
        keyboard: list[list[dict[str, Any]]] | None,
    ) -> None: ...

    async def delete_message(
        self, *, chat_id: int, telegram_message_id: int
    ) -> None: ...

    async def edit_reply_markup(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        keyboard: list[list[dict[str, Any]]] | None,
    ) -> None:
        """Replace (or strip, when ``keyboard`` is None) the inline keyboard.

        Distinct from ``edit_message`` so cancel/expiry paths can unambiguously
        request keyboard removal against the real Bot API.
        """

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> None: ...


@dataclass
class FakeCall:
    method: str
    kwargs: dict[str, Any]


@dataclass
class FakeTelegramBackend:
    """Records calls; assigns monotonic telegram_message_ids."""

    calls: list[FakeCall] = field(default_factory=list)
    sent: list[SentMessage] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, Any]]] | None,
        reply_required: bool,
        message_id: int,
    ) -> int:
        tg_id = next(self._ids)
        self.calls.append(
            FakeCall(
                "send_message",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "keyboard": keyboard,
                    "reply_required": reply_required,
                    "message_id": message_id,
                },
            )
        )
        self.sent.append(
            SentMessage(
                chat_id=chat_id,
                message_id=tg_id,
                text=text,
                keyboard=keyboard,
                reply_required=reply_required,
            )
        )
        return tg_id

    async def send_text(self, *, chat_id: int, text: str) -> None:
        self.calls.append(
            FakeCall("send_text", {"chat_id": chat_id, "text": text})
        )

    async def edit_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        text: str | None,
        keyboard: list[list[dict[str, Any]]] | None,
    ) -> None:
        self.calls.append(
            FakeCall(
                "edit_message",
                {
                    "chat_id": chat_id,
                    "telegram_message_id": telegram_message_id,
                    "text": text,
                    "keyboard": keyboard,
                },
            )
        )

    async def delete_message(
        self, *, chat_id: int, telegram_message_id: int
    ) -> None:
        self.calls.append(
            FakeCall(
                "delete_message",
                {
                    "chat_id": chat_id,
                    "telegram_message_id": telegram_message_id,
                },
            )
        )

    async def edit_reply_markup(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        keyboard: list[list[dict[str, Any]]] | None,
    ) -> None:
        self.calls.append(
            FakeCall(
                "edit_reply_markup",
                {
                    "chat_id": chat_id,
                    "telegram_message_id": telegram_message_id,
                    "keyboard": keyboard,
                },
            )
        )

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> None:
        self.calls.append(
            FakeCall(
                "answer_callback_query",
                {"callback_query_id": callback_query_id, "text": text},
            )
        )


# ---- Real HTTP backend -----------------------------------------------------

_TELEGRAM_BASE = "https://api.telegram.org"


def _inline_keyboard_payload(
    keyboard: list[list[dict[str, Any]]] | None,
    *,
    message_id: int | None,
) -> dict[str, Any] | None:
    """Convert the relay's `[[{label,value},...]]` shape into Telegram's
    `inline_keyboard` form, populating ``callback_data`` with the encoded
    routing token. Returns None for an empty/missing keyboard so callers can
    pass it through directly.

    When ``message_id`` is None we fall back to using the option ``value`` as
    callback_data — only used by ``edit_message``/``edit_reply_markup`` when
    the caller didn't supply the original message_id (Phase 1 path; Phase 2
    edits keep buttons unchanged in practice).
    """
    if not keyboard:
        return None
    rows: list[list[dict[str, Any]]] = []
    for row_idx, row in enumerate(keyboard):
        out_row: list[dict[str, Any]] = []
        for opt_idx, btn in enumerate(row):
            label = btn["label"]
            value = btn.get("value", "")
            if message_id is not None:
                # Flatten (row, col) into a single sequential option index in
                # row-major order — matches how the daemon's old code laid
                # out keyboards and how clients read replies back.
                flat_idx = sum(len(r) for r in keyboard[:row_idx]) + opt_idx
                cb = encode_callback_data(message_id, flat_idx)
            else:
                cb = value or label
            out_row.append({"text": label, "callback_data": cb})
        rows.append(out_row)
    return {"inline_keyboard": rows}


class HttpTelegramBackend:
    """Talks to the real Telegram Bot API via ``httpx.AsyncClient``.

    Thread-safety: a single client instance per app process is fine — httpx's
    AsyncClient is safe for concurrent use.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = _TELEGRAM_BASE,
        timeout: float = 15.0,
    ) -> None:
        self._bot_token = bot_token
        self._base = f"{base_url.rstrip('/')}/bot{bot_token}"
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # TODO(phase-3?): retry transient 5xx and 429 responses with backoff.
    # Today every call is one-shot — sufficient for Phase 2 since the relay's
    # callers retry the higher-level API operation on their own, but a small
    # retry loop here would smooth over Telegram's occasional flakiness.
    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/{method}"
        # Strip None values — Telegram rejects null for some fields.
        clean = {k: v for k, v in payload.items() if v is not None}
        try:
            r = await self._client.post(url, json=clean)
        except httpx.HTTPError as exc:
            logger.exception("telegram transport error: %s %s", method, exc)
            raise TelegramApiError(0, f"transport: {exc}") from exc

        # Telegram returns 200 with ``ok: false`` for app-level errors, and
        # 403 / 4xx for transport-level ones. Handle both.
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            raise TelegramApiError(r.status_code, r.text[:200])

        if r.status_code == 403 or (
            not body.get("ok") and body.get("error_code") == 403
        ):
            desc = body.get("description", "forbidden")
            logger.warning("telegram forbidden: %s %s", method, desc)
            raise TelegramForbidden(desc)

        if not body.get("ok"):
            raise TelegramApiError(
                body.get("error_code", r.status_code),
                body.get("description", "unknown"),
            )
        return body.get("result", {})

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, Any]]] | None,
        reply_required: bool,
        message_id: int,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": PARSE_MODE,
        }
        markup = _inline_keyboard_payload(keyboard, message_id=message_id)
        if markup is not None:
            # Trade-off: when both a keyboard and reply_required are set, we
            # send only the inline keyboard (Telegram's reply_markup is a
            # single field). The webhook fallback path in `_handle_update`
            # compensates: a plain message from the bound user in a chat with
            # an open question is attributed to that question even without
            # the force_reply prompt.
            payload["reply_markup"] = markup
        elif reply_required:
            # No buttons + need free-text answer → force_reply prompt.
            payload["reply_markup"] = {"force_reply": True, "selective": False}
        result = await self._call("sendMessage", payload)
        return int(result["message_id"])

    async def send_text(self, *, chat_id: int, text: str) -> None:
        """Send a plain text reply (no relay tracking)."""
        await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": PARSE_MODE},
        )

    async def edit_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        text: str | None,
        keyboard: list[list[dict[str, Any]]] | None,
    ) -> None:
        if text is not None:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": telegram_message_id,
                "text": text,
                "parse_mode": PARSE_MODE,
            }
            markup = _inline_keyboard_payload(keyboard, message_id=None)
            if markup is not None:
                payload["reply_markup"] = markup
            await self._call("editMessageText", payload)
        elif keyboard is not None:
            await self.edit_reply_markup(
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                keyboard=keyboard,
            )

    async def edit_reply_markup(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        keyboard: list[list[dict[str, Any]]] | None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": telegram_message_id,
        }
        markup = _inline_keyboard_payload(keyboard, message_id=None)
        if markup is not None:
            payload["reply_markup"] = markup
        await self._call("editMessageReplyMarkup", payload)

    async def delete_message(
        self, *, chat_id: int, telegram_message_id: int
    ) -> None:
        await self._call(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": telegram_message_id},
        )

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> None:
        await self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def set_webhook(self, url: str) -> None:
        """Install the webhook URL on the bot. Called at app startup."""
        await self._call(
            "setWebhook",
            {"url": url, "allowed_updates": ["message", "callback_query"]},
        )
