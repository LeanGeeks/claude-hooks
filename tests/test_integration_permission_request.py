#!/usr/bin/env python3
"""
Integration Tests: PermissionRequest Hook with Mocked Telegram

Tests the PermissionRequest hook with:
- Simulated stdin payloads
- Mocked Telegram API responses
- Simulated callback delivery
- Timeout behavior
"""

import json
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

# Import test fixtures
from fixtures import (
    PERMISSION_REQUEST_PAYLOADS,
    TELEGRAM_CALLBACKS,
    TELEGRAM_TEXT_REPLIES,
    EXPECTED_HOOK_OUTPUTS,
)

from permission_request_hook import (
    build_output_decision,
    get_workspace_name,
    get_wait_before_telegram,
    WAIT_BEFORE_TELEGRAM,
    MAX_WAIT_FOR_RESPONSE,
)
import permission_request_hook
from permission_state_store import (
    PermissionRequest,
    RequestState,
    create_request,
    get_request,
    update_request_state,
    cleanup_expired_requests,
)
from telegram_permission_router import (
    parse_callback_data,
    handle_callback_query,
    handle_text_reply,
    generate_whitelist_pattern,
    wait_for_response,
    load_telegram_config,
    send_permission_message,
)


class TestPermissionRequestHook(unittest.TestCase):
    """Integration tests for PermissionRequest hook."""

    def test_build_output_allow(self):
        """Test building output for allow decision."""
        request = PermissionRequest(
            request_id="test-allow",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=["Bash(ls:*)"],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        decision = {"action": "allow"}

        output = build_output_decision(decision, request)

        expected = EXPECTED_HOOK_OUTPUTS["allow"]
        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_build_output_deny(self):
        """Test building output for deny decision."""
        request = PermissionRequest(
            request_id="test-deny",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "rm"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        decision = {"action": "deny"}

        output = build_output_decision(decision, request)

        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertNotIn("interrupt", output["hookSpecificOutput"]["decision"])

    def test_build_output_stop(self):
        """Test building output for stop decision."""
        request = PermissionRequest(
            request_id="test-stop",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        decision = {"action": "stop"}

        output = build_output_decision(decision, request)

        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertTrue(output["hookSpecificOutput"]["decision"]["interrupt"])

    def test_build_output_whitelist(self):
        """Test building output for whitelist decision."""
        request = PermissionRequest(
            request_id="test-whitelist",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "custom_cmd"},
            permission_suggestions=["Bash(custom_cmd:*)"],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        decision = {
            "action": "whitelist",
            "updatedPermissions": {"add": ["Bash(custom_cmd:*)"]}
        }

        with patch("permission_request_hook.process_whitelist_update", return_value=True):
            output = build_output_decision(decision, request)

        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertIn("updatedPermissions", output["hookSpecificOutput"]["decision"])

    def test_build_output_reply(self):
        """Test building output for reply decision."""
        request = PermissionRequest(
            request_id="test-reply",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        decision = {
            "action": "reply",
            "reply_text": "Use a different approach"
        }

        output = build_output_decision(decision, request)

        self.assertEqual(output["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIn("Use a different approach", output["hookSpecificOutput"]["decision"]["reason"])

    def test_timeout_fallback(self):
        """Test that timeout returns None (falls back to terminal)."""
        request = PermissionRequest(
            request_id="test-timeout",
            session_id="test-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "cmd"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

        # No decision = timeout
        output = build_output_decision(None, request)
        self.assertIsNone(output)

    def test_wait_before_telegram_by_tool(self):
        """All tools send immediately to maximize response window."""
        self.assertEqual(WAIT_BEFORE_TELEGRAM, 0)
        self.assertEqual(get_wait_before_telegram("Bash"), 0)
        self.assertEqual(get_wait_before_telegram("Read"), 0)
        self.assertEqual(get_wait_before_telegram("mcp__plugin_figma_figma__get_design_context"), 0)

    @patch("permission_request_hook.cleanup_expired_requests")
    @patch("permission_request_hook.send_permission_message", return_value=12345)
    @patch("permission_request_hook.wait_for_response", return_value={"action": "allow"})
    @patch("permission_request_hook.create_request")
    @patch("permission_request_hook.time.sleep")
    def test_main_uses_runtime_telegram_enabled_flag(
        self,
        _mock_sleep,
        mock_create_request,
        _mock_wait_for_response,
        _mock_send_permission_message,
        _mock_cleanup,
    ):
        """Ensure main checks TELEGRAM_ENABLED from router module at runtime."""
        request = PermissionRequest(
            request_id="runtime-flag",
            session_id="test-session",
            cwd="/tmp/workspace",
            tool_name="Bash",
            tool_input={"command": "unknown_cmd"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        mock_create_request.return_value = request

        payload = {
            "session_id": "test-session",
            "cwd": "/tmp/workspace",
            "tool_name": "Bash",
            "tool_input": {"command": "unknown_cmd"},
            "permission_suggestions": [],
        }

        def _enable_telegram():
            permission_request_hook.telegram_router.TELEGRAM_ENABLED = True

        with patch("permission_request_hook.load_telegram_config", side_effect=_enable_telegram):
            with patch("sys.stdin", io.StringIO(json.dumps(payload))):
                with patch("builtins.print"):
                    with self.assertRaises(SystemExit) as ctx:
                        permission_request_hook.main()

        self.assertEqual(ctx.exception.code, 0)
        mock_create_request.assert_called_once()

    @patch("permission_request_hook.cleanup_expired_requests")
    @patch("permission_request_hook.send_permission_message", return_value=54321)
    @patch("permission_request_hook.wait_for_response", return_value={"action": "deny"})
    @patch("permission_request_hook.create_request")
    @patch("permission_request_hook.time.sleep")
    def test_main_processes_non_bash_permission_request(
        self,
        _mock_sleep,
        mock_create_request,
        _mock_wait_for_response,
        _mock_send_permission_message,
        _mock_cleanup,
    ):
        """Ensure non-Bash PermissionRequest events are also handled."""
        request = PermissionRequest(
            request_id="runtime-flag-read",
            session_id="test-session-read",
            cwd="/tmp/workspace",
            tool_name="Read",
            tool_input={"file_path": "/tmp/file.txt"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )
        mock_create_request.return_value = request

        payload = {
            "session_id": "test-session-read",
            "cwd": "/tmp/workspace",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/file.txt"},
            "permission_suggestions": [],
        }

        def _enable_telegram():
            permission_request_hook.telegram_router.TELEGRAM_ENABLED = True

        with patch("permission_request_hook.load_telegram_config", side_effect=_enable_telegram):
            with patch("sys.stdin", io.StringIO(json.dumps(payload))):
                with patch("builtins.print"):
                    with self.assertRaises(SystemExit) as ctx:
                        permission_request_hook.main()

        self.assertEqual(ctx.exception.code, 0)
        mock_create_request.assert_called_once()


class TestCallbackHandling(unittest.TestCase):
    """Test Telegram callback handling."""

    def setUp(self):
        """Set up test fixtures."""
        # Mock Telegram config
        self.mock_chat_id = 123456789

    def test_parse_callback_data(self):
        """Test parsing callback data."""
        test_cases = [
            ("allow:abc123", ("allow", "abc123")),
            ("deny:def456", ("deny", "def456")),
            ("stop:ghi789", ("stop", "ghi789")),
            ("whitelist:jkl012", ("whitelist", "jkl012")),
            ("allow", ("allow", None)),
        ]

        for data, expected in test_cases:
            with self.subTest(data=data):
                result = parse_callback_data(data)
                self.assertEqual(result, expected)

    @patch("telegram_permission_router.TELEGRAM_CHAT_ID", "123456789")
    @patch("telegram_permission_router.TELEGRAM_ENABLED", True)
    @patch("telegram_permission_router.answer_callback_query")
    @patch("telegram_permission_router.edit_message_text")
    @patch("telegram_permission_router.get_request")
    def test_handle_allow_callback(self, mock_get_request, mock_edit, mock_answer):
        """Test handling allow callback."""
        # Create mock request
        mock_request = MagicMock()
        mock_request.request_id = "test-allow-cb"
        mock_request.state = "pending"
        mock_request.expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        mock_get_request.return_value = mock_request

        # Mock update_request_state
        with patch("telegram_permission_router.update_request_state") as mock_update:
            mock_updated = MagicMock()
            mock_updated.request_id = "test-allow-cb"
            mock_updated.state = "allow"
            mock_update.return_value = mock_updated

            callback = TELEGRAM_CALLBACKS["allow"].copy()
            callback["from"]["id"] = 123456789  # Match mock_chat_id

            decision = handle_callback_query(callback)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "allow")

    @patch("telegram_permission_router.TELEGRAM_CHAT_ID", "123456789")
    def test_unauthorized_user_rejected(self):
        """Test that unauthorized users are rejected."""
        callback = TELEGRAM_CALLBACKS["unauthorized_user"].copy()

        with patch("telegram_permission_router.answer_callback_query") as mock_answer:
            decision = handle_callback_query(callback)

        self.assertIsNone(decision)
        mock_answer.assert_called()

    @patch("telegram_permission_router.TELEGRAM_CHAT_ID", "123456789")
    def test_invalid_action_rejected(self):
        """Test that invalid actions are rejected."""
        callback = TELEGRAM_CALLBACKS["invalid_action"].copy()
        callback["from"]["id"] = 123456789

        with patch("telegram_permission_router.answer_callback_query") as mock_answer:
            with patch("telegram_permission_router.get_request") as mock_get:
                mock_request = MagicMock()
                mock_request.state = "pending"
                mock_request.expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                mock_get.return_value = mock_request

                decision = handle_callback_query(callback)

        self.assertIsNone(decision)


class TestTextReplyHandling(unittest.TestCase):
    """Test Telegram text reply handling."""

    @patch("telegram_permission_router.TELEGRAM_CHAT_ID", "123456789")
    @patch("telegram_permission_router.TELEGRAM_ENABLED", True)
    @patch("telegram_permission_router.edit_message_text")
    @patch("telegram_permission_router.find_request_by_message_id")
    def test_handle_text_reply(self, mock_find, mock_edit):
        """Test handling text reply."""
        mock_request = MagicMock()
        mock_request.request_id = "test-reply-msg"
        mock_find.return_value = mock_request

        with patch("telegram_permission_router.update_request_state") as mock_update:
            mock_updated = MagicMock()
            mock_updated.request_id = "test-reply-msg"
            mock_update.return_value = mock_updated

            reply = TELEGRAM_TEXT_REPLIES["reply_to_permission"].copy()
            reply["from"]["id"] = 123456789

            decision = handle_text_reply(reply)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "reply")
        self.assertEqual(decision["reply_text"], "Please use a different approach")

    @patch("telegram_permission_router.TELEGRAM_CHAT_ID", "123456789")
    def test_unauthorized_reply_rejected(self):
        """Test that unauthorized replies are rejected."""
        reply = TELEGRAM_TEXT_REPLIES["reply_unauthorized"].copy()

        decision = handle_text_reply(reply)
        self.assertIsNone(decision)


class TestWaitForResponse(unittest.TestCase):
    """Test wait_for_response behavior."""

    @patch("telegram_permission_router.TELEGRAM_ENABLED", True)
    @patch("telegram_permission_router.TELEGRAM_CHAT_ID", "123456789")
    @patch("telegram_permission_router.get_updates")
    def test_wait_for_response_timeout(self, mock_get_updates):
        """Test that wait_for_response times out correctly."""
        # Mock no updates (timeout)
        mock_get_updates.return_value = []

        # Create a real request in state store
        request = create_request(
            session_id="test-timeout-session",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "test"},
            permission_suggestions=[],
            ttl_seconds=60,
        )

        # Use very short timeout for testing
        start_time = time.time()
        decision = wait_for_response(
            request_id=request.request_id,
            message_id=12345,
            timeout_seconds=1,  # 1 second timeout
        )
        elapsed = time.time() - start_time

        self.assertIsNone(decision)  # No decision on timeout
        self.assertLess(elapsed, 3)  # Should complete within reasonable time


class TestWorkspaceNameExtraction(unittest.TestCase):
    """Test workspace name extraction."""

    def test_get_workspace_name(self):
        """Test workspace name extraction from cwd."""
        test_cases = [
            ("/home/user/project", "project"),
            ("/home/user/my-project", "my-project"),
            ("/tmp", "tmp"),
            ("/", ""),
        ]

        for cwd, expected in test_cases:
            with self.subTest(cwd=cwd):
                result = get_workspace_name(cwd)
                self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
