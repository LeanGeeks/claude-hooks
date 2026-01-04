#!/bin/bash
# test-02-basic-notifications.sh

set -e

echo "=== Test 2: Basic Notifications ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 2.1: Simple notification
echo "2.1: Simple notification..."
$CLIENT "Test 1" "This is a test" \
    --json > /tmp/test-result.json

NOTIFICATION_ID=$(jq -r '.notification_id' /tmp/test-result.json)
if [ -n "$NOTIFICATION_ID" ]; then
    echo "Got notification ID: $NOTIFICATION_ID"
else
    echo "ERROR: No notification ID"
    exit 1
fi

sleep 1

# Test 2.2: Notification with urgency
echo "2.2: Notification with urgency..."
$CLIENT "High Priority" "This is urgent" \
    --urgency high \
    --action ok:OK

sleep 1

# Test 2.3: Notification with two actions (non-interactive for automation)
echo "2.3: Two action buttons..."
$CLIENT "Binary Choice" "Yes or No?" \
    --action yes:Yes \
    --action no:No

sleep 1

echo "Basic notification tests passed"
