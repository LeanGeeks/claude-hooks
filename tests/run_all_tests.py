#!/usr/bin/env python3
"""
Test Runner for Task 04: Permission Flow Integration Tests

Run all tests:
    python3 run_all_tests.py

Run specific test module:
    python3 run_all_tests.py --module unit_decision

Available modules:
    unit_decision    - Decision mapper tests
    unit_state       - State store tests
    unit_whitelist   - Whitelist logic tests
    integration      - Integration tests
    all              - Run all tests (default)
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Isolate the permission state store BEFORE any test module imports it (the
# imports below trigger ``from permission_state_store import ...``, which reads
# these paths once at import). Keeps test runs from writing ``test-*`` rows into
# the developer's real ~/.claude/permission_requests.jsonl. Mirrors conftest.py
# for the pytest path. ``setdefault`` lets an explicit outer override win.
_tmp_store = Path(tempfile.mkdtemp(prefix="claude-hooks-tests-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_tmp_store / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_tmp_store / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_tmp_store / "permission_state_debug.log"))

# Test modules
TEST_MODULES = {
    "unit_decision": "test_unit_decision_mapper",
    "unit_state": "test_unit_state_store",
    "unit_whitelist": "test_unit_whitelist",
    "unit_session_yolo": "test_unit_session_yolo",
    "unit_notification": "test_unit_notification_hook",
    "unit_reply_injector": "test_unit_reply_injector",
    "unit_amux_spawn": "test_unit_amux_spawn",
    "unit_spawn_producer": "test_unit_spawn_producer",
    "unit_amux_reads": "test_unit_amux_reads",
    "unit_amux_supervise": "test_unit_amux_supervise",
    "unit_amux_ergonomics": "test_unit_amux_ergonomics",
    "unit_amux_rm": "test_unit_amux_rm",
    "unit_profile_loader": "test_profile_loader",
    "unit_roles_config": "test_unit_roles_config",
    "unit_terminal_answers": "test_unit_terminal_answers",
    "unit_claude_roles": "test_unit_claude_roles",
    "integration_pretool": "test_integration_pretool",
    "integration_permission": "test_integration_permission_request",
}


def run_tests(modules=None, verbosity=2):
    """Run specified test modules."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    if modules is None or "all" in modules:
        # Run all tests
        modules = list(TEST_MODULES.keys())

    for module_key in modules:
        if module_key not in TEST_MODULES:
            print(f"Unknown module: {module_key}")
            continue

        module_name = TEST_MODULES[module_key]
        try:
            # Import the module
            test_module = __import__(module_name)
            # Load tests from module
            tests = loader.loadTestsFromModule(test_module)
            suite.addTests(tests)
            print(f"Loaded tests from: {module_name}")
        except ImportError as e:
            print(f"Failed to import {module_name}: {e}")

    # Run the tests
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    return result.wasSuccessful()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Run Task 04 tests")
    parser.add_argument(
        "--module", "-m",
        choices=list(TEST_MODULES.keys()) + ["all"],
        default="all",
        help="Test module to run"
    )
    parser.add_argument(
        "--verbosity", "-v",
        type=int,
        default=2,
        help="Test verbosity (default: 2)"
    )
    parser.add_argument(
        "--quick", "-q",
        action="store_true",
        help="Run quick scenario checks instead of full tests"
    )

    args = parser.parse_args()

    if args.quick:
        # Run quick scenario checks
        import scenario_check
        success = scenario_check.run_all()
    else:
        # Run full test suite
        modules = [args.module] if args.module != "all" else None
        success = run_tests(modules, args.verbosity)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
