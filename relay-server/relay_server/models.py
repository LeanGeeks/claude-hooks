"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MessageKind = Literal["question", "permission", "notification"]
MessageState = Literal["open", "answered", "expired", "cancelled"]


class KeyboardButton(BaseModel):
    label: str
    value: str


class CreateMessageRequest(BaseModel):
    kind: MessageKind
    text: str
    keyboard: list[list[KeyboardButton]] | None = None
    reply_required: bool = False
    ttl_sec: int = Field(gt=0, le=24 * 3600)


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
