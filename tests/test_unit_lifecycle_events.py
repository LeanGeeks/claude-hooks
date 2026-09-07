#!/usr/bin/env python3
"""Unit tests for the lifecycle event log module (epic 37, task 37-01).

Exercises ``lifecycle_events`` in isolation: record construction,
serialization, size bounding, append, and read-back.  The tests use a
throwaway directory for the log files — nothing touches the developer's
real ``~/.amux``.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(_HOOKS))

import lifecycle_events as le  # noqa: E402


class TestMakeEvent(unittest.TestCase):
    """Record construction."""

    def test_minimal_record_has_required_fields(self):
        record = le.make_event(
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid-1",
            seq=1,
        )
        self.assertEqual(record["event"], "stop")
        self.assertEqual(record["state"], "idle")
        self.assertEqual(record["session_id"], "sid-1")
        self.assertEqual(record["seq"], 1)
        self.assertIn("ts", record)
        self.assertEqual(record["background_tasks_count"], 0)
        self.assertFalse(record["permission_pending"])
        self.assertNotIn("last_message", record)
        self.assertNotIn("last_state", record)

    def test_all_optional_fields(self):
        record = le.make_event(
            event=le.EVENT_SESSION_END,
            state="terminated",
            session_id="sid-2",
            seq=5,
            last_message="goodbye",
            background_tasks_count=3,
            permission_pending=True,
            last_state="running",
        )
        self.assertEqual(record["last_message"], "goodbye")
        self.assertEqual(record["background_tasks_count"], 3)
        self.assertTrue(record["permission_pending"])
        self.assertEqual(record["last_state"], "running")


class TestSerializeBounded(unittest.TestCase):
    """Serialization and size bounding."""

    def test_small_record_is_unchanged(self):
        record = le.make_event(
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid",
            seq=1,
            last_message="hello",
        )
        data = le.serialize_bounded(record)
        self.assertLessEqual(len(data), le.MAX_RECORD_BYTES)
        self.assertTrue(data.endswith(b"\n"))
        parsed = json.loads(data)
        self.assertEqual(parsed["last_message"], "hello")

    def test_oversized_last_message_is_truncated(self):
        big_msg = "x" * 10000
        record = le.make_event(
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid",
            seq=1,
            last_message=big_msg,
        )
        data = le.serialize_bounded(record)
        self.assertLessEqual(len(data), le.MAX_RECORD_BYTES)
        # The record still parses.
        parsed = json.loads(data)
        self.assertEqual(parsed["event"], "stop")
        self.assertEqual(parsed["state"], "idle")
        self.assertEqual(parsed["seq"], 1)
        # The message was truncated with the marker.
        self.assertIn(le.TRUNCATION_MARKER, parsed["last_message"])

    def test_truncated_record_keeps_tail(self):
        """Truncation keeps the TAIL of the message (conclusion lives there)."""
        msg = "BEGINNING " + "x" * 5000 + " END_MARKER"
        record = le.make_event(
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid",
            seq=1,
            last_message=msg,
        )
        data = le.serialize_bounded(record)
        parsed = json.loads(data)
        self.assertIn("END_MARKER", parsed["last_message"])
        self.assertNotIn("BEGINNING", parsed["last_message"])

    def test_record_without_last_message_is_small(self):
        record = le.make_event(
            event=le.EVENT_TURN_START,
            state="running",
            session_id="sid",
            seq=1,
        )
        data = le.serialize_bounded(record)
        self.assertLess(len(data), 300)

    def test_json_escape_heavy_message_still_bounded(self):
        """A message with many characters requiring JSON escaping stays bounded."""
        # Backslash, quotes, and control chars all expand under JSON escaping.
        heavy = '\\"\n\t' * 3000
        record = le.make_event(
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid",
            seq=1,
            last_message=heavy,
        )
        data = le.serialize_bounded(record)
        self.assertLessEqual(len(data), le.MAX_RECORD_BYTES)
        parsed = json.loads(data)
        self.assertEqual(parsed["event"], "stop")

    def test_empty_last_message_is_kept(self):
        record = le.make_event(
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid",
            seq=1,
            last_message="",
        )
        data = le.serialize_bounded(record)
        self.assertLessEqual(len(data), le.MAX_RECORD_BYTES)

    def test_pipe_buf_boundary(self):
        """Every bounded record is strictly under PIPE_BUF (4096 on Linux)."""
        for size in (100, 1000, 5000, 20000):
            record = le.make_event(
                event=le.EVENT_STOP,
                state="idle",
                session_id="sid",
                seq=1,
                last_message="a" * size,
            )
            data = le.serialize_bounded(record)
            self.assertLessEqual(
                len(data), 4096,
                f"Record with {size}-char message exceeds PIPE_BUF",
            )


class TestAppendAndRead(unittest.TestCase):
    """Append + read-back."""

    def test_append_creates_file_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            spawn_dir = Path(d)
            le.append_event(
                spawn_dir,
                "test-worker",
                event=le.EVENT_STOP,
                state="idle",
                session_id="sid-1",
                last_message="done",
            )

            log_path = le.lifecycle_log_path(spawn_dir, "test-worker")
            self.assertTrue(log_path.exists())

            events = le.read_events(spawn_dir, "test-worker")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "stop")
            self.assertEqual(events[0]["state"], "idle")
            self.assertEqual(events[0]["seq"], 1)

    def test_multiple_appends_are_ordered(self):
        with tempfile.TemporaryDirectory() as d:
            spawn_dir = Path(d)
            for i, evt in enumerate(
                [le.EVENT_TURN_START, le.EVENT_STOP, le.EVENT_SESSION_END], 1
            ):
                le.append_event(
                    spawn_dir,
                    "worker",
                    event=evt,
                    state="running" if evt == le.EVENT_TURN_START else "idle",
                    session_id="sid",
                )

            events = le.read_events(spawn_dir, "worker")
            self.assertEqual(len(events), 3)
            self.assertEqual([e["seq"] for e in events], [1, 2, 3])
            self.assertEqual(
                [e["event"] for e in events],
                ["turn_start", "stop", "session_end"],
            )

    def test_append_is_fail_soft(self):
        """Appending to an invalid path does not raise."""
        # Path with a non-existent parent — should fail silently.
        bad_dir = Path("/nonexistent/spawn")
        le.append_event(
            bad_dir,
            "test",
            event=le.EVENT_STOP,
            state="idle",
            session_id="sid",
        )
        # No exception, no file.
        self.assertFalse(le.lifecycle_log_path(bad_dir, "test").exists())

    def test_read_empty_log(self):
        with tempfile.TemporaryDirectory() as d:
            events = le.read_events(Path(d), "nonexistent")
            self.assertEqual(events, [])

    def test_file_mode_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            spawn_dir = Path(d)
            le.append_event(
                spawn_dir,
                "priv",
                event=le.EVENT_STOP,
                state="idle",
                session_id="sid",
            )
            log_path = le.lifecycle_log_path(spawn_dir, "priv")
            mode = os.stat(log_path).st_mode & 0o777
            self.assertEqual(mode, 0o600)


class TestLifecycleLogPath(unittest.TestCase):
    def test_path_uses_suffix(self):
        p = le.lifecycle_log_path(Path("/spawn"), "worker-1")
        self.assertEqual(str(p), "/spawn/worker-1.lifecycle.jsonl")

    def test_suffix_does_not_collide_with_handle_glob(self):
        """The .lifecycle.jsonl extension cannot match *.json."""
        import fnmatch

        name = "test.lifecycle.jsonl"
        self.assertFalse(fnmatch.fnmatch(name, "*.json"))


class TestEventTypes(unittest.TestCase):
    def test_all_event_types_in_set(self):
        self.assertEqual(
            le.EVENT_TYPES,
            {"turn_start", "stop", "subagent_stop",
             "permission_prompt", "session_end"},
        )


if __name__ == "__main__":
    unittest.main()
