#!/bin/bash
# test-09-error-handling.sh

set -e

echo "=== Test 9: Error Handling ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 9.1: Empty notification
echo "9.1: Empty title/body..."
if $CLIENT "" "" \
    --action ok:"OK" \
    --json > /dev/null 2>&1; then
    echo "Empty notification handled"
else
    echo "Empty notification caused expected error"
fi

sleep 1

# Test 9.2: Special characters
echo "9.2: Special characters..."
$CLIENT "Special Chars" \
    "Test: <>&\"'\\\$\\\`" \
    --code "echo \"test\" && rm -rf /" \
    --action ok:"OK"

sleep 1

# Test 9.3: Very long strings
echo "9.3: Very long title..."
LONG_TITLE=$(python3 -c "print('A' * 500)")
$CLIENT "$LONG_TITLE" \
    "Body text" \
    --action ok:"OK" \
    --json > /dev/null

sleep 1

# Test 9.4: Unicode characters
echo "9.4: Unicode characters..."
$CLIENT "Unicode Test" \
    "Emoji: 🎉 🚀 💻
Chinese: 你好世界
Arabic: مرحبا بالعالم
Math: ∑(i=1 to n) i²" \
    --action ok:"OK"

sleep 1

# Test 9.5: Newlines and tabs
echo "9.5: Special whitespace..."
$CLIENT "Whitespace Test" \
    "Line 1
Line 2
	Tabbed line
Line 4" \
    --action ok:"OK"

echo "Error handling tests passed"
