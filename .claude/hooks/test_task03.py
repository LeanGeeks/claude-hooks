#!/usr/bin/env python3
"""
Test script for Task 03 validation test matrix.

This script tests:
1. Concurrent two pending requests, each resolved independently
2. Double-click same button -> one final decision only
3. Reply text arrives after request expired -> rejected safely
4. Whitelist writes valid JSON and dedupes existing rule

Run with: python3 test_task03.py
"""

import json
import os
import sys
import time
from pathlib import Path

# Add hooks directory to path
sys.path.insert(0, str(Path(__file__).parent))

from permission_state_store import (
    PermissionRequest,
    RequestState,
    create_request,
    get_request,
    update_request_state,
    cleanup_expired_requests,
)

from telegram_permission_router import (
    generate_whitelist_pattern,
    update_settings_local_json,
)


def test_concurrent_requests():
    """Test: Concurrent two pending requests, each resolved independently"""
    print("\n=== Test 1: Concurrent Requests ===")

    # Create two requests
    req1 = create_request(
        session_id="test-session-1",
        cwd="/test/path1",
        tool_name="Bash",
        tool_input={"command": "ls -la"},
        permission_suggestions=["Bash(ls:*)"],
        ttl_seconds=60,
    )

    req2 = create_request(
        session_id="test-session-2",
        cwd="/test/path2",
        tool_name="Bash",
        tool_input={"command": "rm -rf"},
        permission_suggestions=["Bash(rm:*)"],
        ttl_seconds=60,
    )

    print(f"Created req1: {req1.request_id}")
    print(f"Created req2: {req2.request_id}")

    # Resolve first request
    result1 = update_request_state(req1.request_id, RequestState.ALLOW, actor_user_id=12345)
    print(f"Resolved req1: {result1.state if result1 else 'None'}")

    # Resolve second request differently
    result2 = update_request_state(req2.request_id, RequestState.DENY, actor_user_id=12345)
    print(f"Resolved req2: {result2.state if result2 else 'None'}")

    # Verify they are independent
    check1 = get_request(req1.request_id)
    check2 = get_request(req2.request_id)

    assert check1.state == "allow", f"Expected allow, got {check1.state}"
    assert check2.state == "deny", f"Expected deny, got {check2.state}"

    print("PASS: Both requests resolved independently")
    return True


def test_double_click_idempotency():
    """Test: Double-click same button -> one final decision only"""
    print("\n=== Test 2: Double-Click Idempotency ===")

    req = create_request(
        session_id="test-session-dbl",
        cwd="/test/path",
        tool_name="Bash",
        tool_input={"command": "echo test"},
        permission_suggestions=["Bash(echo:*)"],
        ttl_seconds=60,
    )

    print(f"Created request: {req.request_id}")

    # First click
    result1 = update_request_state(req.request_id, RequestState.ALLOW, actor_user_id=12345)
    print(f"First click: {result1.state if result1 else 'None'}")

    # Second click (duplicate)
    result2 = update_request_state(req.request_id, RequestState.DENY, actor_user_id=12345)
    print(f"Second click (attempted deny): {result2.state if result2 else 'None (blocked)'}")

    # Verify state is still ALLOW from first click
    check = get_request(req.request_id)
    assert check.state == "allow", f"Expected allow, got {check.state}"

    print("PASS: Double-click ignored, state remains unchanged")
    return True


def test_expired_request_rejection():
    """Test: Reply text arrives after request expired -> rejected safely"""
    print("\n=== Test 3: Expired Request Rejection ===")

    # Create request with very short TTL (1 second)
    req = create_request(
        session_id="test-session-exp",
        cwd="/test/path",
        tool_name="Bash",
        tool_input={"command": "sleep 1"},
        permission_suggestions=["Bash(sleep:*)"],
        ttl_seconds=1,  # 1 second TTL
    )

    print(f"Created request: {req.request_id} with 1s TTL")

    # Wait for expiration
    print("Waiting 2 seconds for expiration...")
    time.sleep(2)

    # Attempt to update expired request
    result = update_request_state(
        req.request_id,
        RequestState.ALLOW,
        actor_user_id=12345,
    )

    print(f"Update attempt on expired: {result.state if result else 'None (rejected)'}")

    # Verify request is None or expired
    if result is None:
        print("PASS: Expired request was rejected")
        return True
    else:
        # Request may have been marked as expired
        check = get_request(req.request_id)
        if check is None or check.state == "expired":
            print("PASS: Request is expired")
            return True
        else:
            print(f"FAIL: Request state is {check.state}, expected expired or None")
            return False


def test_whitelist_json_update():
    """Test: Whitelist writes valid JSON and dedupes existing rule"""
    print("\n=== Test 4: Whitelist JSON Update ===")

    test_dir = Path("/tmp/test_task03_whitelist")
    test_dir.mkdir(parents=True, exist_ok=True)

    settings_path = test_dir / ".claude" / "settings.local.json"

    # Clean up any existing test file
    if settings_path.exists():
        settings_path.unlink()

    # First write
    success1 = update_settings_local_json(str(test_dir), "Bash(ls:*)")
    print(f"First write: {success1}")

    # Read and verify
    with open(settings_path) as f:
        settings1 = json.load(f)
    print(f"After first write: {settings1}")

    assert "Bash(ls:*)" in settings1["permissions"]["allow"], "Pattern not in allow list"

    # Second write (different pattern)
    success2 = update_settings_local_json(str(test_dir), "Bash(cat:*)")
    print(f"Second write: {success2}")

    with open(settings_path) as f:
        settings2 = json.load(f)
    print(f"After second write: {settings2}")

    assert "Bash(ls:*)" in settings2["permissions"]["allow"], "First pattern missing"
    assert "Bash(cat:*)" in settings2["permissions"]["allow"], "Second pattern missing"

    # Third write (duplicate - should dedupe)
    success3 = update_settings_local_json(str(test_dir), "Bash(ls:*)")
    print(f"Third write (duplicate): {success3}")

    with open(settings_path) as f:
        settings3 = json.load(f)

    # Count occurrences
    count = settings3["permissions"]["allow"].count("Bash(ls:*)")
    print(f"Occurrences of Bash(ls:*): {count}")

    assert count == 1, f"Expected 1 occurrence, got {count}"

    # Verify JSON is valid
    with open(settings_path) as f:
        json.load(f)  # Will raise if invalid

    print("PASS: JSON is valid and deduplication works")

    # Cleanup
    import shutil
    shutil.rmtree(test_dir)

    return True


def run_all_tests():
    """Run all validation tests"""
    print("=" * 50)
    print("Task 03 Validation Test Suite")
    print("=" * 50)

    results = []

    try:
        results.append(("Concurrent Requests", test_concurrent_requests()))
    except Exception as e:
        print(f"FAILED: {e}")
        results.append(("Concurrent Requests", False))

    try:
        results.append(("Double-Click Idempotency", test_double_click_idempotency()))
    except Exception as e:
        print(f"FAILED: {e}")
        results.append(("Double-Click Idempotency", False))

    try:
        results.append(("Expired Request Rejection", test_expired_request_rejection()))
    except Exception as e:
        print(f"FAILED: {e}")
        results.append(("Expired Request Rejection", False))

    try:
        results.append(("Whitelist JSON Update", test_whitelist_json_update()))
    except Exception as e:
        print(f"FAILED: {e}")
        results.append(("Whitelist JSON Update", False))

    # Summary
    print("\n" + "=" * 50)
    print("Test Summary")
    print("=" * 50)

    passed = 0
    failed = 0
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  {name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")

    return failed == 0


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
