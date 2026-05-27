"""Telegram backend protocol + fake implementation for Phase 1.

Phase 2 will provide a ``HTTPTelegramBackend`` that calls the real Bot API.
The ``TelegramBackend`` protocol is the seam.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol


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
        """Send a message; return the Telegram-side message id."""

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
