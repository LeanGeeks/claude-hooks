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


class TestResolveAmuxSession(unittest.TestCase):
    def test_returns_name_for_amux_session(self):
        completed = type("R", (), {"returncode": 0, "stdout": "amux-hyppie-flow\n", "stderr": ""})()
        with patch.dict(os.environ, {"TMUX_PANE": "%5"}), \
             patch.object(nh.shutil, "which", lambda _: "/usr/bin/tmux"), \
             patch.object(nh.subprocess, "run", lambda *a, **k: completed):
            self.assertEqual(nh.resolve_amux_session(), "hyppie-flow")

    def test_none_when_not_amux_prefixed(self):
        completed = type("R", (), {"returncode": 0, "stdout": "my-plain-session\n", "stderr": ""})()
        with patch.dict(os.environ, {"TMUX_PANE": "%5"}), \
             patch.object(nh.shutil, "which", lambda _: "/usr/bin/tmux"), \
             patch.object(nh.subprocess, "run", lambda *a, **k: completed):
            self.assertIsNone(nh.resolve_amux_session())

    def test_none_when_no_tmux_pane(self):
        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(nh.shutil, "which", lambda _: "/usr/bin/tmux"):
            self.assertIsNone(nh.resolve_amux_session())

    def test_none_when_tmux_missing(self):
        with patch.dict(os.environ, {"TMUX_PANE": "%5"}), \
             patch.object(nh.shutil, "which", lambda _: None):
            self.assertIsNone(nh.resolve_amux_session())

    def test_none_on_tmux_error(self):
        completed = type("R", (), {"returncode": 1, "stdout": "", "stderr": "no server"})()
        with patch.dict(os.environ, {"TMUX_PANE": "%5"}), \
             patch.object(nh.shutil, "which", lambda _: "/usr/bin/tmux"), \
             patch.object(nh.subprocess, "run", lambda *a, **k: completed):
            self.assertIsNone(nh.resolve_amux_session())


class TestMainRouting(unittest.TestCase):
    """main() routing. We patch resolve_amux_session + spawn_reply_injector so
    these never depend on the host being amux or spawn real injector processes;
    amux-specific wiring is asserted explicitly per test."""

    def setUp(self):
        # Default: non-amux host (notify-only) + injector spawn recorded.
        self._resolve = patch.object(nh, "resolve_amux_session", lambda: None)
        self.spawned = []
        self._spawn = patch.object(
            nh, "spawn_reply_injector", lambda mid, name: self.spawned.append((mid, name))
        )
        self._resolve.start()
        self._spawn.start()

    def tearDown(self):
        self._resolve.stop()
        self._spawn.stop()

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

        def fake_send(text, dedupe_key, *, reply_required=False):
            captured["text"] = text
            captured["key"] = dedupe_key
            captured["reply_required"] = reply_required
            return 7

        with patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", True), \
             patch.object(nh, "send_idle_notification", fake_send):
            code = self._run_main(payload)

        self.assertEqual(code, 0)
        self.assertIn("awaiting your call", captured["text"])
        self.assertTrue(captured["key"].startswith("idle:s1:"))
        # Non-amux host → notify-only, no injector.
        self.assertFalse(captured["reply_required"])
        self.assertEqual(self.spawned, [])

    def test_amux_session_force_reply_and_injector(self):
        t = _write_transcript([_assistant("need a decision")])
        payload = {
            "notification_type": "idle_prompt",
            "session_id": "s1",
            "cwd": "/tmp/myrepo",
            "transcript_path": t,
        }
        captured = {}

        def fake_send(text, dedupe_key, *, reply_required=False):
            captured["reply_required"] = reply_required
            return 42

        with patch.object(nh, "resolve_amux_session", lambda: "hyppie-flow"), \
             patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", True), \
             patch.object(nh, "send_idle_notification", fake_send):
            self._run_main(payload)

        # amux-hosted → force-reply + injector armed with (message_id, name).
        self.assertTrue(captured["reply_required"])
        self.assertEqual(self.spawned, [(42, "hyppie-flow")])

    def test_amux_session_send_failure_no_injector(self):
        t = _write_transcript([_assistant("hi")])
        payload = {
            "notification_type": "idle_prompt",
            "session_id": "s1",
            "cwd": "/tmp/x",
            "transcript_path": t,
        }
        with patch.object(nh, "resolve_amux_session", lambda: "hyppie-flow"), \
             patch.object(nh, "load_telegram_config", lambda: None), \
             patch.object(tr, "TELEGRAM_ENABLED", True), \
             patch.object(nh, "send_idle_notification", lambda *a, **k: None):
            self._run_main(payload)
        # Relay send failed → nothing to wait on, no injector.
        self.assertEqual(self.spawned, [])

    def test_dedupe_key_is_deterministic_for_same_state(self):
        t = _write_transcript([_assistant("same message")])
        payload = {
            "notification_type": "idle_prompt",
            "session_id": "s1",
            "cwd": "/tmp/myrepo",
            "transcript_path": t,
        }
        keys = []

        def fake_send(text, dedupe_key, *, reply_required=False):
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
             patch.object(nh, "send_idle_notification", lambda *a, **k: called.append(1)):
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
             patch.object(nh, "send_idle_notification", lambda *a, **k: called.append(1)):
            self._run_main(payload)
        self.assertEqual(called, [])

    def test_suppressed_while_background_shell_task_active(self):
        t = _write_transcript([
            _assistant(blocks=[
                {"type": "tool_use", "id": "toolu_bg1", "name": "Bash",
                 "input": {"command": "sleep 999", "run_in_background": True}},
            ]),
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
             patch.object(nh, "send_idle_notification", lambda *a, **k: called.append(1)):
            self._run_main(payload)
        self.assertEqual(called, [])

    def test_suppressed_while_background_agent_active(self):
        t = _write_transcript([
            _assistant(blocks=[
                {"type": "tool_use", "id": "toolu_agent1", "name": "Agent",
                 "input": {"prompt": "do work", "description": "work"}},
            ]),
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
             patch.object(nh, "send_idle_notification", lambda *a, **k: called.append(1)):
            self._run_main(payload)
        self.assertEqual(called, [])


def _tool_result(tool_use_id, text="done", tool_use_result=None, is_error=False):
    """User entry carrying a tool_result block, shaped like real transcripts:
    launch acks put structured metadata in the entry-level toolUseResult."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": [{"type": "text", "text": text}]}
    if is_error:
        block["is_error"] = True
    entry = {"type": "user", "message": {"role": "user", "content": [block]}}
    if tool_use_result is not None:
        entry["toolUseResult"] = tool_use_result
    return entry


def _agent_launch_ack(tool_use_id, agent_id):
    return _tool_result(
        tool_use_id,
        text=f"Async agent launched successfully.\nagentId: {agent_id} (internal ID)",
        tool_use_result={"isAsync": True, "status": "async_launched", "agentId": agent_id},
    )


def _bash_launch_ack(tool_use_id, task_id):
    return _tool_result(
        tool_use_id,
        text=f"Command running in background with ID: {task_id}. Output is being "
             f"written to: /tmp/tasks/{task_id}.output.",
        tool_use_result={"stdout": "", "stderr": "", "interrupted": False,
                         "backgroundTaskId": task_id},
    )


def _monitor_launch_ack(tool_use_id, task_id, persistent=False):
    text = (f"Monitor started (task {task_id}, persistent — runs until TaskStop)"
            if persistent else f"Monitor started (task {task_id})")
    return _tool_result(
        tool_use_id,
        text=text,
        tool_use_result={"taskId": task_id, "timeoutMs": 0, "persistent": persistent},
    )


def _task_notification(task_id, tool_use_id=None, status="completed", event=None):
    """Delivered completion/event notification: user entry with string content."""
    parts = ["<task-notification>", f"<task-id>{task_id}</task-id>"]
    if tool_use_id:
        parts.append(f"<tool-use-id>{tool_use_id}</tool-use-id>")
    if status:
        parts.append(f"<status>{status}</status>")
        parts.append(f"<summary>Task {task_id} finished</summary>")
    if event:
        parts.append(f"<event>{event}</event>")
    parts.append("</task-notification>")
    return {"type": "user", "message": {"role": "user", "content": "\n".join(parts)}}


def _queue_op_notification(task_id, tool_use_id):
    """Journal entry for the notification enqueue (precedes delivery)."""
    content = _task_notification(task_id, tool_use_id)["message"]["content"]
    return {"type": "queue-operation", "operation": "enqueue", "content": content}


def _attachment_notification(task_id, tool_use_id):
    """Notification delivered as a queued_command attachment."""
    content = _task_notification(task_id, tool_use_id)["message"]["content"]
    return {"type": "attachment",
            "attachment": {"type": "queued_command", "prompt": content}}


def _taskstop_tool_use(tool_id, task_id):
    return _assistant(blocks=[
        {"type": "tool_use", "id": tool_id, "name": "TaskStop",
         "input": {"task_id": task_id}},
    ])


def _sendmessage_tool_use(tool_id, to):
    return _assistant(blocks=[
        {"type": "tool_use", "id": tool_id, "name": "SendMessage",
         "input": {"to": to, "summary": "resume", "message": "continue"}},
    ])


def _sendmessage_resume_ack(tool_use_id, agent_id):
    msg = (f'Agent "{agent_id}" was stopped (completed); resumed it in the '
           f"background with your message. You'll be notified when it finishes.")
    return _tool_result(
        tool_use_id,
        text=json.dumps({"success": True, "message": msg}),
        tool_use_result={"success": True, "message": msg},
    )


def _agent_tool_use(tool_id="toolu_a1", run_in_background=None):
    inp = {"prompt": "do work", "description": "work"}
    if run_in_background is not None:
        inp["run_in_background"] = run_in_background
    return _assistant(blocks=[
        {"type": "tool_use", "id": tool_id, "name": "Agent", "input": inp},
    ])


def _bash_bg_tool_use(tool_id="toolu_b1"):
    return _assistant(blocks=[
        {"type": "tool_use", "id": tool_id, "name": "Bash",
         "input": {"command": "sleep 999", "run_in_background": True}},
    ])


def _monitor_tool_use(tool_id="toolu_m1"):
    return _assistant(blocks=[
        {"type": "tool_use", "id": tool_id, "name": "Monitor",
         "input": {"command": "until false; do sleep 1; done"}},
    ])


def _workflow_tool_use(tool_id="toolu_w1"):
    return _assistant(blocks=[
        {"type": "tool_use", "id": tool_id, "name": "Workflow",
         "input": {"script": "export const meta = {name:'t',description:'t'}"}},
    ])


class TestHasActiveBackgroundWork(unittest.TestCase):
    # --- Agent ---

    def test_agent_launch_without_ack_is_active(self):
        t = _write_transcript([_agent_tool_use()])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_agent_still_active_after_launch_ack(self):
        # THE regression: the launch ack tool_result arrives immediately, the
        # agent is still running until its task-notification lands.
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_agent_completed_by_task_notification(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _task_notification("aaa111", "toolu_a1"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_agent_foreground_flag_not_tracked(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1", run_in_background=False),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_agent_synchronous_result_not_tracked(self):
        # Older Claude Code ran Agent in the foreground: the tool_result IS the
        # final report, with no async-launch marker → treat as completed.
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _tool_result("toolu_a1", text="Here is my full report: ..."),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_agent_launch_error_not_tracked(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _tool_result("toolu_a1", text="InputValidationError: prompt required",
                         is_error=True),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_multiple_agents_one_completed(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_tool_use("toolu_a2"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _agent_launch_ack("toolu_a2", "aaa222"),
            _task_notification("aaa111", "toolu_a1"),
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_multiple_agents_all_completed(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_tool_use("toolu_a2"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _agent_launch_ack("toolu_a2", "aaa222"),
            _task_notification("aaa111", "toolu_a1"),
            _task_notification("aaa222", "toolu_a2"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    # --- SendMessage resume / TaskStop ---

    def test_sendmessage_resume_rearms_agent(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _task_notification("aaa111", "toolu_a1"),
            _sendmessage_tool_use("toolu_s1", "aaa111"),
            _sendmessage_resume_ack("toolu_s1", "aaa111"),
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_resumed_agent_completed_by_second_notification(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _task_notification("aaa111", "toolu_a1"),
            _sendmessage_tool_use("toolu_s1", "aaa111"),
            _sendmessage_resume_ack("toolu_s1", "aaa111"),
            _task_notification("aaa111", "toolu_a1"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_sendmessage_resume_of_unknown_agent_not_tracked(self):
        # Resume ack quoting a *name* (no launch in this transcript maps it to
        # a task id): tracking it would suppress idle pings forever, since the
        # completion notification carries the id, not the name. Fail open.
        t = _write_transcript([
            _sendmessage_tool_use("toolu_s1", "researcher"),
            _sendmessage_resume_ack("toolu_s1", "researcher"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_sendmessage_failure_does_not_rearm(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _task_notification("aaa111", "toolu_a1"),
            _sendmessage_tool_use("toolu_s1", "aaa111"),
            _tool_result("toolu_s1", text="No such agent",
                         tool_use_result={"success": False, "message": "No such agent"}),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_taskstop_clears_running_task(self):
        t = _write_transcript([
            _bash_bg_tool_use("toolu_b1"),
            _bash_launch_ack("toolu_b1", "bqq111"),
            _taskstop_tool_use("toolu_ts1", "bqq111"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    # --- Bash ---

    def test_background_shell_launch_is_active(self):
        t = _write_transcript([_bash_bg_tool_use()])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_background_shell_still_active_after_launch_ack(self):
        t = _write_transcript([
            _bash_bg_tool_use("toolu_b1"),
            _bash_launch_ack("toolu_b1", "bqq111"),
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_background_shell_completed_by_notification(self):
        t = _write_transcript([
            _bash_bg_tool_use("toolu_b1"),
            _bash_launch_ack("toolu_b1", "bqq111"),
            _task_notification("bqq111", "toolu_b1"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_completion_via_queue_operation_entry(self):
        # The enqueue journal entry alone (delivery not yet written) counts.
        t = _write_transcript([
            _bash_bg_tool_use("toolu_b1"),
            _bash_launch_ack("toolu_b1", "bqq111"),
            _queue_op_notification("bqq111", "toolu_b1"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_completion_via_attachment_entry(self):
        # Notifications can be delivered as a queued_command attachment.
        t = _write_transcript([
            _bash_bg_tool_use("toolu_b1"),
            _bash_launch_ack("toolu_b1", "bqq111"),
            _attachment_notification("bqq111", "toolu_b1"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_foreground_bash_not_tracked(self):
        t = _write_transcript([
            _assistant(blocks=[
                {"type": "tool_use", "id": "toolu_fg", "name": "Bash",
                 "input": {"command": "echo hi"}},
            ]),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    # --- Monitor ---

    def test_monitor_launch_is_active(self):
        t = _write_transcript([_monitor_tool_use()])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_monitor_event_completes_nonpersistent(self):
        # One-shot monitor: event notification carries no tool-use-id and no
        # status; matched via the task id recorded from the launch ack.
        t = _write_transcript([
            _monitor_tool_use("toolu_m1"),
            _monitor_launch_ack("toolu_m1", "boil111"),
            _task_notification("boil111", status=None, event="condition met"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_persistent_monitor_event_keeps_running(self):
        t = _write_transcript([
            _monitor_tool_use("toolu_m1"),
            _monitor_launch_ack("toolu_m1", "boil111", persistent=True),
            _task_notification("boil111", status=None, event="tick"),
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_persistent_monitor_taskstop_clears(self):
        t = _write_transcript([
            _monitor_tool_use("toolu_m1"),
            _monitor_launch_ack("toolu_m1", "boil111", persistent=True),
            _task_notification("boil111", status=None, event="tick"),
            _taskstop_tool_use("toolu_ts1", "boil111"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    # --- Workflow ---

    def test_workflow_launch_is_active(self):
        t = _write_transcript([_workflow_tool_use()])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_workflow_completed_by_notification(self):
        t = _write_transcript([
            _workflow_tool_use("toolu_w1"),
            _tool_result("toolu_w1", text="Workflow started",
                         tool_use_result={"runId": "wf_abc123"}),
            _task_notification("wf_abc123", "toolu_w1"),
        ])
        self.assertFalse(nh.has_active_background_agents(t))

    # --- Robustness ---

    def test_sidechain_entries_ignored(self):
        launch = _agent_tool_use("toolu_side")
        launch["isSidechain"] = True
        t = _write_transcript([launch])
        self.assertFalse(nh.has_active_background_agents(t))

    def test_notification_text_inside_tool_result_not_a_completion(self):
        # A tool_result whose *text* quotes a task-notification (grep over a
        # transcript, say) must not clear the running task.
        quoted = ("$ grep transcript\n<task-notification>\n"
                  "<task-id>aaa111</task-id>\n<status>completed</status>\n"
                  "</task-notification>")
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _assistant(blocks=[{"type": "tool_use", "id": "toolu_g1",
                                "name": "Bash", "input": {"command": "grep ..."}}]),
            _tool_result("toolu_g1", text=quoted),
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_notification_quoted_by_assistant_not_a_completion(self):
        # Older transcripts carry assistant content as a plain string; an
        # assistant *quoting* a task-notification must not clear the task.
        quoted = ("The agent will finish with:\n<task-notification>\n"
                  "<task-id>aaa111</task-id>\n<tool-use-id>toolu_a1</tool-use-id>\n"
                  "<status>completed</status>\n</task-notification>")
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            {"type": "assistant",
             "message": {"role": "assistant", "content": quoted}},
        ])
        self.assertTrue(nh.has_active_background_agents(t))

    def test_mixed_agent_bash_monitor(self):
        t = _write_transcript([
            _agent_tool_use("toolu_a1"),
            _bash_bg_tool_use("toolu_b1"),
            _monitor_tool_use("toolu_m1"),
            _agent_launch_ack("toolu_a1", "aaa111"),
            _bash_launch_ack("toolu_b1", "bqq111"),
            _monitor_launch_ack("toolu_m1", "boil111"),
            _task_notification("aaa111", "toolu_a1"),
            _task_notification("bqq111", "toolu_b1"),
        ])
        # Monitor still watching → active.
        self.assertTrue(nh.has_active_background_agents(t))


if __name__ == "__main__":
    unittest.main()
