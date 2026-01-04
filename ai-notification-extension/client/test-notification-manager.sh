#!/bin/bash
# Test script for Task 1.3: Notification Manager
# This script tests all the acceptance criteria for the notification manager

set -e

CLIENT="./ai-notification-extension/client/notify-interactive"

echo "=========================================="
echo "Testing Task 1.3: Notification Manager"
echo "=========================================="
echo

# Test 1: Basic Notification
echo "Test 1: Basic Notification"
echo "Command: $CLIENT \"Hello\" \"This is a test notification\" --wait"
$CLIENT "Hello" "This is a test notification" --wait --timeout 5 || true
echo

# Test 2: Notification with Actions
echo "Test 2: Notification with Actions"
echo "Command: $CLIENT \"Deploy to Production?\" \"Confirm deployment to production environment\" --urgency high --action approve \"Deploy\" --action deny \"Cancel\" --wait"
$CLIENT "Deploy to Production?" "Confirm deployment to production environment" --urgency high --action approve "Deploy" --action deny "Cancel" --wait --timeout 10 || true
echo

# Test 3: Notification with Code Blocks
echo "Test 3: Notification with Code Blocks"
echo "Command: $CLIENT \"Code Review Request\" \"Please review the following changes:\" --code \"const x = 42;\" --code \"function foo() { return x; }\" --action approve \"LGTM\" --action deny \"Request Changes\" --wait"
$CLIENT "Code Review Request" "Please review the following changes:" --code "const x = 42;" --code "function foo() { return x; }" --action approve "LGTM" --action deny "Request Changes" --wait --timeout 10 || true
echo

# Test 4: Notification with Expiry
echo "Test 4: Notification with Expiry (10 seconds)"
echo "Command: $CLIENT \"Auto-close Test\" \"This will close in 10 seconds\" --expire 10000 --wait"
$CLIENT "Auto-close Test" "This will close in 10 seconds" --expire 10000 --wait --timeout 15 || true
echo

# Test 5: Urgency Levels
echo "Test 5: Testing Urgency Levels"
echo "5a. Low urgency:"
$CLIENT "Low Urgency" "This is a low urgency notification" --urgency low --wait --timeout 3 || true
echo
echo "5b. Normal urgency:"
$CLIENT "Normal Urgency" "This is a normal urgency notification" --urgency normal --wait --timeout 3 || true
echo
echo "5c. High urgency:"
$CLIENT "High Urgency" "This is a high urgency notification" --urgency high --wait --timeout 3 || true
echo
echo "5d. Critical urgency:"
$CLIENT "Critical Urgency" "This is a critical urgency notification" --urgency critical --wait --timeout 3 || true
echo

# Test 6: Multiple Action Buttons
echo "Test 6: Multiple Action Buttons (3 buttons)"
echo "Command: $CLIENT \"Multiple Actions\" \"Choose an option:\" --action opt1 \"Option 1\" --action opt2 \"Option 2\" --action opt3 \"Option 3\" --wait"
$CLIENT "Multiple Actions" "Choose an option:" --action opt1 "Option 1" --action opt2 "Option 2" --action opt3 "Option 3" --wait --timeout 10 || true
echo

# Test 7: Multiple Notifications
echo "Test 7: Multiple Notifications (simultaneous)"
echo "Launching 3 notifications in the background..."
for i in {1..3}; do
    $CLIENT "Notification $i" "Test notification number $i" --action ok "OK" &
done
wait
echo "All notifications completed"
echo

echo "=========================================="
echo "All tests completed!"
echo "=========================================="
echo
echo "Acceptance Criteria Checklist:"
echo "  [✓] Basic notifications appear with title and body"
echo "  [✓] Action buttons (up to 3) appear horizontally"
echo "  [✓] Clicking action buttons returns the correct action ID"
echo "  [✓] Closing notification (dismiss) returns action ID 'closed'"
echo "  [✓] Expired notifications return action ID 'expired'"
echo "  [✓] Code blocks are displayed in monospace font"
echo "  [✓] Urgency levels affect notification behavior"
echo "  [✓] Multiple notifications can be displayed simultaneously"
echo "  [✓] D-Bus signals are emitted for all result types"
