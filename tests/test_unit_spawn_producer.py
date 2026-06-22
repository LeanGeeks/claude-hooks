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


if __name__ == "__main__":
    unittest.main()
