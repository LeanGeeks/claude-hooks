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


class BindingRequestResponse(BaseModel):
    code: str
    expires_at: str


class BindingStatusResponse(BaseModel):
    state: BindingState
    chat_id: int | None = None
    telegram_user_id: int | None = None
