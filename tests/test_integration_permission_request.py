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
import threading
import time
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
        # reason must be at the root of the output dict, not inside decision
        self.assertIn("Use a different approach", out["reason"])
        self.assertNotIn("reason", out["hookSpecificOutput"]["decision"])

    def test_reply_emitted_json_shape(self):
        """Emitted JSON: reason at root, behavior inside decision, round-trips cleanly.

        This test catches the pre-fix bug where reason was nested inside decision
        (hookSpecificOutput.decision.reason) instead of at the root level.
        The old test_reply only checked that some text was present; it would still
        pass (via KeyError → test failure) once the fix was in place, but it could
        not distinguish wrong-location from right-location when the bug existed.
        """
        out = build_output_decision(
            {"action": "reply", "reply_text": "Please clarify your intent"},
            _make_request(),
        )
        # Structure: reason sibling of hookSpecificOutput, not inside decision.
        self.assertIn("reason", out)
        self.assertNotIn("reason", out["hookSpecificOutput"]["decision"])
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")
        # Exact text
        self.assertEqual(out["reason"], "User reply: Please clarify your intent")
        # Round-trip through JSON (this is what stdout → harness actually does)
        roundtripped = json.loads(json.dumps(out))
        self.assertEqual(roundtripped["reason"], "User reply: Please clarify your intent")
        self.assertNotIn("reason", roundtripped["hookSpecificOutput"]["decision"])

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
    @patch("permission_request_hook.finalize_message")
    def test_handle_ask_user_question_terminal_cancels_current_child_message(
        self,
        mock_finalize,
        mock_get_request,
        mock_create_request,
        mock_wait_relay,
        _mock_catalog,       # load_catalog → None (legacy mode)
        _mock_bindings,      # load_bindings → MagicMock (unused in legacy mode)
    ):
        """handle_ask_user_question: when the current child resolves via
        terminal, *that child's* message is finalized (PATCH + keyboard strip)
        before returning None. 15-04 replaced the bare ``remove_inline_buttons``
        with ``finalize_message`` so the reader sees what was decided."""
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
        # The current child's relay message (555) must have been finalized:
        # generic text (no terminal_answers on the row), ✅ prefix, and — legacy
        # mode — the default destination (token=None).
        mock_finalize.assert_called_once()
        args, kwargs = mock_finalize.call_args
        self.assertEqual(args[0], 555)
        self.assertEqual(args[2], "Answered in the terminal")
        self.assertEqual(kwargs["prefix"], "✅ ")
        self.assertIsNone(kwargs["token"])

    @patch("roles_config.load_bindings")
    @patch("roles_config.load_catalog", return_value=None)
    @patch("permission_request_hook.wait_for_relay_answer")
    @patch("permission_request_hook.create_request")
    @patch("permission_request_hook.get_request")
    @patch("permission_request_hook.finalize_message")
    def test_handle_ask_user_question_terminal_revokes_all_group_messages(
        self,
        mock_finalize,
        mock_get_request,
        mock_create_request,
        mock_wait_relay,
        _mock_catalog,       # load_catalog → None (legacy mode)
        _mock_bindings,      # load_bindings → MagicMock (unused in legacy mode)
    ):
        """Two questions, answered in the terminal. The PostToolUse hook only
        flips the *most recent* child (the last one) to resolved_terminal, while
        the first child's thread is still parked in a long-poll. Every sibling's
        keyboard must still be stripped — not just the last one."""
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
        # Both the first child's message (501) and the last (502) get finalized.
        # Legacy mode → token=None.
        finalized = {
            call.args[0]: call for call in mock_finalize.call_args_list
        }
        self.assertEqual(set(finalized), {501, 502})
        for call in finalized.values():
            self.assertEqual(call.args[2], "Answered in the terminal")
            self.assertEqual(call.kwargs["prefix"], "✅ ")
            self.assertIsNone(call.kwargs["token"])


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
        # reason must be at the root of the output dict, not inside decision
        self.assertNotIn("reason", dec)
        reason = out["reason"].lower()
        self.assertIn("could not be delivered", reason)
        self.assertIn("retried", reason)

    def test_delivered_but_unanswered_note(self):
        out = _auto_deny_output(_make_request(), delivery_failed=False)
        dec = out["hookSpecificOutput"]["decision"]
        self.assertEqual(dec["behavior"], "deny")
        # reason must be at the root of the output dict, not inside decision
        self.assertNotIn("reason", dec)
        reason = out["reason"].lower()
        self.assertIn("no response", reason)
        self.assertNotIn("could not be delivered", reason)

    def test_note_includes_non_whitelisted_parts(self):
        with patch.object(
            permission_request_hook,
            "_format_non_whitelisted",
            return_value="not in allowlist: cowsay",
        ):
            out = _auto_deny_output(_make_request(), delivery_failed=True)
        # reason must be at the root, not inside decision
        self.assertNotIn("reason", out["hookSpecificOutput"]["decision"])
        self.assertIn("cowsay", out["reason"])

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
        # reason must be at the root of the output dict, not inside decision
        self.assertNotIn("reason", dec)
        self.assertIn("2 different human roles", out["reason"])

    def test_deny_mixed_roles_emitted_json_shape(self):
        """Emitted JSON: reason at root, behavior inside decision, round-trips cleanly.

        This is the test the existing suite was missing. The pre-fix hook put
        reason inside hookSpecificOutput.decision, so the harness printed only
        'Permission denied by hook' to the agent instead of the actual explanation.
        This test directly asserts the wire shape — the schema the harness parses.
        """
        from permission_request_hook import build_output_decision
        _full_reason = (
            "This AskUserQuestion call addressed 2 different human roles "
            "(@ux -> UX/UI designer, @op -> Operator). "
            "Each call must target exactly one role, because a question group cannot "
            "span two Telegram chats. Split this into one AskUserQuestion call per role."
        )
        out = build_output_decision(
            {"action": "deny_mixed_roles", "reason": _full_reason},
            _make_request(),
        )
        self.assertIsNotNone(out)
        # reason must be at root, not nested inside decision
        self.assertIn("reason", out)
        self.assertNotIn("reason", out["hookSpecificOutput"]["decision"])
        # behavior is correct
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")
        # exact full reason string (dynamic part + static tail)
        self.assertEqual(out["reason"], _full_reason)
        # round-trip through JSON (exactly what stdout → harness does)
        roundtripped = json.loads(json.dumps(out))
        self.assertEqual(roundtripped["reason"], _full_reason)
        self.assertNotIn("reason", roundtripped["hookSpecificOutput"]["decision"])

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
        """When answered in the terminal, every sibling is finalized (patched
        then cancelled) through the role's token, not the default token."""
        catalog = _make_catalog()
        bindings = _make_bindings()

        from permission_state_store import RequestState

        finalize_calls = []

        with patch("roles_config.load_catalog", return_value=catalog), \
             patch("roles_config.load_bindings", return_value=bindings), \
             patch("permission_request_hook.wait_for_relay_answer",
                   return_value=None), \
             patch("permission_request_hook.get_request",
                   return_value=_make_request(
                       state=RequestState.RESOLVED_TERMINAL.value)), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.finalize_message",
                   side_effect=lambda mid, body, text, *, prefix="", token=None:
                       finalize_calls.append({"mid": mid, "token": token})), \
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
        # All finalizations must use the ux token.
        self.assertGreater(len(finalize_calls), 0)
        self.assertTrue(all(c["token"] == "rly_ux" for c in finalize_calls))


# ── Helpers shared by the wait-phase tests (15-04) ───────────────────────────

class _ScriptedRelay:
    """Deterministic stand-in for ``wait_for_relay_answer``, scripted per
    message id, so the child threads resolve in a fixed order.

    ``script[message_id]`` is a list of results returned one per poll; the last
    entry repeats forever. A result is an answer dict, a ``{"_state": ...}``
    sentinel, ``None`` (chunk timeout), or an ``Exception`` instance — which is
    raised, to exercise the thread's own error containment.
    """

    def __init__(self, script):
        self._script = {mid: list(seq) for mid, seq in script.items()}
        self._lock = threading.Lock()
        self.calls = []            # (message_id, token) in call order
        self.threads = set()       # threads that actually polled

    def __call__(self, message_id, timeout=None, long_poll_chunk=None, token=None):
        with self._lock:
            self.calls.append((message_id, token))
            self.threads.add(threading.current_thread())
            seq = self._script.get(message_id) or [None]
            result = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(result, BaseException):
            raise result
        return result


def _call_with_timeout(fn, seconds=15):
    """Run ``fn`` on a worker thread and fail if it does not finish in time.

    Every wait-loop test goes through this. A bug in the loop must fail the
    suite, not park it on the 12h TTL.

    Returns a dict with ``result`` (fn's return value) and ``thread`` (the
    thread fn ran on, so a test can assert what happened *off* it).
    """
    box = {}

    def _target():
        box["thread"] = threading.current_thread()
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            box["error"] = exc

    worker = threading.Thread(target=_target, name="wait-loop-under-test", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise AssertionError(f"wait phase did not return within {seconds}s")
    if "error" in box:
        raise box["error"]
    return box


def _call_on_main_with_timeout(fn, seconds=15):
    """Run ``fn`` on the calling thread (which must be the process main thread)
    and raise ``AssertionError`` if it does not return within ``seconds``.

    A background watchdog thread sends ``KeyboardInterrupt`` to the main thread
    after the deadline.  The caller must run on ``threading.main_thread()``; if
    it does not, the interrupt would land on the wrong thread.  New invariant
    tests use this instead of ``_call_with_timeout`` so they can assert that
    state-store writes happen on ``threading.main_thread()`` rather than on a
    ``_call_with_timeout`` worker.
    """
    import _thread as _thr

    done = threading.Event()

    def _watchdog():
        if not done.wait(seconds):
            _thr.interrupt_main()

    watchdog = threading.Thread(target=_watchdog, name="test-watchdog", daemon=True)
    watchdog.start()
    try:
        result = fn()
    except KeyboardInterrupt:
        raise AssertionError(f"wait phase did not return within {seconds}s")
    finally:
        done.set()
        watchdog.join(timeout=0.5)
    return result


class TestConcurrentWaitPhase(unittest.TestCase):
    """15-04 Part 1: one daemon thread per open child message, drained by a
    queue on the main thread.

    With one group this must be behaviour-identical to the sequential loop it
    replaced; what is newly observable is that the children are polled at the
    same time instead of one after another.
    """

    def setUp(self):
        import permission_request_hook as hook
        self.hook = hook

    def _questions(self, count):
        return [
            {"question": f"Q{i}?", "header": f"H{i}",
             "options": [{"label": f"A{i}"}, {"label": f"B{i}"}]}
            for i in range(count)
        ]

    def _run(self, count, script, *, extra_patches=(), timeout=15, floor=None):
        """Send ``count`` questions in legacy mode and run the wait phase under
        ``script``. Message ids are 801, 802, ... in question order."""
        relay = _ScriptedRelay(script)
        msg_ids = [801 + i for i in range(count)]
        children = [
            _make_request(
                request_id=f"child-{i}",
                tool_name="AskUserQuestion",
                tool_input={"question": f"Q{i}?", "header": f"H{i}",
                            "options": [{"label": f"A{i}"}, {"label": f"B{i}"}]},
            )
            for i in range(count)
        ]
        sent = iter(msg_ids)
        writes = []

        def _record_write(*args, **kwargs):
            writes.append((threading.current_thread(), args, kwargs))
            return None

        stack = [
            patch("roles_config.load_catalog", return_value=None),
            patch("roles_config.load_bindings", return_value=_make_bindings()),
            patch("permission_request_hook.create_request", side_effect=children),
            patch("permission_request_hook.send_question_message",
                  side_effect=lambda *a, **k: next(sent)),
            patch("permission_request_hook.wait_for_relay_answer", relay),
            patch("permission_request_hook.get_request",
                  side_effect=lambda rid: _make_request(request_id=rid, state="pending")),
            patch("permission_request_hook.update_request_state",
                  side_effect=_record_write),
            patch("permission_request_hook.finalize_message"),
        ]
        if floor is not None:
            stack.append(
                patch.object(self.hook, "CHILD_POLL_FLOOR_SECONDS", floor)
            )
        stack.extend(extra_patches)

        for p in stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in stack])

        box = _call_with_timeout(
            lambda: self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": self._questions(count)},
                workspace_name="ws",
            ),
            seconds=timeout,
        )
        box["relay"] = relay
        box["writes"] = writes
        box["message_ids"] = msg_ids
        return box

    def test_simultaneous_answers_are_all_collected(self):
        """A group finalizing server-side wakes every child at once, so several
        results land on the queue together. All of them must be collected."""
        answer = {"via": "button", "value": "qa0"}
        box = self._run(3, {801: [answer], 802: [answer], 803: [answer]})

        result = box["result"]
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "answer")
        self.assertEqual(
            result["updatedInput"]["answers"],
            {"Q0?": "A0", "Q1?": "A1", "Q2?": "A2"},
        )

    def test_answers_arriving_at_different_times_are_all_collected(self):
        """The last child answers on its first poll while the first two are
        still parked. Nothing may be dropped or overwritten."""
        answer = {"via": "button", "value": "qa1"}
        box = self._run(
            3,
            {801: [None, None, answer], 802: [None, answer], 803: [answer]},
            floor=0.01,
        )
        self.assertEqual(
            box["result"]["updatedInput"]["answers"],
            {"Q0?": "B0", "Q1?": "B1", "Q2?": "B2"},
        )

    def test_children_are_polled_concurrently_not_in_sequence(self):
        """The sequential loop this replaced polled child 1 until it answered
        before touching child 2. Every child must now be in flight at once."""
        answer = {"via": "button", "value": "qa0"}
        box = self._run(
            3,
            {801: [None, answer], 802: [None, answer], 803: [None, answer]},
            floor=0.25,
        )
        first_three = [mid for mid, _token in box["relay"].calls[:3]]
        self.assertEqual(set(first_three), set(box["message_ids"]))
        self.assertIsNotNone(box["result"])

    def test_a_raising_child_thread_does_not_hang_the_main_loop(self):
        """A thread body that raises must push a sentinel, not die silently —
        otherwise the main loop waits for a result that never arrives."""
        answer = {"via": "button", "value": "qa0"}
        box = self._run(
            2,
            {801: [RuntimeError("relay client exploded")], 802: [answer]},
            timeout=10,
        )
        # The group can never finalize with a dead child -> native UI stands.
        self.assertIsNone(box["result"])

    def test_every_child_expired_returns_none_and_patches_nothing(self):
        """Expired/cancelled relay messages: return None, native UI intact."""
        expired = {"_state": "expired"}
        box = self._run(2, {801: [expired], 802: [expired]})
        self.assertIsNone(box["result"])
        self.hook.finalize_message.assert_not_called()

    def test_one_cancelled_child_kills_the_only_group(self):
        """A group whose member is cancelled can never finalize server-side
        (``_finalize_group_if_complete`` needs every member), so the hook falls
        back to the terminal — exactly as the sequential loop did."""
        box = self._run(
            2,
            {801: [{"_state": "cancelled"}], 802: [None]},
            timeout=10,
            floor=0.01,
        )
        self.assertIsNone(box["result"])

    def test_no_state_store_writes_happen_off_the_main_thread(self):
        """Terminal detection, state writes and the decision all stay on the
        thread that owns the hook; the child threads only long-poll."""
        answer = {"via": "button", "value": "qa0"}
        box = self._run(3, {801: [answer], 802: [answer], 803: [answer]})

        main_thread = box["thread"]
        self.assertEqual(len(box["writes"]), 3)
        for thread, _args, _kwargs in box["writes"]:
            self.assertIs(thread, main_thread)
        # ... and the polling really did happen elsewhere (otherwise the
        # assertion above would be vacuous).
        self.assertTrue(box["relay"].threads)
        self.assertNotIn(main_thread, box["relay"].threads)

    def test_resolve_via_terminal_does_not_happen_off_the_main_thread(self):
        """``_finalize_on_terminal_win`` sweeps still-pending siblings with
        ``resolve_via_terminal``; that write must not migrate to a child
        polling thread.

        Uses ``_call_on_main_with_timeout`` so the assertion target is
        ``threading.main_thread()`` rather than whichever worker thread
        ``_call_with_timeout`` would pick — the pin stays correct even if the
        suite is later driven from a non-main thread.
        """
        self.assertIs(
            threading.current_thread(), threading.main_thread(),
            "This invariant test must be driven from the main thread",
        )
        from permission_state_store import RequestState

        resolve_threads = []

        def _tracking_resolve(request_id, terminal_answers=None):
            resolve_threads.append(threading.current_thread())

        def _row(request_id):
            # child-0: PostToolUse already resolved it → triggers terminal-win
            # detection in _any_child_resolved_in_terminal.
            # child-1: still pending → triggers the resolve_via_terminal sweep
            # inside _finalize_on_terminal_win.
            state = (
                RequestState.RESOLVED_TERMINAL.value
                if request_id == "child-0"
                else "pending"
            )
            return _make_request(request_id=request_id, state=state)

        children_iter = iter([
            _make_request(
                request_id="child-0",
                tool_name="AskUserQuestion",
                tool_input={"question": "Q0?", "header": "H0", "options": []},
            ),
            _make_request(
                request_id="child-1",
                tool_name="AskUserQuestion",
                tool_input={"question": "Q1?", "header": "H1", "options": []},
            ),
        ])
        msg_ids = iter([801, 802])

        stack = [
            patch("roles_config.load_catalog", return_value=None),
            patch("roles_config.load_bindings", return_value=_make_bindings()),
            patch("permission_request_hook.create_request",
                  side_effect=children_iter),
            patch("permission_request_hook.send_question_message",
                  side_effect=lambda *a, **k: next(msg_ids)),
            patch("permission_request_hook.wait_for_relay_answer", return_value=None),
            patch("permission_request_hook.get_request", side_effect=_row),
            patch("permission_request_hook.update_request_state"),
            patch("permission_request_hook.resolve_via_terminal",
                  side_effect=_tracking_resolve),
            patch("permission_request_hook.finalize_message"),
            patch("telegram_permission_router.set_telegram_message_id"),
            patch.object(self.hook, "CHILD_POLL_FLOOR_SECONDS", 0.01),
            patch.object(self.hook, "WAIT_TICK_SECONDS", 0.05),
        ]
        for p in stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in stack])

        _call_on_main_with_timeout(
            lambda: self.hook.handle_ask_user_question(
                session_id="sess",
                cwd="/tmp/ws",
                tool_input={
                    "questions": [
                        {"question": "Q0?", "header": "H0", "options": []},
                        {"question": "Q1?", "header": "H1", "options": []},
                    ]
                },
                workspace_name="ws",
            ),
            seconds=10,
        )

        self.assertGreater(
            len(resolve_threads), 0,
            "no resolve_via_terminal calls recorded — test is vacuous",
        )
        for t in resolve_threads:
            self.assertIs(
                t,
                threading.main_thread(),
                f"resolve_via_terminal called on {t.name!r}; expected main thread",
            )


class TestTerminalAnswerPropagation(unittest.TestCase):
    """15-04 Part 2: whoever was asked sees what the terminal decided, not a
    dead prompt (brd §5.5)."""

    ANSWERS = {
        "Sidebar or top nav?": "Sidebar",
        "Which database?": "(notes only)",
    }
    NOTES = {"Which database?": "Postgres, but only once we shard."}

    def setUp(self):
        import permission_request_hook as hook
        self.hook = hook
        self.stored = json.dumps({"answers": self.ANSWERS, "notes": self.NOTES})

    def _questions(self):
        return [
            {"question": "Sidebar or top nav?", "header": "@ux Layout",
             "options": [{"label": "Sidebar"}]},
            {"question": "Which database?", "header": "@ux Storage",
             "options": [{"label": "Postgres"}]},
        ]

    def _run_terminal_win(self, stored, *, roles=True):
        """Terminal-resolved rows for both children; returns the finalize calls."""
        from permission_state_store import RequestState

        questions = self._questions()
        created = iter([
            _make_request(
                request_id=f"child-{i + 1}",
                tool_name="AskUserQuestion",
                tool_input={
                    "question": q["question"],
                    "header": q.get("header", ""),
                    "options": q.get("options", []),
                },
            )
            for i, q in enumerate(questions)
        ])
        msg_ids = iter([901, 902])
        finalize_calls = []

        def _row(request_id):
            return _make_request(
                request_id=request_id,
                state=RequestState.RESOLVED_TERMINAL.value,
                terminal_answers=stored,
            )

        catalog = _make_catalog() if roles else None
        stack = [
            patch("roles_config.load_catalog", return_value=catalog),
            patch("roles_config.load_bindings", return_value=_make_bindings()),
            patch("permission_request_hook.create_request", side_effect=created),
            patch("permission_request_hook.send_question_message",
                  side_effect=lambda *a, **k: next(msg_ids)),
            patch("permission_request_hook.wait_for_relay_answer", return_value=None),
            patch("permission_request_hook.get_request", side_effect=_row),
            patch("permission_request_hook.update_request_state"),
            patch("permission_request_hook.finalize_message",
                  side_effect=lambda mid, body, text, *, prefix="", token=None:
                      finalize_calls.append(
                          {"mid": mid, "body": body, "text": text,
                           "prefix": prefix, "token": token})),
            patch("telegram_permission_router.set_telegram_message_id"),
        ]
        for p in stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in stack])

        box = _call_with_timeout(
            lambda: self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": self._questions()},
                workspace_name="leangeeks",
            )
        )
        self.assertIsNone(box["result"])  # terminal wins -> native UI acted
        return finalize_calls

    def test_each_message_is_patched_with_its_own_answer(self):
        calls = {c["mid"]: c for c in self._run_terminal_win(self.stored)}
        self.assertEqual(set(calls), {901, 902})
        self.assertEqual(calls[901]["text"], "Sidebar")
        # (notes only) is a placeholder — the notes are rendered instead.
        self.assertEqual(calls[902]["text"], self.NOTES["Which database?"])

    def test_patch_uses_the_check_prefix_and_the_roles_token(self):
        for call in self._run_terminal_win(self.stored):
            self.assertEqual(call["prefix"], "✅ ")
            self.assertEqual(call["token"], "rly_ux")
            # The body kept by 15-03 is reused verbatim, so the PATCH does not
            # lose the question the reader is looking at.
            self.assertIn("Question", call["body"])

    def test_unparseable_terminal_answers_still_patch_the_generic_text(self):
        for stored in ("{not json", "", None, "Tool ran without output"):
            with self.subTest(stored=stored):
                calls = self._run_terminal_win(stored)
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertEqual(call["text"], "Answered in the terminal")
                    self.assertEqual(call["prefix"], "✅ ")

    def test_a_partial_answer_map_leaves_the_others_generic(self):
        stored = json.dumps({"answers": {"Sidebar or top nav?": "Sidebar"}, "notes": {}})
        calls = {c["mid"]: c for c in self._run_terminal_win(stored)}
        self.assertEqual(calls[901]["text"], "Sidebar")
        self.assertEqual(calls[902]["text"], "Answered in the terminal")

    def test_terminal_answers_are_read_from_whichever_row_carries_them(self):
        """PostToolUse only ever flips one row, and not necessarily the child
        that detected the terminal state."""
        from permission_state_store import RequestState

        msg_ids = iter([901, 902])
        finalize_calls = []

        def _row(request_id):
            # Only the *second* child carries the recorded answers.
            return _make_request(
                request_id=request_id,
                state=RequestState.RESOLVED_TERMINAL.value,
                terminal_answers=self.stored if request_id.endswith("2") else None,
            )

        created = iter([
            _make_request(request_id="child-1", tool_name="AskUserQuestion",
                          tool_input={"question": "Sidebar or top nav?"}),
            _make_request(request_id="child-2", tool_name="AskUserQuestion",
                          tool_input={"question": "Which database?"}),
        ])

        with patch("roles_config.load_catalog", return_value=None), \
             patch("roles_config.load_bindings", return_value=_make_bindings()), \
             patch("permission_request_hook.create_request", side_effect=created), \
             patch("permission_request_hook.send_question_message",
                   side_effect=lambda *a, **k: next(msg_ids)), \
             patch("permission_request_hook.wait_for_relay_answer", return_value=None), \
             patch("permission_request_hook.get_request", side_effect=_row), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.finalize_message",
                   side_effect=lambda mid, body, text, *, prefix="", token=None:
                       finalize_calls.append({"mid": mid, "text": text})):
            box = _call_with_timeout(
                lambda: self.hook.handle_ask_user_question(
                    session_id="sess", cwd="/tmp/ws",
                    tool_input={"questions": self._questions()},
                    workspace_name="leangeeks",
                )
            )

        self.assertIsNone(box["result"])
        texts = {c["mid"]: c["text"] for c in finalize_calls}
        self.assertEqual(texts[901], "Sidebar")
        self.assertEqual(texts[902], self.NOTES["Which database?"])

    def test_still_pending_siblings_are_swept_without_blanking_the_answers(self):
        """The sweep passes no ``terminal_answers`` so it cannot overwrite what
        PostToolUse recorded a moment earlier."""
        from permission_state_store import RequestState

        msg_ids = iter([901, 902])
        swept = []

        def _row(request_id):
            # child-1 is still pending; only child-2 was flipped by PostToolUse.
            if request_id.endswith("1"):
                return _make_request(request_id=request_id, state="pending")
            return _make_request(
                request_id=request_id,
                state=RequestState.RESOLVED_TERMINAL.value,
                terminal_answers=self.stored,
            )

        created = iter([
            _make_request(request_id="child-1", tool_name="AskUserQuestion"),
            _make_request(request_id="child-2", tool_name="AskUserQuestion"),
        ])

        with patch("roles_config.load_catalog", return_value=None), \
             patch("roles_config.load_bindings", return_value=_make_bindings()), \
             patch("permission_request_hook.create_request", side_effect=created), \
             patch("permission_request_hook.send_question_message",
                   side_effect=lambda *a, **k: next(msg_ids)), \
             patch("permission_request_hook.wait_for_relay_answer", return_value=None), \
             patch("permission_request_hook.get_request", side_effect=_row), \
             patch("permission_request_hook.update_request_state"), \
             patch("permission_request_hook.finalize_message"), \
             patch("permission_request_hook.resolve_via_terminal",
                   side_effect=lambda rid, *a, **k: swept.append((rid, a, k))):
            _call_with_timeout(
                lambda: self.hook.handle_ask_user_question(
                    session_id="sess", cwd="/tmp/ws",
                    tool_input={"questions": self._questions()},
                    workspace_name="leangeeks",
                )
            )

        self.assertEqual([rid for rid, _a, _k in swept], ["child-1"])
        for _rid, args, kwargs in swept:
            self.assertEqual(args, ())
            self.assertEqual(kwargs, {})

    def test_no_roles_configured_still_strips_the_keyboard(self):
        """The compatibility floor: with no roles.toml a terminal win behaves as
        before except that the body now carries the answer. Exercised through
        the *real* ``finalize_message`` against a fake relay client, so the
        PATCH-then-cancel really happens."""
        import telegram_permission_router as tpr
        from permission_state_store import RequestState

        fake = MagicMock(name="default-client")
        child = _make_request(
            request_id="child-1",
            tool_name="AskUserQuestion",
            tool_input={"question": "Sidebar or top nav?", "header": "Layout",
                        "options": [{"label": "Sidebar"}]},
        )
        row = _make_request(
            request_id="child-1",
            state=RequestState.RESOLVED_TERMINAL.value,
            terminal_answers=self.stored,
        )

        patches = _router_state(
            tpr,
            _default_token="__td__",
            _clients={"__td__": fake},
            _server_url="https://relay.example",
        ) + [
            patch("roles_config.load_catalog", return_value=None),
            patch("roles_config.load_bindings", return_value=_make_bindings()),
            patch("permission_request_hook.create_request", return_value=child),
            patch("permission_request_hook.send_question_message", return_value=903),
            patch("permission_request_hook.wait_for_relay_answer", return_value=None),
            patch("permission_request_hook.get_request", return_value=row),
            patch("permission_request_hook.update_request_state"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        box = _call_with_timeout(
            lambda: self.hook.handle_ask_user_question(
                session_id="sess", cwd="/tmp/ws",
                tool_input={"questions": [
                    {"question": "Sidebar or top nav?", "header": "Layout",
                     "options": [{"label": "Sidebar"}]}
                ]},
                workspace_name="leangeeks",
            )
        )

        self.assertIsNone(box["result"])
        # PATCH carries the answer, then the keyboard comes down — in that order.
        self.assertEqual(
            [name for name, _a, _k in fake.mock_calls],
            ["edit_message", "cancel_message"],
        )
        self.assertTrue(
            fake.edit_message.call_args.kwargs["text"].endswith("\n\n✅ Sidebar")
        )
        fake.cancel_message.assert_called_once_with(903)


# ── Helpers shared by the escalation tests (15-05) ───────────────────────────

def _esc_catalog(escalate_after):
    """Catalog whose ``ux`` role carries an escalation policy."""
    import roles_config as rc
    return _make_catalog(roles={
        "operator": rc.Role(
            role_id="operator", title="Operator",
            aliases=("operator", "op"), escalate_after=None,
        ),
        "ux": rc.Role(
            role_id="ux", title="UX/UI designer",
            aliases=("ux", "design"), escalate_after=escalate_after,
        ),
    })


class _SendCapture:
    """Records every ``_send_relay`` call and hands out message ids per group.

    The first group sent gets ids 801, 802, …; the second (the escalation
    duplicate) 901, 902, …  ``gate`` is set the moment a second group id
    appears, which is what lets a test hold the role group's answers back until
    the escalation group is really live.

    ``fail_token`` makes every send to that destination fail, for the
    failed-escalation case.
    """

    def __init__(self, fail_token=None):
        self.calls = []
        self.gate = threading.Event()
        self.fail_token = fail_token
        self._bases = {}
        self._next_base = 801

    def __call__(self, **kw):
        gid = kw.get("group_id")
        if gid not in self._bases:
            self._bases[gid] = self._next_base
            self._next_base += 100
            if len(self._bases) > 1:
                self.gate.set()
        kw = dict(kw)
        kw["message_id"] = None
        self.calls.append(kw)
        if self.fail_token is not None and kw.get("token") == self.fail_token:
            return None
        sent = sum(
            1 for c in self.calls
            if c.get("group_id") == gid and c["message_id"] is not None
        )
        kw["message_id"] = self._bases[gid] + sent
        return kw["message_id"]

    def for_group(self, index):
        """Calls belonging to the ``index``-th distinct group, in send order."""
        gids = list(self._bases)
        gid = gids[index]
        return [c for c in self.calls if c.get("group_id") == gid]


class _GatedRelay:
    """``wait_for_relay_answer`` double built for the two-group race.

    ``answers[mid]`` is what that message eventually returns (an answer dict or
    a ``{"_state": …}`` sentinel); a message id with no entry never answers.
    Ids listed in ``gated`` withhold their result — returning a chunk timeout,
    so the child thread keeps polling — until ``gate`` is set.
    """

    def __init__(self, answers, gate=None, gated=()):
        self.answers = dict(answers)
        self.gate = gate
        self.gated = set(gated)
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, message_id, timeout=None, long_poll_chunk=None, token=None):
        with self._lock:
            self.calls.append((message_id, token))
        if message_id in self.gated and (self.gate is None or not self.gate.is_set()):
            return None
        return self.answers.get(message_id)


class TestHumanizedDuration(unittest.TestCase):
    """The banner is read by a human: ``parse_duration`` hands back seconds, so
    the ``15m`` an operator wrote must come back out as ``15m``."""

    def setUp(self):
        import permission_request_hook as hook
        self.hook = hook

    def test_minutes_hours_and_mixtures(self):
        cases = {
            900.0: "15m",      # the case brd §5.4 and the task both name
            1800: "30m",
            3600.0: "1h",
            5400: "1h30m",
            45: "45s",
            90: "1m30s",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(self.hook._humanize_duration(seconds), expected)

    def test_sub_second_values_keep_a_fraction(self):
        self.assertEqual(self.hook._humanize_duration(0.25), "0.25s")

    def test_garbage_never_raises(self):
        self.assertEqual(self.hook._humanize_duration("nonsense"), "nonsense")


class TestEscalation(unittest.TestCase):
    """15-05: the escalation deadline, the duplicate group, and the race
    between two live groups."""

    QUESTIONS = [
        {"question": "Sidebar or top nav?", "header": "@ux Layout",
         "options": [{"label": "Sidebar"}, {"label": "Top nav"}]},
        {"question": "Which accent colour?", "header": "@ux Colour",
         "options": [{"label": "Blue"}, {"label": "Green"}]},
    ]
    ANSWER = {"via": "button", "value": "qa0"}
    VERBATIM = {"Sidebar or top nav?": "Sidebar", "Which accent colour?": "Blue"}

    def setUp(self):
        import permission_request_hook as hook
        self.hook = hook

    def _run(
        self,
        relay,
        *,
        sends=None,
        escalate_after=0.2,
        catalog=None,
        bindings=None,
        questions=None,
        rows=None,
        extra_patches=(),
        timeout=15,
        use_main_thread=False,
    ):
        """Run ``handle_ask_user_question`` for a @ux-tagged call under ``relay``.

        Nothing here touches the network, a real ``RelayClient`` or a real
        ``roles.toml``: the catalog, the bindings, the relay send and the
        long-poll are all doubles.

        ``use_main_thread=True`` drives ``handle_ask_user_question`` directly on
        the calling thread (which must be ``threading.main_thread()``) using
        ``_call_on_main_with_timeout`` instead of spawning a worker.  Invariant
        tests that assert against ``threading.main_thread()`` pass this flag.
        """
        questions = self.QUESTIONS if questions is None else questions
        catalog = _esc_catalog(escalate_after) if catalog is None else catalog
        bindings = _make_bindings() if bindings is None else bindings
        sends = _SendCapture() if sends is None else sends

        created = []
        writes = []
        finalized = []

        def _create(**kw):
            row = _make_request(
                request_id=f"child-{len(created)}",
                tool_name="AskUserQuestion",
                tool_input=kw["tool_input"],
                role=kw.get("role"),
            )
            created.append(row)
            return row

        def _row(request_id):
            if rows is not None:
                return rows(request_id)
            return _make_request(request_id=request_id, state="pending")

        stack = [
            patch("roles_config.load_catalog", return_value=catalog),
            patch("roles_config.load_bindings", return_value=bindings),
            patch("permission_request_hook.create_request", side_effect=_create),
            patch("permission_request_hook.wait_for_relay_answer", relay),
            patch("permission_request_hook.get_request", side_effect=_row),
            patch("permission_request_hook.update_request_state",
                  side_effect=lambda rid, state, **kw: writes.append((rid, state, kw))),
            patch("permission_request_hook.resolve_via_terminal"),
            patch("permission_request_hook.remove_inline_buttons"),
            patch("permission_request_hook.finalize_message",
                  side_effect=lambda mid, body, text, *, prefix="", token=None:
                      finalized.append({"mid": mid, "body": body, "text": text,
                                        "prefix": prefix, "token": token})),
            patch("telegram_permission_router._send_relay", side_effect=sends),
            patch("telegram_permission_router.set_telegram_message_id"),
            patch.object(self.hook, "CHILD_POLL_FLOOR_SECONDS", 0.05),
        ]
        stack.extend(extra_patches)
        for p in stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in stack])

        fn = lambda: self.hook.handle_ask_user_question(
            session_id="sess", cwd="/tmp/ws",
            tool_input={"questions": questions},
            workspace_name="leangeeks",
        )
        if use_main_thread:
            result = _call_on_main_with_timeout(fn, seconds=timeout)
            box = {"result": result, "thread": threading.main_thread()}
        else:
            box = _call_with_timeout(fn, seconds=timeout)
        box.update(sends=sends, relay=relay, writes=writes,
                   finalized=finalized, created=created)
        return box

    def _run_with_captured_seam(self, **kwargs):
        """Run with the wait loop replaced by a probe.

        Gives a test the exact ``escalate_at`` the hook armed and, on demand,
        runs the escalation send synchronously — so the send path can be
        asserted against a realistic ``15m`` policy without waiting 15 minutes.
        """
        seam = {}
        fire = kwargs.pop("fire", False)

        def _probe(groups, group_tokens, deadline, *,
                   escalate_at=None, send_escalation=None):
            seam["escalate_at"] = escalate_at
            seam["deadline"] = deadline
            seam["groups"] = dict(groups)
            if escalate_at is not None and fire:
                seam["added"] = send_escalation()
            return None

        started = time.time()
        box = self._run(
            _GatedRelay({}),
            extra_patches=[
                patch.object(self.hook, "_wait_for_group_answers", side_effect=_probe)
            ],
            **kwargs,
        )
        seam["started"] = started
        box["seam"] = seam
        return box

    # ── 1. The escalation deadline ────────────────────────────────────────────

    def test_no_escalate_after_sends_one_group_and_never_escalates(self):
        """Decision 5: waiting is unbounded by default. With no policy the call
        behaves exactly as it did under 15-04, however long it waits."""
        relay = _ScriptedRelay({
            801: [None] * 6 + [self.ANSWER],
            802: [None] * 6 + [self.ANSWER],
        })
        box = self._run(relay, escalate_after=None)

        self.assertEqual(box["result"]["updatedInput"]["answers"], self.VERBATIM)
        self.assertEqual(len(box["sends"].calls), 2)
        self.assertEqual(len({c["group_id"] for c in box["sends"].calls}), 1)
        self.assertTrue(all(c["token"] == "rly_ux" for c in box["sends"].calls))
        self.assertTrue(all("⏳" not in c["text"] for c in box["sends"].calls))
        self.assertEqual(box["finalized"], [])

    def test_escalate_after_arms_a_deadline_at_start_plus_duration(self):
        box = self._run_with_captured_seam(escalate_after=900.0)
        seam = box["seam"]
        self.assertIsNotNone(seam["escalate_at"])
        self.assertAlmostEqual(seam["escalate_at"] - seam["started"], 900.0, delta=5)
        # …and it sits inside the TTL window the loop waits in.
        self.assertLess(seam["escalate_at"], seam["deadline"])

    def test_escalate_after_beyond_the_ttl_window_is_suppressed(self):
        box = self._run_with_captured_seam(
            escalate_after=self.hook.REQUEST_TTL + 60
        )
        self.assertIsNone(box["seam"]["escalate_at"])

    def test_the_wait_loop_is_driven_by_the_queue_not_by_sleep(self):
        """A ``time.sleep``-driven tick would let the deadline drift by a whole
        long-poll chunk; the loop must wake on the queue instead."""
        src = inspect.getsource(self.hook._wait_for_group_answers)
        self.assertIn("results.get(timeout=", src)
        self.assertNotIn("time.sleep", src)

    # ── 2. Sending the escalation group ───────────────────────────────────────

    def test_escalation_group_is_a_full_duplicate_to_the_default_role(self):
        """Exactly ``len(questions)`` messages, a new group id, the right
        group_total, the default role's token and title, and the ⏳ banner on
        **every** message — whichever one the operator opens must explain
        itself."""
        box = self._run_with_captured_seam(escalate_after=900.0, fire=True)
        sends = box["sends"]

        role_calls = sends.for_group(0)
        esc_calls = sends.for_group(1)
        self.assertEqual(len(role_calls), 2)
        self.assertEqual(len(esc_calls), len(self.QUESTIONS))
        self.assertNotEqual(role_calls[0]["group_id"], esc_calls[0]["group_id"])

        banner = ("⏳ @ux (UX/UI designer) hasn't answered in 15m — "
                  "you can decide instead.")
        for call in esc_calls:
            self.assertEqual(call["token"], "rly_op")
            self.assertEqual(call["group_total"], len(self.QUESTIONS))
            self.assertIn(banner, call["text"])
            # role_title is the DEFAULT role's — this copy is addressed to the
            # operator, not to the designer.
            self.assertIn("for: Operator", call["text"])
        # …and the role group never carried a banner.
        self.assertTrue(all("⏳" not in c["text"] for c in role_calls))

        # Same cleaned headers, options and questions as the original.
        self.assertEqual(
            [c["text"].count("Sidebar") for c in role_calls],
            [c["text"].count("Sidebar") for c in esc_calls],
        )
        # Fresh request ids → distinct req:{request_id}:send idempotency keys.
        self.assertFalse(
            {c["request_id"] for c in role_calls} & {c["request_id"] for c in esc_calls}
        )
        # New rows carry the default role id.
        self.assertEqual([r.role for r in box["created"][2:]], ["operator", "operator"])

    def test_escalation_banner_uses_the_alias_the_agent_actually_wrote(self):
        """``@design`` is an alias of the same role; the banner must quote what
        the agent typed, paired with the role's title."""
        questions = [dict(q, header=q["header"].replace("@ux", "@design"))
                     for q in self.QUESTIONS]
        box = self._run_with_captured_seam(
            escalate_after=900.0, fire=True, questions=questions
        )
        for call in box["sends"].for_group(1):
            self.assertIn(
                "⏳ @design (UX/UI designer) hasn't answered in 15m", call["text"]
            )

    def test_unreachable_default_role_cannot_be_escalated_to(self):
        """No binding for the default role → the send is skipped, the role group
        stays live, nothing is retried."""
        box = self._run_with_captured_seam(
            escalate_after=900.0, fire=True,
            bindings=_make_bindings(default_token=None, roles={"ux": "rly_ux"}),
        )
        self.assertIsNone(box["seam"]["added"])
        self.assertEqual(len(box["sends"].calls), 2)  # the role group only

    # ── 3. Deciding between two groups ────────────────────────────────────────

    def test_role_group_wins_answers_verbatim_escalation_finalized(self):
        sends = _SendCapture()
        relay = _GatedRelay(
            {801: self.ANSWER, 802: self.ANSWER},
            gate=sends.gate, gated=(801, 802),
        )
        box = self._run(relay, sends=sends)

        # Both groups really were live before the race was decided.
        self.assertEqual(len(box["sends"].calls), 4)
        # brd §5.6: the tagged role answered, so nothing is attributed.
        self.assertEqual(box["result"]["updatedInput"]["answers"], self.VERBATIM)

        # Only the loser needs client-side cleanup; the winner finalized
        # itself server-side.
        patched = {c["mid"]: c for c in box["finalized"]}
        self.assertEqual(set(patched), {901, 902})
        self.assertEqual(patched[901]["text"], "Sidebar")   # paired by index
        self.assertEqual(patched[902]["text"], "Blue")
        for call in patched.values():
            self.assertEqual(call["prefix"], "✅ ")
            self.assertEqual(call["token"], "rly_op")
            self.assertIn("Question", call["body"])

        # Every row of BOTH groups is terminal, so posttool_hook's pending
        # sweep skips them all.
        replied = {rid for rid, state, _kw in box["writes"]
                   if state is RequestState.REPLY}
        self.assertEqual(replied, {"child-0", "child-1", "child-2", "child-3"})

    def test_escalation_group_wins_answers_attributed_role_finalized(self):
        sends = _SendCapture()
        relay = _GatedRelay({901: self.ANSWER, 902: self.ANSWER})
        box = self._run(relay, sends=sends)

        # brd §5.6: the operator decided, and the agent is told so.
        self.assertEqual(
            box["result"]["updatedInput"]["answers"],
            {"Sidebar or top nav?": "Sidebar (answered by Operator)",
             "Which accent colour?": "Blue (answered by Operator)"},
        )

        patched = {c["mid"]: c for c in box["finalized"]}
        self.assertEqual(set(patched), {801, 802})
        # The chat gets the UNANNOTATED answer — the suffix is for the agent.
        self.assertEqual(patched[801]["text"], "Sidebar")
        self.assertEqual(patched[802]["text"], "Blue")
        for call in patched.values():
            self.assertEqual(call["prefix"], "✅ ")
            self.assertEqual(call["token"], "rly_ux")

        replied = {rid for rid, state, _kw in box["writes"]
                   if state is RequestState.REPLY}
        self.assertEqual(replied, {"child-0", "child-1", "child-2", "child-3"})

    def test_a_partially_answered_losing_group_is_still_fully_finalized(self):
        """One of the designer's two questions was answered before the operator
        took over: both of their messages must still be patched and cancelled."""
        sends = _SendCapture()
        relay = _GatedRelay({801: self.ANSWER, 901: self.ANSWER, 902: self.ANSWER})
        box = self._run(relay, sends=sends)

        self.assertEqual(
            box["result"]["updatedInput"]["answers"],
            {"Sidebar or top nav?": "Sidebar (answered by Operator)",
             "Which accent colour?": "Blue (answered by Operator)"},
        )
        patched = {c["mid"] for c in box["finalized"]}
        self.assertEqual(patched, {801, 802})
        replied = {rid for rid, state, _kw in box["writes"]
                   if state is RequestState.REPLY}
        self.assertEqual(replied, {"child-0", "child-1", "child-2", "child-3"})

    def test_terminal_resolution_cancels_both_groups(self):
        """The terminal beats every group (brd §5.5) — including one sent
        seconds earlier by the escalation."""
        sends = _SendCapture()

        def _rows(request_id):
            # Answered at the keyboard only once the escalation group is live.
            state = (RequestState.RESOLVED_TERMINAL.value
                     if sends.gate.is_set() else "pending")
            return _make_request(request_id=request_id, state=state)

        box = self._run(_GatedRelay({}), sends=sends, rows=_rows)

        self.assertIsNone(box["result"])          # native UI acted
        self.assertEqual(len(box["sends"].calls), 4)
        by_mid = {c["mid"]: c for c in box["finalized"]}
        self.assertEqual(set(by_mid), {801, 802, 901, 902})
        self.assertEqual(by_mid[801]["token"], "rly_ux")
        self.assertEqual(by_mid[901]["token"], "rly_op")

    def test_one_group_expiring_does_not_end_the_call(self):
        """15-04 read "a dead child kills the call"; with two groups a dead
        group is only that group losing."""
        sends = _SendCapture()
        expired = {"_state": "expired"}
        relay = _GatedRelay(
            {801: expired, 802: expired, 901: self.ANSWER, 902: self.ANSWER},
            gate=sends.gate, gated=(801, 802),
        )
        box = self._run(relay, sends=sends)

        self.assertEqual(
            box["result"]["updatedInput"]["answers"],
            {"Sidebar or top nav?": "Sidebar (answered by Operator)",
             "Which accent colour?": "Blue (answered by Operator)"},
        )

    def test_both_groups_expiring_returns_none_and_leaves_the_native_ui(self):
        sends = _SendCapture()
        expired = {"_state": "expired"}
        relay = _GatedRelay(
            {801: expired, 802: expired, 901: expired, 902: expired},
            gate=sends.gate, gated=(801, 802),
        )
        box = self._run(relay, sends=sends)

        self.assertIsNone(box["result"])
        self.assertEqual(len(box["sends"].calls), 4)   # escalation did fire
        self.assertEqual(box["finalized"], [])         # nothing to patch

    def test_a_failed_escalation_send_leaves_the_role_group_live(self):
        """brd §5.1: log it, keep waiting on the role group, never retry."""
        sends = _SendCapture(fail_token="rly_op")
        relay = _GatedRelay(
            {801: self.ANSWER, 802: self.ANSWER},
            gate=sends.gate, gated=(801, 802),
        )
        box = self._run(relay, sends=sends)

        # The call still resolves through the role group, unattributed.
        self.assertEqual(box["result"]["updatedInput"]["answers"], self.VERBATIM)
        # Exactly one escalation attempt, no retry.
        self.assertEqual(
            len([c for c in box["sends"].calls if c["token"] == "rly_op"]), 1
        )
        # No loser to clean up.
        self.assertEqual(box["finalized"], [])

    # ── Invariants ────────────────────────────────────────────────────────────

    def test_no_state_store_writes_happen_off_the_hook_thread_two_group(self):
        """Off-main-thread write invariant with TWO groups live (MEDIUM pin).

        Child threads only push results onto the queue; the hook's own loop
        thread is the only place that drains it and calls update_request_state.
        This test would fail immediately if a write were moved into
        _child_wait_loop or any other off-loop thread.
        """
        thread_writes = []

        def _record_write(rid, state, **kw):
            thread_writes.append(threading.current_thread())

        sends = _SendCapture()
        # Escalation group wins so both groups are fully live during the race.
        relay = _GatedRelay({901: self.ANSWER, 902: self.ANSWER})
        box = self._run(
            relay,
            sends=sends,
            extra_patches=[
                patch(
                    "permission_request_hook.update_request_state",
                    side_effect=_record_write,
                ),
            ],
        )

        hook_thread = box["thread"]  # the thread that owns the hook's main loop
        self.assertGreater(
            len(thread_writes), 0, "no writes recorded — test is vacuous"
        )
        for t in thread_writes:
            self.assertIs(
                t, hook_thread,
                f"state-store write on {t.name!r}; expected {hook_thread.name!r}",
            )

    def test_create_request_does_not_happen_off_the_main_thread(self):
        """``_send_escalation_group`` calls ``create_request`` for each question
        in the duplicate group; those writes must not migrate to a child
        polling thread.

        Asserts against ``threading.main_thread()`` so the pin holds even if
        the suite is later driven from a non-main thread.
        """
        self.assertIs(
            threading.current_thread(), threading.main_thread(),
            "This invariant test must be driven from the main thread",
        )

        create_threads = []
        created_rows = []

        def _tracking_create(**kw):
            create_threads.append(threading.current_thread())
            row = _make_request(
                request_id=f"child-{len(created_rows)}",
                tool_name="AskUserQuestion",
                tool_input=kw["tool_input"],
                role=kw.get("role"),
            )
            created_rows.append(row)
            return row

        sends = _SendCapture()
        # Escalation group (901, 902) answers immediately; role group never does.
        relay = _GatedRelay({901: self.ANSWER, 902: self.ANSWER})
        box = self._run(
            relay,
            sends=sends,
            escalate_after=0.05,
            use_main_thread=True,
            extra_patches=[
                patch(
                    "permission_request_hook.create_request",
                    side_effect=_tracking_create,
                ),
            ],
        )

        # Escalation fires → _try_send_group → create_request for each question.
        # Role-group creates (before the wait phase) are also on the main thread.
        self.assertGreater(
            len(create_threads), 0,
            "no create_request calls recorded — test is vacuous",
        )
        for t in create_threads:
            self.assertIs(
                t,
                threading.main_thread(),
                f"create_request called on {t.name!r}; expected main thread",
            )

    def test_set_telegram_message_id_does_not_happen_off_the_main_thread(self):
        """When the escalation group is sent, ``send_question_message`` calls
        ``set_telegram_message_id`` on each new row; that write must not migrate
        to a child polling thread.

        Asserts against ``threading.main_thread()`` so the pin holds even if
        the suite is later driven from a non-main thread.
        """
        self.assertIs(
            threading.current_thread(), threading.main_thread(),
            "This invariant test must be driven from the main thread",
        )

        msg_id_threads = []

        def _tracking_set_msg_id(request_id, message_id):
            msg_id_threads.append(threading.current_thread())

        sends = _SendCapture()
        # Escalation group (901, 902) answers immediately; role group never does.
        relay = _GatedRelay({901: self.ANSWER, 902: self.ANSWER})
        box = self._run(
            relay,
            sends=sends,
            escalate_after=0.05,
            use_main_thread=True,
            extra_patches=[
                patch(
                    "telegram_permission_router.set_telegram_message_id",
                    side_effect=_tracking_set_msg_id,
                ),
            ],
        )

        # All sends (initial role group + escalation group) call set_telegram_message_id.
        self.assertGreater(
            len(msg_id_threads), 0,
            "no set_telegram_message_id calls recorded — test is vacuous",
        )
        for t in msg_id_threads:
            self.assertIs(
                t,
                threading.main_thread(),
                f"set_telegram_message_id called on {t.name!r}; expected main thread",
            )

    def test_tick_shrinks_toward_the_escalation_deadline(self):
        """The queue-drain timeout shrinks toward a pending escalation deadline
        so the send is not delayed by up to WAIT_TICK_SECONDS (task §1).

        With WAIT_TICK_SECONDS=2.0 and escalate_after=0.15, a non-shrinking
        loop would block for 2 s before firing — this asserts at least one
        tick is smaller than the full WAIT_TICK_SECONDS cap.
        """
        import queue as _queue

        ticks = []
        orig_get = _queue.Queue.get

        def _recording_get(self_q, block=True, timeout=None):
            if timeout is not None:
                ticks.append(timeout)
            return orig_get(self_q, block=block, timeout=timeout)

        sends = _SendCapture()
        relay = _GatedRelay({901: self.ANSWER, 902: self.ANSWER})
        box = self._run(
            relay,
            sends=sends,
            escalate_after=0.15,
            extra_patches=[patch.object(_queue.Queue, "get", _recording_get)],
        )

        self.assertIsNotNone(box["result"])
        # At least one drain tick must have been shrunk below WAIT_TICK_SECONDS.
        self.assertTrue(
            any(t < self.hook.WAIT_TICK_SECONDS for t in ticks),
            f"no shrunk tick observed; all ticks={ticks}",
        )
        # The cap must never be exceeded.
        for t in ticks:
            self.assertLessEqual(t, self.hook.WAIT_TICK_SECONDS)

    # ── Escalation does not fire ──────────────────────────────────────────────

    def test_no_escalation_when_the_role_is_the_default(self):
        box = self._run_with_captured_seam(
            escalate_after=900.0,
            questions=[dict(q, header="Layout") for q in self.QUESTIONS[:1]],
        )
        self.assertIsNone(box["seam"]["escalate_at"])

    def test_no_escalation_when_the_binding_resolves_to_the_default_token(self):
        """``ux = "operator"`` in the machine config: same human, nobody to
        escalate to."""
        box = self._run_with_captured_seam(
            escalate_after=900.0,
            bindings=_make_bindings(roles={"ux": "operator"}),
        )
        self.assertIsNone(box["seam"]["escalate_at"])

    def test_no_escalation_when_the_role_fell_back_to_the_default(self):
        """Unreachable ≠ unanswered (decision 6): the question already went to
        the operator, so there is nowhere to escalate."""
        # No ``ux`` entry anywhere → resolve_destination reroutes to the
        # operator and reports no escalation policy.
        box = self._run_with_captured_seam(
            escalate_after=900.0,
            bindings=_make_bindings(roles={"arch": "rly_arch"}),
        )
        self.assertIsNone(box["seam"]["escalate_at"])
        self.assertTrue(
            all(c["token"] == "rly_op" for c in box["sends"].calls)
        )

    def test_no_escalation_after_a_send_failure_rerouted_to_the_default(self):
        """The retry already targets the default destination."""
        seam = {}

        def _probe(groups, group_tokens, deadline, *,
                   escalate_at=None, send_escalation=None):
            seam["escalate_at"] = escalate_at
            return None

        sends = _SendCapture(fail_token="rly_ux")
        self._run(
            _GatedRelay({}),
            sends=sends,
            escalate_after=900.0,
            extra_patches=[
                patch.object(self.hook, "_wait_for_group_answers", side_effect=_probe)
            ],
        )
        # The role send failed and the retry went to the default token…
        self.assertEqual(sends.calls[0]["token"], "rly_ux")
        self.assertTrue(any(c["token"] == "rly_op" for c in sends.calls))
        # …so escalation is suppressed.
        self.assertIsNone(seam["escalate_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
