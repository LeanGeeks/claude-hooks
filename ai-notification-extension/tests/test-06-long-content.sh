#!/bin/bash
# test-06-long-content.sh

set -e

echo "=== Test 6: Long Content Handling ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 6.1: Long body text
echo "6.1: Long body with truncation..."
LONG_TEXT=$(for i in {1..50}; do echo "Line $i: Lorem ipsum dolor sit amet"; done)
$CLIENT "Long Content" \
    "$LONG_TEXT" \
    --max-lines 10 \
    --action ok:"OK"

sleep 1

# Test 6.2: Multiple code blocks
echo "6.2: Multiple code blocks..."
$CLIENT "Config Review" \
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
    --action discard:"Discard"

sleep 1

echo "Long content tests passed"
