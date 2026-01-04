# AI Notification Extension - Test Suite

This directory contains comprehensive test scripts for validating all features of the AI Notification Extension.

## Test Files

### Individual Test Scripts

- **test-01-installation.sh** - Extension installation and enablement tests
- **test-02-basic-notifications.sh** - Simple notification display tests
- **test-03-vertical-buttons.sh** - Vertical button layout tests
- **test-04-countdown.sh** - Countdown indicator tests
- **test-05-code-blocks.sh** - Code block formatting tests
- **test-06-long-content.sh** - Long content handling tests
- **test-07-dbus-communication.sh** - D-Bus communication tests
- **test-08-integration.sh** - Integration and stress tests
- **test-09-error-handling.sh** - Edge cases and error handling tests
- **test-10-python-library.py** - Python library functionality tests

### Main Test Runner

- **run-all-tests.sh** - Executes all test scripts in sequence with colored output

## Running Tests

### Run All Tests

```bash
cd /data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/tests
./run-all-tests.sh
```

### Run Individual Tests

```bash
# Bash scripts
./test-02-basic-notifications.sh

# Python test
python3 ./test-10-python-library.py
```

## Test Requirements

- GNOME Shell 46, 47, or 48
- Extension installed and enabled
- CLI tool: `/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive`
- Python library installed (for test-10)
- `jq` for JSON parsing (for some tests)

## Test Automation

The tests are designed to be non-interactive where possible for automation purposes. Most tests use the `--json` flag and avoid `--wait` to allow automated execution.

Interactive tests (using `--wait`) are minimized and only used where necessary to demonstrate functionality.

## Expected Output

A successful test run shows:

```
========================================================
   AI Notification Extension - Test Suite
========================================================

Running: [Test Name]
========================================================
[Test output]
PASSED: [Test Name]

...
========================================================
                    TEST SUMMARY
========================================================
  PASSED: 10
  FAILED: 0
========================================================
All tests passed!
```

## Troubleshooting

If tests fail:

1. Check GNOME Shell logs: `journalctl -f --user -t gnome-shell`
2. Verify extension is enabled: `gnome-extensions list | grep ai-notification`
3. Check extension status: `gnome-extensions info ai-notification-extension@local`
4. Use Looking Glass (Alt+F2, type `lg`) to inspect the extension

## Test Coverage

- Installation and enablement
- Basic notification display
- Action buttons (horizontal and vertical)
- Countdown/timer indicators
- Code block formatting
- Long content handling
- D-Bus communication
- Integration scenarios
- Error handling and edge cases
- Python library API
