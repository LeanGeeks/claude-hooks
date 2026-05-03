#!/bin/bash
#
# Manual Smoke Test Script for Permission Flow
#
# This script provides instructions and helper commands for manual
# end-to-end testing in a real Claude session.
#
# Usage: ./manual_smoke_test.sh [test_case]
#
# Test Cases:
#   all        - Show all test instructions
#   telegram   - Telegram notification tests
#   buttons    - Button action tests (allow/deny/stop/whitelist/reply)
#   timeout    - Timeout/fallback behavior test
#   logs       - Log verification commands
#

CLAUDE_DIR="$HOME/.claude"
LOG_DIR="$CLAUDE_DIR"

# Colors for output
GREEN='\033[92m'
RED='\033[91m'
YELLOW='\033[93m'
BLUE='\033[94m'
RESET='\033[0m'

show_header() {
    echo ""
    echo "============================================================"
    echo "  Manual Smoke Test: $1"
    echo "============================================================"
    echo ""
}

show_instruction() {
    echo -e "${BLUE}INSTRUCTION:${RESET} $1"
}

show_expected() {
    echo -e "${GREEN}EXPECTED:${RESET} $1"
}

show_command() {
    echo -e "${YELLOW}COMMAND:${RESET} $1"
}

show_warning() {
    echo -e "${RED}WARNING:${RESET} $1"
}

test_telegram() {
    show_header "Telegram Notification Tests"

    echo "Prerequisites:"
    echo "  1. Telegram bot token and chat ID configured in:"
    echo "     ~/.config/claude/telegram.conf"
    echo "     (or environment variables TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)"
    echo "  2. Claude Code running in a workspace with hooks enabled"
    echo ""

    echo "Test 1: Unknown Command Triggers Telegram"
    echo "-----------------------------------------"
    show_instruction "In a Claude Code session, ask Claude to run a command that is not in the allow list."
    show_command "Claude, please run: some_unknown_command_xyz --test"
    show_expected "1. After 15 seconds, a Telegram message should arrive"
    show_expected "2. Message should contain the command and action buttons"
    show_expected "3. Buttons: Allow, Deny, Stop, Whitelist"
    echo ""

    echo "Test 2: Denied Command Triggers Telegram"
    echo "----------------------------------------"
    show_instruction "Ask Claude to run a command that matches a deny pattern."
    show_command "Claude, please run: git push --force origin main"
    show_expected "1. After 15 seconds, a Telegram message should arrive"
    show_expected "2. Message should show the denied command"
    echo ""
}

test_buttons() {
    show_header "Button Action Tests"

    echo "Prerequisites:"
    echo "  1. Have a pending permission request in Telegram"
    echo "  2. Claude Code session should be waiting for permission"
    echo ""

    echo "Test 3: Allow Button"
    echo "--------------------"
    show_instruction "Click the 'Allow' button on a Telegram permission request"
    show_expected "1. Claude should proceed with the command"
    show_expected "2. Command output should be visible"
    show_expected "3. Audit log should show 'allow' action"
    show_command "Check: tail -5 ~/.claude/permission_actions.jsonl"
    echo ""

    echo "Test 4: Deny Button"
    echo "-------------------"
    show_instruction "Click the 'Deny' button on a Telegram permission request"
    show_expected "1. Claude should NOT execute the command"
    show_expected "2. Claude should show a denial message"
    show_expected "3. Audit log should show 'deny' action"
    echo ""

    echo "Test 5: Stop Button"
    echo "-------------------"
    show_instruction "Click the 'Stop' button on a Telegram permission request"
    show_expected "1. Claude should NOT execute the command"
    show_expected "2. Claude's agentic loop should be interrupted"
    show_expected "3. Audit log should show 'stop' action"
    echo ""

    echo "Test 6: Whitelist Button"
    echo "------------------------"
    show_instruction "Click the 'Whitelist' button on a Telegram permission request"
    show_expected "1. Claude should execute the command"
    show_expected "2. A new permission pattern should be added to settings.local.json"
    show_expected "3. Future similar commands should be auto-allowed"
    show_command "Check: cat .claude/settings.local.json"
    echo ""

    echo "Test 7: Text Reply"
    echo "------------------"
    show_instruction "Reply to the Telegram message with text (not a button click)"
    show_command "Reply: Use a different approach"
    show_expected "1. Claude should receive the reply as a denial with message"
    show_expected "2. Claude should see: 'User reply: Use a different approach'"
    show_expected "3. Audit log should show 'reply' action"
    echo ""

    echo "Test 8: Ignore/Fallback"
    echo "-----------------------"
    show_instruction "Do not respond to the Telegram message (let it timeout)"
    show_expected "1. After ~60 seconds, Claude should fall back to terminal prompt"
    show_expected "2. No decision should be recorded in audit log"
    echo ""
}

test_timeout() {
    show_header "Timeout Behavior Tests"

    echo "Test 9: Telegram Response Timeout"
    echo "---------------------------------"
    show_instruction "Trigger a permission request but do not respond via Telegram"
    show_command "Claude, please run: unknown_command_for_timeout_test"
    show_instruction "Wait for 60+ seconds without responding"
    show_expected "1. Claude should fall back to terminal prompt"
    show_expected "2. User can approve/deny in terminal"
    echo ""

    echo "Test 10: Request Expiration"
    echo "---------------------------"
    show_instruction "Create a request, wait 5+ minutes, then try to respond"
    show_expected "1. Telegram response should show 'Request expired' or similar"
    show_expected "2. State should be marked as 'expired'"
    show_command "Check: grep 'expired' ~/.claude/permission_requests.jsonl"
    echo ""
}

test_logs() {
    show_header "Log Verification Commands"

    echo "Log Files Locations:"
    echo "  State file:      ~/.claude/permission_requests.jsonl"
    echo "  Audit log:       ~/.claude/permission_actions.jsonl"
    echo "  Debug log:       ~/.claude/permission_state_debug.log"
    echo "  Hook debug log:  ~/.claude/bash_hook_debug.log"
    echo ""

    echo "Useful Commands:"
    echo "----------------"

    show_command "# View recent permission requests"
    echo "tail -10 ~/.claude/permission_requests.jsonl | jq ."
    echo ""

    show_command "# View recent audit entries"
    echo "tail -10 ~/.claude/permission_actions.jsonl | jq ."
    echo ""

    show_command "# Count decisions by type"
    echo "cat ~/.claude/permission_actions.jsonl | jq -r '.action' | sort | uniq -c"
    echo ""

    show_command "# Find specific request by ID"
    echo "grep 'REQUEST_ID' ~/.claude/permission_requests.jsonl | jq ."
    echo ""

    show_command "# Enable debug logging"
    echo "export CLAUDE_HOOK_DEBUG=1"
    echo ""

    show_command "# View debug log (if enabled)"
    echo "tail -50 ~/.claude/permission_state_debug.log"
    echo ""

    show_command "# Clear state (for testing)"
    show_warning "This will delete all pending requests!"
    echo "rm ~/.claude/permission_requests.jsonl"
    echo ""
}

test_all() {
    test_telegram
    echo ""
    test_buttons
    echo ""
    test_timeout
    echo ""
    test_logs
}

case "${1:-all}" in
    telegram)
        test_telegram
        ;;
    buttons)
        test_buttons
        ;;
    timeout)
        test_timeout
        ;;
    logs)
        test_logs
        ;;
    all|*)
        test_all
        ;;
esac
