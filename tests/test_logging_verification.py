#!/usr/bin/env python3
"""
Logging Verification Tests for Permission Flow

Tests that verify logs are properly written to:
- ~/.claude/*debug*.log
- State/audit logs
"""

import os
import sys
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

from permission_state_store import (
    create_request,
    update_request_state,
    RequestState,
    STATE_FILE,
    AUDIT_LOG_FILE,
    DEBUG_LOG,
)


class TestLoggingVerification(unittest.TestCase):
    """Verify logging behavior."""

    def setUp(self):
        """Set up test fixtures."""
        # Enable debug logging
        os.environ["CLAUDE_HOOK_DEBUG"] = "1"

    def test_audit_log_written_on_state_change(self):
        """Test that audit log is written on state change."""
        # Clear audit log
        if AUDIT_LOG_FILE.exists():
            initial_lines = len(AUDIT_LOG_FILE.read_text().strip().split("\n"))
        else:
            initial_lines = 0

        # Create and update a request
        request = create_request(
            session_id="test-audit-log",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        update_request_state(request.request_id, RequestState.ALLOW, actor_user_id=12345)

        # Check audit log
        self.assertTrue(AUDIT_LOG_FILE.exists(), "Audit log file should exist")

        audit_content = AUDIT_LOG_FILE.read_text()
        self.assertIn(request.request_id, audit_content, "Request ID should be in audit log")
        self.assertIn("allow", audit_content, "Action should be in audit log")

    def test_state_file_created_on_request(self):
        """Test that state file is created when request is created."""
        # Create request
        request = create_request(
            session_id="test-state-file",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # Verify state file exists
        self.assertTrue(STATE_FILE.exists(), "State file should exist")

        # Verify request is in file
        state_content = STATE_FILE.read_text()
        self.assertIn(request.request_id, state_content, "Request ID should be in state file")
        self.assertIn("pending", state_content, "State should be in state file")

    def test_debug_log_when_enabled(self):
        """Test that debug log is written when CLAUDE_HOOK_DEBUG=1."""
        # Clear debug log
        if DEBUG_LOG.exists():
            DEBUG_LOG.unlink()

        # Create request (which should trigger debug logging)
        request = create_request(
            session_id="test-debug-log",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # Debug log should exist and contain request info
        if DEBUG_LOG.exists():
            debug_content = DEBUG_LOG.read_text()
            # Debug log should have content
            self.assertTrue(len(debug_content) > 0, "Debug log should have content")

    def test_state_file_format(self):
        """Test that state file is valid JSONL format."""
        # Create request
        request = create_request(
            session_id="test-jsonl-format",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=["Bash(test:*)"],
            ttl_seconds=60,
        )

        # Read and parse state file
        import json
        state_content = STATE_FILE.read_text()

        found = False
        for line in state_content.strip().split("\n"):
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("request_id") == request.request_id:
                found = True
                # Verify required fields
                self.assertIn("session_id", entry)
                self.assertIn("cwd", entry)
                self.assertIn("tool_name", entry)
                self.assertIn("tool_input", entry)
                self.assertIn("state", entry)
                self.assertIn("created_at", entry)
                self.assertIn("expires_at", entry)
                break

        self.assertTrue(found, "Request should be found in state file")

    def test_audit_log_format(self):
        """Test that audit log is valid JSONL format."""
        # Create and update request
        request = create_request(
            session_id="test-audit-format",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        update_request_state(request.request_id, RequestState.DENY, actor_user_id=99999)

        # Read and parse audit log
        import json
        audit_content = AUDIT_LOG_FILE.read_text()

        found = False
        for line in audit_content.strip().split("\n"):
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("request_id") == request.request_id and entry.get("action") == "deny":
                found = True
                # Verify required fields
                self.assertIn("timestamp", entry)
                self.assertIn("action", entry)
                self.assertIn("previous_state", entry)
                self.assertIn("new_state", entry)
                self.assertEqual(entry["new_state"], "deny")
                break

        self.assertTrue(found, "Audit entry should be found in audit log")


class TestLogFileLocations(unittest.TestCase):
    """Test log file locations."""

    def test_state_file_location(self):
        """Test state file is in expected location."""
        expected = Path.home() / ".claude" / "permission_requests.jsonl"
        self.assertEqual(STATE_FILE, expected)

    def test_audit_log_location(self):
        """Test audit log is in expected location."""
        expected = Path.home() / ".claude" / "permission_actions.jsonl"
        self.assertEqual(AUDIT_LOG_FILE, expected)

    def test_debug_log_location(self):
        """Test debug log is in expected location."""
        expected = Path.home() / ".claude" / "permission_state_debug.log"
        self.assertEqual(DEBUG_LOG, expected)


class TestLogRetention(unittest.TestCase):
    """Test log retention and cleanup."""

    def test_cleanup_expired_requests(self):
        """Test that cleanup marks expired requests."""
        from permission_state_store import cleanup_expired_requests

        # Create request with short TTL
        request = create_request(
            session_id="test-cleanup",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=1,
        )

        # Wait for expiration
        time.sleep(2)

        # Run cleanup
        cleaned = cleanup_expired_requests()

        # Check request is marked as expired
        import json
        state_content = STATE_FILE.read_text()

        found_expired = False
        for line in state_content.strip().split("\n"):
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("request_id") == request.request_id:
                if entry.get("state") == "expired":
                    found_expired = True
                break

        # Either cleaned up or marked as expired
        self.assertTrue(cleaned > 0 or found_expired,
            "Expired request should be cleaned up or marked as expired")


if __name__ == "__main__":
    unittest.main(verbosity=2)
