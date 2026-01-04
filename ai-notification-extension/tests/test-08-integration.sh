#!/bin/bash
# test-08-integration.sh

set -e

echo "=== Test 8: Integration Tests ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 8.1: Multiple notifications in sequence
echo "8.1: Sequential notifications..."
for i in {1..5}; do
    $CLIENT "Notification $i" \
        "This is notification number $i" \
        --action ok:"OK"
    sleep 0.5
done
echo "Sequential notifications sent"

sleep 2

# Test 8.2: Parallel notifications
echo "8.2: Parallel notifications..."
for i in {1..3}; do
    $CLIENT "Parallel $i" \
        "Parallel notification $i" \
        --action ok:"OK" &
done
wait
echo "Parallel notifications sent"

sleep 2

# Test 8.3: Extension restart
echo "8.3: Extension disable/enable..."
gnome-extensions disable ai-notification-extension@local
sleep 1
gnome-extensions enable ai-notification-extension@local
sleep 3

$CLIENT "Restart Test" \
    "Extension was restarted" \
    --action ok:"OK"

echo "Integration tests passed"
