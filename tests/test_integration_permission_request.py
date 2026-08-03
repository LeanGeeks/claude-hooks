#!/usr/bin/env python3
"""
Integration Tests: PermissionRequest Hook

Tests the PermissionRequest hook output mapping and verifies that the
Telegram helpers route through RelayClient (Phase 4 of task 08).
"""

import inspect
import io
import json
import os
import sys
import tempfile
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

    def test_yolo(self):
        req = _make_request()
        with patch("permission_request_hook.session_yolo_store.enable") as mock_enable:
            out = build_output_decision({"action": "yolo"}, req)
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "allow")
        mock_enable.assert_called_once_with(req.session_id)

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
             patch.object(tpr, "_default_token", "__td__"), \
             patch.object(tpr, "_clients", {"__td__": fake_client}), \
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
             patch.object(tpr, "_default_token", "__td__"), \
             patch.object(tpr, "_clients", {"__td__": fake_client}), \
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
             patch.object(tpr, "_default_token", "__td__"), \
             patch.object(tpr, "_clients", {"__td__": fake_client}), \
             patch("telegram_permission_router.set_telegram_message_id"):
            mid = tpr.send_permission_message(_make_request(), "workspace", "session")

        self.assertEqual(mid, 42)
        fake_client.send_message.assert_called_once()
        kwargs = fake_client.send_message.call_args.kwargs
        self.assertEqual(kwargs["kind"], "permission")
        # Keyboard contains allow/deny/stop/yolo by ``value``.
        values = [btn["value"] for row in kwargs["keyboard"] for btn in row]
        self.assertEqual(set(values), {"allow", "deny", "stop", "yolo"})

    def test_remove_inline_buttons_cancels_relay_message(self):
        import telegram_permission_router as tpr

        fake_client = MagicMock()
        with patch.object(tpr, "TELEGRAM_ENABLED", True), \
             patch.object(tpr, "_default_token", "__td__"), \
             patch.object(tpr, "_clients", {"__td__": fake_client}):
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

    def test_relay_answer_to_decision_button_yolo(self):
        import telegram_permission_router as tpr

        decision = tpr.relay_answer_to_decision(
            _make_request(),
            {"via": "button", "value": "yolo", "label": "YOLO", "option_idx": 3},
        )
        self.assertEqual(decision, {"action": "yolo"})

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
        # Option buttons start with an empty checkbox (untapped state).
        self.assertTrue(keyboard[0][0]["label"].startswith(tpr.QUESTION_UNCHECKED_PREFIX))
        self.assertTrue(keyboard[1][0]["label"].startswith(tpr.QUESTION_UNCHECKED_PREFIX))
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

    @patch("roles_config.load_bindings")
    @patch("roles_config.load_catalog", return_value=None)
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
        _mock_catalog,       # load_catalog → None (legacy mode)
        _mock_bindings,      # load_bindings → MagicMock (unused in legacy mode)
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
        # Legacy mode → token=None.
        mock_remove_buttons.assert_any_call(555, token=None)

    @patch("roles_config.load_bindings")
    @patch("roles_config.load_catalog", return_value=None)
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
        _mock_catalog,       # load_catalog → None (legacy mode)
        _mock_bindings,      # load_bindings → MagicMock (unused in legacy mode)
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
        # Legacy mode → token=None.
        mock_remove_buttons.assert_any_call(501, token=None)
        mock_remove_buttons.assert_any_call(502, token=None)


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


class TestCrossAgentIsolation(unittest.TestCase):
    """Verify that PostToolUse from one agent doesn't cancel another agent's
    pending permission request (the cross-agent race condition)."""

    def test_different_agent_id_not_matched(self):
        """A pending request from subagent A is NOT found when searching with
        subagent B's agent_id."""
        from permission_state_store import (
            create_request,
            find_pending_request_by_tool_session,
        )

        req = create_request(
            session_id="shared-session",
            cwd="/project",
            tool_name="Bash",
            tool_input={"command": "rm -rf dist"},
            permission_suggestions=[],
            ttl_seconds=300,
            agent_id="subagent-A",
        )

        # PostToolUse from a DIFFERENT agent should NOT find this request
        found = find_pending_request_by_tool_session(
            session_id="shared-session",
            tool_name="Bash",
            cwd="/project",
            agent_id="subagent-B",
        )
        self.assertIsNone(found)

        # PostToolUse from the parent session (agent_id=None) should NOT find it
        found = find_pending_request_by_tool_session(
            session_id="shared-session",
            tool_name="Bash",
            cwd="/project",
            agent_id=None,
        )
        self.assertIsNone(found)

        # PostToolUse from the SAME agent SHOULD find it
        found = find_pending_request_by_tool_session(
            session_id="shared-session",
            tool_name="Bash",
            cwd="/project",
            agent_id="subagent-A",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.request_id, req.request_id)

    def test_parent_session_request_matched_by_parent_only(self):
        """A pending request from the parent (agent_id=None) is only found
        when searching with agent_id=None."""
        from permission_state_store import (
            create_request,
            find_pending_request_by_tool_session,
        )

        req = create_request(
            session_id="parent-session",
            cwd="/project",
            tool_name="Bash",
            tool_input={"command": "rm -rf dist"},
            permission_suggestions=[],
            ttl_seconds=300,
            agent_id=None,
        )

        # Subagent's PostToolUse should NOT find parent's request
        found = find_pending_request_by_tool_session(
            session_id="parent-session",
            tool_name="Bash",
            cwd="/project",
            agent_id="some-subagent",
        )
        self.assertIsNone(found)

        # Parent's PostToolUse SHOULD find it
        found = find_pending_request_by_tool_session(
            session_id="parent-session",
            tool_name="Bash",
            cwd="/project",
            agent_id=None,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.request_id, req.request_id)

    def test_agent_id_stored_in_request(self):
        """create_request persists agent_id so it can be filtered on."""
        from permission_state_store import create_request, get_request

        req = create_request(
            session_id="sess",
            cwd="/project",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=[],
            ttl_seconds=300,
            agent_id="agent-xyz",
        )

        loaded = get_request(req.request_id)
        self.assertEqual(loaded.agent_id, "agent-xyz")


class _FakeRelayClient:
    """Stand-in for ``RelayClient``: records the (server_url, token) it was
    built with and swallows every relay call."""

    def __init__(self, server_url, installation_token):
        self.server_url = server_url
        self.token = installation_token

    def __getattr__(self, name):  # any relay method is a no-op MagicMock
        mock = MagicMock(name=name)
        setattr(self, name, mock)
        return mock


def _router_state(tpr, **overrides):
    """Context managers pinning the router's client-registry globals.

    Every test that touches the registry must patch all of them so nothing
    leaks into the next test (or the developer's real relay config).

    ``_clients`` is now the single source of truth; ``_default_token`` points at
    the default entry inside it (Issue 3 unification). Pass both together:
        _router_state(tpr,
                      _default_token="__td__",
                      _clients={"__td__": default_mock, "rly_ux": role_mock},
                      _server_url="https://relay.example")
    """
    state = {
        "TELEGRAM_ENABLED": True,
        "_default_token": None,
        "_clients": {},
        "_server_url": None,
    }
    state.update(overrides)
    return [patch.object(tpr, key, value) for key, value in state.items()]


class TestClientRegistry(unittest.TestCase):
    """``_client`` is a token-keyed registry over one shared server_url."""

    def setUp(self):
        import telegram_permission_router as tpr
        self.tpr = tpr

    def test_client_none_returns_default_client(self):
        default = object()
        with patch.object(self.tpr, "TELEGRAM_ENABLED", True), \
             patch.object(self.tpr, "_default_token", "__td__"), \
             patch.object(self.tpr, "_clients", {"__td__": default}), \
             patch.object(self.tpr, "_server_url", "https://relay.example"):
            self.assertIs(self.tpr._client(), default)
            self.assertIs(self.tpr._client(None), default)

    def test_client_token_is_distinct_and_memoized(self):
        default = object()
        with patch.object(self.tpr, "TELEGRAM_ENABLED", True), \
             patch.object(self.tpr, "RelayClient", _FakeRelayClient), \
             patch.object(self.tpr, "_default_token", "__td__"), \
             patch.object(self.tpr, "_clients", {"__td__": default}), \
             patch.object(self.tpr, "_server_url", "https://relay.example"):
            first = self.tpr._client("rly_x")
            second = self.tpr._client("rly_x")
            other = self.tpr._client("rly_y")

            self.assertIsNot(first, default)
            self.assertIs(first, second)  # same token -> same object
            self.assertIsNot(first, other)
            # Built on the shared server_url with its own token.
            self.assertEqual(first.server_url, "https://relay.example")
            self.assertEqual(first.token, "rly_x")
            self.assertEqual(other.token, "rly_y")

    def test_client_raises_when_relay_disabled(self):
        with patch.object(self.tpr, "TELEGRAM_ENABLED", False), \
             patch.object(self.tpr, "_default_token", "__td__"), \
             patch.object(self.tpr, "_clients", {"__td__": object()}), \
             patch.object(self.tpr, "_server_url", "https://relay.example"):
            with self.assertRaises(self.tpr.RelayError):
                self.tpr._client()
            with self.assertRaises(self.tpr.RelayError):
                self.tpr._client("rly_x")

    def test_client_raises_when_server_url_unknown(self):
        """The ``RelayClient.from_config`` fallback leaves ``_server_url``
        unknown; role destinations are then unreachable but the default one
        still works."""
        default = object()
        with patch.object(self.tpr, "TELEGRAM_ENABLED", True), \
             patch.object(self.tpr, "RelayClient", _FakeRelayClient), \
             patch.object(self.tpr, "_default_token", "__td__"), \
             patch.object(self.tpr, "_clients", {"__td__": default}), \
             patch.object(self.tpr, "_server_url", None):
            self.assertIs(self.tpr._client(), default)
            with self.assertRaises(self.tpr.RelayError):
                self.tpr._client("rly_x")


class TestLoadTelegramConfig(unittest.TestCase):
    """``load_telegram_config`` reads credentials through ``roles_config`` and
    degrades to ``RelayClient.from_config`` when that module is missing."""

    def setUp(self):
        import telegram_permission_router as tpr
        self.tpr = tpr
        self.tmp = tempfile.mkdtemp(prefix="claude-hooks-relaycfg-")
        self.cfg = Path(self.tmp) / "config.toml"
        self.cfg.write_text(
            'server_url = "https://relay.example"\n'
            'installation_token = "rly_default"\n'
            "\n[roles]\nux = \"rly_ux\"\n"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_builds_default_client_from_bindings(self):
        tpr = self.tpr
        with patch.object(tpr, "RelayClient", _FakeRelayClient), \
             patch.object(tpr, "RELAY_CONFIG_FILE", self.cfg), \
             patch.object(tpr, "TELEGRAM_ENABLED", False), \
             patch.object(tpr, "RELAY_CONFIG_SOURCE", "unknown"), \
             patch.object(tpr, "_default_token", None), \
             patch.object(tpr, "_clients", {}), \
             patch.object(tpr, "_server_url", None):
            tpr.load_telegram_config()

            self.assertTrue(tpr.TELEGRAM_ENABLED)
            self.assertEqual(tpr._server_url, "https://relay.example")
            self.assertEqual(tpr._default_token, "rly_default")
            self.assertEqual(tpr._clients["rly_default"].token, "rly_default")
            # _client(None) and _client("rly_default") return the same object —
            # the registry is the single source of truth.
            self.assertIs(tpr._client(None), tpr._clients["rly_default"])
            self.assertIs(tpr._client("rly_default"), tpr._clients["rly_default"])

    def test_falls_back_to_from_config_when_roles_config_missing(self):
        """A stale ~/.claude/hooks/ without roles_config must still produce a
        working default client — today's behaviour, not a lost Telegram."""
        tpr = self.tpr
        fake_cls = MagicMock()
        sentinel = object()
        fake_cls.from_config.return_value = sentinel
        with patch.object(tpr, "roles_config", None), \
             patch.object(tpr, "RelayClient", fake_cls), \
             patch.object(tpr, "RELAY_CONFIG_FILE", self.cfg), \
             patch.object(tpr, "TELEGRAM_ENABLED", False), \
             patch.object(tpr, "RELAY_CONFIG_SOURCE", "unknown"), \
             patch.object(tpr, "_default_token", None), \
             patch.object(tpr, "_clients", {}), \
             patch.object(tpr, "_server_url", "stale"):
            tpr.load_telegram_config()

            self.assertTrue(tpr.TELEGRAM_ENABLED)
            fake_cls.from_config.assert_called_once_with(self.cfg)
            # Unknown server_url -> role destinations unreachable, default fine.
            self.assertIsNone(tpr._server_url)
            # Default client is stored in the registry under a sentinel key;
            # _client(None) reaches it via _default_token.
            self.assertIn(tpr._default_token, tpr._clients)
            self.assertIs(tpr._clients[tpr._default_token], sentinel)
            self.assertIs(tpr._client(), sentinel)

    def test_import_guard_survives_non_importerror_in_roles_config(self):
        """The module-level guard is ``except Exception``, not ``except ImportError``.

        A stale/partially-broken roles_config.py that raises SyntaxError at
        import time must still degrade to the RelayClient.from_config() path
        rather than losing Telegram — the guard must survive any import failure,
        not only a missing file. Simulate by poisoning sys.modules so the import
        raises AttributeError (a non-ImportError) when the router tries to access
        the module.
        """
        import builtins as _builtins
        tpr = self.tpr
        fake_cls = MagicMock()
        sentinel = object()
        fake_cls.from_config.return_value = sentinel

        original_import = _builtins.__import__

        def _raise_syntax_error(name, *args, **kwargs):
            if name == "roles_config":
                raise SyntaxError("simulated broken roles_config.py")
            return original_import(name, *args, **kwargs)

        # Snapshot all module globals that the reload will re-initialise.
        # The reload is necessary to trigger the module-level import guard, but
        # it leaves every global at its "freshly imported" initial value — which
        # may differ from whatever the suite had set up before this test ran.
        # Restoring via addCleanup (not a bare finally statement) guarantees the
        # snapshot is applied even when the test raises an exception mid-way.
        _pre_test_globals = {
            "roles_config": tpr.roles_config,
            "TELEGRAM_ENABLED": tpr.TELEGRAM_ENABLED,
            "RELAY_CONFIG_SOURCE": tpr.RELAY_CONFIG_SOURCE,
            "_clients": tpr._clients,
            "_clients_lock": tpr._clients_lock,
            "_default_token": tpr._default_token,
            "_server_url": tpr._server_url,
            "RelayClient": tpr.RelayClient,
            "Answer": tpr.Answer,
            "MessageHandle": tpr.MessageHandle,
            "NotBoundError": tpr.NotBoundError,
            "RelayError": tpr.RelayError,
        }
        _saved_sys_mod = sys.modules.get("roles_config")

        def _restore_module():
            # Restore sys.modules entry (idempotent with the finally block below).
            if _saved_sys_mod is not None:
                sys.modules["roles_config"] = _saved_sys_mod
            else:
                sys.modules.pop("roles_config", None)
            # Restore every snapshotted global directly — no second reload, so
            # this cleanup cannot introduce a new ordering dependency of its own.
            for _attr, _val in _pre_test_globals.items():
                setattr(tpr, _attr, _val)

        self.addCleanup(_restore_module)

        # Remove any cached copy so the guard's import actually fires.
        saved = sys.modules.pop("roles_config", None)
        try:
            with patch.object(_builtins, "__import__", side_effect=_raise_syntax_error), \
                 patch.object(tpr, "RelayClient", fake_cls), \
                 patch.object(tpr, "RELAY_CONFIG_FILE", self.cfg), \
                 patch.object(tpr, "TELEGRAM_ENABLED", False), \
                 patch.object(tpr, "RELAY_CONFIG_SOURCE", "unknown"), \
                 patch.object(tpr, "_default_token", None), \
                 patch.object(tpr, "_clients", {}), \
                 patch.object(tpr, "_server_url", "stale"):
                # Reload the module so its top-level guard fires under the patched
                # import; capture the resulting roles_config binding.
                import importlib
                import telegram_permission_router as _tpr_mod
                importlib.reload(_tpr_mod)
                # The guard must have caught the SyntaxError and set roles_config=None.
                self.assertIsNone(_tpr_mod.roles_config)
        finally:
            if saved is not None:
                sys.modules["roles_config"] = saved
            else:
                sys.modules.pop("roles_config", None)
            # Module globals are restored by _restore_module() via addCleanup,
            # which runs after tearDown and is robust to mid-test failure.
            # A second importlib.reload() here would re-initialise all globals
            # and defeat the snapshot/restore above.


class TestQuestionBodyRendering(unittest.TestCase):
    """``render_question_body`` is the pure body builder; with no role context
    its output is byte-identical to the pre-roles message."""

    SINGLE_INPUT = {
        "question": "Sidebar or top nav?",
        "header": "Layout",
        "options": [
            {"label": "Sidebar", "description": "Left rail"},
            {"label": "Top nav"},
        ],
    }
    # Captured from the implementation as it stood before task 15-02.
    SINGLE_BODY = "\n".join([
        "<b>Question</b> — leangeeks",
        "",
        "<b><i>Layout</i></b>",
        "",
        "Sidebar or top nav?",
        "",
        "<i>Tap a number below, or reply with text:</i>",
        "",
        "<b>1. Sidebar</b>",
        "    <i>Left rail</i>",
        "",
        "<b>2. Top nav</b>",
    ])

    MULTI_INPUT = {
        "question": "Which shades?",
        "header": "Palette",
        "multiSelect": True,
        "options": [
            {"label": "Blue"},
            {"label": "Green", "description": "Forest"},
        ],
    }
    MULTI_BODY = "\n".join([
        "<b>Question</b> — leangeeks",
        "",
        "<b><i>Palette</i></b>",
        "",
        "Which shades?",
        "",
        "<i>Tap numbers to toggle (select as many as you like), "
        "then tap ✓ Submit. Or reply with text:</i>",
        "",
        "<b>1. Blue</b>",
        "",
        "<b>2. Green</b>",
        "    <i>Forest</i>",
    ])

    def setUp(self):
        import telegram_permission_router as tpr
        self.tpr = tpr

    def _sent_text(self, request, **kwargs):
        captured = {}

        def _fake_send_relay(**kw):
            captured.update(kw)
            return 1

        with patch.object(self.tpr, "_send_relay", side_effect=_fake_send_relay), \
             patch.object(self.tpr, "set_telegram_message_id"):
            self.tpr.send_question_message(request, "leangeeks", 0, 1, **kwargs)
        return captured

    def test_single_select_body_unchanged(self):
        req = _make_request(tool_name="AskUserQuestion", tool_input=self.SINGLE_INPUT)
        self.assertEqual(
            self.tpr.render_question_body(req, "leangeeks", 0, 1), self.SINGLE_BODY
        )
        self.assertEqual(self._sent_text(req)["text"], self.SINGLE_BODY)

    def test_multi_select_body_unchanged(self):
        req = _make_request(tool_name="AskUserQuestion", tool_input=self.MULTI_INPUT)
        self.assertEqual(
            self.tpr.render_question_body(req, "leangeeks", 0, 1), self.MULTI_BODY
        )
        self.assertEqual(self._sent_text(req)["text"], self.MULTI_BODY)

    def test_role_title_notes_and_banner_render_under_the_title(self):
        req = _make_request(tool_name="AskUserQuestion", tool_input=self.SINGLE_INPUT)
        body = self.tpr.render_question_body(
            req,
            "leangeeks",
            0,
            1,
            role_title="UX/UI designer",
            notes=("Unknown role @uxx — routed to Operator.",),
            banner="@ux hasn't answered in 15m — you can decide instead.",
        )
        lines = body.split("\n")
        self.assertEqual(lines[0], "<b>Question</b> — leangeeks")
        # Banner sits above the ``for:`` line; notes below it.
        self.assertEqual(
            lines[1], "⏳ @ux hasn't answered in 15m — you can decide instead."
        )
        self.assertEqual(lines[2], "<i>for: UX/UI designer</i>")
        self.assertEqual(lines[3], "⚠️ Unknown role @uxx — routed to Operator.")
        self.assertEqual(lines[4], "")
        # Everything after the inserted block is unchanged.
        self.assertEqual("\n".join(lines[4:]), "\n".join(self.SINGLE_BODY.split("\n")[1:]))

    def test_role_context_is_html_escaped(self):
        req = _make_request(tool_name="AskUserQuestion", tool_input=self.SINGLE_INPUT)
        body = self.tpr.render_question_body(
            req, "leangeeks", 0, 1, role_title="A<&>B", notes=("n<&>",), banner="b<&>"
        )
        self.assertIn("<i>for: A&lt;&amp;&gt;B</i>", body)
        self.assertIn("⚠️ n&lt;&amp;&gt;", body)
        self.assertIn("⏳ b&lt;&amp;&gt;", body)

    def test_send_question_message_forwards_all_role_arguments(self):
        """The four keyword-only parameters must reach the renderer verbatim —
        a send that routes correctly but renders no role context is the easy
        mistake here."""
        req = _make_request(tool_name="AskUserQuestion", tool_input=self.SINGLE_INPUT)
        seen = {}

        def _fake_render(*args, **kwargs):
            seen.update(kwargs)
            return "BODY"

        captured = {}
        with patch.object(self.tpr, "render_question_body", side_effect=_fake_render), \
             patch.object(self.tpr, "_send_relay",
                          side_effect=lambda **kw: (captured.update(kw), 5)[1]), \
             patch.object(self.tpr, "set_telegram_message_id"):
            msg_id = self.tpr.send_question_message(
                req,
                "leangeeks",
                0,
                1,
                group_id="g",
                token="rly_ux",
                role_title="UX/UI designer",
                notes=("note",),
                banner="banner",
            )

        self.assertEqual(msg_id, 5)
        self.assertIsInstance(msg_id, int)  # return type must stay an int
        self.assertEqual(seen["role_title"], "UX/UI designer")
        self.assertEqual(seen["notes"], ("note",))
        self.assertEqual(seen["banner"], "banner")
        self.assertEqual(captured["token"], "rly_ux")
        self.assertEqual(captured["text"], "BODY")


class TestTokenRouting(unittest.TestCase):
    """Every send/wait/cancel/edit helper honours an explicit ``token`` and
    keeps using the default client when none is given."""

    def setUp(self):
        import telegram_permission_router as tpr
        self.tpr = tpr
        self.default = MagicMock(name="default")
        self.role = MagicMock(name="role")
        self._patches = _router_state(
            tpr,
            _default_token="__td__",
            _clients={"__td__": self.default, "rly_ux": self.role},
            _server_url="https://relay.example",
        )
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_send_relay_routes_by_token(self):
        self.role.send_message.return_value = MagicMock(message_id=11)
        mid = self.tpr._send_relay(
            text="t", keyboard=None, kind="question", reply_required=True,
            request_id="r", token="rly_ux",
        )
        self.assertEqual(mid, 11)
        self.role.send_message.assert_called_once()
        self.default.send_message.assert_not_called()

    def test_send_relay_defaults_to_default_client(self):
        self.default.send_message.return_value = MagicMock(message_id=12)
        mid = self.tpr._send_relay(
            text="t", keyboard=None, kind="question", reply_required=True,
            request_id="r",
        )
        self.assertEqual(mid, 12)
        self.default.send_message.assert_called_once()
        self.role.send_message.assert_not_called()

    def test_remove_inline_buttons_routes_by_token(self):
        self.assertTrue(self.tpr.remove_inline_buttons(7, token="rly_ux"))
        self.role.cancel_message.assert_called_once_with(7)
        self.default.cancel_message.assert_not_called()

        self.assertTrue(self.tpr.remove_inline_buttons(8))
        self.default.cancel_message.assert_called_once_with(8)

    def test_edit_message_text_routes_by_token(self):
        self.assertTrue(self.tpr.edit_message_text(7, "hi", token="rly_ux"))
        self.role.edit_message.assert_called_once_with(7, text="hi")
        self.default.edit_message.assert_not_called()

        self.assertTrue(self.tpr.edit_message_text(8, "yo"))
        self.default.edit_message.assert_called_once_with(8, text="yo")

    def test_delete_message_routes_by_token(self):
        self.assertTrue(self.tpr.delete_message(7, token="rly_ux"))
        self.role.delete_message.assert_called_once_with(7)
        self.default.delete_message.assert_not_called()

        self.assertTrue(self.tpr.delete_message(8))
        self.default.delete_message.assert_called_once_with(8)

    def test_wait_for_relay_answer_routes_by_token(self):
        self.role.wait_for_answer.return_value = MagicMock(
            state="answered", answer={"via": "button", "value": "qa0"}
        )
        answer = self.tpr.wait_for_relay_answer(7, timeout=1, token="rly_ux")
        self.assertEqual(answer, {"via": "button", "value": "qa0"})
        self.role.wait_for_answer.assert_called_once_with(
            7, timeout=1, long_poll_chunk=5
        )
        self.default.wait_for_answer.assert_not_called()

    def test_send_question_message_routes_by_token(self):
        self.role.send_message.return_value = MagicMock(message_id=13)
        req = _make_request(
            tool_name="AskUserQuestion",
            tool_input={"question": "q", "options": [{"label": "A"}]},
        )
        with patch.object(self.tpr, "set_telegram_message_id"):
            mid = self.tpr.send_question_message(req, "ws", 0, 1, token="rly_ux")
        self.assertEqual(mid, 13)
        self.role.send_message.assert_called_once()
        self.default.send_message.assert_not_called()

    def test_default_destination_helpers_have_no_token_parameter(self):
        """Permissions and idle notifications are default-destination only
        (brd §6) — they must not grow a ``token`` knob."""
        for fn in (self.tpr.send_permission_message, self.tpr.send_idle_notification):
            self.assertNotIn(
                "token", inspect.signature(fn).parameters, f"{fn.__name__} gained token"
            )


class TestFinalizeMessage(unittest.TestCase):
    """``finalize_message`` bakes an answer in, then strips the keyboard."""

    def setUp(self):
        import telegram_permission_router as tpr
        self.tpr = tpr
        self.default = MagicMock(name="default")
        self.role = MagicMock(name="role")
        self._patches = _router_state(
            tpr,
            _default_token="__td__",
            _clients={"__td__": self.default, "rly_ux": self.role},
            _server_url="https://relay.example",
        )
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_patches_then_cancels_against_the_tokens_client(self):
        ok = self.tpr.finalize_message(7, "BODY", "Yes <b>", token="rly_ux")
        self.assertTrue(ok)
        # PATCH first, cancel second: a cancelled message can still be edited,
        # the reverse leaves a live keyboard up for the round trip.
        self.assertEqual(
            [name for name, _a, _k in self.role.mock_calls],
            ["edit_message", "cancel_message"],
        )
        self.role.edit_message.assert_called_once_with(
            7, text="BODY\n\n✍️ Yes &lt;b&gt;"
        )
        self.role.cancel_message.assert_called_once_with(7)
        self.assertEqual(self.default.mock_calls, [])

    def test_default_prefix_and_override(self):
        self.tpr.finalize_message(7, "B", "A")
        self.default.edit_message.assert_called_once_with(7, text="B\n\n✍️ A")

        self.default.reset_mock()
        self.tpr.finalize_message(8, "B", "A", prefix="✅ ")
        self.default.edit_message.assert_called_once_with(8, text="B\n\n✅ A")

    def test_returns_false_when_patch_fails_but_still_cancels(self):
        self.role.edit_message.side_effect = self.tpr.RelayError("boom")
        ok = self.tpr.finalize_message(7, "BODY", "A", token="rly_ux")
        self.assertFalse(ok)
        # The keyboard must still come down even when the body edit failed.
        self.role.cancel_message.assert_called_once_with(7)

    def test_returns_false_when_cancel_fails(self):
        self.role.cancel_message.side_effect = self.tpr.RelayError("boom")
        ok = self.tpr.finalize_message(7, "BODY", "A", token="rly_ux")
        self.assertFalse(ok)
        self.role.edit_message.assert_called_once()


class TestPostToolRoleRevoke(unittest.TestCase):
    """PostToolUse re-resolves ``role`` -> token so it revokes against the chat
    the message actually went to."""

    ROLES_TOML = (
        'default = "operator"\n'
        "\n[role.operator]\ntitle = \"Operator\"\n"
        "\n[role.ux]\naliases = [\"design\"]\ntitle = \"UX/UI designer\"\n"
    )
    BINDINGS_TOML = (
        'server_url = "https://relay.example"\n'
        'installation_token = "rly_op"\n'
        "\n[roles]\nux = \"rly_ux\"\n"
    )

    def setUp(self):
        import posttool_hook
        import roles_config
        self.posttool_hook = posttool_hook
        self.roles_config = roles_config

        self.tmp = tempfile.mkdtemp(prefix="claude-hooks-roles-")
        ws = Path(self.tmp) / "workspace"
        (ws / ".claude").mkdir(parents=True)
        (ws / ".claude" / "roles.toml").write_text(self.ROLES_TOML)
        self.workspace = str(ws)

        bindings_path = Path(self.tmp) / "config.toml"
        bindings_path.write_text(self.BINDINGS_TOML)
        self.bindings = roles_config.load_bindings(bindings_path)

        # find_roles_file walks *up* the filesystem and honours
        # CLAUDE_PROJECT_DIR, which is set inside a Claude Code session — pin it
        # at the fixture so the walk cannot reach real config.
        env = patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.workspace})
        env.start()
        self.addCleanup(env.stop)
        # load_bindings() with no argument would read the developer's real
        # ~/.config/claude-tg-relay/config.toml.
        binds = patch.object(
            roles_config, "load_bindings", lambda *a, **k: self.bindings
        )
        binds.start()
        self.addCleanup(binds.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _revoke(self, request):
        tpr = self.posttool_hook.telegram_permission_router
        with patch.object(tpr, "remove_inline_buttons") as remove, \
             patch.object(tpr, "set_message_reaction", return_value=True) as react:
            ok = self.posttool_hook.revoke_telegram_message(request)
        return ok, remove, react

    def test_role_tagged_request_revoked_through_the_roles_client(self):
        req = _make_request(
            cwd=self.workspace, role="ux", telegram_message_id=99,
        )
        ok, remove, react = self._revoke(req)
        self.assertTrue(ok)
        remove.assert_called_once_with(99, token="rly_ux")
        react.assert_called_once_with(99, "✅")

    def test_request_without_role_uses_the_default_client(self):
        req = _make_request(cwd=self.workspace, telegram_message_id=99)
        ok, remove, _ = self._revoke(req)
        self.assertTrue(ok)
        remove.assert_called_once_with(99, token=None)

    def test_resolution_failure_falls_back_to_the_default_client(self):
        req = _make_request(cwd=self.workspace, role="ux", telegram_message_id=99)
        with patch.object(
            self.roles_config, "load_catalog", side_effect=RuntimeError("broken")
        ):
            ok, remove, _ = self._revoke(req)
        self.assertTrue(ok)
        remove.assert_called_once_with(99, token=None)

    def test_no_roles_catalog_falls_back_to_the_default_client(self):
        """A row tagged with a role whose workspace has since lost its
        roles.toml still revokes — against the default client."""
        empty = tempfile.mkdtemp(prefix="claude-hooks-noroles-")
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        req = _make_request(cwd=empty, role="ux", telegram_message_id=99)
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": empty}):
            ok, remove, _ = self._revoke(req)
        self.assertTrue(ok)
        remove.assert_called_once_with(99, token=None)

    def test_unknown_role_id_falls_back_to_the_default_role_token(self):
        """An unknown role id resolves through 15-01's fallback chain to the
        default role — whose token is the machine's default installation."""
        req = _make_request(cwd=self.workspace, role="ghost", telegram_message_id=99)
        ok, remove, _ = self._revoke(req)
        self.assertTrue(ok)
        remove.assert_called_once_with(99, token="rly_op")


# ── Helpers shared by TestAliasRouting ────────────────────────────────────────

def _make_catalog(default="operator", roles=None, workspace_id="leangeeks"):
    """Build a minimal RoleCatalog fixture for alias-routing tests."""
    import roles_config as rc
    if roles is None:
        roles = {
            "operator": rc.Role(
                role_id="operator", title="Operator",
                aliases=("operator", "op"), escalate_after=None,
            ),
            "ux": rc.Role(
                role_id="ux", title="UX/UI designer",
                aliases=("ux", "design"), escalate_after=None,
            ),
        }
    alias_index = {}
    for rid, role in roles.items():
        for a in role.aliases:
            alias_index[a] = rid
    return rc.RoleCatalog(
        workspace_id=workspace_id,
        default_role=default,
        roles=roles,
        alias_index=alias_index,
        errors=(),
        path=None,
    )


def _make_bindings(default_token="rly_op", roles=None, server_url="https://relay.example"):
    """Build a minimal Bindings fixture for alias-routing tests."""
    import roles_config as rc
    return rc.Bindings(
        server_url=server_url,
        default_token=default_token,
        roles=roles or {"ux": "rly_ux"},
        workspace_roles={},
        escalate_after={},
        workspace_escalate_after={},
        errors=(),
    )


class TestAliasRouting(unittest.TestCase):
    """handle_ask_user_question: alias resolution, mixed-role rejection,
    send-failure retry, and the compatibility floor (no roles.toml)."""

    def setUp(self):
        import permission_request_hook as hook
        import telegram_permission_router as tpr
        import roles_config as rc
        self.hook = hook
        self.tpr = tpr
        self.rc = rc

        self.catalog = _make_catalog()
        self.bindings = _make_bindings()

        # A minimal _send_relay capture helper used by several tests.
        self._sends = []

    # ── Compatibility floor ────────────────────────────────────────────────────

    def test_no_roles_toml_identical_relay_payload(self):
        """No roles.toml (catalog=None) → identical relay call to pre-15-03.

        The compatibility-floor criterion is asserted on the recorded relay
        call, not merely the return value (first standing risk in state.md).
        """
        import permission_request_hook as hook
        captured = []

        def _capture_send(**kw):
            captured.append(kw)
            return 100

        with patch("roles_config.load_catalog", return_value=None), \
             patch("roles_config.load_bindings", return_value=_make_bindings()), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_capture_send), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = hook.handle_ask_user_question(
                session_id="sess",
                cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Sidebar or top nav?",
                     "header": "Layout",
                     "options": [{"label": "Sidebar"}, {"label": "Top"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(captured), 1)
        call = captured[0]
        # Token must be None (default client — no role routing).
        self.assertIsNone(call["token"])
        # Body must NOT contain a "for:" role line or any ⚠️ note.
        self.assertNotIn("for:", call["text"])
        self.assertNotIn("⚠️", call["text"])
        # Body must contain the header and question verbatim.
        self.assertIn("Layout", call["text"])
        self.assertIn("Sidebar or top nav?", call["text"])
        # updatedInput preserves original tool_input (raw header untouched).
        answers = result["updatedInput"]["answers"]
        self.assertIn("Sidebar or top nav?", answers)

    def test_no_roles_toml_no_role_in_state_store(self):
        """Legacy mode: create_request must NOT receive a role argument."""
        import permission_request_hook as hook

        with patch("roles_config.load_catalog", return_value=None), \
             patch("roles_config.load_bindings", return_value=_make_bindings()), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", return_value=42), \
             patch("telegram_permission_router.set_telegram_message_id"), \
             patch("permission_request_hook.create_request",
                   return_value=_make_request()) as mock_cr:
            hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [{"question": "Q?", "options": []}]},
                workspace_name="ws",
            )

        mock_cr.assert_called_once()
        self.assertIsNone(mock_cr.call_args.kwargs.get("role"))

    # ── Role-tagged routing ────────────────────────────────────────────────────

    def test_ux_tag_routes_to_ux_token(self):
        """@ux Layout → sent to rly_ux, for: UX/UI designer, header stripped."""
        captured = []

        def _se(**kw):
            captured.append(kw)
            return 200

        with patch("roles_config.load_catalog", return_value=self.catalog), \
             patch("roles_config.load_bindings", return_value=self.bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Sidebar or top nav?",
                     "header": "@ux Layout",
                     "options": [{"label": "Sidebar"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(captured), 1)
        call = captured[0]
        # Routed to the ux token.
        self.assertEqual(call["token"], "rly_ux")
        # for: line present.
        self.assertIn("for: UX/UI designer", call["text"])
        # Alias stripped from rendered header (only "Layout" remains, not "@ux").
        self.assertIn("Layout", call["text"])
        self.assertNotIn("@ux", call["text"])
        # updatedInput retains the raw header (terminal chip untouched).
        orig_qs = result["updatedInput"]["questions"]
        self.assertEqual(orig_qs[0]["header"], "@ux Layout")

    def test_untagged_question_in_roles_workspace_goes_to_default(self):
        """No alias → default token, for: Operator shown."""
        captured = []

        def _se(**kw):
            captured.append(kw)
            return 300

        with patch("roles_config.load_catalog", return_value=self.catalog), \
             patch("roles_config.load_bindings", return_value=self.bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Any Q?", "header": "NoAlias", "options": [{"label": "A"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNotNone(result)
        call = captured[0]
        self.assertEqual(call["token"], "rly_op")
        self.assertIn("for: Operator", call["text"])

    def test_unknown_alias_routes_to_default_with_warning(self):
        """@uxx (unknown) → default token, ⚠️ note in body."""
        captured = []

        def _se(**kw):
            captured.append(kw)
            return 400

        with patch("roles_config.load_catalog", return_value=self.catalog), \
             patch("roles_config.load_bindings", return_value=self.bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q?", "header": "@uxx Something", "options": [{"label": "A"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNotNone(result)
        call = captured[0]
        self.assertEqual(call["token"], "rly_op")
        # Full exact phrase sourced from roles_config (15-01 owns it).
        # A loose substring check would pass even if the wording regressed.
        self.assertIn("Unknown role @uxx — routed to Operator.", call["text"])

    def test_role_no_binding_routes_to_default_with_note(self):
        """Role exists in catalog but has no binding → default + ⚠️ Intended for."""
        rc = self.rc
        import roles_config as _rc

        # Build a catalog with an "arch" role but no binding for it.
        arch_role = _rc.Role(
            role_id="arch", title="Tech lead",
            aliases=("arch",), escalate_after=None,
        )
        catalog = _make_catalog(roles={
            "operator": _rc.Role(
                role_id="operator", title="Operator",
                aliases=("operator",), escalate_after=None,
            ),
            "arch": arch_role,
        })
        # Bindings: only default token, no "arch" entry.
        bindings = _make_bindings(default_token="rly_op", roles={})

        captured = []

        def _se(**kw):
            captured.append(kw)
            return 500

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "DB?", "header": "@arch Storage", "options": [{"label": "PG"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNotNone(result)
        call = captured[0]
        self.assertEqual(call["token"], "rly_op")
        # Full exact phrase from roles_config.resolve_destination (line 544).
        self.assertIn(
            "Intended for Tech lead — not reachable from this machine.",
            call["text"],
        )

    # ── Mixed-role rejection ───────────────────────────────────────────────────

    def test_two_distinct_roles_returns_deny_mixed(self):
        """Two questions targeting different roles → deny, zero relay sends,
        zero state-store rows."""
        import os
        import roles_config as _rc
        catalog = _make_catalog()
        bindings = _make_bindings()

        send_calls = []
        create_calls = []

        # Count rows in the isolated state store BEFORE the call.
        # The runner redirects CLAUDE_PERMISSION_STATE_FILE to a temp dir
        # (run_all_tests.py / conftest.py), so this never touches the
        # developer's real ~/.claude/permission_requests.jsonl.
        _state_file = os.environ.get("CLAUDE_PERMISSION_STATE_FILE", "")

        def _row_count():
            if not _state_file or not os.path.exists(_state_file):
                return 0
            with open(_state_file) as _f:
                return sum(1 for _l in _f if _l.strip())

        rows_before = _row_count()

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("telegram_permission_router._send_relay",
                   side_effect=lambda **kw: (send_calls.append(kw), 1)[1]), \
             patch("permission_request_hook.create_request",
                   side_effect=lambda **kw: (create_calls.append(kw), _make_request())[1]):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q1?", "header": "@ux Layout", "options": []},
                    {"question": "Q2?", "header": "@op Something", "options": []},
                ]},
                workspace_name="leangeeks",
            )

        # Must return a deny_mixed_roles decision with the full task-specified
        # reason string — both the dynamic count+alias pairs and the static
        # explanatory tail that the hook owns.
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "deny_mixed_roles")
        self.assertEqual(
            result["reason"],
            "This AskUserQuestion call addressed 2 different human roles "
            "(@ux -> UX/UI designer, @op -> Operator). "
            "Each call must target exactly one role, because a question group cannot "
            "span two Telegram chats. Split this into one AskUserQuestion call per role.",
        )
        # Zero relay sends and zero mock create_request calls.
        self.assertEqual(len(send_calls), 0)
        self.assertEqual(len(create_calls), 0)
        # Zero rows written to the isolated state store (direct file count —
        # absence-of-error is not sufficient per task spec).
        self.assertEqual(
            _row_count(), rows_before,
            "Mixed-role deny must not write any state-store rows",
        )

    def test_deny_mixed_roles_becomes_behavior_deny(self):
        """build_output_decision maps deny_mixed_roles → behavior: deny."""
        from permission_request_hook import build_output_decision
        out = build_output_decision(
            {"action": "deny_mixed_roles",
             "reason": "This AskUserQuestion call addressed 2 different human roles..."},
            _make_request(),
        )
        self.assertIsNotNone(out)
        dec = out["hookSpecificOutput"]["decision"]
        self.assertEqual(dec["behavior"], "deny")
        self.assertIn("2 different human roles", dec["reason"])

    def test_two_unknown_aliases_same_role_sends_normally(self):
        """Two unknown aliases → both fall back to default → one role → send OK."""
        catalog = _make_catalog()
        bindings = _make_bindings()
        captured = []
        msg_counter = {"n": 0}

        def _se(**kw):
            captured.append(kw)
            msg_counter["n"] += 1
            return msg_counter["n"] * 100

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q1?", "header": "@ghost1 X", "options": [{"label": "A"}]},
                    {"question": "Q2?", "header": "@ghost2 Y", "options": [{"label": "B"}]},
                ]},
                workspace_name="leangeeks",
            )

        # Two questions sent, result is an answer decision.
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "answer")
        self.assertEqual(len(captured), 2)
        # Both go to the default token.
        self.assertTrue(all(c["token"] == "rly_op" for c in captured))

    # ── destination.token is None ─────────────────────────────────────────────

    def test_unreachable_token_returns_none(self):
        """destination.token is None → return None, nothing sent."""
        import roles_config as _rc

        # Build a catalog with a "ux" role that has no binding resolution.
        catalog = _make_catalog()
        # Empty bindings: default role has no token either (server_url + no token).
        # We want only "ux" unreachable but default reachable for the default dest.
        # For this test, force destination.token=None by patching resolve_destination.
        _unreachable = _rc.Destination(
            role_id="ux", title="UX/UI designer",
            token=None, is_default=False,
            requested_role_id=None, requested_title=None,
            escalate_after=None, notes=(),
        )
        send_calls = []

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=self.bindings), \
             patch("roles_config.resolve_destination", return_value=_unreachable), \
             patch("telegram_permission_router._send_relay",
                   side_effect=lambda **kw: (send_calls.append(kw), 1)[1]):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q?", "header": "@ux H", "options": []}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNone(result)
        self.assertEqual(len(send_calls), 0)

    # ── Send failure fallback ─────────────────────────────────────────────────

    def test_send_failure_to_role_retries_against_default(self):
        """A failed send to a role token → cancel siblings → retry against default
        with fresh ids and delivery-failure note, escalation suppressed (None)."""
        catalog = _make_catalog()
        bindings = _make_bindings()

        send_calls = []
        msg_counter = {"n": 0}

        def _se(**kw):
            send_calls.append(kw)
            # First send (to ux token) fails; subsequent (default) succeed.
            if kw.get("token") == "rly_ux":
                return None
            msg_counter["n"] += 1
            return msg_counter["n"] * 100

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q?", "header": "@ux Layout", "options": [{"label": "A"}]}
                ]},
                workspace_name="leangeeks",
            )

        # Result is a successful answer (retry worked).
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "answer")

        # First call is to rly_ux (failed); retry calls are to rly_op.
        self.assertTrue(send_calls[0]["token"] == "rly_ux")
        retry_calls = [c for c in send_calls if c.get("token") == "rly_op"]
        self.assertGreater(len(retry_calls), 0)

        # Retry body contains the delivery-failure note — full exact phrase
        # (15-03 owns this wording; a partial match would pass if it regressed).
        retry_text = retry_calls[0]["text"]
        self.assertIn(
            "Intended for UX/UI designer — "
            "the relay could not deliver to them (not bound or send failed).",
            retry_text,
        )

        # Fresh ids: retry must use a new request_id AND a new group_id.
        # _send_relay keys idempotency on req:{request_id}:send, so reusing
        # either id on a body that differs by one note earns a 422 from the
        # relay.  Removing the `group_id = uuid.uuid4().hex` line at line 679
        # of the hook would fail the second assertion below.
        first_rid = send_calls[0]["request_id"]
        retry_rid = retry_calls[0]["request_id"]
        self.assertNotEqual(first_rid, retry_rid)
        first_gid = send_calls[0]["group_id"]
        retry_gid = retry_calls[0]["group_id"]
        self.assertNotEqual(first_gid, retry_gid)

    def test_send_failure_to_default_returns_none(self):
        """A failed send to the default token → return None, no retry."""
        catalog = _make_catalog()
        bindings = _make_bindings()

        send_calls = []

        def _se(**kw):
            send_calls.append(kw)
            return None  # every send fails

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("telegram_permission_router._send_relay", side_effect=_se), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    # No alias → default destination → failure → no retry.
                    {"question": "Q?", "header": "Plain", "options": []}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNone(result)
        # Only one send attempt (no retry).
        self.assertEqual(len(send_calls), 1)

    # ── State-store row carries role; updatedInput raw headers untouched ───────

    def test_role_stored_in_state_row(self):
        """create_request receives role=<role_id> for a tagged question."""
        catalog = _make_catalog()
        bindings = _make_bindings()

        create_calls = []

        def _cr(**kw):
            create_calls.append(kw)
            return _make_request()

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.create_request", side_effect=_cr), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", return_value=42), \
             patch("telegram_permission_router.set_telegram_message_id"):
            self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q?", "header": "@ux Layout", "options": [{"label": "A"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["role"], "ux")

    def test_updated_input_raw_headers_untouched(self):
        """updatedInput keeps original headers with the @alias prefix."""
        catalog = _make_catalog()
        bindings = _make_bindings()

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value={"via": "button", "value": "qa0"}), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(state="pending")), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons"), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", return_value=42), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Sidebar or nav?",
                     "header": "@ux Layout",
                     "options": [{"label": "Sidebar"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNotNone(result)
        # updatedInput carries original tool_input structure.
        updated = result["updatedInput"]
        # The questions list is from the original tool_input.
        self.assertEqual(updated["questions"][0]["header"], "@ux Layout")
        # answers key is the question text.
        self.assertIn("Sidebar or nav?", updated["answers"])

    # ── Terminal resolution cancels via role token ─────────────────────────────

    def test_terminal_resolution_cancels_via_role_token(self):
        """When answered in the terminal, every sibling is cancelled through
        the role's token (not the default token)."""
        catalog = _make_catalog()
        bindings = _make_bindings()

        from permission_state_store import RequestState

        cancel_calls = []

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value=None), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(
                       state=RequestState.RESOLVED_TERMINAL.value)), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.remove_inline_buttons",
                   side_effect=lambda mid, *, token=None:
                       cancel_calls.append({"mid": mid, "token": token})), \
             patch("permission_request_hook.set_message_reaction"), \
             patch("telegram_permission_router._send_relay", return_value=77), \
             patch("telegram_permission_router.set_telegram_message_id"):
            result = self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Q?", "header": "@ux Layout", "options": [{"label": "A"}]}
                ]},
                workspace_name="leangeeks",
            )

        self.assertIsNone(result)
        # All cancellations must use the ux token.
        self.assertGreater(len(cancel_calls), 0)
        self.assertTrue(all(c["token"] == "rly_ux" for c in cancel_calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
