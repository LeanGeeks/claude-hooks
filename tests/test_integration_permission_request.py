#!/usr/bin/env python3
"""
Integration Tests: PermissionRequest Hook

Tests the PermissionRequest hook output mapping and verifies that the
Telegram helpers route through RelayClient (Phase 4 of task 08).
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

from permission_request_hook import (  # noqa: E402
    build_output_decision,
    get_wait_before_telegram,
    get_workspace_name,
    WAIT_BEFORE_TELEGRAM,
    _auto_deny_output,
    _record_auto_deny,
)
import permission_request_hook  # noqa: E402
from permission_state_store import (  # noqa: E402
    PermissionRequest,
    RequestState,
    create_request,
    get_request,
)


def _make_request(**overrides):
    base = dict(
        request_id="test-id",
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
    base.update(overrides)
    return PermissionRequest(**base)


class TestBuildOutputDecision(unittest.TestCase):
    """Decision -> hook output mapping (transport-agnostic)."""

    def test_allow(self):
        out = build_output_decision({"action": "allow"}, _make_request())
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_deny(self):
        out = build_output_decision({"action": "deny"}, _make_request())
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertNotIn("interrupt", out["hookSpecificOutput"]["decision"])

    def test_stop(self):
        out = build_output_decision({"action": "stop"}, _make_request())
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertTrue(out["hookSpecificOutput"]["decision"]["interrupt"])

    def test_whitelist(self):
        req = _make_request(
            tool_input={"command": "custom_cmd"},
            permission_suggestions=["Bash(custom_cmd:*)"],
        )
        decision = {
            "action": "whitelist",
            "updatedPermissions": ["Bash(custom_cmd:*)"],
        }
        with patch("permission_request_hook.process_whitelist_update", return_value=True):
            out = build_output_decision(decision, req)
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertIn("updatedPermissions", out["hookSpecificOutput"]["decision"])

    def test_reply(self):
        out = build_output_decision(
            {"action": "reply", "reply_text": "Use a different approach"},
            _make_request(),
        )
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIn(
            "Use a different approach",
            out["hookSpecificOutput"]["decision"]["reason"],
        )

    def test_no_decision_returns_none(self):
        self.assertIsNone(build_output_decision(None, _make_request()))


class TestPermissionMessageAnnotation(unittest.TestCase):
    """The Telegram permission message must surface which sub-commands tripped
    the prompt. PreToolUse's reason is not forwarded to the PermissionRequest
    hook, so the router re-derives it via ``_unallowlisted_bash_parts``."""

    def setUp(self):
        from telegram_permission_router import MessageHandle  # noqa: WPS433
        self.MessageHandle = MessageHandle

    def _capture_text(self, tpr):
        fake_client = MagicMock()
        fake_client.send_message.return_value = self.MessageHandle(
            message_id=42, telegram_message_id=99
        )
        with patch.object(tpr, "TELEGRAM_ENABLED", True), \
             patch.object(tpr, "_relay_client", fake_client), \
             patch("telegram_permission_router.set_telegram_message_id"):
            tpr.send_permission_message(_make_request(), "workspace", "session")
        return fake_client.send_message.call_args.kwargs["text"]

    def test_message_includes_denied_and_unknown_parts(self):
        import telegram_permission_router as tpr
        with patch.object(
            tpr, "_unallowlisted_bash_parts",
            return_value=(["dd if=/dev/zero"], ["mysteryfoo --bar"]),
        ):
            text = self._capture_text(tpr)
        self.assertIn("Matches a denied pattern", text)
        self.assertIn("dd if=/dev/zero", text)
        self.assertIn("Not in allowlist", text)
        self.assertIn("mysteryfoo --bar", text)
        self.assertIn("Approve this command?", text)

    def test_message_has_no_annotation_when_all_allowed(self):
        import telegram_permission_router as tpr
        with patch.object(tpr, "_unallowlisted_bash_parts", return_value=([], [])):
            text = self._capture_text(tpr)
        self.assertNotIn("Not in allowlist", text)
        self.assertNotIn("denied pattern", text)
        self.assertIn("Approve this command?", text)

    def test_command_fragments_are_html_escaped(self):
        import telegram_permission_router as tpr
        # A fragment with shell metacharacters must not break Telegram HTML.
        with patch.object(
            tpr, "_unallowlisted_bash_parts",
            return_value=([], ['weird <tag> & "q"']),
        ):
            text = self._capture_text(tpr)
        self.assertIn("&lt;tag&gt; &amp;", text)
        self.assertNotIn("<tag>", text)

    def test_unallowlisted_parts_categorizes_against_real_allowlist(self):
        """End-to-end: the helper runs the real validator against the repo's
        own allowlist (deny includes Bash(dd:*); mysteryfoo matches nothing)."""
        import telegram_permission_router as tpr
        repo_root = str(Path(__file__).parent.parent)
        req = _make_request(
            cwd=repo_root,
            tool_input={"command": "echo hi; mysteryfoo --bar; dd if=/dev/zero"},
        )
        denied, unknown = tpr._unallowlisted_bash_parts(req)
        # `if=/dev/zero` is parsed as an env-assignment token and dropped by
        # command normalization, so the denied entry reduces to bare `dd`.
        self.assertIn("dd", denied)
        self.assertIn("mysteryfoo --bar", unknown)
        # `echo hi` is allowlisted, so it appears in neither bucket.
        self.assertNotIn("echo hi", denied + unknown)

    def test_command_summary_and_names_are_html_escaped(self):
        """The <pre> command summary and the workspace/session names must be
        HTML-escaped — commands routinely contain <, >, & (e.g. `2>&1`), which
        would otherwise corrupt Telegram's HTML parse."""
        import telegram_permission_router as tpr
        fake_client = MagicMock()
        fake_client.send_message.return_value = self.MessageHandle(
            message_id=42, telegram_message_id=99
        )
        req = _make_request(tool_input={"command": "echo hi 2>&1 | grep '<x>'"})
        with patch.object(tpr, "TELEGRAM_ENABLED", True), \
             patch.object(tpr, "_relay_client", fake_client), \
             patch.object(tpr, "_unallowlisted_bash_parts", return_value=([], [])), \
             patch("telegram_permission_router.set_telegram_message_id"):
            tpr.send_permission_message(req, "ws<&>name", "sess&ion")
        text = fake_client.send_message.call_args.kwargs["text"]
        self.assertIn("2&gt;&amp;1", text)
        self.assertIn("&lt;x&gt;", text)
        self.assertIn("ws&lt;&amp;&gt;name", text)
        self.assertIn("sess&amp;ion", text)
        # No raw metacharacters survive in the command body / names.
        self.assertNotIn("2>&1", text)
        self.assertNotIn("<x>", text)

    def test_unallowlisted_parts_empty_for_non_bash(self):
        import telegram_permission_router as tpr
        req = _make_request(tool_name="Read", tool_input={"file_path": "/x"})
        self.assertEqual(tpr._unallowlisted_bash_parts(req), ([], []))


class TestRouting(unittest.TestCase):
    """Smoke tests confirming hook helpers route through RelayClient.

    The relay client is monkeypatched on ``telegram_permission_router`` to
    avoid any HTTP, and we assert the right methods are called with the
    right arguments. Deeper coverage of the wire protocol lives in
    ``relay-server/tests/test_relay_client.py``.
    """

    def setUp(self):
        from telegram_permission_router import MessageHandle  # noqa: WPS433
        self.MessageHandle = MessageHandle

    def test_send_permission_message_calls_relay(self):
        import telegram_permission_router as tpr

        fake_client = MagicMock()
        fake_client.send_message.return_value = self.MessageHandle(
            message_id=42, telegram_message_id=99
        )
        with patch.object(tpr, "TELEGRAM_ENABLED", True), \
             patch.object(tpr, "_relay_client", fake_client), \
             patch("telegram_permission_router.set_telegram_message_id"):
            mid = tpr.send_permission_message(_make_request(), "workspace", "session")

        self.assertEqual(mid, 42)
        fake_client.send_message.assert_called_once()
        kwargs = fake_client.send_message.call_args.kwargs
        self.assertEqual(kwargs["kind"], "permission")
        # Keyboard contains allow/deny/stop/whitelist by ``value``.
        values = [btn["value"] for row in kwargs["keyboard"] for btn in row]
        self.assertEqual(set(values), {"allow", "deny", "stop", "whitelist"})

    def test_remove_inline_buttons_cancels_relay_message(self):
        import telegram_permission_router as tpr

        fake_client = MagicMock()
        with patch.object(tpr, "TELEGRAM_ENABLED", True), \
             patch.object(tpr, "_relay_client", fake_client):
            ok = tpr.remove_inline_buttons(123)
        self.assertTrue(ok)
        fake_client.cancel_message.assert_called_once_with(123)

    def test_relay_answer_to_decision_button_allow(self):
        import telegram_permission_router as tpr

        decision = tpr.relay_answer_to_decision(
            _make_request(),
            {"via": "button", "value": "allow", "label": "Allow", "option_idx": 0},
        )
        self.assertEqual(decision, {"action": "allow"})

    def test_relay_answer_to_decision_freetext(self):
        import telegram_permission_router as tpr

        decision = tpr.relay_answer_to_decision(
            _make_request(), {"via": "reply", "text": "please clarify"}
        )
        self.assertEqual(decision, {"action": "reply", "reply_text": "please clarify"})

    def test_relay_answer_to_decision_question_button(self):
        import telegram_permission_router as tpr

        req = _make_request(
            tool_name="AskUserQuestion",
            tool_input={
                "question": "pick one",
                "options": [{"label": "A"}, {"label": "B"}],
            },
        )
        decision = tpr.relay_answer_to_decision(
            req, {"via": "button", "value": "qa1", "label": "B", "option_idx": 1}
        )
        self.assertEqual(decision, {"action": "reply", "reply_text": "B"})

    def test_relay_answer_to_decision_multi_select(self):
        """A multi-select answer maps the chosen option indices back to their
        clean labels, joined with ', ' (the format AskUserQuestion expects)."""
        import telegram_permission_router as tpr

        req = _make_request(
            tool_name="AskUserQuestion",
            tool_input={
                "question": "pick any",
                "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
                "multiSelect": True,
            },
        )
        decision = tpr.relay_answer_to_decision(
            req,
            {
                "via": "button_multi",
                "option_idxs": [0, 2],
                "labels": ["1. A", "3. C"],
            },
        )
        self.assertEqual(decision, {"action": "reply", "reply_text": "A, C"})

    def test_send_question_message_multi_select_adds_submit_button(self):
        """A multiSelect question renders a Submit button and flags the relay
        message as multi_select so taps toggle rather than finalize."""
        import telegram_permission_router as tpr

        req = _make_request(
            tool_name="AskUserQuestion",
            tool_input={
                "question": "pick any",
                "options": [{"label": "A"}, {"label": "B"}],
                "multiSelect": True,
            },
        )
        captured = {}

        def _fake_send_relay(**kwargs):
            captured.update(kwargs)
            return 777

        with patch.object(tpr, "_send_relay", side_effect=_fake_send_relay), patch.object(
            tpr, "set_telegram_message_id"
        ):
            msg_id = tpr.send_question_message(req, "ws", 0, 1, group_id="g")

        self.assertEqual(msg_id, 777)
        self.assertTrue(captured["multi_select"])
        keyboard = captured["keyboard"]
        # Last keyboard row is the Submit button with the sentinel value.
        self.assertEqual(keyboard[-1][0]["value"], tpr.QUESTION_SUBMIT_VALUE)
        # The "first answer wins" caveat is gone.
        self.assertNotIn("first answer wins", captured["text"])


class TestHookMainPath(unittest.TestCase):
    """Smoke test that ``permission_request_hook.main`` no longer talks to a daemon."""

    @patch("permission_request_hook.cleanup_expired_requests")
    @patch("permission_request_hook.send_permission_message", return_value=12345)
    @patch("permission_request_hook.wait_for_response", return_value={"action": "allow"})
    @patch("permission_request_hook.create_request")
    @patch("permission_request_hook.time.sleep")
    def test_main_routes_through_relay(
        self,
        _sleep,
        mock_create_request,
        _wait,
        _send,
        _cleanup,
    ):
        mock_create_request.return_value = _make_request(
            request_id="runtime",
            tool_input={"command": "unknown_cmd"},
            permission_suggestions=[],
        )

        payload = {
            "session_id": "test-session",
            "cwd": "/tmp/workspace",
            "tool_name": "Bash",
            "tool_input": {"command": "unknown_cmd"},
            "permission_suggestions": [],
        }

        def _enable():
            permission_request_hook.telegram_router.TELEGRAM_ENABLED = True

        with patch("permission_request_hook.load_telegram_config", side_effect=_enable):
            with patch("sys.stdin", io.StringIO(json.dumps(payload))):
                with patch("builtins.print"):
                    with self.assertRaises(SystemExit) as ctx:
                        permission_request_hook.main()

        self.assertEqual(ctx.exception.code, 0)
        mock_create_request.assert_called_once()


class TestWorkspaceNameExtraction(unittest.TestCase):
    def test_get_workspace_name(self):
        self.assertEqual(get_workspace_name("/home/user/project"), "project")
        self.assertEqual(get_workspace_name("/tmp"), "tmp")
        self.assertEqual(get_workspace_name("/"), "")

    def test_wait_before_telegram_is_zero(self):
        self.assertEqual(WAIT_BEFORE_TELEGRAM, 0)
        self.assertEqual(get_wait_before_telegram("Bash"), 0)


class TestTerminalResolutionRace(unittest.TestCase):
    """Tests for the local-terminal-wins race in wait_for_response and
    handle_ask_user_question."""

    def setUp(self):
        # Import here so the sys.path insert above is already in effect.
        import permission_request_hook as hook
        import telegram_permission_router as tpr
        self.hook = hook
        self.tpr = tpr

    @patch("permission_request_hook.wait_for_relay_answer")
    @patch("permission_request_hook.get_request")
    @patch("permission_request_hook.remove_inline_buttons")
    def test_wait_for_response_terminal_win_cancels_relay_message(
        self,
        mock_remove_buttons,
        mock_get_request,
        mock_wait_relay,
    ):
        """wait_for_response: when local state flips to RESOLVED_TERMINAL after
        a 204 relay poll, cancel_message is called for the relay row and the
        function returns None (terminal wins)."""
        from permission_request_hook import wait_for_response
        from permission_state_store import RequestState

        # First poll returns 204 (no answer yet); second poll we've already
        # exited via the terminal check, so this counter should only be 1.
        mock_wait_relay.return_value = None  # 204 → keep polling

        call_count = {"n": 0}

        def _get_request_side_effect(request_id):
            call_count["n"] += 1
            req = _make_request(request_id=request_id)
            # On the second state-store check, report RESOLVED_TERMINAL.
            if call_count["n"] >= 2:
                return _make_request(
                    request_id=request_id,
                    state=RequestState.RESOLVED_TERMINAL.value,
                )
            return req

        mock_get_request.side_effect = _get_request_side_effect

        result = wait_for_response("test-id", message_id=777, ttl_seconds=10)

        self.assertIsNone(result)
        mock_remove_buttons.assert_called_once_with(777)

    @patch("permission_request_hook.update_request_state")
    @patch("permission_request_hook.wait_for_relay_answer")
    @patch("permission_request_hook.get_request")
    @patch("permission_request_hook.remove_inline_buttons")
    def test_wait_for_response_relay_answer_strips_keyboard(
        self,
        mock_remove_buttons,
        mock_get_request,
        mock_wait_relay,
        mock_update_state,
    ):
        """wait_for_response: a Telegram button answer must strip the keyboard
        immediately (not wait for PostToolUse) and mark the request terminal so
        the PostToolUse sweep won't re-cancel it."""
        from permission_request_hook import wait_for_response
        from permission_state_store import RequestState

        # State store stays pending (the relay, not the terminal, resolves this).
        mock_get_request.return_value = _make_request(state="pending")
        # The relay long-poll returns a real button answer.
        mock_wait_relay.return_value = {"via": "button", "value": "allow"}

        result = wait_for_response("test-id", message_id=888, ttl_seconds=10)

        self.assertEqual(result, {"action": "allow"})
        mock_remove_buttons.assert_called_once_with(888)
        # Marked terminal as a Telegram resolution so PostToolUse leaves it alone.
        args, kwargs = mock_update_state.call_args
        self.assertEqual(args[0], "test-id")
        self.assertEqual(args[1], RequestState.ALLOW)

    @patch("permission_request_hook.wait_for_relay_answer")
    @patch("permission_request_hook.create_request")
    @patch("permission_request_hook.get_request")
    @patch("permission_request_hook.remove_inline_buttons")
    @patch("permission_request_hook.set_message_reaction")
    def test_handle_ask_user_question_terminal_cancels_current_child_message(
        self,
        mock_set_reaction,
        mock_remove_buttons,
        mock_get_request,
        mock_create_request,
        mock_wait_relay,
    ):
        """handle_ask_user_question: when the current child resolves via
        terminal, remove_inline_buttons is called for *that child's* message_id
        before returning None."""
        import telegram_permission_router as tpr
        from permission_request_hook import handle_ask_user_question
        from permission_state_store import RequestState

        # Fake child requests created for each question.
        child_req = _make_request(request_id="child-1")
        mock_create_request.return_value = child_req

        # send_question_message returns a relay message_id of 555.
        with patch("permission_request_hook.send_question_message", return_value=555):
            # The relay long-polls return None (204 — still waiting).
            mock_wait_relay.return_value = None

            call_count = {"n": 0}

            def _get_request_side_effect(request_id):
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    # Second check: terminal resolved this child.
                    return _make_request(
                        request_id=request_id,
                        state=RequestState.RESOLVED_TERMINAL.value,
                    )
                return _make_request(request_id=request_id)

            mock_get_request.side_effect = _get_request_side_effect

            result = handle_ask_user_question(
                session_id="sess",
                cwd="/tmp",
                tool_input={"questions": [{"question": "Are you sure?", "options": []}]},
                workspace_name="workspace",
            )

        self.assertIsNone(result)
        # The current child's relay message (555) must have been cancelled.
        mock_remove_buttons.assert_any_call(555)

    @patch("permission_request_hook.wait_for_relay_answer")
    @patch("permission_request_hook.create_request")
    @patch("permission_request_hook.get_request")
    @patch("permission_request_hook.remove_inline_buttons")
    @patch("permission_request_hook.set_message_reaction")
    def test_handle_ask_user_question_terminal_revokes_all_group_messages(
        self,
        mock_set_reaction,
        mock_remove_buttons,
        mock_get_request,
        mock_create_request,
        mock_wait_relay,
    ):
        """Two questions, answered in the terminal. The PostToolUse hook only
        flips the *most recent* child (the last one) to resolved_terminal, while
        the loop is parked on the first child. Every sibling's keyboard must
        still be revoked — not just the last one."""
        import telegram_permission_router as tpr
        from permission_request_hook import handle_ask_user_question
        from permission_state_store import RequestState

        # Distinct child per question, distinct relay message id per child.
        mock_create_request.side_effect = [
            _make_request(request_id="child-1"),
            _make_request(request_id="child-2"),
        ]
        msg_ids = {"child-1": 501, "child-2": 502}

        # Only the *last* child is flipped to resolved_terminal (mirrors
        # find_pending_request_by_tool_session returning the most recent row).
        def _get_request_side_effect(request_id):
            if request_id == "child-2":
                return _make_request(
                    request_id=request_id,
                    state=RequestState.RESOLVED_TERMINAL.value,
                )
            return _make_request(request_id=request_id)  # child-1 still pending

        mock_get_request.side_effect = _get_request_side_effect
        mock_wait_relay.return_value = None  # relay never answers

        def _send(child, *_a, **_k):
            return msg_ids[child.request_id]

        with patch("permission_request_hook.send_question_message", side_effect=_send):
            result = handle_ask_user_question(
                session_id="sess",
                cwd="/tmp",
                tool_input={
                    "questions": [
                        {"question": "Q1?", "options": []},
                        {"question": "Q2?", "options": []},
                    ]
                },
                workspace_name="workspace",
            )

        self.assertIsNone(result)
        # Both the first child's message (501) and the last (502) get revoked.
        mock_remove_buttons.assert_any_call(501)
        mock_remove_buttons.assert_any_call(502)


class TestAutoDenyAtTtl(unittest.TestCase):
    """Auto-deny-with-note when a permission request goes unanswered for the TTL.

    Permission requests fail safe at the TTL (deny, never allow), carrying a note
    the agent can act on. The note distinguishes a delivery failure from a
    delivered-but-ignored request. AskUserQuestion never auto-denies (tested by
    its absence — handle_ask_user_question returns None to keep the native UI)."""

    def test_delivery_failed_note_says_retry(self):
        out = _auto_deny_output(_make_request(), delivery_failed=True)
        dec = out["hookSpecificOutput"]["decision"]
        self.assertEqual(dec["behavior"], "deny")
        self.assertNotIn("interrupt", dec)  # agent continues so it can retry
        reason = dec["reason"].lower()
        self.assertIn("could not be delivered", reason)
        self.assertIn("retried", reason)

    def test_delivered_but_unanswered_note(self):
        out = _auto_deny_output(_make_request(), delivery_failed=False)
        reason = out["hookSpecificOutput"]["decision"]["reason"].lower()
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIn("no response", reason)
        self.assertNotIn("could not be delivered", reason)

    def test_note_includes_non_whitelisted_parts(self):
        with patch.object(
            permission_request_hook,
            "_format_non_whitelisted",
            return_value="not in allowlist: cowsay",
        ):
            out = _auto_deny_output(_make_request(), delivery_failed=True)
        self.assertIn("cowsay", out["hookSpecificOutput"]["decision"]["reason"])

    def test_record_auto_deny_transitions_pending_to_deny(self):
        req = create_request(
            session_id="auto-deny-test",
            cwd="/test",
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/x"},
            permission_suggestions=[],
            ttl_seconds=300,
        )
        self.assertEqual(req.state, RequestState.PENDING.value)
        _record_auto_deny(req.request_id)
        self.assertEqual(get_request(req.request_id).state, RequestState.DENY.value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
