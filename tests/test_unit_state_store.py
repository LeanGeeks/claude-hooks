#!/usr/bin/env python3
"""
Unit Tests: Permission State Store

Tests the state management for permission requests:
- Request creation
- State transitions (pending -> terminal states)
- Idempotency (no double-transitions)
- Locking/thread safety
- Expiration handling
- Cleanup of expired requests
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

from permission_state_store import (
    PermissionRequest,
    RequestState,
    TERMINAL_STATES,
    create_request,
    get_request,
    get_pending_request_for_session,
    update_request_state,
    set_telegram_message_id,
    find_request_by_message_id,
    cleanup_expired_requests,
    _is_expired,
    _utc_now,
    _expires_at,
    STATE_FILE,
    AUDIT_LOG_FILE,
)


class TestRequestCreation(unittest.TestCase):
    """Test permission request creation."""

    def setUp(self):
        """Set up test fixtures."""
        # Use a temp state file for testing
        self.temp_dir = tempfile.mkdtemp()
        self.original_state_file = STATE_FILE
        self.original_audit_file = AUDIT_LOG_FILE

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_request_basic(self):
        """Test basic request creation."""
        request = create_request(
            session_id="test-session-001",
            cwd="/test/path",
            tool_name="Bash",
            tool_input={"command": "ls -la"},
            permission_suggestions=["Bash(ls:*)"],
            ttl_seconds=300,
        )

        self.assertIsNotNone(request.request_id)
        self.assertEqual(request.session_id, "test-session-001")
        self.assertEqual(request.cwd, "/test/path")
        self.assertEqual(request.tool_name, "Bash")
        self.assertEqual(request.state, "pending")
        self.assertIsNotNone(request.created_at)
        self.assertIsNotNone(request.expires_at)

    def test_create_request_with_custom_ttl(self):
        """Test request creation with custom TTL."""
        request = create_request(
            session_id="test-session-002",
            cwd="/test/path",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # Check that expires_at is roughly 60 seconds in the future
        from datetime import datetime
        expires_dt = datetime.fromisoformat(request.expires_at.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        delta = expires_dt - now
        self.assertGreater(delta.total_seconds(), 50)  # Allow some slack
        self.assertLess(delta.total_seconds(), 70)

    def test_request_id_uniqueness(self):
        """Test that each request gets a unique ID."""
        request1 = create_request(
            session_id="test-1",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd1"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        request2 = create_request(
            session_id="test-2",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd2"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        self.assertNotEqual(request1.request_id, request2.request_id)


class TestStateTransitions(unittest.TestCase):
    """Test state transitions for permission requests."""

    def test_pending_to_allow(self):
        """Test transition from pending to allow."""
        request = create_request(
            session_id="test-allow",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        updated = update_request_state(request.request_id, RequestState.ALLOW)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, "allow")

    def test_pending_to_deny(self):
        """Test transition from pending to deny."""
        request = create_request(
            session_id="test-deny",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "rm"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        updated = update_request_state(request.request_id, RequestState.DENY)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, "deny")

    def test_pending_to_stop(self):
        """Test transition from pending to stop."""
        request = create_request(
            session_id="test-stop",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        updated = update_request_state(request.request_id, RequestState.STOP)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, "stop")

    def test_pending_to_whitelist(self):
        """Test transition from pending to whitelist."""
        request = create_request(
            session_id="test-whitelist",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=["Bash(cmd:*)"],
            ttl_seconds=60,
        )

        decision = {"action": "whitelist", "updatedPermissions": {"add": ["Bash(cmd:*)"]}}
        updated = update_request_state(
            request.request_id,
            RequestState.WHITELIST,
            decision=decision,
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, "whitelist")
        self.assertEqual(updated.decision, decision)

    def test_pending_to_reply(self):
        """Test transition from pending to reply."""
        request = create_request(
            session_id="test-reply",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        updated = update_request_state(
            request.request_id,
            RequestState.REPLY,
            reply_text="User response",
            actor_user_id=12345,
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, "reply")
        self.assertEqual(updated.reply_text, "User response")
        self.assertEqual(updated.actor_user_id, 12345)


class TestIdempotency(unittest.TestCase):
    """Test idempotency - double transitions should be blocked."""

    def test_double_allow_blocked(self):
        """Test that double-allow is blocked."""
        request = create_request(
            session_id="test-dbl-allow",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # First transition
        first = update_request_state(request.request_id, RequestState.ALLOW)
        self.assertIsNotNone(first)
        self.assertEqual(first.state, "allow")

        # Second transition (should be blocked)
        second = update_request_state(request.request_id, RequestState.DENY)
        self.assertIsNone(second)

        # Verify original state is preserved
        retrieved = get_request(request.request_id)
        self.assertEqual(retrieved.state, "allow")

    def test_double_deny_blocked(self):
        """Test that double-deny is blocked."""
        request = create_request(
            session_id="test-dbl-deny",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # First transition
        first = update_request_state(request.request_id, RequestState.DENY)
        self.assertIsNotNone(first)

        # Second transition (should be blocked)
        second = update_request_state(request.request_id, RequestState.ALLOW)
        self.assertIsNone(second)

    def test_terminal_state_to_pending_blocked(self):
        """Test that terminal state cannot go back to pending."""
        request = create_request(
            session_id="test-terminal",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # Move to terminal state
        update_request_state(request.request_id, RequestState.STOP)

        # Try to go back to pending (should be blocked)
        result = update_request_state(request.request_id, RequestState.PENDING)
        self.assertIsNone(result)


class TestExpiration(unittest.TestCase):
    """Test expiration handling."""

    def test_is_expired_logic(self):
        """Test _is_expired helper function."""
        # Past time
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(_is_expired(past))

        # Future time
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertFalse(_is_expired(future))

        # Invalid format
        self.assertTrue(_is_expired("invalid"))

    def test_expired_request_not_returned(self):
        """Test that get_request does not return expired pending requests."""
        # Create request with 1 second TTL
        request = create_request(
            session_id="test-expired-get",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=1,
        )

        # Should exist immediately
        retrieved = get_request(request.request_id)
        self.assertIsNotNone(retrieved)

        # Wait for expiration
        time.sleep(2)

        # Should not return expired pending request
        retrieved = get_request(request.request_id)
        self.assertIsNone(retrieved)

    def test_update_expired_request_blocked(self):
        """Test that updating expired request is blocked."""
        # Create request with 1 second TTL
        request = create_request(
            session_id="test-expired-update",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=1,
        )

        # Wait for expiration
        time.sleep(2)

        # Try to update expired request
        result = update_request_state(request.request_id, RequestState.ALLOW)
        self.assertIsNone(result)


class TestLockingAndConcurrency(unittest.TestCase):
    """Test file locking and concurrency safety."""

    def test_concurrent_updates(self):
        """Test that concurrent updates are handled safely."""
        request = create_request(
            session_id="test-concurrent",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        results = []
        errors = []

        def update_thread(state):
            try:
                result = update_request_state(request.request_id, state)
                results.append((state, result))
            except Exception as e:
                errors.append(e)

        # Start multiple threads trying to update simultaneously
        threads = [
            threading.Thread(target=update_thread, args=(RequestState.ALLOW,)),
            threading.Thread(target=update_thread, args=(RequestState.DENY,)),
            threading.Thread(target=update_thread, args=(RequestState.STOP,)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no errors
        self.assertEqual(len(errors), 0, f"Errors: {errors}")

        # Only one update should have succeeded
        successful_updates = [r for r in results if r[1] is not None]
        self.assertEqual(len(successful_updates), 1)

        # Final state should be consistent
        final = get_request(request.request_id)
        self.assertIn(final.state, ["allow", "deny", "stop"])


class TestMessageIdTracking(unittest.TestCase):
    """Test Telegram message ID tracking."""

    def test_set_message_id(self):
        """Test setting Telegram message ID."""
        # Use unique message ID to avoid conflicts with other tests
        import time
        unique_msg_id = int(time.time() * 1000) % 10000000  # Unique but reasonable

        request = create_request(
            session_id=f"test-msg-id-{unique_msg_id}",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        success = set_telegram_message_id(request.request_id, unique_msg_id)
        self.assertTrue(success, f"set_telegram_message_id failed for {request.request_id}")

        # Find by message ID
        found = find_request_by_message_id(unique_msg_id)
        self.assertIsNotNone(found, f"Request not found for message_id {unique_msg_id}")
        self.assertEqual(found.request_id, request.request_id,
            f"Found request_id {found.request_id} != expected {request.request_id}")

    def test_find_by_nonexistent_message_id(self):
        """Test finding by non-existent message ID."""
        found = find_request_by_message_id(999999999)
        self.assertIsNone(found)


class TestSessionQueries(unittest.TestCase):
    """Test session-based queries."""

    def test_get_pending_for_session(self):
        """Test getting pending request for a session."""
        session_id = "test-session-pending"

        # Create a request
        request = create_request(
            session_id=session_id,
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # Get pending request
        pending = get_pending_request_for_session(session_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.request_id, request.request_id)

    def test_no_pending_for_nonexistent_session(self):
        """Test that non-existent session returns None."""
        pending = get_pending_request_for_session("nonexistent-session-xyz")
        self.assertIsNone(pending)

    def test_non_pending_not_returned(self):
        """Test that non-pending requests are not returned."""
        session_id = "test-session-resolved"

        # Create and resolve a request
        request = create_request(
            session_id=session_id,
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            ttl_seconds=60,
        )
        update_request_state(request.request_id, RequestState.ALLOW)

        # Should not return resolved request
        pending = get_pending_request_for_session(session_id)
        self.assertIsNone(pending)


class TestRoleField(unittest.TestCase):
    """``PermissionRequest.role`` — the resolved role id a request was routed
    to. Only the id is persisted; the installation token never is."""

    def test_create_request_defaults_role_to_none(self):
        request = create_request(
            session_id="test-role-default",
            cwd="/test",
            tool_name="AskUserQuestion",
            tool_input={"question": "q"},
            permission_suggestions=[],
            ttl_seconds=60,
        )
        self.assertIsNone(request.role)
        self.assertIsNone(get_request(request.request_id).role)

    def test_role_round_trips_through_the_jsonl_store(self):
        request = create_request(
            session_id="test-role-roundtrip",
            cwd="/test",
            tool_name="AskUserQuestion",
            tool_input={"question": "q"},
            permission_suggestions=[],
            ttl_seconds=60,
            role="ux",
        )
        self.assertEqual(request.role, "ux")
        self.assertEqual(request.to_dict()["role"], "ux")

        loaded = get_request(request.request_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.role, "ux")

    def test_row_written_before_this_change_loads_with_role_none(self):
        """Rows persisted before ``role`` existed must deserialize unchanged."""
        legacy = {
            "request_id": "legacy-row-no-role",
            "session_id": "legacy-session",
            "cwd": "/test",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "permission_suggestions": [],
            "state": "pending",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "expires_at": _expires_at(600),
            "telegram_message_id": None,
            "decision": None,
            "reply_text": None,
            "actor_user_id": None,
            "resolution_source": None,
            "resolved_at": None,
            "expired_notified_at": None,
            "agent_id": None,
        }
        self.assertNotIn("role", legacy)

        # Straight from_dict, and through the JSONL store.
        self.assertIsNone(PermissionRequest.from_dict(legacy).role)

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "a") as f:
            f.write(json.dumps(legacy) + "\n")

        loaded = get_request("legacy-row-no-role")
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.role)
        self.assertEqual(loaded.tool_name, "Bash")

    def test_from_dict_filters_unknown_keys(self):
        """Forward compatibility: a row carrying a field this version does not
        know about still loads."""
        loaded = PermissionRequest.from_dict({
            "request_id": "unknown-key-row",
            "session_id": "s",
            "cwd": "/test",
            "tool_name": "Bash",
            "tool_input": {},
            "permission_suggestions": [],
            "state": "pending",
            "created_at": "t",
            "updated_at": "t",
            "expires_at": "t",
            "role": "ux",
            "a_field_from_the_future": 42,
        })
        self.assertEqual(loaded.role, "ux")
        self.assertFalse(hasattr(loaded, "a_field_from_the_future"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
