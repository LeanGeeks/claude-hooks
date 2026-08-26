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

import contextlib
import fcntl
import io
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

import permission_state_store as pss
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
    get_requests,
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


class TestActorAgentField(unittest.TestCase):
    """``PermissionRequest.actor_agent`` and the ``agent`` resolution source
    (epic 22): every agent-written decision must be attributable after the
    fact — on the row and in the audit log."""

    def test_agent_decision_persists_actor_and_source(self):
        from permission_state_store import RESOLUTION_SOURCE_AGENT

        request = create_request(
            session_id="test-actor-agent",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=[],
            ttl_seconds=60,
        )
        self.assertIsNone(request.actor_agent)

        updated = update_request_state(
            request.request_id,
            RequestState.ALLOW,
            decision={"action": "allow"},
            resolution_source=RESOLUTION_SOURCE_AGENT,
            actor_agent="abc123def456 @ claude-hooks",
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.actor_agent, "abc123def456 @ claude-hooks")
        self.assertEqual(updated.resolution_source, RESOLUTION_SOURCE_AGENT)

        loaded = get_request(request.request_id)
        self.assertEqual(loaded.actor_agent, "abc123def456 @ claude-hooks")
        self.assertEqual(loaded.resolution_source, "agent")
        self.assertEqual(loaded.decision, {"action": "allow"})

    def test_audit_entry_carries_actor_agent(self):
        from permission_state_store import RESOLUTION_SOURCE_AGENT

        request = create_request(
            session_id="test-actor-agent-audit",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=[],
            ttl_seconds=60,
        )
        update_request_state(
            request.request_id,
            RequestState.DENY,
            decision={"action": "deny"},
            resolution_source=RESOLUTION_SOURCE_AGENT,
            actor_agent="sess-9f8e @ worktree",
        )

        entries = []
        with open(AUDIT_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("request_id") == request.request_id:
                    entries.append(entry)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor_agent"], "sess-9f8e @ worktree")
        self.assertEqual(entries[0]["new_state"], "deny")
        self.assertIsNone(entries[0]["actor_user_id"])

    def test_human_write_leaves_actor_agent_untouched(self):
        """The guarded write (like ``actor_user_id``): a later human/timeout
        write that passes no ``actor_agent`` must not blank an existing one, and
        must not invent one."""
        from permission_state_store import RESOLUTION_SOURCE_TELEGRAM

        request = create_request(
            session_id="test-actor-agent-guard",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=[],
            ttl_seconds=60,
        )
        updated = update_request_state(
            request.request_id,
            RequestState.ALLOW,
            decision={"action": "allow"},
            actor_user_id=42,
            resolution_source=RESOLUTION_SOURCE_TELEGRAM,
        )
        self.assertIsNone(updated.actor_agent)
        self.assertEqual(get_request(request.request_id).actor_agent, None)

    def test_row_written_before_this_change_loads_with_actor_agent_none(self):
        """Old-format rows (no ``actor_agent`` key) must deserialize unchanged."""
        legacy = {
            "request_id": "legacy-row-no-actor-agent",
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
            "role": None,
        }
        self.assertNotIn("actor_agent", legacy)
        self.assertIsNone(PermissionRequest.from_dict(legacy).actor_agent)

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "a") as f:
            f.write(json.dumps(legacy) + "\n")

        loaded = get_request("legacy-row-no-actor-agent")
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.actor_agent)
        self.assertEqual(loaded.tool_name, "Bash")

        # …and a new agent write onto that old row still attributes.
        from permission_state_store import RESOLUTION_SOURCE_AGENT

        updated = update_request_state(
            "legacy-row-no-actor-agent",
            RequestState.ALLOW,
            decision={"action": "allow"},
            resolution_source=RESOLUTION_SOURCE_AGENT,
            actor_agent="legacy-writer @ test",
        )
        self.assertEqual(updated.actor_agent, "legacy-writer @ test")


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


class TestCompaction(unittest.TestCase):
    """Store compaction (epic 22, task 22-05 §1).

    Every case points the module's file constants at a scratch directory: the
    functions read them as globals at call time, so patching the module
    attribute is enough and nothing touches the developer's real ~/.claude.
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="compact-test-"))
        self.state_file = self.temp_dir / "permission_requests.jsonl"
        self.archive_file = self.temp_dir / "permission_requests.archive.jsonl"
        self.confirm_log = self.temp_dir / "bash_manual_confirm.log"
        self._patches = [
            patch.object(pss, "STATE_FILE", self.state_file),
            patch.object(pss, "ARCHIVE_FILE", self.archive_file),
            patch.object(pss, "MANUAL_CONFIRM_LOG_FILE", self.confirm_log),
        ]
        for entry in self._patches:
            entry.start()

    def tearDown(self):
        for entry in self._patches:
            entry.stop()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _row(self, request_id, state, age_days, **extra):
        stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        row = {
            "request_id": request_id,
            "session_id": "s-" + request_id,
            "cwd": "/test",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "permission_suggestions": [],
            "state": state,
            "created_at": stamp,
            "updated_at": stamp,
            "expires_at": stamp,
            "resolved_at": stamp if state != "pending" else None,
        }
        row.update(extra)
        return row

    def _write_rows(self, rows):
        with open(self.state_file, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def _hot_ids(self):
        ids = []
        with open(self.state_file) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    ids.append(json.loads(line)["request_id"])
        return ids

    def _archive_ids(self):
        if not self.archive_file.exists():
            return []
        ids = []
        with open(self.archive_file) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    ids.append(json.loads(line)["request_id"])
        return ids

    def test_old_terminal_rows_move_to_archive(self):
        """A terminal row past the retention window leaves the hot file."""
        self._write_rows([
            self._row("old-allow", "allow", 45),
            self._row("old-deny", "deny", 90),
            self._row("recent-allow", "allow", 3),
        ])

        counts = pss.compact(max_age_days=30, rotate_confirm_log=False)

        self.assertEqual(counts["scanned"], 3)
        self.assertEqual(counts["archived"], 2)
        self.assertEqual(counts["kept"], 1)
        self.assertEqual(self._hot_ids(), ["recent-allow"])
        self.assertEqual(sorted(self._archive_ids()), ["old-allow", "old-deny"])

    def test_pending_rows_never_move_regardless_of_age(self):
        """Expiry retires pending rows; compaction must not touch them."""
        self._write_rows([
            self._row("ancient-pending", "pending", 400),
            self._row("old-expired", "expired", 400),
        ])

        counts = pss.compact(max_age_days=30, rotate_confirm_log=False)

        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["kept_pending"], 1)
        self.assertEqual(self._hot_ids(), ["ancient-pending"])
        self.assertEqual(self._archive_ids(), ["old-expired"])

    def test_archive_appends_across_two_runs(self):
        """The second run adds to cold storage, it does not replace it."""
        self._write_rows([
            self._row("run-one-old", "allow", 60),
            self._row("run-two-old", "deny", 5),
        ])

        first = pss.compact(max_age_days=30, rotate_confirm_log=False)
        self.assertEqual(first["archived"], 1)
        self.assertEqual(self._archive_ids(), ["run-one-old"])

        # Second run with a tighter window catches the row the first one kept.
        second = pss.compact(max_age_days=1, rotate_confirm_log=False)
        self.assertEqual(second["archived"], 1)
        self.assertEqual(self._archive_ids(), ["run-one-old", "run-two-old"])
        self.assertEqual(self._hot_ids(), [])

    def test_hot_file_remains_valid_jsonl(self):
        """Every surviving line still parses, and the store's own reader agrees."""
        self._write_rows([
            self._row("old-allow", "allow", 40),
            self._row("keep-pending", "pending", 200),
            self._row("keep-recent", "resolved_terminal", 1),
        ])

        pss.compact(max_age_days=30, rotate_confirm_log=False)

        raw = self.state_file.read_text()
        self.assertTrue(raw.endswith("\n"))
        for line in raw.splitlines():
            json.loads(line)
        ids = [request.request_id for request in pss.get_requests()]
        self.assertEqual(ids, ["keep-pending", "keep-recent"])

    def test_undatable_and_unparseable_rows_are_kept(self):
        """Evidence is never deleted on a bad timestamp or a corrupt line."""
        undatable = self._row("no-timestamps", "allow", 500)
        undatable["created_at"] = ""
        undatable["updated_at"] = ""
        undatable["resolved_at"] = None
        with open(self.state_file, "w") as handle:
            handle.write(json.dumps(undatable) + "\n")
            handle.write("{not json at all\n")
            handle.write(json.dumps(self._row("old-allow", "allow", 99)) + "\n")

        counts = pss.compact(max_age_days=30, rotate_confirm_log=False)

        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["kept_unparseable"], 2)
        raw = self.state_file.read_text().splitlines()
        self.assertIn("{not json at all", raw)
        self.assertEqual(self._archive_ids(), ["old-allow"])

    def test_lock_is_held_across_the_rewrite(self):
        """The exclusive flock spans read → archive → truncate → fsync.

        The probe runs from inside ``_release_lock``, i.e. after the hot file
        has already been rewritten and fsynced but before the lock is dropped.
        A second ``open()`` is a separate open file description, so a
        non-blocking flock from it fails exactly when the lock is genuinely
        held.
        """
        self._write_rows([
            self._row("old-allow", "allow", 60),
            self._row("keep-recent", "allow", 1),
        ])

        observed = {}
        original_release = pss._release_lock

        def probing_release(file_obj):
            with open(self.state_file, "r") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed["locked_during_rewrite"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    observed["locked_during_rewrite"] = True
                # The rewrite is already on disk at this point.
                observed["hot_lines"] = len(
                    [line for line in probe.read().splitlines() if line.strip()]
                )
            return original_release(file_obj)

        with patch.object(pss, "_release_lock", probing_release):
            pss.compact(max_age_days=30, rotate_confirm_log=False)

        self.assertTrue(observed.get("locked_during_rewrite"))
        self.assertEqual(observed.get("hot_lines"), 1)

        # …and the lock is gone once compact returns.
        with open(self.state_file, "r") as after:
            fcntl.flock(after.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(after.fileno(), fcntl.LOCK_UN)

    def test_missing_state_file_is_a_no_op(self):
        counts = pss.compact(max_age_days=30, rotate_confirm_log=False)
        self.assertEqual(counts["scanned"], 0)
        self.assertEqual(counts["archived"], 0)
        self.assertFalse(self.archive_file.exists())

    def test_confirm_log_rotates_only_when_oversized(self):
        """Rotation renames aside; ``open(..., 'a')`` recreates on next append."""
        self.confirm_log.write_text("x" * 100)

        untouched = pss.compact(
            max_age_days=30, rotate_confirm_log=True, confirm_log_max_bytes=1000
        )
        self.assertIsNone(untouched["confirm_log_rotated_to"])
        self.assertTrue(self.confirm_log.exists())

        rotated = pss.compact(
            max_age_days=30, rotate_confirm_log=True, confirm_log_max_bytes=10
        )
        destination = rotated["confirm_log_rotated_to"]
        self.assertIsNotNone(destination)
        self.assertTrue(Path(destination).exists())
        self.assertFalse(self.confirm_log.exists())

        # The append mode the pretool hook uses recreates the live log.
        with open(self.confirm_log, "a") as handle:
            handle.write("{}\n")
        self.assertTrue(self.confirm_log.exists())

    def test_cli_compact_prints_counts_and_archives(self):
        """The CLI entry is the path the daily reviewer runs."""
        self._write_rows([self._row("old-allow", "allow", 60)])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pss._cli(["compact", "--max-age-days", "30", "--no-rotate-log", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["archived"], 1)
        self.assertEqual(self._archive_ids(), ["old-allow"])

    def test_cli_without_a_subcommand_does_not_write(self):
        """A bare invocation prints help — it must never run the selftest."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pss._cli([])
        self.assertEqual(code, 0)
        self.assertIn("compact", buffer.getvalue())
        self.assertFalse(self.state_file.exists())



if __name__ == "__main__":
    unittest.main(verbosity=2)
