#!/usr/bin/env python3
"""Unit tests for claude_event_reducer (epic 37, task 37-02).

Covers the pure reducer module in isolation — no subprocess, no network,
no amux_spawn_lib.  Mirrors the pattern established by
test_unit_codex_reducer.py.

Test categories:
  - empty_reduction shape and defaults
  - reduce_events with each event type
  - State hint derivation across the table in the module docstring
  - SubagentStop bg-drain semantics (no premature idle)
  - Follow-up turn after settlement (turn_start + stop + turn_start → running)
  - reduce_log: no file, empty file, truncated last line, malformed lines,
    non-dict values, unknown event types, large file
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(_HOOKS))

import claude_event_reducer as reducer  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ev(event: str, state: str = "running", **kwargs) -> dict:
    """Build a minimal lifecycle event dict for testing."""
    base = {
        "seq": 1,
        "ts": "2026-09-07T10:00:00+00:00",
        "event": event,
        "state": state,
        "session_id": "sid-test",
        "background_tasks_count": 0,
        "permission_pending": False,
    }
    base.update(kwargs)
    return base


# ── empty_reduction ───────────────────────────────────────────────────────────

class TestEmptyReduction(unittest.TestCase):
    def test_shape(self):
        r = reducer.empty_reduction()
        self.assertIsNone(r["state_hint"])
        self.assertFalse(r["turn_open"])
        self.assertFalse(r["has_stop_event"])
        self.assertFalse(r["permission_pending"])
        self.assertEqual(r["background_tasks_count"], 0)
        self.assertIsNone(r["last_stop_state"])
        self.assertIsNone(r["last_event_ts"])
        self.assertFalse(r["terminated"])
        self.assertEqual(r["events"], 0)
        self.assertEqual(r["malformed_lines"], 0)
        self.assertFalse(r["truncated_tail"])
        self.assertIsInstance(r["unknown_event_types"], list)
        self.assertEqual(r["unknown_event_types"], [])

    def test_idempotent(self):
        """Two calls return equal but distinct dicts."""
        a = reducer.empty_reduction()
        b = reducer.empty_reduction()
        self.assertEqual(a, b)
        # Mutating one must not affect the other.
        a["unknown_event_types"].append("x")
        self.assertEqual(b["unknown_event_types"], [])


# ── reduce_events: individual event types ─────────────────────────────────────

class TestReduceEventsTurnStart(unittest.TestCase):
    def test_opens_turn(self):
        r = reducer.reduce_events([_ev("turn_start")])
        self.assertTrue(r["turn_open"])
        self.assertEqual(r["state_hint"], "running")

    def test_clears_permission_pending(self):
        """A new turn start resets a pending permission gate."""
        events = [
            _ev("permission_prompt", permission_pending=True),
            _ev("turn_start"),
        ]
        r = reducer.reduce_events(events)
        self.assertFalse(r["permission_pending"])

    def test_reopens_after_stop(self):
        """turn_start after stop → turn is open again → running."""
        events = [
            _ev("turn_start"),
            _ev("stop"),
            _ev("turn_start"),
        ]
        r = reducer.reduce_events(events)
        self.assertTrue(r["turn_open"])
        self.assertEqual(r["state_hint"], "running")


class TestReduceEventsStop(unittest.TestCase):
    def test_closes_turn(self):
        events = [_ev("turn_start"), _ev("stop", background_tasks_count=0)]
        r = reducer.reduce_events(events)
        self.assertFalse(r["turn_open"])
        self.assertTrue(r["has_stop_event"])
        self.assertEqual(r["state_hint"], "idle")

    def test_stop_with_bg_tasks(self):
        """Stop with bg > 0 → state_hint = running (background work outstanding)."""
        events = [
            _ev("turn_start"),
            _ev("stop", state="running", background_tasks_count=2),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "running")
        self.assertEqual(r["background_tasks_count"], 2)

    def test_last_stop_state_recorded(self):
        events = [_ev("turn_start"), _ev("stop", state="idle")]
        r = reducer.reduce_events(events)
        self.assertEqual(r["last_stop_state"], "idle")

    def test_stop_clears_permission_pending(self):
        events = [
            _ev("permission_prompt", permission_pending=True),
            _ev("turn_start"),
            _ev("stop", background_tasks_count=0),
        ]
        r = reducer.reduce_events(events)
        self.assertFalse(r["permission_pending"])


class TestReduceEventsSubagentStop(unittest.TestCase):
    def test_subagent_stop_bg_zero_still_running(self):
        """SubagentStop(bg=0) must NOT produce idle — the final stop hasn't fired."""
        events = [
            _ev("turn_start"),
            _ev("stop", state="running", background_tasks_count=1),
            _ev("subagent_stop", background_tasks_count=0),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "running",
                         "subagent_stop draining bg to 0 must not produce idle")

    def test_subagent_stop_updates_bg_count(self):
        events = [
            _ev("turn_start"),
            _ev("stop", state="running", background_tasks_count=3),
            _ev("subagent_stop", background_tasks_count=2),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["background_tasks_count"], 2)

    def test_stop_after_subagent_stop_can_idle(self):
        """A stop AFTER a subagent_stop(bg=0) → final stop confirms idle."""
        events = [
            _ev("turn_start"),
            _ev("stop", state="running", background_tasks_count=1),
            _ev("subagent_stop", background_tasks_count=0),
            _ev("turn_start"),
            _ev("stop", state="idle", background_tasks_count=0),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "idle")


class TestReduceEventsPermissionPrompt(unittest.TestCase):
    def test_sets_permission_pending(self):
        r = reducer.reduce_events([_ev("turn_start"),
                                   _ev("permission_prompt", permission_pending=True)])
        self.assertTrue(r["permission_pending"])
        # turn is still open
        self.assertEqual(r["state_hint"], "running")

    def test_permission_prompt_always_sets_pending(self):
        """A permission_prompt event always marks a gate open.

        The only events that clear permission_pending are turn_start and stop —
        the permission_prompt hook always fires when a prompt is pending.
        """
        events = [
            _ev("permission_prompt", permission_pending=True),
        ]
        r = reducer.reduce_events(events)
        # After a permission_prompt event, pending is always True regardless
        # of what the event's own permission_pending field says (the branch
        # unconditionally sets it, which is correct — the hook fires BECAUSE
        # the prompt was raised).
        self.assertTrue(r["permission_pending"])

    def test_turn_start_clears_pending(self):
        """A new turn clears a prior pending permission gate (it was resolved)."""
        events = [
            _ev("turn_start"),
            _ev("permission_prompt", permission_pending=True),
            _ev("turn_start"),  # next turn — permission was resolved
        ]
        r = reducer.reduce_events(events)
        self.assertFalse(r["permission_pending"])


class TestReduceEventsSessionEnd(unittest.TestCase):
    def test_terminated(self):
        events = [_ev("turn_start"), _ev("session_end")]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "terminated")
        self.assertTrue(r["terminated"])
        self.assertFalse(r["turn_open"])

    def test_terminated_dominates_open_turn(self):
        """session_end wins even if we later see a stale turn_start (edge case)."""
        events = [_ev("turn_start"), _ev("session_end"), _ev("turn_start")]
        r = reducer.reduce_events(events)
        # Terminated is set; state_hint must be terminated because we set
        # turn_open=True again but then the hint logic checks terminated first.
        # Note: turn_open is technically True here, but state_hint is still
        # terminated because the check is ordered: terminated → running → …
        self.assertEqual(r["state_hint"], "terminated")


# ── State hint table ──────────────────────────────────────────────────────────

class TestStateHintTable(unittest.TestCase):
    """Validate every row in the docstring's state-hint table."""

    def test_row_terminated(self):
        r = reducer.reduce_events([_ev("session_end")])
        self.assertEqual(r["state_hint"], "terminated")

    def test_row_running_open_turn(self):
        r = reducer.reduce_events([_ev("turn_start")])
        self.assertEqual(r["state_hint"], "running")

    def test_row_running_bg_outstanding(self):
        events = [_ev("turn_start"), _ev("stop", background_tasks_count=1)]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "running")

    def test_row_running_subagent_stop_bg_zero(self):
        events = [
            _ev("turn_start"),
            _ev("stop", background_tasks_count=1),
            _ev("subagent_stop", background_tasks_count=0),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "running")

    def test_row_idle(self):
        events = [_ev("turn_start"), _ev("stop", background_tasks_count=0)]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "idle")

    def test_row_none(self):
        r = reducer.reduce_events([])
        self.assertIsNone(r["state_hint"])

    def test_row_none_no_stop(self):
        """Events present but no stop → None (caller uses stored state)."""
        r = reducer.reduce_events([_ev("permission_prompt")])
        self.assertIsNone(r["state_hint"])


# ── Activity clock ────────────────────────────────────────────────────────────

class TestActivityClock(unittest.TestCase):
    def test_last_event_ts_tracks_latest(self):
        ts_early = "2026-09-07T10:00:00+00:00"
        ts_late = "2026-09-07T11:00:00+00:00"
        events = [
            _ev("turn_start", ts=ts_early),
            _ev("stop", ts=ts_late),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["last_event_ts"], ts_late)

    def test_no_ts_field(self):
        """An event without a ts field does not crash and does not update clock."""
        ev = {"event": "turn_start", "state": "running", "background_tasks_count": 0,
              "permission_pending": False, "session_id": "s"}
        r = reducer.reduce_events([ev])
        self.assertIsNone(r["last_event_ts"])


# ── Malformed and unknown events ──────────────────────────────────────────────

class TestForwardCompatibility(unittest.TestCase):
    def test_unknown_event_type_counted(self):
        ev = _ev("future_event_type_unknown")
        r = reducer.reduce_events([ev])
        self.assertIn("future_event_type_unknown", r["unknown_event_types"])

    def test_unknown_event_does_not_affect_state(self):
        r = reducer.reduce_events([_ev("future_event_type_unknown")])
        self.assertIsNone(r["state_hint"])

    def test_malformed_non_dict_counts(self):
        """Non-dict items in the events list count as malformed."""
        r = reducer.reduce_events(["not a dict", 42, None])
        self.assertEqual(r["malformed_lines"], 3)

    def test_malformed_no_event_field(self):
        """A dict without 'event' counts as malformed."""
        r = reducer.reduce_events([{"ts": "2026-09-07T10:00:00+00:00"}])
        self.assertEqual(r["malformed_lines"], 1)

    def test_max_unknown_types_capped(self):
        """At most MAX_UNKNOWN_TYPES distinct unknown types are collected."""
        events = [_ev(f"unknown_type_{i}") for i in range(100)]
        r = reducer.reduce_events(events)
        self.assertLessEqual(len(r["unknown_event_types"]), reducer.MAX_UNKNOWN_TYPES)

    def test_unknown_types_deduplicated(self):
        events = [_ev("unknown_x"), _ev("unknown_x"), _ev("unknown_x")]
        r = reducer.reduce_events(events)
        self.assertEqual(r["unknown_event_types"].count("unknown_x"), 1)


# ── reduce_log file I/O ───────────────────────────────────────────────────────

class TestReduceLog(unittest.TestCase):
    def _write_log(self, spawn_dir: Path, name: str, lines: list[str]) -> None:
        log = spawn_dir / f"{name}{reducer.CLAUDE_EVENT_SUFFIX}"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(lines) + ("\n" if lines else ""))

    def test_no_file(self):
        """Missing log returns empty_reduction, does not raise."""
        with tempfile.TemporaryDirectory() as d:
            r = reducer.reduce_log(Path(d), "nonexistent")
        self.assertEqual(r, reducer.empty_reduction())

    def test_empty_file(self):
        """Empty log returns empty_reduction."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            self._write_log(spawn, "w1", [])
            r = reducer.reduce_log(spawn, "w1")
        self.assertEqual(r, reducer.empty_reduction())

    def test_valid_log(self):
        """A well-formed log is reduced correctly."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            events = [
                _ev("turn_start"),
                _ev("stop", background_tasks_count=0),
            ]
            self._write_log(spawn, "w1",
                            [json.dumps(e) for e in events])
            r = reducer.reduce_log(spawn, "w1")
        self.assertEqual(r["state_hint"], "idle")
        self.assertEqual(r["events"], 2)

    def test_truncated_last_line(self):
        """A partial last line (live O_APPEND write) is tolerated as truncated_tail."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            good_line = json.dumps(_ev("turn_start"))
            partial = '{"event": "stop", "state": "idle", "ts": "20'  # incomplete
            self._write_log(spawn, "w1", [good_line, partial])
            r = reducer.reduce_log(spawn, "w1")
        self.assertTrue(r["truncated_tail"])
        self.assertEqual(r["events"], 1)   # only the valid line counted
        self.assertEqual(r["malformed_lines"], 0)  # tail not counted as malformed

    def test_malformed_middle_line(self):
        """A malformed line in the middle is counted as malformed_lines."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            good = json.dumps(_ev("turn_start"))
            bad = "not valid json {"
            also_good = json.dumps(_ev("stop"))
            self._write_log(spawn, "w1", [good, bad, also_good])
            r = reducer.reduce_log(spawn, "w1")
        # Middle bad line is a malformed_line, not truncated_tail.
        self.assertEqual(r["malformed_lines"], 1)
        self.assertFalse(r["truncated_tail"])
        self.assertEqual(r["events"], 2)

    def test_only_blank_lines(self):
        """A file with only blank lines returns empty_reduction."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            (spawn / "w1.lifecycle.jsonl").write_text("\n\n\n")
            r = reducer.reduce_log(spawn, "w1")
        self.assertIsNone(r["state_hint"])

    def test_non_dict_json_value(self):
        """A valid JSON line that is not a dict (e.g. a number) is malformed."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            self._write_log(spawn, "w1", ["42", json.dumps(_ev("turn_start"))])
            r = reducer.reduce_log(spawn, "w1")
        # 42 is valid JSON but not a dict → malformed in reduce_events
        self.assertEqual(r["malformed_lines"], 1)
        self.assertEqual(r["events"], 1)

    def test_log_suffix_constant_matches_lifecycle_events(self):
        """The suffix constant must equal lifecycle_events.CLAUDE_EVENT_SUFFIX."""
        try:
            import lifecycle_events
            self.assertEqual(reducer.CLAUDE_EVENT_SUFFIX,
                             lifecycle_events.CLAUDE_EVENT_SUFFIX)
        except ImportError:
            self.skipTest("lifecycle_events not importable in this environment")

    def test_permission_pending_from_log(self):
        """permission_pending is extracted from the log events."""
        with tempfile.TemporaryDirectory() as d:
            spawn = Path(d)
            events = [
                _ev("turn_start"),
                _ev("permission_prompt", permission_pending=True),
            ]
            self._write_log(spawn, "w1", [json.dumps(e) for e in events])
            r = reducer.reduce_log(spawn, "w1")
        self.assertTrue(r["permission_pending"])


# ── Follow-up turn from event log ─────────────────────────────────────────────

class TestFollowUpTurn(unittest.TestCase):
    """After a settled idle state, a new turn_start opens the turn again."""

    def test_turn_start_after_idle_is_running(self):
        events = [
            _ev("turn_start"),
            _ev("stop", background_tasks_count=0),
            _ev("turn_start"),  # operator sends a follow-up
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "running")
        self.assertTrue(r["turn_open"])

    def test_second_stop_settles_idle(self):
        events = [
            _ev("turn_start"),
            _ev("stop", background_tasks_count=0),
            _ev("turn_start"),
            _ev("stop", background_tasks_count=0),
        ]
        r = reducer.reduce_events(events)
        self.assertEqual(r["state_hint"], "idle")
        self.assertFalse(r["turn_open"])

    def test_events_counter_accumulates(self):
        events = [_ev("turn_start"), _ev("stop"), _ev("turn_start"), _ev("stop")]
        r = reducer.reduce_events(events)
        self.assertEqual(r["events"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
