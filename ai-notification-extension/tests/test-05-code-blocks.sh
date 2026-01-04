#!/bin/bash
# test-05-code-blocks.sh

set -e

echo "=== Test 5: Code Block Formatting ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 5.1: Single code block
echo "5.1: Single code block..."
$CLIENT "Code Review" \
    "Please review this code:" \
    --code "const x = 42;" \
    --code "return x * 2;" \
    --action lgtm:"LGTM" \
    --action changes:"Request Changes"

sleep 1

# Test 5.2: Markdown code blocks
echo "5.2: Markdown code blocks..."
$CLIENT "Deployment" \
    "Deploy this change?
    \`\`\`bash
    git push origin main
    \`\`\`" \
    --markdown \
    --action deploy:"Deploy Now" \
    --action cancel:"Cancel"

sleep 1

# Test 5.3: Multi-line code with indentation
echo "5.3: Multi-line indented code..."
$CLIENT "Function Review" \
    "Review this function:" \
    --code "
    def process(data):
        result = []
        for item in data:
            result.append(item * 2)
        return result
    " \
    --action approve:"Approve" \
    --action reject:"Reject"

sleep 1

echo "Code block tests passed"
