#!/bin/bash
# run-all-tests.sh

set -e

TEST_DIR="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/tests"
cd "$TEST_DIR"

echo "========================================================"
echo "   AI Notification Extension - Test Suite"
echo "========================================================"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

PASSED=0
FAILED=0
SKIPPED=0

run_test() {
    local test_file=$1
    local test_name=$2

    echo -e "${BLUE}========================================================${NC}"
    echo -e "${BOLD}Running: $test_name${NC}"
    echo -e "${BLUE}========================================================${NC}"

    if bash "$test_file"; then
        echo -e "${GREEN}PASSED: $test_name${NC}"
        ((PASSED++))
    else
        echo -e "${RED}FAILED: $test_name${NC}"
        ((FAILED++))
    fi
    echo ""
}

# Check if extension is installed
echo -e "${BLUE}========================================================${NC}"
echo -e "${BOLD}Pre-flight Check${NC}"
echo -e "${BLUE}========================================================${NC}"

if gnome-extensions list | grep -q "ai-notification-extension@local"; then
    echo -e "${GREEN}Extension is installed${NC}"
else
    echo -e "${YELLOW}WARNING: Extension not found in list${NC}"
    echo "Installing extension..."
    cd /data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension
    ./install.sh
    sleep 2
    cd "$TEST_DIR"
fi

if gnome-extensions info ai-notification-extension@local 2>&1 | grep -qi "error"; then
    echo -e "${RED}ERROR: Extension has errors, cannot proceed${NC}"
    exit 1
fi

echo ""

# Run all test suites
run_test "test-01-installation.sh" "Installation Tests"
run_test "test-02-basic-notifications.sh" "Basic Notifications"
run_test "test-03-vertical-buttons.sh" "Vertical Button Layout"
run_test "test-04-countdown.sh" "Countdown Indicator"
run_test "test-05-code-blocks.sh" "Code Block Formatting"
run_test "test-06-long-content.sh" "Long Content Handling"
run_test "test-07-dbus-communication.sh" "D-Bus Communication"
run_test "test-08-integration.sh" "Integration Tests"
run_test "test-09-error-handling.sh" "Error Handling"

# Run Python tests
echo -e "${BLUE}========================================================${NC}"
echo -e "${BOLD}Running: Python Library Tests${NC}"
echo -e "${BLUE}========================================================${NC}"

if python3 test-10-python-library.py; then
    echo -e "${GREEN}PASSED: Python Library Tests${NC}"
    ((PASSED++))
else
    echo -e "${RED}FAILED: Python Library Tests${NC}"
    ((FAILED++))
fi
echo ""

# Summary
echo "========================================================"
echo -e "${BOLD}                    TEST SUMMARY${NC}"
echo "========================================================"
echo -e "  ${GREEN}PASSED${NC}: $PASSED"
if [ $FAILED -gt 0 ]; then
    echo -e "  ${RED}FAILED${NC}: $FAILED"
else
    echo "  FAILED: 0"
fi
echo "========================================================"

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}${BOLD}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}Some tests failed!${NC}"
    exit 1
fi
