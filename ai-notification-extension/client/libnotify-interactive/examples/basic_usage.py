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
