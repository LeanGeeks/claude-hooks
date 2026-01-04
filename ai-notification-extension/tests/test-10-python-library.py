#!/usr/bin/env python3
# test-10-python-library.py

import sys
import time

# Add the libnotify-interactive package to the path
sys.path.insert(0, '/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/libnotify-interactive')

try:
    from notify_interactive import (
        NotificationClient,
        Action,
        NotificationOptions,
        NotificationResult,
        ExtensionNotFoundError,
    )
except ImportError as e:
    print(f"ERROR: Cannot import notify_interactive module: {e}")
    print("\nTrying to install the package...")
    import subprocess
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e",
             "/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/libnotify-interactive"],
            check=True
        )
        from notify_interactive import (
            NotificationClient,
            Action,
            NotificationOptions,
            NotificationResult,
            ExtensionNotFoundError,
        )
    except Exception as install_error:
        print(f"ERROR: Failed to install package: {install_error}")
        sys.exit(1)

def test_basic_notification():
    """Test basic notification"""
    print("10.1: Basic notification...")
    client = NotificationClient()

    notification_id = client.show_notification(
        title="Python Test",
        body="Sent from Python library",
    )

    assert notification_id, "No notification ID returned"
    print(f"Got notification ID: {notification_id}")
    time.sleep(1)

def test_with_actions():
    """Test notification with actions"""
    print("10.2: Notification with actions...")
    client = NotificationClient()

    # Non-blocking version for automation
    notification_id = client.show_notification(
        title="Python Choice",
        body="Choose from Python",
        actions=[
            Action(id="yes", label="Yes"),
            Action(id="no", label="No"),
        ],
    )

    print(f"Sent notification with actions: {notification_id}")
    time.sleep(1)

def test_vertical_layout():
    """Test vertical button layout"""
    print("10.3: Vertical layout from Python...")
    client = NotificationClient()

    notification_id = client.show_notification(
        title="Python Multi-choice",
        body="Select one:",
        actions=[
            Action(id="opt1", label="Option 1"),
            Action(id="opt2", label="Option 2"),
            Action(id="opt3", label="Option 3"),
            Action(id="opt4", label="Option 4"),
        ],
        options=NotificationOptions(
            action_layout="vertical",
        ),
    )

    print(f"Sent vertical layout notification: {notification_id}")
    time.sleep(1)

def test_code_blocks():
    """Test code blocks"""
    print("10.4: Code blocks from Python...")
    client = NotificationClient()

    notification_id = client.show_notification(
        title="Python Code Review",
        body="Review this code:",
        actions=[
            Action(id="approve", label="Approve"),
            Action(id="reject", label="Reject"),
        ],
        options=NotificationOptions(
            code_blocks=[
                "def hello():",
                "    print('world')",
                "    return 42",
            ],
        ),
    )

    print(f"Sent code block notification: {notification_id}")
    time.sleep(1)

def test_urgency_levels():
    """Test different urgency levels"""
    print("10.5: Urgency levels from Python...")
    client = NotificationClient()

    # Low urgency
    client.show_notification(
        title="Low Priority",
        body="This is low priority",
        options=NotificationOptions(urgency="low"),
    )
    time.sleep(0.5)

    # Normal urgency
    client.show_notification(
        title="Normal Priority",
        body="This is normal priority",
        options=NotificationOptions(urgency="normal"),
    )
    time.sleep(0.5)

    # High urgency
    client.show_notification(
        title="High Priority",
        body="This is high priority",
        options=NotificationOptions(urgency="high"),
    )
    time.sleep(0.5)

    # Critical urgency
    client.show_notification(
        title="Critical Priority",
        body="This is critical",
        options=NotificationOptions(urgency="critical"),
    )
    time.sleep(1)

def test_expiration():
    """Test notification expiration"""
    print("10.6: Notification expiration from Python...")
    client = NotificationClient()

    notification_id = client.show_notification(
        title="Expiring Test",
        body="This will expire in 5 seconds",
        options=NotificationOptions(
            expiration=5000,
        ),
    )

    print(f"Sent expiring notification: {notification_id}")
    time.sleep(1)

def main():
    """Run all tests"""
    print("=== Test 10: Python Library ===")

    try:
        test_basic_notification()
        test_with_actions()
        test_vertical_layout()
        test_code_blocks()
        test_urgency_levels()
        test_expiration()
        print("\nAll Python library tests passed")
    except ExtensionNotFoundError as e:
        print(f"\nERROR: Extension not found: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
