#!/bin/bash
# Test script for countdown indicator functionality
# This script demonstrates the various countdown indicator features

set -e

CLIENT="./client/notify-interactive"

echo "=========================================="
echo "Testing Countdown Indicator Functionality"
echo "=========================================="
echo

# Test 1: 10 Second Countdown
echo "Test 1: 10 second countdown with horizontal buttons"
echo "Command: $CLIENT 'Timeout Test' 'This will expire in 10 seconds' --expire 10000 --action approve 'Approve' --action deny 'Deny'"
echo
$CLIENT "Timeout Test" "This will expire in 10 seconds" \
    --expire 10000 \
    --action approve:Approve \
    --action deny:Deny
echo
echo "✓ Test 1 passed"
echo
sleep 2

# Test 2: 30 Second Countdown
echo "Test 2: 30 second countdown with high urgency"
echo "Command: $CLIENT 'Long Operation' 'Please confirm within 30 seconds' --expire 30000 --urgency high --action confirm 'Confirm' --action cancel 'Cancel'"
echo
$CLIENT "Long Operation" "Please confirm within 30 seconds" \
    --expire 30000 \
    --urgency high \
    --action confirm:Confirm \
    --action cancel:Cancel
echo
echo "✓ Test 2 passed"
echo
sleep 2

# Test 3: Countdown with Vertical Buttons
echo "Test 3: 15 second countdown with vertical button layout"
echo "Command: $CLIENT 'Quick Choice' 'Pick one before time runs out!' --expire 15000 --layout vertical --action opt1 'Option 1' --action opt2 'Option 2' --action opt3 'Option 3' --action opt4 'Option 4'"
echo
$CLIENT "Quick Choice" "Pick one before time runs out!" \
    --expire 15000 \
    --layout vertical \
    --action opt1:"Option 1" \
    --action opt2:"Option 2" \
    --action opt3:"Option 3" \
    --action opt4:"Option 4"
echo
echo "✓ Test 3 passed"
echo
sleep 2

# Test 4: Critical Urgency with Countdown
echo "Test 4: 20 second countdown with critical urgency"
echo "Command: $CLIENT 'URGENT: Rollback' 'Database migration failed. Rollback changes?' --expire 20000 --urgency critical --action rollback 'Rollback Now' --action investigate 'Investigate First'"
echo
$CLIENT "URGENT: Rollback" "Database migration failed. Rollback changes?" \
    --expire 20000 \
    --urgency critical \
    --action rollback:"Rollback Now" \
    --action investigate:"Investigate First"
echo
echo "✓ Test 4 passed"
echo

echo "=========================================="
echo "All countdown indicator tests completed!"
echo "=========================================="
echo
echo "Features demonstrated:"
echo "  • Circular countdown indicator animation"
echo "  • Counter-clockwise erasure of the circle"
echo "  • Integration with horizontal and vertical button layouts"
echo "  • Support for different urgency levels"
echo "  • Automatic countdown stop on user interaction"
echo
