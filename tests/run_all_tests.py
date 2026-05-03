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

import sys
import unittest
from pathlib import Path

# Test modules
TEST_MODULES = {
    "unit_decision": "test_unit_decision_mapper",
    "unit_state": "test_unit_state_store",
    "unit_whitelist": "test_unit_whitelist",
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
