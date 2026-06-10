#!/usr/bin/env python3
"""
Unit tests: reply_injector (Telegram reply → amux send).

Covers the detached injector that turns an answered idle notification into an
injected user turn: the answer→amux-send path, the Escape-clear ordering, and
graceful no-ops on missing/terminal/empty answers.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

import reply_injector as ri  # noqa: E402
import telegram_permission_router as tr  # noqa: E402


def _run_main(message_id=42, amux="hyppie-flow"):
    argv = ["reply_injector.py", "--message-id", str(message_id), "--amux", amux]
    with patch.object(sys, "argv", argv):
        ri.main()


class TestSanitize(unittest.TestCase):
    def test_collapses_newlines(self):
        self.assertEqual(ri.sanitize_reply("line1\nline2\n  line3 "), "line1 line2   line3")

    def test_strips(self):
        self.assertEqual(ri.sanitize_reply("  hello  "), "hello")


class TestInjectReply(unittest.TestCase):
    def test_clears_with_line_kills_then_sends(self):
        calls = []

        def fake_send(name, *args):
            calls.append((name, args))
            return True

        with patch.object(ri, "amux_send", fake_send):
            ri.inject_reply("hyppie-flow", "do the thing")

        # First: clear any half-typed draft in both directions via Ctrl-U (up) +
        # Ctrl-K (down) — NOT Escape (Rewind shortcut / aborts a turn) and NOT
        # arrows (recall input history). Then: the reply text, which auto-submits.
        expected_kills = ("--keys", *(["C-u"] * ri.CLEAR_LINE_KILLS), *(["C-k"] * ri.CLEAR_LINE_KILLS))
        self.assertEqual(calls[0], ("hyppie-flow", expected_kills))
        self.assertEqual(calls[1], ("hyppie-flow", ("do the thing",)))
        # Never send an Escape- or arrow-based pre-clear.
        sent = [a for _, args in calls for a in args]
        for forbidden in ("Escape", "Up", "Down"):
            self.assertNotIn(forbidden, sent)


class TestMain(unittest.TestCase):
    def _patch_relay(self, answer):
        return (
            patch.object(ri, "load_telegram_config", lambda: None),
            patch.object(tr, "TELEGRAM_ENABLED", True),
            patch.object(ri, "wait_for_relay_answer", lambda *a, **k: answer),
        )

    def test_text_answer_injects(self):
        injected = []
        patches = self._patch_relay({"text": "ship it", "via": "reply"})
        with patches[0], patches[1], patches[2], \
             patch.object(ri, "inject_reply", lambda name, text: injected.append((name, text))):
            _run_main()
        self.assertEqual(injected, [("hyppie-flow", "ship it")])

    def test_terminal_state_no_injection(self):
        injected = []
        patches = self._patch_relay({"_state": "expired"})
        with patches[0], patches[1], patches[2], \
             patch.object(ri, "inject_reply", lambda *a: injected.append(a)):
            _run_main()
        self.assertEqual(injected, [])

    def test_timeout_no_injection(self):
        injected = []
        patches = self._patch_relay(None)
        with patches[0], patches[1], patches[2], \
             patch.object(ri, "inject_reply", lambda *a: injected.append(a)):
            _run_main()
        self.assertEqual(injected, [])

    def test_empty_text_no_injection(self):
        injected = []
        patches = self._patch_relay({"text": "   \n  ", "via": "reply"})
        with patches[0], patches[1], patches[2], \
             patch.object(ri, "inject_reply", lambda *a: injected.append(a)):
            _run_main()
        self.assertEqual(injected, [])

    def test_relay_disabled_no_injection(self):
        injected = []
        with patch.object(ri, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", False), \
             patch.object(ri, "inject_reply", lambda *a: injected.append(a)):
            _run_main()
        self.assertEqual(injected, [])

    def test_text_is_sanitized_before_injection(self):
        injected = []
        patches = self._patch_relay({"text": "first line\nsecond line", "via": "reply"})
        with patches[0], patches[1], patches[2], \
             patch.object(ri, "inject_reply", lambda name, text: injected.append(text)):
            _run_main()
        self.assertEqual(injected, ["first line second line"])


if __name__ == "__main__":
    unittest.main()
