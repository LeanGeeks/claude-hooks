# notify-interactive

Python client library for the AI Notification Extension - interactive desktop notifications with action buttons for GNOME Shell.

## Features

- Send desktop notifications with custom action buttons
- Wait for user responses synchronously
- Support for code blocks and rich formatting
- AI agent approval workflows
- Type-safe API with full type hints
- Python 3.8+ compatible

## Installation

```bash
# Install from local source
pip install -e /path/to/libnotify-interactive

# Install in development mode
cd libnotify-interactive
pip install -e .
```

## Quick Start

### Simple Notification

```python
from notify_interactive import notify

# Quick notification (fire and forget)
notify("Hello World", "This is a notification")

# Wait for user response
result = notify(
    "Deploy to production?",
    "Push code to production server?",
    actions=[("yes", "Deploy"), ("no", "Cancel")],
    wait=True
)

if result.is_approved:
    print("User approved deployment!")
```

### Using the Client API

```python
from notify_interactive import NotificationClient, Action, NotificationOptions

client = NotificationClient()

# Show notification and wait for response
result = client.show_and_wait(
    title="Code Review",
    body="Please review the proposed changes",
    actions=[
        Action(id="lgtm", label="LGTM"),
        Action(id="changes", label="Request Changes"),
    ],
    options=NotificationOptions(
        urgency="high",
        action_layout="vertical",
        code_blocks=["def hello():\n    print('world')"],
    )
)

print(f"User selected: {result.action_id}")
```

## API Reference

### `notify()` Function

Convenience function for quick notifications.

```python
notify(
    title: str,
    body: str = "",
    actions: List[Tuple[str, str]] = None,
    wait: bool = False,
    **kwargs
) -> Union[str, NotificationResult]
```

### `NotificationClient` Class

Main client for interacting with the notification extension.

#### Methods

- `show_notification(title, body="", actions=None, options=None) -> str`
  - Show a notification and return its ID

- `wait_for_result(notification_id, timeout=300) -> NotificationResult`
  - Wait for user interaction with a notification

- `show_and_wait(title, body="", actions=None, options=None, timeout=300) -> NotificationResult`
  - Show notification and wait for result in one call

### `Action` Dataclass

Represents an action button.

```python
@dataclass
class Action:
    id: str
    label: str
```

### `NotificationOptions` Dataclass

Options for notification display.

```python
@dataclass
class NotificationOptions:
    urgency: Literal["low", "normal", "high", "critical"] = "normal"
    expire_timeout_ms: int = 0
    action_layout: Literal["horizontal", "vertical"] = "horizontal"
    max_lines: int = 0
    code_blocks: List[str] = field(default_factory=list)
```

### `NotificationResult` Dataclass

Result from user interaction.

```python
@dataclass
class NotificationResult:
    action_id: str
    timestamp: int

    @property
    def is_approved(self) -> bool: ...

    @property
    def is_denied(self) -> bool: ...

    @property
    def is_closed(self) -> bool: ...

    @property
    def is_expired(self) -> bool: ...
```

## Examples

See the `examples/` directory:

- `basic_usage.py` - Basic notification examples
- `ai_agent.py` - AI agent approval workflow
- `async_example.py` - Concurrent operations example

## AI Agent Integration

Perfect for AI agents that need human approval:

```python
from notify_interactive import AIAgent, AIAction

agent = AIAgent()

action = AIAction(
    description="Deploy to production",
    command="kubectl apply -f deployment.yaml",
    urgency="critical"
)

result = agent.request_approval(action)
if result:
    print("Action executed successfully")
```

## Requirements

- Python 3.8+
- PyGObject 3.42.0+
- GNOME Shell with AI Notification Extension installed

## License

MIT License
