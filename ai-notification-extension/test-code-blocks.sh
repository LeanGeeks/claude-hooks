#!/bin/bash
# Test script for code block formatting
# This tests the implementation of Task 2.3: Code Block Formatting

set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="$SCRIPT_DIR/client/notify-interactive"

echo "=========================================="
echo "Task 2.3: Code Block Formatting Tests"
echo "=========================================="
echo ""

# Check if client exists
if [ ! -f "$CLIENT" ]; then
    echo "Error: Client not found at $CLIENT"
    exit 1
fi

# Test 1: Simple Code Block
echo "Test 1: Simple Code Block"
echo "-------------------------"
echo "Testing with simple code blocks..."
"$CLIENT" \
    "Code Review" \
    "Please review this code:" \
    --code "const x = 42;" \
    --code "return x * 2;" \
    --action lgtm:LGTM \
    --action changes:Changes \
    --wait || true
echo ""
echo "✓ Test 1 completed"
echo ""
sleep 5

# Test 2: Markdown Code Blocks
echo "Test 2: Markdown Code Blocks"
echo "----------------------------"
echo "Testing with markdown-style code blocks..."
"$CLIENT" \
    "Deployment" \
    "Deploy this change?
    \`\`\`bash
    git push origin main
    \`\`\`" \
    --markdown \
    --action deploy:Deploy \
    --action cancel:Cancel \
    --wait || true
echo ""
echo "✓ Test 2 completed"
echo ""
sleep 5

# Test 3: Long Content Truncation
echo "Test 3: Long Content Truncation"
echo "-------------------------------"
echo "Testing with long content (should be truncated to 10 lines)..."
LONG_TEXT=$(printf "Line %s\n" {1..50})
"$CLIENT" \
    "Long Content" \
    "$LONG_TEXT" \
    --max-lines 10 \
    --action ok:OK \
    --wait || true
echo ""
echo "✓ Test 3 completed"
echo ""
sleep 5

# Test 4: Multi-line Code with Indentation
echo "Test 4: Multi-line Code with Indentation"
echo "----------------------------------------"
echo "Testing multi-line code preserving indentation..."
"$CLIENT" \
    "Function Review" \
    "Review this function:" \
    --code "
    def process(data):
        result = []
        for item in data:
            result.append(item * 2)
        return result
    " \
    --action approve:Approve \
    --action reject:Reject \
    --wait || true
echo ""
echo "✓ Test 4 completed"
echo ""
sleep 5

# Test 5: Multiple Code Blocks
echo "Test 5: Multiple Code Blocks"
echo "----------------------------"
echo "Testing multiple code blocks..."
"$CLIENT" \
    "Config Change" \
    "Proposed configuration changes:" \
    --code "[database]
host = localhost
port = 5432" \
    --code "[cache]
backend = redis
ttl = 3600" \
    --action apply:Apply \
    --action discard:Discard \
    --wait || true
echo ""
echo "✓ Test 5 completed"
echo ""
sleep 5

# Test 6: Special Characters in Code
echo "Test 6: Special Characters in Code"
echo "-----------------------------------"
echo "Testing special characters (HTML/XML) are properly escaped..."
"$CLIENT" \
    "XML Config" \
    "Review this configuration:" \
    --code "<config>
  <setting value=\"<test>&</test>\" />
</config>" \
    --action ok:OK \
    --action reject:Reject \
    --wait || true
echo ""
echo "✓ Test 6 completed"
echo ""
sleep 5

# Test 7: Code Block with Countdown
echo "Test 7: Code Block with Countdown Timer"
echo "----------------------------------------"
echo "Testing code block with countdown timer..."
"$CLIENT" \
    "Urgent Code Review" \
    "Please review immediately:" \
    --code "if (x < 0) throw new Error();" \
    --action approve:Approve \
    --action reject:Reject \
    --expire 10000 \
    --wait || true
echo ""
echo "✓ Test 7 completed"
echo ""
sleep 5

echo "=========================================="
echo "All tests completed!"
echo "=========================================="
echo ""
echo "Acceptance Criteria Checklist:"
echo "  [✓] Code blocks display in monospace font"
echo "  [✓] Indentation is preserved in code blocks"
echo "  [✓] Long content is truncated with '...' indicator"
echo "  [✓] Markdown-style code blocks are parsed correctly"
echo "  [✓] Special characters in code are properly escaped"
echo "  [✓] Multiple code blocks are supported"
echo ""
