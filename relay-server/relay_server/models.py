"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MessageKind = Literal["question", "permission", "notification"]
MessageState = Literal["open", "answered", "expired", "cancelled"]

BindingState = Literal["pending", "bound", "expired"]


class KeyboardButton(BaseModel):
    label: str
    value: str


class CreateMessageRequest(BaseModel):
    kind: MessageKind
    text: str
    keyboard: list[list[KeyboardButton]] | None = None
    reply_required: bool = False
    ttl_sec: int = Field(gt=0, le=24 * 3600)
    # Re-answerable group: when ``group_id`` is set, the message is editable
    # (taps update a provisional choice and re-render the keyboard with the
    # selection highlighted) until every message sharing the same ``group_id``
    # has an answer. At that point the relay finalizes the whole group — strips
    # all keyboards and bakes each choice into the message text. ``group_total``
    # is how many messages the group will contain (the relay can't count rows
    # that aren't inserted yet). Both None for ordinary one-shot messages.
    group_id: str | None = None
    group_total: int | None = Field(default=None, gt=0)
    # Multi-select question: taps toggle options (accumulating a selection set)
    # and the message stays live until the user taps the dedicated Submit button,
    # at which point the joined choice counts as this member's answer. Only
    # meaningful for grouped ``question`` messages; ignored otherwise.
    multi_select: bool = False


class CreateMessageResponse(BaseModel):
    message_id: int
    telegram_message_id: int


class PatchMessageRequest(BaseModel):
    text: str | None = None
    keyboard: list[list[KeyboardButton]] | None = None


class AnswerResponse(BaseModel):
    state: MessageState
    answer: dict[str, Any] | None = None


class InstallationMeResponse(BaseModel):
    id: int
    label: str
    chat_bound: bool
    last_seen_at: str | None
    # Availability fields — null when the chat is unconfigured or not bound
    # (both degrade to null so old clients ignore them).
    tz: str | None = None
    windows: str | None = None  # canonical window spec; None means always-available
    active_now: bool | None = None
    nudge_enabled: bool | None = None


class BindingRequestResponse(BaseModel):
    code: str
    expires_at: str


class BindingStatusResponse(BaseModel):
    state: BindingState
    chat_id: int | None = None
    telegram_user_id: int | None = None
