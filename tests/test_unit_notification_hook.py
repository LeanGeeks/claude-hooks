#!/usr/bin/env python3
"""
Unit tests: Notification hook (idle → Telegram).

Covers the new relay-backed idle notification path:
- extracting the main agent's last text message from a transcript
- HTML escaping + tail truncation
- end-to-end main() routing through the relay (mocked)
- suppression while background agents run / when relay is disabled
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

import notification_hook as nh  # noqa: E402
import telegram_permission_router as tr  # noqa: E402


def _write_transcript(rows):
    tdir = tempfile.mkdtemp()
    tpath = os.path.join(tdir, "transcript.jsonl")
    with open(tpath, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return tpath


def _assistant(text=None, blocks=None, sidechain=False):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    entry = {"type": "assistant", "message": {"role": "assistant", "content": content}}
    if sidechain:
        entry["isSidechain"] = True
    return entry


class TestExtractLastAgentMessage(unittest.TestCase):
    def test_returns_last_text_block(self):
        t = _write_transcript([
            _assistant("first"),
            _assistant("final answer"),
        ])
        self.assertEqual(nh.extract_last_agent_message(t), "final answer")

    def test_skips_trailing_tool_use_turn(self):
        t = _write_transcript([
            _assistant("the real message"),
            _assistant(blocks=[{"type": "tool_use", "name": "Read", "input": {}}]),
        ])
        self.assertEqual(nh.extract_last_agent_message(t), "the real message")

    def test_skips_sidechain_subagent(self):
        t = _write_transcript([
            _assistant("orchestrator message"),
            _assistant("subagent chatter", sidechain=True),
        ])
        self.assertEqual(nh.extract_last_agent_message(t), "orchestrator message")

    def test_skips_thinking_only(self):
        t = _write_transcript([
            _assistant("spoken text"),
            _assistant(blocks=[{"type": "thinking", "thinking": "hmm"}]),
        ])
        self.assertEqual(nh.extract_last_agent_message(t), "spoken text")

    def test_none_when_no_text(self):
        t = _write_transcript([
            _assistant(blocks=[{"type": "tool_use", "name": "Read", "input": {}}]),
        ])
        self.assertIsNone(nh.extract_last_agent_message(t))

    def test_missing_file_returns_none(self):
        self.assertIsNone(nh.extract_last_agent_message("/no/such/file.jsonl"))


class TestTailEscape(unittest.TestCase):
    def test_escapes_html_specials(self):
        esc, trunc = nh._tail_escape("a <b> & c", 1000)
        self.assertEqual(esc, "a &lt;b&gt; &amp; c")
        self.assertFalse(trunc)

    def test_truncates_from_front_and_marks(self):
        raw = "X" * 10000 + "\nTAIL"
        esc, trunc = nh._tail_escape(raw, 3800)
        self.assertTrue(trunc)
        self.assertLessEqual(len(esc), 3800)
        # The tail (most recent content) is what we keep.
        self.assertTrue(esc.endswith("TAIL") or "TAIL" in esc[-50:])

    def test_no_split_entity(self):
        # All-ampersand worst case: each char escapes to 5 chars.
        raw = "&" * 2000
        esc, trunc = nh._tail_escape(raw, 3800)
        self.assertLessEqual(len(esc), 3800)
        # Escaped output must not end mid-entity.
        self.assertTrue(esc.endswith("amp;"))


class TestBuildNotificationText(unittest.TestCase):
    def test_includes_title_session_and_body(self):
        text = nh.build_notification_text("repo", "feat/x", "do the thing", "fb")
        self.assertIn("<b>repo</b>", text)
        self.assertIn("<i>feat/x</i>", text)
        self.assertIn("<blockquote>do the thing</blockquote>", text)

    def test_falls_back_when_no_message(self):
        text = nh.build_notification_text("repo", None, None, "canned idle string")
        self.assertIn("canned idle string", text)
        self.assertNotIn("<blockquote>", text)


class TestMainRouting(unittest.TestCase):
    def _run_main(self, payload):
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            try:
                nh.main()
            except SystemExit as e:
                return e.code
        return None

    def test_idle_routes_through_relay(self):
        t = _write_transcript([_assistant("awaiting your call")])
        payload = {
            "notification_type": "idle_prompt",
            "message": "Claude is waiting",
            "session_id": "s1",
            "cwd": "/tmp/myrepo",
            "transcript_path": t,
        }
        captured = {}

        def fake_send(text, dedupe_key):
            captured["text"] = text
            captured["key"] = dedupe_key
            return 7

        with patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", True), \
             patch.object(nh, "send_idle_notification", fake_send):
            code = self._run_main(payload)

        self.assertEqual(code, 0)
        self.assertIn("awaiting your call", captured["text"])
        self.assertTrue(captured["key"].startswith("idle:s1:"))

    def test_dedupe_key_is_deterministic_for_same_state(self):
        t = _write_transcript([_assistant("same message")])
        payload = {
            "notification_type": "idle_prompt",
            "session_id": "s1",
            "cwd": "/tmp/myrepo",
            "transcript_path": t,
        }
        keys = []

        def fake_send(text, dedupe_key):
            keys.append(dedupe_key)
            return 1

        for _ in range(2):
            with patch.object(nh, "load_telegram_config", lambda: None), \
                 patch.object(tr, "TELEGRAM_ENABLED", True), \
                 patch.object(nh, "send_idle_notification", fake_send):
                self._run_main(payload)

        # Identical idle state must yield identical keys so the relay's
        # idempotency layer suppresses the duplicate.
        self.assertEqual(keys[0], keys[1])

    def test_non_idle_type_is_ignored(self):
        called = []
        payload = {"notification_type": "other", "session_id": "s", "cwd": "/tmp/x"}
        with patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", True), \
             patch.object(nh, "send_idle_notification", lambda *a: called.append(1)):
            self._run_main(payload)
        self.assertEqual(called, [])

    def test_relay_disabled_no_send(self):
        t = _write_transcript([_assistant("hi")])
        payload = {
            "notification_type": "idle_prompt",
            "session_id": "s",
            "cwd": "/tmp/x",
            "transcript_path": t,
        }
        called = []
        with patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", False), \
             patch.object(nh, "send_idle_notification", lambda *a: called.append(1)):
            self._run_main(payload)
        self.assertEqual(called, [])

    def test_suppressed_while_background_agent_active(self):
        t = _write_transcript([
            _assistant("parent waiting"),
            {"toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": "a1"}},
        ])
        payload = {
            "notification_type": "idle_prompt",
            "session_id": "s",
            "cwd": "/tmp/x",
            "transcript_path": t,
        }
        called = []
        with patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", True), \
             patch.object(nh, "send_idle_notification", lambda *a: called.append(1)):
            self._run_main(payload)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
