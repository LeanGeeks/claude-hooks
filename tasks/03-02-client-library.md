# Task 3.2: Python Client Library

## Objective

Create a Python library that wraps the CLI functionality, making it easy to use the notification system programmatically from Python code.

## Use Cases

- AI agents requesting approval
- Long-running scripts needing user input
- Automation workflows with human-in-the-loop
- Python applications needing desktop notifications

---

## Implementation

### 1. Library Structure

```
libnotify-interactive/
├── setup.py
├── notify_interactive/
│   ├── __init__.py
│   ├── client.py
│   ├── models.py
│   └── exceptions.py
└── examples/
    ├── basic_usage.py
    ├── ai_agent.py
    └── async_example.py
```

### 2. `setup.py`

```python
from setuptools import setup, find_packages

setup(
    name="notify-interactive",
    version="0.1.0",
    description="Interactive desktop notifications with action buttons",
    author="Your Name",
    author_email="your.email@example.com",
    url="https://github.com/yourusername/ai-notification-extension",
    packages=find_packages(),
    install_requires=[
        "PyGObject>=3.42.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "mypy>=1.0.0",
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    entry_points={
        "console_scripts": [
            "notify-interactive=notify_interactive.cli:main",
        ],
    },
)
```

### 3. `notify_interactive/exceptions.py`

```python
"""Exception classes for notify-interactive"""


class NotificationError(Exception):
    """Base exception for notification errors"""

    pass


class ExtensionNotFoundError(NotificationError):
    """Raised when the GNOME extension is not available"""

    pass


class NotificationTimeoutError(NotificationError):
    """Raised when notification waiting times out"""

    pass


class InvalidActionError(NotificationError):
    """Raised when an invalid action is specified"""

    pass
```

### 4. `notify_interactive/models.py`

```python
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
```

### 5. `notify_interactive/client.py`

```python
"""Client for AI Notification Extension"""

import logging
import time
from typing import List, Optional

from gi.repository import GLib, Gio

from .exceptions import ExtensionNotFoundError, NotificationTimeoutError
from .models import Action, NotificationOptions, NotificationResult

logger = logging.getLogger(__name__)

# D-Bus constants
BUS_NAME = "org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH = "/org/gnome/Shell/Extensions/AINotifications"
INTERFACE = "org.gnome.Shell.Extensions.AINotifications"


class NotificationClient:
    """Client for AI Notification Extension"""

    def __init__(self):
        """Initialize the notification client"""
        self._bus: Optional[Gio.DBusConnection] = None
        self._results: dict[str, dict] = {}
        self._signal_id: Optional[int] = None

    @property
    def bus(self) -> Gio.DBusConnection:
        """Get or create D-Bus connection"""
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def show_notification(
        self,
        title: str,
        body: str = "",
        actions: Optional[List[Action]] = None,
        options: Optional[NotificationOptions] = None,
    ) -> str:
        """
        Show a notification and return its ID.

        Args:
            title: Notification title
            body: Notification body text
            actions: List of Action objects
            options: NotificationOptions for display settings

        Returns:
            Notification ID string

        Raises:
            ExtensionNotFoundError: If extension is not available
        """
        if options is None:
            options = NotificationOptions()

        opts_dict = options.to_dict()

        if actions:
            opts_dict["actions"] = [{"id": a.id, "label": a.label} for a in actions]

        options_variant = self._build_options_variant(opts_dict)

        try:
            result = self.bus.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE,
                "ShowNotification",
                GLib.Variant("(ssa{sv})", (title, body, options_variant)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.GError as e:
            if "org.freedesktop.DBus.Error.ServiceUnknown" in str(e):
                raise ExtensionNotFoundError(
                    "AI Notification Extension not found. "
                    "Ensure it's installed and enabled."
                ) from e
            raise

        if result:
            notification_id: str = result.unpack()[0]
            logger.info(f"Notification sent: {notification_id}")
            return notification_id

        raise ExtensionNotFoundError("Failed to send notification")

    def wait_for_result(
        self,
        notification_id: str,
        timeout: int = 300,
    ) -> NotificationResult:
        """
        Wait for notification result.

        Args:
            notification_id: ID from show_notification()
            timeout: Maximum seconds to wait

        Returns:
            NotificationResult with user's choice

        Raises:
            NotificationTimeoutError: If timeout is reached
        """
        start_time = time.time()

        # Subscribe to result signals
        self._signal_id = self.bus.signal_subscribe(
            BUS_NAME,
            INTERFACE,
            "NotificationResult",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_result_signal,
            None,
        )

        try:
            while time.time() - start_time < timeout:
                # Check local cache first
                if notification_id in self._results:
                    result_dict = self._results.pop(notification_id)
                    return NotificationResult(**result_dict)

                # Poll via D-Bus
                result = self._poll_result(notification_id)
                if result:
                    return result

                time.sleep(0.1)

            raise NotificationTimeoutError(
                f"No response within {timeout} seconds"
            )

        finally:
            if self._signal_id:
                self.bus.signal_unsubscribe(self._signal_id)
                self._signal_id = None

    def show_and_wait(
        self,
        title: str,
        body: str = "",
        actions: Optional[List[Action]] = None,
        options: Optional[NotificationOptions] = None,
        timeout: int = 300,
    ) -> NotificationResult:
        """
        Show notification and wait for result in one call.

        This is a convenience method combining show_notification() and
        wait_for_result().

        Args:
            title: Notification title
            body: Notification body text
            actions: List of Action objects
            options: NotificationOptions for display settings
            timeout: Maximum seconds to wait

        Returns:
            NotificationResult with user's choice
        """
        notification_id = self.show_notification(title, body, actions, options)
        return self.wait_for_result(notification_id, timeout)

    def _on_result_signal(self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data):
        """Handle result signal from extension"""
        notification_id, result = parameters.unpack()
        self._results[notification_id] = result

    def _poll_result(self, notification_id: str) -> Optional[NotificationResult]:
        """Poll for result via D-Bus GetResult method"""
        result = self.bus.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "GetResult",
            GLib.Variant("(s)", (notification_id,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

        if result:
            result_dict = result.unpack()[0]
            if result_dict:
                return NotificationResult(**result_dict)

        return None

    def _build_options_variant(self, options: dict) -> GLib.Variant:
        """Build D-Bus variant for options dictionary"""
        builder = GLib.VariantBuilder(GLib.VariantType("a{sv}"))

        for key, value in options.items():
            if key == "actions":
                # Array of {id, label} dictionaries
                action_variants = [
                    GLib.Variant("a{ss}", {"id": a["id"], "label": a["label"]})
                    for a in value
                ]
                builder.add("{sv}", key, GLib.Variant("aa{ss}", action_variants))

            elif key == "code_blocks":
                builder.add("{sv}", key, GLib.Variant("as", value))

            else:
                # Primitive types
                type_map = {
                    "urgency": "s",
                    "expire_timeout_ms": "i",
                    "action_layout": "s",
                    "max_lines": "i",
                }
                if key in type_map:
                    builder.add("{sv}", key, GLib.Variant(type_map[key], value))

        return builder.end()
```

### 6. `notify_interactive/__init__.py`

```python
"""AI Notification Extension - Python Client Library"""

from .client import NotificationClient
from .models import Action, NotificationOptions, NotificationResult
from .exceptions import (
    NotificationError,
    ExtensionNotFoundError,
    NotificationTimeoutError,
    InvalidActionError,
)

__version__ = "0.1.0"
__all__ = [
    "NotificationClient",
    "Action",
    "NotificationOptions",
    "NotificationResult",
    "NotificationError",
    "ExtensionNotFoundError",
    "NotificationTimeoutError",
    "InvalidActionError",
]


# Convenience function for quick notifications
def notify(
    title: str,
    body: str = "",
    actions: list[tuple[str, str]] | None = None,
    wait: bool = False,
    **kwargs,
) -> str | NotificationResult:
    """
    Quick notification function.

    Args:
        title: Notification title
        body: Notification body
        actions: List of (id, label) tuples
        wait: If True, wait for and return result
        **kwargs: Passed to NotificationOptions

    Returns:
        Notification ID if wait=False, NotificationResult if wait=True

    Example:
        >>> result = notify(
        ...     "Deploy?",
        ...     "Push to production?",
        ...     actions=[("yes", "Deploy"), ("no", "Cancel")],
        ...     wait=True
        ... )
        >>> if result.is_approved:
        ...     print("User approved!")
    """
    client = NotificationClient()

    action_objs = [Action(id=id, label=label) for id, label in (actions or [])]
    options = NotificationOptions(**kwargs)

    if wait:
        return client.show_and_wait(title, body, action_objs, options)
    else:
        return client.show_notification(title, body, action_objs, options)
```

### 7. Example: `examples/basic_usage.py`

```python
#!/usr/bin/env python3
"""Basic usage examples"""

from notify_interactive import (
    NotificationClient,
    Action,
    NotificationOptions,
)

# Example 1: Simple notification
client = NotificationClient()
notification_id = client.show_notification(
    title="Hello",
    body="This is a test notification",
)
print(f"Sent: {notification_id}")

# Example 2: Yes/No choice
result = client.show_and_wait(
    title="Confirm",
    body="Delete this file?",
    actions=[
        Action(id="yes", label="Yes, delete it"),
        Action(id="no", label="No, keep it"),
    ],
)

if result.is_approved:
    print("User confirmed deletion")
elif result.is_denied:
    print("User cancelled")
elif result.is_closed:
    print("User closed the notification")

# Example 3: Multiple choice
result = client.show_and_wait(
    title="Color Scheme",
    body="Choose a color scheme:",
    actions=[
        Action(id="light", label="Light"),
        Action(id="dark", label="Dark"),
        Action(id="auto", label="Auto (system)"),
        Action(id="sepia", label="Sepia"),
    ],
    options=NotificationOptions(
        action_layout="vertical",
        urgency="high",
    ),
)

print(f"User chose: {result.action_id}")

# Example 4: With code blocks
result = client.show_and_wait(
    title="Code Review",
    body="Please review this code:",
    actions=[
        Action(id="lgtm", label="LGTM"),
        Action(id="changes", label="Request Changes"),
    ],
    options=NotificationOptions(
        code_blocks=[
            "const x = 42;",
            "function double(x) { return x * 2; }",
        ],
    ),
)
```

### 8. Example: `examples/ai_agent.py`

```python
#!/usr/bin/env python3
"""Example AI agent that requests approval for actions"""

import sys
from notify_interactive import NotificationClient, Action, NotificationOptions


class AIAction:
    """Represents an AI action that needs approval"""

    def __init__(self, description, command, urgency="normal"):
        self.description = description
        self.command = command
        self.urgency = urgency


class AIAgent:
    """AI agent that asks for approval before executing actions"""

    def __init__(self):
        self.client = NotificationClient()
        self.history = []

    def request_approval(self, action: AIAction):
        """Request user approval for an action"""
        print(f"🤖 AI wants to: {action.description}")
        print(f"   Command: {action.command}")

        result = self.client.show_and_wait(
            title="AI Action Approval",
            body=f"{action.description}\n\nExecute: {action.command}",
            actions=[
                Action(id="approve", label="✓ Approve"),
                Action(id="deny", label="✗ Deny"),
                Action(id="modify", label="✎ Modify"),
            ],
            options=NotificationOptions(
                urgency=action.urgency,
                code_blocks=[action.command],
            ),
        )

        self.history.append((action, result))

        if result.is_approved:
            print("✅ User approved - executing...")
            return self._execute(action.command)
        elif result.is_denied:
            print("❌ User denied - action cancelled")
            return None
        else:
            print("🔄 User wants to modify")
            # Could implement modification dialog here
            return None

    def _execute(self, command):
        """Execute the command (placeholder)"""
        print(f"   Executing: {command}")
        # In real implementation, execute the command
        return f"Executed: {command}"

    def show_history(self):
        """Show approval history"""
        print("\n📋 Approval History:")
        for action, result in self.history:
            status = "✅" if result.is_approved else "❌"
            print(f"   {status} {action.description}")


# Example usage
if __name__ == "__main__":
    agent = AIAgent()

    # Simulate AI agent actions
    actions = [
        AIAction("Update dependencies", "pip install -r requirements.txt --upgrade"),
        AIAction("Run database migration", "python manage.py migrate", urgency="high"),
        AIAction("Clear cache", "redis-cli FLUSHDB"),
        AIAction("Deploy to production", "kubectl apply -f deployment.yaml", urgency="critical"),
    ]

    for action in actions:
        agent.request_approval(action)
        print()

    agent.show_history()
```

## Installation

```bash
# Install in development mode
pip install -e .

# Or from remote
pip install git+https://github.com/yourusername/ai-notification-extension.git
```

## Testing

```bash
# Run examples
python examples/basic_usage.py
python examples/ai_agent.py
```

## Acceptance Criteria

- [ ] Library installs via pip
- [ ] All examples run without errors
- [ ] API is intuitive and well-documented
- [ ] Type hints are complete
- [ ] Exceptions are properly raised
- [ ] Logger is configurable

## Next Task

[`04-01-testing.md`](./04-01-testing.md) - Testing and validation.
