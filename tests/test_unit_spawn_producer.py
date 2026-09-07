#!/usr/bin/env python3
"""Unit tests for the epic-10 producer hook (task 10-02).

Exercises ``spawn_producer_hook`` against a throwaway ``~/.amux`` (redirected via
the shared lib's path constants) so nothing touches the developer's real
``~/.claude`` / ``~/.amux``, and nothing hits the network. The producer is
hooks-only and reads its lifecycle payload from stdin + ``--event`` from argv.

Coverage (mirrors the task's Testing bullets):
- tracked session first turn  -> idle + last_message + mtime_at_stop set
- live background child        -> running + background_tasks; then a draining Stop
                                  self-drains the handle to idle
- gated command (Notification) -> permission_pending set + not idle; next Stop clears it
- SubagentStop                 -> refreshes bg/mtime but NEVER sets idle
- plain/non-tracked session    -> NO handle writes
- other-repo session           -> no-op (its handle is under a different name)
- SessionEnd                   -> terminated, preserving last_state
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402
import spawn_producer_hook as producer  # noqa: E402


def _redirect_amux_home(tmp: Path):
    """Point the lib's amux paths at a throwaway dir for a test."""
    return patch.multiple(
        lib,
        AMUX_HOME=tmp,
        AMUX_SESSIONS_DIR=tmp / "sessions",
        SPAWN_DIR=tmp / "spawn",
        SPAWN_LOCK=tmp / "spawn" / ".lock",
    )


def _seed_handle(name: str, abs_dir: str, transcript_path: str) -> dict:
    """Create a tracked handle in ``spawning`` (as 10-01 would) and persist it."""
    h = lib.new_handle(
        name=name,
        session_id="11111111-2222-3333-4444-555555555555",
        run_id="rid",
        abs_dir=abs_dir,
        transcript_path=transcript_path,
        stuck_after_s=600,
    )
    lib.write_handle(name, h)
    return h


def _write_transcript(path: Path, text: str = "x\n") -> float:
    """Write a transcript file and return its mtime (epoch float)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return os.path.getmtime(path)


def _run(event: str, payload: dict, *, amux_name: str | None):
    """Drive ``producer.main()`` with a given event/payload and resolved amux name.

    ``amux_name=None`` simulates a non-amux / plain session (resolve returns None).
    Patches stdin + argv; ``main`` calls ``sys.exit(0)`` always (fail-open).
    """
    raw = json.dumps(payload)
    with patch.object(sys, "argv", ["spawn_producer_hook.py", "--event", event]), \
            patch.object(sys, "stdin", _FakeStdin(raw)), \
            patch.object(lib, "resolve_amux_session", return_value=amux_name):
        try:
            producer.main()
        except SystemExit as e:
            return e.code
    return None


class _FakeStdin:
    def __init__(self, data: str):
        self._data = data

    def read(self) -> str:
        return self._data


class TestStopFirstTurn(unittest.TestCase):
    def test_tracked_first_turn_goes_idle_with_last_message_and_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                mtime = _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                code = _run(
                    "Stop",
                    {
                        "last_assistant_message": "done with the task",
                        "background_tasks": [],
                        "transcript_path": str(tpath),
                    },
                    amux_name="proj-2",
                )
                self.assertEqual(code, 0)

                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "idle")
                self.assertEqual(h["last_message"], "done with the task")
                self.assertEqual(h["background_tasks"], [])
                self.assertFalse(h["permission_pending"])
                # mtime_at_stop is the transcript's real fs mtime (same clock 10-03 uses).
                self.assertEqual(h["mtime_at_stop"], mtime)
                # transcript_path captured from the payload.
                self.assertEqual(h["transcript_path"], str(tpath))
                # No schema fields invented.
                self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))

    def test_stop_prefers_payload_transcript_path_over_handle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                real = tmp / "proj" / "real.jsonl"
                _write_transcript(real)
                # Handle was seeded with a (wrong) computed guess.
                _seed_handle("proj-2", "/ws/proj", str(tmp / "guess.jsonl"))
                _run("Stop",
                     {"last_assistant_message": "hi", "background_tasks": [],
                      "transcript_path": str(real)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["transcript_path"], str(real))


class TestBackgroundRunningThenDrain(unittest.TestCase):
    def test_running_with_bg_then_self_drains_to_idle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                # Turn ends with a live background shell -> running.
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 30", "description": "sleep"}]
                _run("Stop",
                     {"last_assistant_message": "kicked off a build",
                      "background_tasks": bg, "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "running")
                self.assertEqual(h["background_tasks"], bg)

                # Background completion fires a fresh Stop with bg drained -> idle.
                _write_transcript(tpath, "x\ny\n")
                _run("Stop",
                     {"last_assistant_message": "kicked off a build",
                      "background_tasks": [], "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "idle")
                self.assertEqual(h["background_tasks"], [])


class TestPermissionPending(unittest.TestCase):
    def test_permission_prompt_sets_pending_not_idle_then_stop_clears(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                # Pretend the session was running before the gate.
                h0["state"] = "running"
                lib.write_handle("proj-2", h0)

                _run("Notification",
                     {"notification_type": "permission_prompt"},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertTrue(h["permission_pending"])
                # The marker must NOT flip state to idle.
                self.assertNotEqual(h["state"], "idle")
                self.assertEqual(h["state"], "running")

                # Post-resolution Stop clears the marker.
                _run("Stop",
                     {"last_assistant_message": "ran the tool",
                      "background_tasks": [], "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertFalse(h["permission_pending"])
                self.assertEqual(h["state"], "idle")

    def test_idle_prompt_notification_is_ignored_by_producer(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))
                before = lib.read_handle("proj-2")
                _run("Notification",
                     {"notification_type": "idle_prompt"},
                     amux_name="proj-2")
                after = lib.read_handle("proj-2")
                # idle_prompt is the existing Telegram hook's job; producer no-ops.
                self.assertEqual(after, before)


class TestSubagentStop(unittest.TestCase):
    def test_refreshes_bg_and_mtime_but_never_sets_idle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                h0["state"] = "running"
                lib.write_handle("proj-2", h0)

                # SubagentStop carries an empty bg list — must NOT flip to idle.
                _write_transcript(tpath, "x\ny\n")
                new_mtime = os.path.getmtime(tpath)
                _run("SubagentStop",
                     {"background_tasks": [], "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "running")  # idle left to Stop
                self.assertEqual(h["background_tasks"], [])
                self.assertEqual(h["mtime_at_stop"], new_mtime)

    def test_subagent_stop_refreshes_nonempty_bg(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                # Set state explicitly to "running" so SubagentStop cannot override it.
                h0["state"] = "running"
                lib.write_handle("proj-2", h0)
                bg = [{"type": "shell", "status": "running", "id": "t2"}]
                _run("SubagentStop",
                     {"background_tasks": bg, "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["background_tasks"], bg)
                # Must be exactly "running" — SubagentStop must not override an
                # explicitly-running state (not merely != "idle").
                self.assertEqual(h["state"], "running")


class TestCCVersionTolerance(unittest.TestCase):
    """Issue 1 — well-formed payload that lacks background_tasks and last_assistant_message.

    Exercises the CC < 2.1.145 compatibility path: a structurally valid Stop JSON
    that simply omits the new fields. This is DISTINCT from the malformed-JSON test
    (which exercises the JSONDecodeError path → payload={}); here the JSON parses
    fine but the keys are absent.
    """

    def test_stop_missing_bg_and_last_message_goes_idle_and_preserves_last_message(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                # Seed a last_message so we can verify it is NOT overwritten.
                h0["last_message"] = "seeded message from spawn"
                lib.write_handle("proj-2", h0)

                # Well-formed JSON that lacks background_tasks and last_assistant_message.
                code = _run(
                    "Stop",
                    {"transcript_path": str(tpath)},
                    amux_name="proj-2",
                )
                self.assertEqual(code, 0)

                h = lib.read_handle("proj-2")
                # background_tasks absent → defaults to [] → state must be "idle".
                self.assertEqual(h["state"], "idle")
                self.assertEqual(h["background_tasks"], [])
                # last_assistant_message absent (None) → the None branch must NOT
                # overwrite the existing last_message value.
                self.assertEqual(h["last_message"], "seeded message from spawn")


class TestSessionEnd(unittest.TestCase):
    def test_terminated_preserves_last_state(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                h0["state"] = "running"
                lib.write_handle("proj-2", h0)

                _run("SessionEnd", {"reason": "clear"}, amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "terminated")
                self.assertEqual(h["last_state"], "running")
                # No invented schema fields (reason not persisted — not in s6.0).
                self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))


    def test_session_end_on_spawning_leaves_last_state_none(self):
        """Issue 3 — SessionEnd on a session still in state 'spawning'.

        Documents the accepted edge case (§6.0 schema): when a session is killed
        before its first Stop, last_state stays None (there is no meaningful prior
        state to record). state becomes 'terminated'. Do NOT change the hook to
        'fix' this — it is correct behaviour per the schema.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                # _seed_handle creates a handle with state == "spawning" (as 10-01 would).
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                h_before = lib.read_handle("proj-2")
                self.assertEqual(h_before["state"], "spawning")
                self.assertIsNone(h_before["last_state"])

                _run("SessionEnd", {"reason": "killed"}, amux_name="proj-2")
                h = lib.read_handle("proj-2")
                # Session killed before first turn: terminated but no last_state context.
                self.assertEqual(h["state"], "terminated")
                self.assertIsNone(h["last_state"])


class TestHandleGating(unittest.TestCase):
    def test_plain_session_writes_no_handle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # resolve returns None -> not an amux/tracked session at all.
                code = _run("Stop",
                            {"last_assistant_message": "hi", "background_tasks": []},
                            amux_name=None)
                self.assertEqual(code, 0)
                # No handle files created anywhere.
                self.assertEqual(list(lib.SPAWN_DIR.glob("*.json")), [])

    def test_amux_session_without_handle_no_ops(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # In an amux session 'plainhuman' but no handle exists (plain spawn).
                code = _run("Stop",
                            {"last_assistant_message": "hi", "background_tasks": []},
                            amux_name="plainhuman")
                self.assertEqual(code, 0)
                self.assertIsNone(lib.read_handle("plainhuman"))
                self.assertEqual(list(lib.SPAWN_DIR.glob("*.json")), [])

    def test_other_repo_session_does_not_touch_this_handle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                ours = _seed_handle("proj-2", "/ws/proj", str(tpath))
                # A Stop fires in a *different* tracked session ('other-9') that has
                # no handle here — our handle must be untouched, and no new one made.
                _run("Stop",
                     {"last_assistant_message": "from elsewhere",
                      "background_tasks": []},
                     amux_name="other-9")
                self.assertEqual(lib.read_handle("proj-2"), ours)
                self.assertIsNone(lib.read_handle("other-9"))


class TestProviderGate(unittest.TestCase):
    """Epic 20 (20-01): this Claude producer owns Claude handles only."""

    def test_legacy_handle_without_provider_key_is_still_written(self):
        """Cross-task invariant 2 — a no-provider handle IS a Claude handle."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                mtime = _write_transcript(tpath)
                legacy = _seed_handle("proj-2", "/ws/proj", str(tpath))
                # Strip every epic-20 field: this is exactly an epic-10 handle.
                for field in lib.HANDLE_FIELDS_ADDED_20_01:
                    legacy.pop(field, None)
                lib.write_handle("proj-2", legacy)
                self.assertNotIn("provider", lib.read_handle("proj-2"))

                code = _run("Stop",
                            {"last_assistant_message": "done",
                             "background_tasks": [],
                             "transcript_path": str(tpath)},
                            amux_name="proj-2")
                self.assertEqual(code, 0)
                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "idle")
                self.assertEqual(h["last_message"], "done")
                self.assertEqual(h["mtime_at_stop"], mtime)
                # The producer does not invent the new fields on a legacy handle.
                self.assertNotIn("provider", h)

    def test_codex_handle_is_never_written_by_the_claude_producer(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events = tmp / "review.events.jsonl"
                events.parent.mkdir(parents=True, exist_ok=True)
                events.write_text('{"type":"turn.started"}\n')
                codex = lib.new_handle(
                    name="review-123",
                    session_id="01a00000-0000-7000-8000-000000000001",
                    run_id="rid", abs_dir="/ws/proj", transcript_path="",
                    stuck_after_s=600, provider=lib.PROVIDER_CODEX,
                    activity_path=str(events),
                    result_path=str(tmp / "review.last.md"),
                )
                lib.write_handle("review-123", codex)

                for event, payload in (
                    ("Stop", {"last_assistant_message": "claude text",
                              "background_tasks": []}),
                    ("SubagentStop", {"background_tasks": []}),
                    ("Notification", {"notification_type": "permission_prompt"}),
                    ("SessionEnd", {"reason": "clear"}),
                ):
                    code = _run(event, payload, amux_name="review-123")
                    self.assertEqual(code, 0)
                    self.assertEqual(
                        lib.read_handle("review-123"), codex,
                        f"{event} mutated a Codex handle",
                    )

    def test_claude_handle_stop_preserves_the_epic20_fields(self):
        """Read-modify-write must not drop fields this hook does not own."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                h0["process_pid"] = 4242
                h0["result_path"] = "/some/result.md"
                lib.write_handle("proj-2", h0)

                _run("Stop",
                     {"last_assistant_message": "done", "background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h = lib.read_handle("proj-2")
                self.assertEqual(h["provider"], lib.PROVIDER_CLAUDE)
                self.assertEqual(h["activity_path"], str(tpath))
                self.assertEqual(h["process_pid"], 4242)
                self.assertEqual(h["result_path"], "/some/result.md")
                self.assertIsNone(h["exit_code"])
                self.assertIsNone(h["failure"])
                self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))


class TestFailOpen(unittest.TestCase):
    def test_unknown_event_no_ops(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                code = _run("Bogus", {}, amux_name="proj-2")
                self.assertEqual(code, 0)

    def test_malformed_stdin_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))
                with patch.object(sys, "argv",
                                  ["spawn_producer_hook.py", "--event", "Stop"]), \
                        patch.object(sys, "stdin", _FakeStdin("{not json")), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value="proj-2"):
                    try:
                        producer.main()
                    except SystemExit as e:
                        self.assertEqual(e.code, 0)
                # Malformed payload -> empty dict -> bg [] -> idle, last_message
                # untouched (None). The point: no crash, handle stays valid.
                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "idle")


import lifecycle_events  # noqa: E402


class TestLifecycleEventLog(unittest.TestCase):
    """Epic 37 (task 37-01): lifecycle event log production."""

    def test_stop_appends_lifecycle_event(self):
        """A Stop on a tracked Claude handle appends a stop event to the log."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                _run("Stop",
                     {"last_assistant_message": "task complete",
                      "background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 1)
                ev = events[0]
                self.assertEqual(ev["event"], "stop")
                self.assertEqual(ev["state"], "idle")
                self.assertEqual(ev["seq"], 1)
                self.assertEqual(ev["last_message"], "task complete")
                self.assertEqual(ev["background_tasks_count"], 0)
                self.assertFalse(ev["permission_pending"])
                self.assertIn("ts", ev)
                self.assertEqual(
                    ev["session_id"], "11111111-2222-3333-4444-555555555555"
                )

    def test_stop_with_bg_appends_running_event(self):
        """A Stop with live background tasks records state=running in the log."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                bg = [{"type": "shell", "status": "running", "id": "t1"}]
                _run("Stop",
                     {"last_assistant_message": "kicked off CI",
                      "background_tasks": bg,
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["state"], "running")
                self.assertEqual(events[0]["background_tasks_count"], 1)

    def test_subagent_stop_appends_event(self):
        """SubagentStop appends a subagent_stop event without changing state."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                h0["state"] = "running"
                lib.write_handle("proj-2", h0)

                _run("SubagentStop",
                     {"background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event"], "subagent_stop")
                self.assertEqual(events[0]["state"], "running")

    def test_permission_prompt_appends_event(self):
        """A permission_prompt Notification appends a permission_prompt event."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                h0["state"] = "running"
                lib.write_handle("proj-2", h0)

                _run("Notification",
                     {"notification_type": "permission_prompt"},
                     amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event"], "permission_prompt")
                self.assertTrue(events[0]["permission_pending"])
                self.assertEqual(events[0]["state"], "running")

    def test_session_end_appends_event_with_last_state(self):
        """SessionEnd appends a session_end event with last_state."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                h0["state"] = "idle"
                lib.write_handle("proj-2", h0)

                _run("SessionEnd", {"reason": "clear"}, amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event"], "session_end")
                self.assertEqual(events[0]["state"], "terminated")
                self.assertEqual(events[0]["last_state"], "idle")

    def test_idle_prompt_notification_produces_no_event(self):
        """An idle_prompt Notification must NOT append a lifecycle event."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                _run("Notification",
                     {"notification_type": "idle_prompt"},
                     amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 0)

    def test_plain_session_produces_no_event(self):
        """An untracked session produces no lifecycle log at all."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _run("Stop",
                     {"last_assistant_message": "hi", "background_tasks": []},
                     amux_name=None)
                # No .lifecycle.jsonl files anywhere.
                self.assertEqual(
                    list(lib.SPAWN_DIR.glob("*.lifecycle.jsonl")), []
                )

    def test_codex_handle_produces_no_event(self):
        """A Codex handle must NOT get a lifecycle log written."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                codex = lib.new_handle(
                    name="review-123",
                    session_id="01a00000-0000-7000-8000-000000000001",
                    run_id="rid", abs_dir="/ws/proj", transcript_path="",
                    stuck_after_s=600, provider=lib.PROVIDER_CODEX,
                )
                lib.write_handle("review-123", codex)

                _run("Stop",
                     {"last_assistant_message": "done",
                      "background_tasks": []},
                     amux_name="review-123")

                events = lifecycle_events.read_events(
                    lib.SPAWN_DIR, "review-123"
                )
                self.assertEqual(len(events), 0)

    def test_events_are_ordered_with_monotonic_seq(self):
        """Multiple events produce monotonically increasing seq numbers."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                # Stop (turn 1)
                _run("Stop",
                     {"last_assistant_message": "turn 1",
                      "background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")

                # Turn start (follow-up)
                _run("UserPromptSubmit", {}, amux_name="proj-2")

                # Stop (turn 2)
                _run("Stop",
                     {"last_assistant_message": "turn 2",
                      "background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")

                # SessionEnd
                _run("SessionEnd", {"reason": "clear"}, amux_name="proj-2")

                events = lifecycle_events.read_events(lib.SPAWN_DIR, "proj-2")
                self.assertEqual(len(events), 4)
                seqs = [e["seq"] for e in events]
                self.assertEqual(seqs, [1, 2, 3, 4])
                event_types = [e["event"] for e in events]
                self.assertEqual(
                    event_types,
                    ["stop", "turn_start", "stop", "session_end"],
                )


class TestUserPromptSubmit(unittest.TestCase):
    """Epic 37 (task 37-01): UserPromptSubmit turn-start recording."""

    def test_turn_start_sets_running_and_clears_permission(self):
        """UserPromptSubmit transitions handle to running."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                h0 = _seed_handle("proj-2", "/ws/proj", str(tpath))
                # Simulate: worker was idle, then gets a follow-up.
                h0["state"] = "idle"
                h0["permission_pending"] = True  # stale from a prior gate
                lib.write_handle("proj-2", h0)

                code = _run("UserPromptSubmit", {}, amux_name="proj-2")
                self.assertEqual(code, 0)

                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "running")
                self.assertFalse(h["permission_pending"])

    def test_turn_start_on_spawning_transitions_to_running(self):
        """UserPromptSubmit on a fresh (spawning) handle goes to running."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                h_before = lib.read_handle("proj-2")
                self.assertEqual(h_before["state"], "spawning")

                code = _run("UserPromptSubmit", {}, amux_name="proj-2")
                self.assertEqual(code, 0)

                h = lib.read_handle("proj-2")
                self.assertEqual(h["state"], "running")

    def test_turn_start_is_handle_gated(self):
        """UserPromptSubmit with no tracked handle produces nothing."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                code = _run("UserPromptSubmit", {}, amux_name=None)
                self.assertEqual(code, 0)
                self.assertEqual(list(lib.SPAWN_DIR.glob("*.json")), [])
                self.assertEqual(
                    list(lib.SPAWN_DIR.glob("*.lifecycle.jsonl")), []
                )

    def test_turn_start_no_handle_no_event(self):
        """UserPromptSubmit for an amux session without handle no-ops."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                code = _run("UserPromptSubmit", {}, amux_name="untracked")
                self.assertEqual(code, 0)
                self.assertEqual(
                    list(lib.SPAWN_DIR.glob("*.lifecycle.jsonl")), []
                )


class TestStoppedAtField(unittest.TestCase):
    """Epic 37 (task 37-01): producer-owned stopped_at timestamp."""

    def test_stop_writes_stopped_at(self):
        """A Stop sets stopped_at to an ISO timestamp."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                _run("Stop",
                     {"last_assistant_message": "done",
                      "background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")

                h = lib.read_handle("proj-2")
                self.assertIsNotNone(h["stopped_at"])
                self.assertIsInstance(h["stopped_at"], str)
                # stopped_at == updated_at (both set in the same call).
                self.assertEqual(h["stopped_at"], h["updated_at"])

    def test_stopped_at_in_handle_fields(self):
        """stopped_at is in the schema and produced by new_handle."""
        self.assertIn("stopped_at", lib.HANDLE_FIELDS)
        h = lib.new_handle(
            name="test", session_id="sid", run_id="rid",
            abs_dir="/ws", transcript_path="/t.jsonl", stuck_after_s=600,
        )
        self.assertIn("stopped_at", h)
        self.assertIsNone(h["stopped_at"])
        self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))

    def test_stopped_at_preserved_by_turn_start(self):
        """UserPromptSubmit does NOT reset stopped_at (it records the last Stop)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                # First Stop sets stopped_at.
                _run("Stop",
                     {"last_assistant_message": "done",
                      "background_tasks": [],
                      "transcript_path": str(tpath)},
                     amux_name="proj-2")
                h_after_stop = lib.read_handle("proj-2")
                stopped_at = h_after_stop["stopped_at"]
                self.assertIsNotNone(stopped_at)

                # Follow-up: UserPromptSubmit should NOT clear stopped_at.
                _run("UserPromptSubmit", {}, amux_name="proj-2")
                h_after_turn = lib.read_handle("proj-2")
                self.assertEqual(h_after_turn["stopped_at"], stopped_at)
                self.assertEqual(h_after_turn["state"], "running")


class TestNoTranscriptWorker(unittest.TestCase):
    """Epic 37 (task 37-01): a tracked worker with NO transcript file.

    This is the fixture shape the driving run exposed (evidence.md s1):
    every fleet handle had mtime_at_stop=None because the transcript
    did not exist. The lifecycle event log must still produce a complete
    record.
    """

    def test_full_lifecycle_without_transcript(self):
        """Complete lifecycle with no transcript file produces ordered events."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # No transcript file created.
                _seed_handle("no-tx", "/ws/proj", "/nonexistent/path.jsonl")

                # 1. Turn start (seed prompt submitted).
                _run("UserPromptSubmit", {}, amux_name="no-tx")
                h = lib.read_handle("no-tx")
                self.assertEqual(h["state"], "running")

                # 2. Stop with background work.
                _run("Stop",
                     {"last_assistant_message": "kicked off CI",
                      "background_tasks": [{"type": "shell", "id": "t1"}],
                      "transcript_path": ""},
                     amux_name="no-tx")
                h = lib.read_handle("no-tx")
                self.assertEqual(h["state"], "running")
                self.assertIsNone(h["mtime_at_stop"])  # no transcript
                self.assertIsNotNone(h["stopped_at"])  # producer-owned!

                # 3. SubagentStop (bg drains).
                _run("SubagentStop",
                     {"background_tasks": []},
                     amux_name="no-tx")

                # 4. Turn start (follow-up via amux send).
                _run("UserPromptSubmit", {}, amux_name="no-tx")
                h = lib.read_handle("no-tx")
                self.assertEqual(h["state"], "running")

                # 5. Stop clean (idle transition).
                _run("Stop",
                     {"last_assistant_message": "all done",
                      "background_tasks": []},
                     amux_name="no-tx")
                h = lib.read_handle("no-tx")
                self.assertEqual(h["state"], "idle")

                # 6. SessionEnd.
                _run("SessionEnd", {"reason": "clear"}, amux_name="no-tx")

                # Verify the complete event record.
                events = lifecycle_events.read_events(lib.SPAWN_DIR, "no-tx")
                self.assertEqual(len(events), 6)
                types = [e["event"] for e in events]
                self.assertEqual(types, [
                    "turn_start", "stop", "subagent_stop",
                    "turn_start", "stop", "session_end",
                ])
                # Seqs are monotonically increasing.
                seqs = [e["seq"] for e in events]
                self.assertEqual(seqs, [1, 2, 3, 4, 5, 6])

                # The turn-start after the first idle proves "a turn is
                # open right now" is derivable from the log alone.
                self.assertEqual(events[3]["event"], "turn_start")
                self.assertEqual(events[3]["state"], "running")

                # The idle transition is the stop with bg_count=0.
                self.assertEqual(events[4]["event"], "stop")
                self.assertEqual(events[4]["state"], "idle")
                self.assertEqual(events[4]["background_tasks_count"], 0)

                # SessionEnd carries last_state.
                self.assertEqual(events[5]["last_state"], "idle")

    def test_idle_transition_recorded_for_agent_spawned_session(self):
        """The idle event is the producer's Stop with bg=[], not notification_hook.

        This is what notification_hook's origin gate currently drops for
        agent-spawned sessions. The lifecycle log records it from the
        producer path instead, using the hook payload (not the transcript).
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("worker-1", "/ws/proj", "/no/transcript.jsonl")

                _run("Stop",
                     {"last_assistant_message": "finished the implementation",
                      "background_tasks": []},
                     amux_name="worker-1")

                events = lifecycle_events.read_events(
                    lib.SPAWN_DIR, "worker-1"
                )
                self.assertEqual(len(events), 1)
                ev = events[0]
                self.assertEqual(ev["event"], "stop")
                self.assertEqual(ev["state"], "idle")
                self.assertEqual(ev["background_tasks_count"], 0)
                self.assertEqual(
                    ev["last_message"], "finished the implementation"
                )

    def test_follow_up_after_idle_shows_turn_open(self):
        """After a settled worker gets a follow-up, the log shows a turn is open.

        This is the regression open_turn exists for: without a turn_start
        event, a worker sent a follow-up via amux send still reads idle.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("worker-2", "/ws/proj", "/no/tx.jsonl")

                # Worker finishes -> idle.
                _run("Stop",
                     {"last_assistant_message": "done",
                      "background_tasks": []},
                     amux_name="worker-2")

                # Follow-up sent.
                _run("UserPromptSubmit", {}, amux_name="worker-2")

                events = lifecycle_events.read_events(
                    lib.SPAWN_DIR, "worker-2"
                )
                self.assertEqual(len(events), 2)
                self.assertEqual(events[0]["event"], "stop")
                self.assertEqual(events[0]["state"], "idle")
                self.assertEqual(events[1]["event"], "turn_start")
                self.assertEqual(events[1]["state"], "running")

                # The handle also says running (not stale idle).
                h = lib.read_handle("worker-2")
                self.assertEqual(h["state"], "running")


class TestLifecycleFailOpen(unittest.TestCase):
    """Epic 37: lifecycle event write failure must never disrupt the session."""

    def test_event_write_failure_does_not_block_handle_write(self):
        """If the lifecycle log directory is unwritable, handle still updates."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "proj" / "sid.jsonl"
                _write_transcript(tpath)
                _seed_handle("proj-2", "/ws/proj", str(tpath))

                # Make the spawn dir read-only so the lifecycle log can't be
                # written (but the handle write has already happened via
                # tmp+rename, which works in the dir).
                # Actually, we need to test that append_event failure doesn't
                # prevent the main function from succeeding. We mock it.
                original_append = lifecycle_events.append_event

                def failing_append(*args, **kwargs):
                    raise IOError("disk full")

                lifecycle_events.append_event = failing_append
                try:
                    code = _run(
                        "Stop",
                        {"last_assistant_message": "done",
                         "background_tasks": [],
                         "transcript_path": str(tpath)},
                        amux_name="proj-2",
                    )
                    self.assertEqual(code, 0)

                    # Handle was still written correctly.
                    h = lib.read_handle("proj-2")
                    self.assertEqual(h["state"], "idle")
                    self.assertEqual(h["last_message"], "done")
                finally:
                    lifecycle_events.append_event = original_append


if __name__ == "__main__":
    unittest.main()

