"""Data models for notifications"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass
class Action:
    """An action button on a notification"""

    id: str
    label: str

    def __post_init__(self):
        if not self.id:
            raise ValueError("Action id cannot be empty")
        if not self.label:
            self.label = self.id.title()


@dataclass
class NotificationResult:
    """Result from a notification interaction"""

    action_id: str
    timestamp: int

    @property
    def is_approved(self) -> bool:
        """Check if result is an affirmative action"""
        return self.action_id in (
            "approve",
            "yes",
            "confirm",
            "ok",
            "accept",
            "allow",
            "continue",
        )

    @property
    def is_denied(self) -> bool:
        """Check if result is a negative action"""
        return self.action_id in (
            "deny",
            "no",
            "cancel",
            "reject",
            "decline",
            "disallow",
            "abort",
        )

    @property
    def is_closed(self) -> bool:
        """Check if notification was closed without action"""
        return self.action_id == "closed"

    @property
    def is_expired(self) -> bool:
        """Check if notification expired"""
        return self.action_id == "expired"


@dataclass
class NotificationOptions:
    """Options for displaying a notification"""

    urgency: Literal["low", "normal", "high", "critical"] = "normal"
    expire_timeout_ms: int = 0
    action_layout: Literal["horizontal", "vertical"] = "horizontal"
    max_lines: int = 0
    code_blocks: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for D-Bus transmission"""
        return {
            "urgency": self.urgency,
            "expire_timeout_ms": self.expire_timeout_ms,
            "action_layout": self.action_layout,
            "max_lines": self.max_lines,
            "code_blocks": self.code_blocks,
        }
