# Task 4.1: Testing and Validation

## Objective

Create comprehensive test scenarios to validate all features of the AI Notification Extension.

## Test Plan

### Phase 1: Basic Functionality
- Extension installation and enablement
- Simple notification display
- Basic action buttons

### Phase 2: Advanced Features
- Vertical button layout
- Countdown indicators
- Code block formatting
- Long content truncation

### Phase 3: Integration
- D-Bus communication
- CLI tool functionality
- Python library
- Error handling

### Phase 4: Edge Cases
- Multiple simultaneous notifications
- Extension disable/enable cycles
- Very long content
- Special characters in text

---

## Test Scenarios

### 1. Installation Tests

```bash
#!/bin/bash
# test-01-installation.sh

set -e

echo "=== Test 1: Installation ==="

# Check GNOME version
echo "GNOME Shell version:"
gnome-shell --version

# Install extension
echo "Installing extension..."
./install.sh

# Check installation
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/ai-notification-extension@local"
if [ -d "$EXT_DIR" ]; then
    echo "✅ Extension directory exists"
else
    echo "❌ Extension directory not found"
    exit 1
fi

# Enable extension
echo "Enabling extension..."
gnome-extensions enable ai-notification-extension@local

# Check if enabled
if gnome-extensions list | grep -q "ai-notification-extension@local"; then
    echo "✅ Extension is enabled"
else
    echo "❌ Extension not found in list"
    exit 1
fi

# Check for errors
ERRORS=$(gnome-extensions info ai-notification-extension@local 2>&1 || true)
if echo "$ERRORS" | grep -qi "error"; then
    echo "❌ Extension has errors:"
    echo "$ERRORS"
    exit 1
fi

echo "✅ Installation test passed"
```

### 2. Basic Notification Tests

```bash
#!/bin/bash
# test-02-basic-notifications.sh

set -e

echo "=== Test 2: Basic Notifications ==="

# Test 2.1: Simple notification
echo "2.1: Simple notification..."
./client/notify-interactive "Test 1" "This is a test" \
    --json > /tmp/test-result.json

NOTIFICATION_ID=$(jq -r '.notification_id' /tmp/test-result.json)
if [ -n "$NOTIFICATION_ID" ]; then
    echo "✅ Got notification ID: $NOTIFICATION_ID"
else
    echo "❌ No notification ID"
    exit 1
fi

# Test 2.2: Notification with urgency
echo "2.2: Notification with urgency..."
./client/notify-interactive "High Priority" "This is urgent" \
    --urgency high \
    --action ok:OK

# Test 2.3: Notification with two actions
echo "2.3: Two action buttons..."
./client/notify-interactive "Binary Choice" "Yes or No?" \
    --action yes:Yes \
    --action no:No \
    --wait

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ User clicked first action"
elif [ $EXIT_CODE -eq 1 ]; then
    echo "✅ User clicked second action"
else
    echo "⚠️  User closed or expired (exit code: $EXIT_CODE)"
fi

echo "✅ Basic notification tests passed"
```

### 3. Vertical Button Tests

```bash
#!/bin/bash
# test-03-vertical-buttons.sh

set -e

echo "=== Test 3: Vertical Button Layout ==="

# Test 3.1: Four options (should use vertical)
echo "3.1: Four vertical options..."
./client/notify-interactive "Choose Background" \
    "How should the background be displayed?" \
    --layout vertical \
    --action white:"White background" \
    --action transparent:"Transparent background" \
    --action inherit:"Inherit from parent" \
    --action gradient:"Gradient fill" \
    --wait

# Test 3.2: Five options
echo "3.2: Five vertical options..."
./client/notify-interactive "Color Scheme" \
    "Select your preferred color scheme:" \
    --layout vertical \
    --action light:"Light mode" \
    --action dark:"Dark mode" \
    --action auto:"Auto (system preference)" \
    --action sepia:"Sepia tone" \
    --action grayscale:"Grayscale" \
    --wait

echo "✅ Vertical button tests passed"
```

### 4. Countdown Tests

```bash
#!/bin/bash
# test-04-countdown.sh

set -e

echo "=== Test 4: Countdown Indicator ==="

# Test 4.1: 10 second countdown
echo "4.1: 10 second countdown..."
./client/notify-interactive "Timeout Test" \
    "This will expire in 10 seconds" \
    --expire 10000 \
    --action approve:"Approve" \
    --action deny:"Deny" \
    --wait \
    --show-progress

EXIT_CODE=$?
if [ $EXIT_CODE -eq 2 ]; then
    echo "✅ Notification expired as expected"
else
    echo "⚠️  User interacted (exit code: $EXIT_CODE)"
fi

# Test 4.2: Quick 5 second test
echo "4.2: 5 second countdown..."
./client/notify-interactive "Quick Choice" \
    "You have 5 seconds!" \
    --expire 5000 \
    --urgency high \
    --action yes:"Yes!" \
    --action no:"No!" \
    --wait

echo "✅ Countdown tests passed"
```

### 5. Code Block Tests

```bash
#!/bin/bash
# test-05-code-blocks.sh

set -e

echo "=== Test 5: Code Block Formatting ==="

# Test 5.1: Single code block
echo "5.1: Single code block..."
./client/notify-interactive "Code Review" \
    "Please review this code:" \
    --code "const x = 42;" \
    --code "return x * 2;" \
    --action lgtm:"LGTM" \
    --action changes:"Request Changes" \
    --wait

# Test 5.2: Markdown code blocks
echo "5.2: Markdown code blocks..."
./client/notify-interactive "Deployment" \
    "Deploy this change?
    \`\`\`bash
    git push origin main
    \`\`\`" \
    --markdown \
    --action deploy:"Deploy Now" \
    --action cancel:"Cancel" \
    --wait

# Test 5.3: Multi-line code with indentation
echo "5.3: Multi-line indented code..."
./client/notify-interactive "Function Review" \
    "Review this function:" \
    --code "
    def process(data):
        result = []
        for item in data:
            result.append(item * 2)
        return result
    " \
    --action approve:"Approve" \
    --action reject:"Reject" \
    --wait

echo "✅ Code block tests passed"
```

### 6. Long Content Tests

```bash
#!/bin/bash
# test-06-long-content.sh

set -e

echo "=== Test 6: Long Content Handling ==="

# Test 6.1: Long body text
echo "6.1: Long body with truncation..."
LONG_TEXT=$(for i in {1..50}; do echo "Line $i: Lorem ipsum dolor sit amet"; done)
./client/notify-interactive "Long Content" \
    "$LONG_TEXT" \
    --max-lines 10 \
    --action ok:"OK"

# Test 6.2: Multiple code blocks
echo "6.2: Multiple code blocks..."
./client/notify-interactive "Config Review" \
    "Review these configuration changes:" \
    --code "[database]
host = localhost
port = 5432
name = myapp" \
    --code "[cache]
backend = redis
ttl = 3600
max_size = 1gb" \
    --code "[logging]
level = info
format = json" \
    --action apply:"Apply All" \
    --action discard:"Discard" \
    --wait

echo "✅ Long content tests passed"
```

### 7. D-Bus Communication Tests

```bash
#!/bin/bash
# test-07-dbus-communication.sh

set -e

echo "=== Test 7: D-Bus Communication ==="

# Test 7.1: Check bus name
echo "7.1: Checking D-Bus service..."
if gdbus call --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.GetNameOwner \
    "org.gnome.Shell.Extensions.AINotifications" 2>/dev/null; then
    echo "✅ D-Bus service is running"
else
    echo "❌ D-Bus service not found"
    exit 1
fi

# Test 7.2: Direct D-Bus method call
echo "7.2: Direct D-Bus call..."
RESULT=$(gdbus call --session \
    --dest org.gnome.Shell.Extensions.AINotifications \
    --object-path /org/gnome/Shell/Extensions/AINotifications \
    --method org.gnome.Shell.Extensions.AINotifications.ShowNotification \
    "'D-Bus Test'" \
    "'This was sent via D-Bus'" \
    "{'urgency': <'normal'>, 'actions': <[{'id': <'ok'>, 'label': <'OK'>}]>}" 2>&1)

if echo "$RESULT" | grep -q "notif-"; then
    echo "✅ D-Bus method call successful"
else
    echo "❌ D-Bus method call failed"
    echo "$RESULT"
    exit 1
fi

# Test 7.3: Monitor D-Bus signals (background)
echo "7.3: Monitoring D-Bus signals..."
timeout 5s dbus-monitor --session \
    "interface=org.gnome.Shell.Extensions.AINotifications" \
    "member=NotificationResult" > /tmp/dbus-signals.log &
MONITOR_PID=$!

# Send a notification
./client/notify-interactive "Signal Test" \
    "Testing signal emission" \
    --action test:"Test" \
    --json > /dev/null

sleep 2

# Check if signal was captured
if grep -q "NotificationResult" /tmp/dbus-signals.log; then
    echo "✅ D-Bus signals working"
else
    echo "⚠️  No D-Bus signals captured"
fi

kill $MONITOR_PID 2>/dev/null || true

echo "✅ D-Bus communication tests passed"
```

### 8. Integration Tests

```bash
#!/bin/bash
# test-08-integration.sh

set -e

echo "=== Test 8: Integration Tests ==="

# Test 8.1: Multiple notifications in sequence
echo "8.1: Sequential notifications..."
for i in {1..5}; do
    ./client/notify-interactive "Notification $i" \
        "This is notification number $i" \
        --action ok:"OK"
    sleep 0.5
done
echo "✅ Sequential notifications sent"

# Test 8.2: Parallel notifications
echo "8.2: Parallel notifications..."
for i in {1..3}; do
    ./client/notify-interactive "Parallel $i" \
        "Parallel notification $i" \
        --action ok:"OK" &
done
wait
echo "✅ Parallel notifications sent"

# Test 8.3: Extension restart
echo "8.3: Extension disable/enable..."
gnome-extensions disable ai-notification-extension@local
sleep 1
gnome-extensions enable ai-notification-extension@local
sleep 2

./client/notify-interactive "Restart Test" \
    "Extension was restarted" \
    --action ok:"OK"

echo "✅ Integration tests passed"
```

### 9. Error Handling Tests

```bash
#!/bin/bash
# test-09-error-handling.sh

set -e

echo "=== Test 9: Error Handling ==="

# Test 9.1: Empty notification
echo "9.1: Empty title/body..."
./client/notify-interactive "" "" \
    --action ok:"OK" \
    --json > /dev/null 2>&1 && echo "✅ Empty notification handled"

# Test 9.2: Special characters
echo "9.2: Special characters..."
./client/notify-interactive "Special Chars" \
    "Test: <>&\"'\\\$\\\`" \
    --code "echo \"test\" && rm -rf /" \
    --action ok:"OK"

# Test 9.3: Very long strings
echo "9.3: Very long title..."
LONG_TITLE=$(python3 -c "print('A' * 500)")
./client/notify-interactive "$LONG_TITLE" \
    "Body text" \
    --action ok:"OK" \
    --json > /dev/null

echo "✅ Error handling tests passed"
```

### 10. Python Library Tests

```python
#!/usr/bin/env python3
# test-10-python-library.py

import sys
import time
from notify_interactive import (
    NotificationClient,
    Action,
    NotificationOptions,
    NotificationResult,
    ExtensionNotFoundError,
)

def test_basic_notification():
    """Test basic notification"""
    print("10.1: Basic notification...")
    client = NotificationClient()

    notification_id = client.show_notification(
        title="Python Test",
        body="Sent from Python library",
    )

    assert notification_id, "No notification ID returned"
    print(f"✅ Got notification ID: {notification_id}")

def test_with_actions():
    """Test notification with actions"""
    print("10.2: Notification with actions...")
    client = NotificationClient()

    result: NotificationResult = client.show_and_wait(
        title="Python Choice",
        body="Choose from Python",
        actions=[
            Action(id="yes", label="Yes"),
            Action(id="no", label="No"),
        ],
        timeout=60,
    )

    print(f"✅ Result: {result.action_id}")
    assert isinstance(result, NotificationResult)

def test_vertical_layout():
    """Test vertical button layout"""
    print("10.3: Vertical layout from Python...")
    client = NotificationClient()

    result = client.show_and_wait(
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
        timeout=60,
    )

    print(f"✅ Selected: {result.action_id}")

def test_code_blocks():
    """Test code blocks"""
    print("10.4: Code blocks from Python...")
    client = NotificationClient()

    result = client.show_and_wait(
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
        timeout=60,
    )

    print(f"✅ Code review result: {result.action_id}")

def test_convenience_function():
    """Test convenience notify() function"""
    print("10.5: Convenience notify() function...")
    from notify_interactive import notify

    result = notify(
        title="Quick Notify",
        body="From convenience function",
        actions=[("yes", "Yes"), ("no", "No")],
        wait=True,
        timeout=60,
    )

    print(f"✅ Quick notify result: {result.action_id}")

def main():
    """Run all tests"""
    print("=== Test 10: Python Library ===")

    try:
        test_basic_notification()
        test_with_actions()
        test_vertical_layout()
        test_code_blocks()
        test_convenience_function()
        print("\n✅ All Python library tests passed")
    except ExtensionNotFoundError as e:
        print(f"\n❌ Extension not found: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### 11. Run All Tests

```bash
#!/bin/bash
# run-all-tests.sh

set -e

echo "╔═══════════════════════════════════════════════════════╗"
echo "║   AI Notification Extension - Test Suite              ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASSED=0
FAILED=0

run_test() {
    local test_file=$1
    local test_name=$2

    echo "═══════════════════════════════════════════════════════"
    echo "Running: $test_name"
    echo "═══════════════════════════════════════════════════════"

    if bash "$test_file"; then
        echo -e "${GREEN}✅ PASSED: $test_name${NC}"
        ((PASSED++))
    else
        echo -e "${RED}❌ FAILED: $test_name${NC}"
        ((FAILED++))
    fi
    echo ""
}

# Run all test suites
run_test "test-01-installation.sh" "Installation Tests"
run_test "test-02-basic-notifications.sh" "Basic Notifications"
run_test "test-03-vertical-buttons.sh" "Vertical Button Layout"
run_test "test-04-countdown.sh" "Countdown Indicator"
run_test "test-05-code-blocks.sh" "Code Block Formatting"
run_test "test-06-long-content.sh" "Long Content Handling"
run_test "test-07-dbus-communication.sh" "D-Bus Communication"
run_test "test-08-integration.sh" "Integration Tests"
run_test "test-09-error-handling.sh" "Error Handling"

# Run Python tests
echo "═══════════════════════════════════════════════════════"
echo "Running: Python Library Tests"
echo "═══════════════════════════════════════════════════════"

if python3 test-10-python-library.py; then
    echo -e "${GREEN}✅ PASSED: Python Library Tests${NC}"
    ((PASSED++))
else
    echo -e "${RED}❌ FAILED: Python Library Tests${NC}"
    ((FAILED++))
fi
echo ""

# Summary
echo "╔═══════════════════════════════════════════════════════╗"
echo "║                    TEST SUMMARY                        ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo -e "║  ${GREEN}PASSED${NC}: $PASSED                                        ║"
if [ $FAILED -gt 0 ]; then
    echo -e "║  ${RED}FAILED${NC}: $FAILED                                        ║"
else
    echo "║  FAILED: 0                                        ║"
fi
echo "╚═══════════════════════════════════════════════════════╝"

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All tests passed! 🎉${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
fi
```

## Test Checklist

- [ ] All test scripts are executable
- [ ] All tests pass individually
- [ ] Full test suite passes
- [ ] Extension works on GNOME 46, 47, 48
- [ ] No memory leaks detected
- [ ] No errors in journal logs
- [ ] Edge cases handled gracefully

## Debugging

If tests fail, check:

```bash
# View GNOME Shell logs
journalctl -f --user -t gnome-shell

# Use Looking Glass
# 1. Press Alt+F2
# 2. Type 'lg' and press Enter
# 3. Check Extensions tab for errors
# 4. Evaluate: imports.misc.extensionUtils.getCurrentExtension()

# Check D-Bus
busctl --user tree org.gnome.Shell.Extensions.AINotifications
```

## Acceptance Criteria

- [ ] All test scripts run without errors
- [ ] Test coverage includes all features
- [ ] Edge cases are handled
- [ ] Documentation is clear
- [ ] Extension is stable across multiple enable/disable cycles
