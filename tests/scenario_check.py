#!/usr/bin/env python3
"""
Quick Scenario Check Script for Permission Flow

This script provides quick checks for common scenarios:
1. PreToolUse classifier behavior
2. PermissionRequest decision mapping
3. State store operations
4. Whitelist updates

Run with: python3 scenario_check.py [scenario]

Scenarios:
  all        - Run all quick checks
  pretool    - Check PreToolUse classifier
  decision   - Check decision mapper
  state      - Check state store
  whitelist  - Check whitelist logic
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))
sys.path.insert(0, str(Path(__file__).parent))

# Color codes for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def pass_fail(condition, message):
    """Print pass/fail message."""
    if condition:
        print(f"  {GREEN}PASS{RESET}: {message}")
        return True
    else:
        print(f"  {RED}FAIL{RESET}: {message}")
        return False


def check_pretool():
    """Check PreToolUse classifier behavior."""
    print("\n" + "=" * 50)
    print("PreToolUse Classifier Checks")
    print("=" * 50)

    from pretool_hook import BashPermissionValidator, BashCommandParser
    from settings_loader import SettingsLoader

    workspace = str(Path(__file__).parent.parent)
    settings_loader = SettingsLoader(workspace)
    parser = BashCommandParser()
    validator = BashPermissionValidator(settings_loader, parser)

    results = []

    # Check 1: Allowed command
    result = validator.validate_bash_command("ls -la")
    results.append(pass_fail(
        result["decision"] == "allow",
        "ls -la should be allowed"
    ))

    # Check 2: Allowed compound command
    result = validator.validate_bash_command("git status && git diff")
    results.append(pass_fail(
        result["decision"] == "allow",
        "git status && git diff should be allowed"
    ))

    # Check 3: Unknown command
    result = validator.validate_bash_command("unknown_command_xyz")
    results.append(pass_fail(
        result["decision"] == "ask",
        "unknown_command_xyz should return 'ask'"
    ))

    # Check 4: Denied command
    result = validator.validate_bash_command("git push --force origin main")
    results.append(pass_fail(
        result["decision"] == "ask",
        "git push --force should return 'ask' (denied pattern)"
    ))

    # Check 5: Pipeline
    result = validator.validate_bash_command("git diff | head -100")
    results.append(pass_fail(
        result["decision"] == "allow" and len(result["sub_commands"]) == 2,
        "git diff | head -100 should be allowed (2 sub-commands)"
    ))

    # Check 6: Heredoc (known fragility)
    try:
        result = validator.validate_bash_command('cat << "EOF"\nhello\nEOF')
        results.append(pass_fail(
            True,  # Just check no crash
            "Heredoc parsing does not crash (may have known issues)"
        ))
    except Exception as e:
        results.append(pass_fail(
            False,
            f"Heredoc parsing crashed: {e}"
        ))

    return all(results)


def check_decision():
    """Check decision mapper behavior."""
    print("\n" + "=" * 50)
    print("Decision Mapper Checks")
    print("=" * 50)

    from permission_request_hook import build_output_decision
    from permission_state_store import PermissionRequest

    results = []

    def make_request():
        return PermissionRequest(
            request_id="test-decision",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

    # Check 1: Allow decision
    output = build_output_decision({"action": "allow"}, make_request())
    results.append(pass_fail(
        output and output["hookSpecificOutput"]["decision"]["behavior"] == "allow",
        "allow -> behavior: 'allow'"
    ))

    # Check 2: Deny decision
    output = build_output_decision({"action": "deny"}, make_request())
    results.append(pass_fail(
        output and output["hookSpecificOutput"]["decision"]["behavior"] == "deny",
        "deny -> behavior: 'deny'"
    ))

    # Check 3: Stop decision
    output = build_output_decision({"action": "stop"}, make_request())
    results.append(pass_fail(
        output and output["hookSpecificOutput"]["decision"]["behavior"] == "deny" and
        output["hookSpecificOutput"]["decision"]["interrupt"] == True,
        "stop -> behavior: 'deny' + interrupt: true"
    ))

    # Check 4: Whitelist decision
    with patch_if_available("permission_request_hook.process_whitelist_update", return_value=True):
        output = build_output_decision({
            "action": "whitelist",
            "updatedPermissions": {"add": ["Bash(test:*)"]}
        }, make_request())
    results.append(pass_fail(
        output and output["hookSpecificOutput"]["decision"]["behavior"] == "allow" and
        "updatedPermissions" in output["hookSpecificOutput"]["decision"],
        "whitelist -> behavior: 'allow' + updatedPermissions"
    ))

    # Check 5: Reply decision
    output = build_output_decision({"action": "reply", "reply_text": "Nope"}, make_request())
    results.append(pass_fail(
        output and output["hookSpecificOutput"]["decision"]["behavior"] == "deny" and
        "Nope" in output["hookSpecificOutput"]["decision"]["reason"],
        "reply -> behavior: 'deny' + reason with text"
    ))

    # Check 6: Timeout fallback
    output = build_output_decision(None, make_request())
    results.append(pass_fail(
        output is None,
        "None decision -> None (fallback to terminal)"
    ))

    # Check 7: Unknown action fallback
    output = build_output_decision({"action": "invalid"}, make_request())
    results.append(pass_fail(
        output is None,
        "Unknown action -> None (fallback to terminal)"
    ))

    return all(results)


def patch_if_available(module, **kwargs):
    """Try to patch, return dummy context manager if not available."""
    try:
        from unittest.mock import patch
        return patch(module, **kwargs)
    except:
        class DummyContext:
            def __enter__(self): return None
            def __exit__(self, *args): pass
        return DummyContext()


def check_state():
    """Check state store behavior."""
    print("\n" + "=" * 50)
    print("State Store Checks")
    print("=" * 50)

    from permission_state_store import (
        create_request,
        get_request,
        update_request_state,
        RequestState,
        cleanup_expired_requests,
    )

    results = []

    # Check 1: Create and retrieve
    req = create_request(
        session_id="check-state-1",
        cwd="/test",
        tool_name="Bash",
        tool_input={"command": "test"},
        permission_suggestions=[],
        ttl_seconds=60,
    )
    retrieved = get_request(req.request_id)
    results.append(pass_fail(
        retrieved and retrieved.request_id == req.request_id and retrieved.state == "pending",
        "Create and retrieve request"
    ))

    # Check 2: State transition
    updated = update_request_state(req.request_id, RequestState.ALLOW)
    check = get_request(req.request_id)
    results.append(pass_fail(
        updated and check and check.state == "allow",
        "Transition pending -> allow"
    ))

    # Check 3: Idempotency (double update blocked)
    second = update_request_state(req.request_id, RequestState.DENY)
    check = get_request(req.request_id)
    results.append(pass_fail(
        second is None and check.state == "allow",
        "Double update blocked (idempotent)"
    ))

    # Check 4: Expiration
    short_req = create_request(
        session_id="check-state-expire",
        cwd="/test",
        tool_name="Bash",
        tool_input={"command": "test"},
        permission_suggestions=[],
        ttl_seconds=1,
    )
    time.sleep(2)
    expired = get_request(short_req.request_id)
    results.append(pass_fail(
        expired is None,
        "Expired request returns None"
    ))

    return all(results)


def check_whitelist():
    """Check whitelist logic."""
    print("\n" + "=" * 50)
    print("Whitelist Logic Checks")
    print("=" * 50)

    from telegram_permission_router import (
        generate_whitelist_pattern,
        update_settings_local_json,
    )
    from permission_state_store import PermissionRequest
    import tempfile
    import shutil

    results = []

    # Create temp directory for testing
    temp_dir = tempfile.mkdtemp()

    try:
        # Check 1: Pattern from suggestions
        req = PermissionRequest(
            request_id="wl-test",
            session_id="test",
            cwd=temp_dir,
            tool_name="Bash",
            tool_input={"command": "npm install"},
            permission_suggestions=["Bash(npm:*)"],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        pattern = generate_whitelist_pattern(req)
        results.append(pass_fail(
            pattern == "Bash(npm:*)",
            f"Pattern from suggestions: {pattern}"
        ))

        # Check 2: Pattern generated from command
        req2 = PermissionRequest(
            request_id="wl-test-2",
            session_id="test",
            cwd=temp_dir,
            tool_name="Bash",
            tool_input={"command": "git status"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        pattern = generate_whitelist_pattern(req2)
        results.append(pass_fail(
            pattern == "Bash(git:*)",
            f"Pattern generated from command: {pattern}"
        ))

        # Check 3: Settings update creates file
        success = update_settings_local_json(temp_dir, "Bash(test:*)")
        settings_path = Path(temp_dir) / ".claude" / "settings.local.json"
        results.append(pass_fail(
            success and settings_path.exists(),
            "Settings file created"
        ))

        # Check 4: Deduplication
        update_settings_local_json(temp_dir, "Bash(test:*)")
        with open(settings_path) as f:
            settings = json.load(f)
        count = settings["permissions"]["allow"].count("Bash(test:*)")
        results.append(pass_fail(
            count == 1,
            f"Deduplication works (count: {count})"
        ))

        # Check 5: Add different pattern
        update_settings_local_json(temp_dir, "Bash(other:*)")
        with open(settings_path) as f:
            settings = json.load(f)
        results.append(pass_fail(
            "Bash(other:*)" in settings["permissions"]["allow"],
            "Second pattern added"
        ))

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return all(results)


def run_all():
    """Run all quick checks."""
    print("\n" + "=" * 60)
    print("  TASK 04: Permission Flow Quick Scenario Checks")
    print("=" * 60)

    all_passed = True

    scenarios = [
        ("PreToolUse Classifier", check_pretool),
        ("Decision Mapper", check_decision),
        ("State Store", check_state),
        ("Whitelist Logic", check_whitelist),
    ]

    for name, check_func in scenarios:
        try:
            passed = check_func()
            if not passed:
                all_passed = False
        except Exception as e:
            print(f"{RED}ERROR{RESET} in {name}: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print(f"  {GREEN}ALL CHECKS PASSED{RESET}")
    else:
        print(f"  {RED}SOME CHECKS FAILED{RESET}")
    print("=" * 60)

    return all_passed


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        scenario = sys.argv[1].lower()
        if scenario == "all":
            success = run_all()
        elif scenario == "pretool":
            success = check_pretool()
        elif scenario == "decision":
            success = check_decision()
        elif scenario == "state":
            success = check_state()
        elif scenario == "whitelist":
            success = check_whitelist()
        else:
            print(f"Unknown scenario: {scenario}")
            print(__doc__)
            sys.exit(1)
    else:
        success = run_all()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
