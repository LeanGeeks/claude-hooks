#!/bin/bash
# test-04-countdown.sh

set -e

echo "=== Test 4: Countdown Indicator ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 4.1: 10 second countdown
echo "4.1: 10 second countdown..."
$CLIENT "Timeout Test" \
    "This will expire in 10 seconds" \
    --expire 10000 \
    --action approve:"Approve" \
    --action deny:"Deny"

sleep 1

# Test 4.2: Quick 5 second test
echo "4.2: 5 second countdown..."
$CLIENT "Quick Choice" \
    "You have 5 seconds!" \
    --expire 5000 \
    --urgency high \
    --action yes:"Yes!" \
    --action no:"No!"

sleep 1

echo "Countdown tests passed"
