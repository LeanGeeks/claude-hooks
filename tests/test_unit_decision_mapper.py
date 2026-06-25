#!/usr/bin/env python3
"""
Unit Tests: Decision Mapper for Permission Flow

Tests the decision mapping logic:
- allow -> behavior: "allow"
- deny -> behavior: "deny"
- stop -> behavior: "deny" + interrupt: true
- yolo -> behavior: "allow" + enables session allow-all
- reply -> behavior: "deny" + message (text reply from user)
- timeout fallback -> None (falls back to terminal prompt)
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

from permission_request_hook import build_output_decision
from permission_state_store import (
    PermissionRequest,
    RequestState,
)


class TestDecisionMapper(unittest.TestCase):
    """Test the build_output_decision function."""

    def create_mock_request(self, request_id="test-123", suggestions=None):
        """Helper to create a mock PermissionRequest."""
        return PermissionRequest(
            request_id=request_id,
            session_id="test-session",
            cwd="/test/path",
            tool_name="Bash",
            tool_input={"command": "test command"},
            permission_suggestions=suggestions or [],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

    def test_allow_decision(self):
        """Test allow action returns correct output."""
        request = self.create_mock_request()
        decision = {"action": "allow"}

        output = build_output_decision(decision, request)

        self.assertIsNotNone(output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertNotIn("interrupt", output["hookSpecificOutput"]["decision"])
        self.assertNotIn("updatedPermissions", output["hookSpecificOutput"]["decision"])

    def test_deny_decision(self):
        """Test deny action returns correct output."""
        request = self.create_mock_request()
        decision = {"action": "deny"}

        output = build_output_decision(decision, request)

        self.assertIsNotNone(output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertNotIn("interrupt", output["hookSpecificOutput"]["decision"])

    def test_stop_decision(self):
        """Test stop action returns deny with interrupt."""
        request = self.create_mock_request()
        decision = {"action": "stop"}

        output = build_output_decision(decision, request)

        self.assertIsNotNone(output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertTrue(output["hookSpecificOutput"]["decision"]["interrupt"])

    def test_yolo_decision_allows_and_enables_session(self):
        """Test yolo action allows the current request and flags the session."""
        request = self.create_mock_request()
        decision = {"action": "yolo"}

        with patch("permission_request_hook.session_yolo_store.enable") as mock_enable:
            output = build_output_decision(decision, request)

        self.assertIsNotNone(output)
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertNotIn("interrupt", output["hookSpecificOutput"]["decision"])
        # The session flag must be set, keyed by the request's session_id.
        mock_enable.assert_called_once_with("test-session")

    def test_reply_decision(self):
        """Test reply action returns deny with reason."""
        request = self.create_mock_request()
        decision = {
            "action": "reply",
            "reply_text": "Please use a different approach"
        }

        output = build_output_decision(decision, request)

        self.assertIsNotNone(output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIn("reason", output["hookSpecificOutput"]["decision"])
        self.assertIn("Please use a different approach", output["hookSpecificOutput"]["decision"]["reason"])

    def test_reply_decision_short(self):
        """Test reply action with short text."""
        request = self.create_mock_request()
        decision = {"action": "reply", "reply_text": "No"}

        output = build_output_decision(decision, request)

        self.assertIsNotNone(output)
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIn("No", output["hookSpecificOutput"]["decision"]["reason"])

    def test_unknown_action_fallback(self):
        """Test unknown action returns None (fallback to terminal)."""
        request = self.create_mock_request()
        decision = {"action": "unknown_action"}

        output = build_output_decision(decision, request)

        # Unknown actions should return None (fallback to terminal)
        self.assertIsNone(output)

    def test_none_decision_fallback(self):
        """Test None decision returns None (fallback to terminal)."""
        request = self.create_mock_request()

        output = build_output_decision(None, request)

        self.assertIsNone(output)

    def test_empty_decision_fallback(self):
        """Test empty decision returns None (fallback to terminal)."""
        request = self.create_mock_request()
        decision = {}

        output = build_output_decision(decision, request)

        # Empty decision should treat as unknown action -> None
        self.assertIsNone(output)


class TestTimeoutFallback(unittest.TestCase):
    """Test timeout fallback behavior."""

    def test_timeout_returns_none(self):
        """Test that timeout (no decision) returns None for terminal fallback."""
        request = PermissionRequest(
            request_id="timeout-test",
            session_id="test-session",
            cwd="/test/path",
            tool_name="Bash",
            tool_input={"command": "timeout command"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

        # Simulate timeout by passing None decision
        output = build_output_decision(None, request)

        # Timeout should result in None -> falls back to terminal prompt
        self.assertIsNone(output)


class TestDecisionActionValidation(unittest.TestCase):
    """Test decision action validation."""

    def test_valid_actions(self):
        """Test all valid action types."""
        valid_actions = ["allow", "deny", "stop", "yolo", "reply"]
        request = PermissionRequest(
            request_id="validation-test",
            session_id="test-session",
            cwd="/test/path",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

        for action in valid_actions:
            decision = {"action": action}
            if action == "reply":
                decision["reply_text"] = "test"

            with patch("permission_request_hook.session_yolo_store.enable"):
                output = build_output_decision(decision, request)

            # All valid actions should produce output (not None)
            self.assertIsNotNone(output, f"Action '{action}' should produce output")


if __name__ == "__main__":
    unittest.main(verbosity=2)
