#!/usr/bin/env python3
"""
Tests for the prompt-cache freshness cell (statusline.py + subagent.py).

Neither payload carries a last-activity timestamp, so both scripts read it off
the transcript each session and each subagent writes as it works. These tests
pin that down:

- The TTL is read back from recorded usage (1h vs 5m), never assumed, and a
  turn that only read the cache does not confuse the lookup.
- Warm counts down what is left; cold reports how long it has been idle.
- A subagent's transcript is found flat and one workflow level down, and a task
  id that is not an id is never turned into a path.
- An agent with no usage of its own inherits the session's TTL.
- Nothing known anywhere means no column at all, not an empty one.

Run: python3 .claude/statusline/test_cache_freshness.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUBAGENT = os.path.join(HERE, "subagent.py")
STATUSLINE = os.path.join(HERE, "statusline.py")

sys.path.insert(0, HERE)

import statusline  # noqa: E402
import subagent  # noqa: E402

SESSION = "c0ffee11-2222-3333-4444-555555555555"


def usage_line(ephemeral_1h=0, ephemeral_5m=0, kind="assistant") -> str:
    return json.dumps({
        "type": kind,
        "timestamp": "2026-09-17T20:00:00.000Z",
        "message": {"usage": {
            "cache_read_input_tokens": 103868,
            "cache_creation": {
                "ephemeral_1h_input_tokens": ephemeral_1h,
                "ephemeral_5m_input_tokens": ephemeral_5m,
            },
        }},
    })


def write(path: str, lines: list, age_seconds: float = 0.0) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class TtlDetectionTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def path(self, name="t.jsonl"):
        return os.path.join(self.dir, name)

    def test_one_hour_ttl_is_read_from_the_recorded_usage(self):
        p = write(self.path(), [usage_line(ephemeral_1h=954)])
        self.assertEqual(statusline.detect_cache_ttl(p), 3600)

    def test_five_minute_ttl_is_read_from_the_recorded_usage(self):
        p = write(self.path(), [usage_line(ephemeral_5m=1759)])
        self.assertEqual(statusline.detect_cache_ttl(p), 300)

    def test_cache_read_only_turns_are_walked_past(self):
        # A turn that wrote nothing new records zeros in both counters; the TTL
        # still stands from the call that did write the entry.
        p = write(self.path(), [
            usage_line(ephemeral_1h=954),
            usage_line(ephemeral_1h=0, ephemeral_5m=0),
            usage_line(ephemeral_1h=0, ephemeral_5m=0),
        ])
        self.assertEqual(statusline.detect_cache_ttl(p), 3600)

    def test_entries_without_usage_are_ignored(self):
        p = write(self.path(), [
            json.dumps({"type": "attachment", "timestamp": "2026-09-17T20:00:00.000Z"}),
            json.dumps({"type": "user", "message": {"content": "hello"}}),
        ])
        self.assertIsNone(statusline.detect_cache_ttl(p))

    def test_a_tail_of_one_huge_entry_does_not_hide_the_ttl(self):
        # The first read starts mid-line and yields nothing usable; the second,
        # larger read is what rescues it.
        p = write(self.path(), [
            usage_line(ephemeral_1h=954),
            json.dumps({"type": "user", "message": {"content": "x" * 200000}}),
        ])
        self.assertEqual(statusline.detect_cache_ttl(p), 3600)

    def test_a_few_read_only_turns_do_not_hide_a_ttl_further_back(self):
        # The decisive call sits beyond the first read; a handful of cache-read
        # turns at the tail is normal, so the larger read still happens.
        p = write(self.path(), [
            usage_line(ephemeral_1h=954),
            json.dumps({"type": "user", "message": {"content": "x" * 70000}}),
        ] + [usage_line() for _ in range(5)])
        self.assertEqual(statusline.detect_cache_ttl(p), 3600)

    def test_a_provider_that_never_reports_buckets_is_not_chased_further(self):
        # Every turn recording zeros is not a gap to read past, it is the
        # provider saying it does not attribute a TTL. Reading the whole
        # transcript on every tick to learn that again would be waste.
        padded = json.dumps({"type": "assistant", "pad": "x" * 3000, "message": {"usage": {
            "cache_read_input_tokens": 184768,
            "cache_creation": {"ephemeral_1h_input_tokens": 0,
                               "ephemeral_5m_input_tokens": 0}}}})
        p = write(self.path(), [usage_line(ephemeral_1h=954)] + [padded] * 25)
        self.assertGreater(os.path.getsize(p), 64 * 1024)
        self.assertIsNone(statusline.detect_cache_ttl(p))

    def test_malformed_lines_do_not_raise(self):
        p = write(self.path(), ["{not json", usage_line(ephemeral_5m=10), "}}}"])
        self.assertEqual(statusline.detect_cache_ttl(p), 300)

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(statusline.detect_cache_ttl(self.path("nope.jsonl")))
        self.assertIsNone(statusline.detect_cache_ttl(""))
        self.assertIsNone(statusline.last_activity_epoch(self.path("nope.jsonl")))

    def test_last_activity_is_the_transcripts_mtime(self):
        p = write(self.path(), [usage_line(ephemeral_1h=1)], age_seconds=600)
        self.assertAlmostEqual(statusline.last_activity_epoch(p), time.time() - 600, delta=5)


class CacheStateTest(unittest.TestCase):

    def test_warm_counts_down_what_is_left(self):
        now = 1_000_000.0
        word, age, color = statusline.cache_state(now - 120, 3600, now)
        self.assertEqual((word, age), ("warm", "58m"))
        self.assertEqual(color, "32")

    def test_cold_reports_idle_time_instead_of_a_countdown(self):
        now = 1_000_000.0
        word, age, color = statusline.cache_state(now - 8100, 300, now)
        self.assertEqual((word, age), ("cold", "2h15m"))
        self.assertEqual(color, "2")

    def test_expiry_warning_starts_inside_the_last_tenth(self):
        now = 1_000_000.0
        self.assertEqual(statusline.cache_state(now - 3300, 3600, now)[2], "33")  # 5m left
        self.assertEqual(statusline.cache_state(now - 3000, 3600, now)[2], "32")  # 10m left

    def test_short_ttls_still_get_a_full_minute_of_warning(self):
        # A tenth of 5m is 30s; that is too late to be worth showing.
        now = 1_000_000.0
        self.assertEqual(statusline.cache_state(now - 250, 300, now)[2], "33")

    def test_the_moment_of_expiry_reads_cold(self):
        now = 1_000_000.0
        self.assertEqual(statusline.cache_state(now - 300, 300, now)[0], "cold")

    def test_an_unknown_ttl_reports_idle_time_and_claims_no_state(self):
        # GLM and friends report cache reads without a 5m/1h bucket, so the
        # lifetime is unknowable — but the clock still is not.
        now = 1_000_000.0
        self.assertEqual(statusline.cache_state(now - 740, None, now), ("idle", "12m", "2"))

    def test_no_clock_means_no_state_at_all(self):
        self.assertIsNone(statusline.cache_state(None, 3600, 1.0))
        self.assertIsNone(statusline.cache_state(None, None, 1.0))

    def test_durations_are_coarse_enough_to_read(self):
        self.assertEqual(statusline.format_cache_age(45), "45s")
        self.assertEqual(statusline.format_cache_age(59.9), "59s")
        self.assertEqual(statusline.format_cache_age(60), "1m")
        self.assertEqual(statusline.format_cache_age(3480), "58m")
        self.assertEqual(statusline.format_cache_age(3600), "1h00m")
        self.assertEqual(statusline.format_cache_age(8100), "2h15m")
        self.assertEqual(statusline.format_cache_age(-5), "0s")


class AgentTranscriptPathTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.project = os.path.join(self.dir, ".claude", "projects", "-repo")
        self.transcript = os.path.join(self.project, f"{SESSION}.jsonl")
        write(self.transcript, [usage_line(ephemeral_1h=954)])
        self.subagents = os.path.join(self.project, SESSION, "subagents")

    def resolve(self, task_id, session_id=SESSION):
        return subagent.agent_transcript_path(self.transcript, session_id, task_id)

    def test_finds_the_flat_agent_transcript(self):
        want = write(os.path.join(self.subagents, "agent-a1.jsonl"), [usage_line()])
        self.assertEqual(self.resolve("a1"), want)

    def test_finds_a_workflow_agent_one_level_down(self):
        want = write(os.path.join(self.subagents, "workflows", "run1", "agent-a2.jsonl"),
                     [usage_line()])
        self.assertEqual(self.resolve("a2"), want)

    def test_missing_transcript_resolves_to_nothing(self):
        # Forks run with skipTranscript, so this is a normal outcome.
        os.makedirs(self.subagents, exist_ok=True)
        self.assertIsNone(self.resolve("a3"))

    def test_a_task_id_is_never_turned_into_a_path(self):
        for hostile in ("../../etc/passwd", "a/b", ".", "", None, "a.jsonl"):
            self.assertIsNone(self.resolve(hostile), hostile)

    def test_a_session_id_is_never_turned_into_a_path(self):
        write(os.path.join(self.subagents, "agent-a1.jsonl"), [usage_line()])
        # A served session reports `served:<caller>`; the filesystem never saw
        # that name, so the transcript's own name is what is used.
        self.assertEqual(self.resolve("a1", "served:elsewhere"),
                         os.path.join(self.subagents, "agent-a1.jsonl"))
        self.assertEqual(self.resolve("a1", None),
                         os.path.join(self.subagents, "agent-a1.jsonl"))

    def test_no_transcript_path_resolves_to_nothing(self):
        self.assertIsNone(subagent.agent_transcript_path("", SESSION, "a1"))
        self.assertIsNone(subagent.agent_transcript_path(None, SESSION, "a1"))


class RowRenderingTest(unittest.TestCase):
    """The cell as it reaches the agent panel."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.project = os.path.join(self.home, ".claude", "projects", "-repo")
        self.transcript = os.path.join(self.project, f"{SESSION}.jsonl")
        write(self.transcript, [usage_line(ephemeral_1h=954)], age_seconds=120)
        self.subagents = os.path.join(self.project, SESSION, "subagents")

    def agent(self, task_id, lines, age_seconds):
        write(os.path.join(self.subagents, f"agent-{task_id}.jsonl"), lines, age_seconds)

    def rows(self, tasks, columns=120, no_color="1"):
        payload = {"session_id": SESSION, "transcript_path": self.transcript,
                   "columns": columns, "tasks": tasks}
        result = subprocess.run(
            [sys.executable, SUBAGENT], input=json.dumps(payload),
            env={"PATH": "", "HOME": self.home, "CC_STATUS_NO_COLOR": no_color},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line)["content"] for line in result.stdout.splitlines()]

    def task(self, task_id, **overrides):
        base = {"id": task_id, "type": "local_agent", "status": "running",
                "description": "do the thing", "startTime": int((time.time() - 72) * 1000),
                "model": "claude-opus-5", "contextWindowSize": 1000000,
                "tokenCount": 100000}
        base.update(overrides)
        return base

    def test_a_working_agent_shows_the_time_its_cache_has_left(self):
        self.agent("a1", [usage_line(ephemeral_1h=800)], age_seconds=130)
        self.assertIn("warm 57m", self.rows([self.task("a1")])[0])

    def test_a_long_finished_agent_shows_how_long_it_has_been_cold(self):
        self.agent("a1", [usage_line(ephemeral_5m=500)], age_seconds=8100)
        row = self.rows([self.task("a1", status="completed")])[0]
        self.assertIn("cold 2h15m", row)

    def test_a_spawning_agent_never_inherits_the_sessions_hour(self):
        """The session caches for 1h; the agents it spawns cache for 5m.

        Regression: reading the session's TTL into a just-spawned row showed
        `warm 59m` on spawn, then dropped to `warm 4m` the moment the agent
        answered and its own usage could be read.
        """
        self.agent("done", [usage_line(ephemeral_5m=1759)], age_seconds=60)
        self.agent("fresh", [json.dumps({"type": "attachment"})], age_seconds=30)
        row = self.rows([self.task("fresh")])[0]
        self.assertIn("warm 4m", row)          # 5m window, 30s in
        self.assertNotIn("59m", row)

    def test_a_spawning_agent_reads_the_ttl_its_siblings_record(self):
        # Sibling evidence, not a guess: whatever the other agents in this
        # session recorded is what this one will record.
        self.agent("done", [usage_line(ephemeral_1h=954)], age_seconds=60)
        self.agent("fresh", [json.dumps({"type": "attachment"})], age_seconds=610)
        self.assertIn("warm 49m", self.rows([self.task("fresh")])[0])

    def test_the_first_agent_of_a_session_waits_for_its_own_usage(self):
        # Nothing has answered yet anywhere, so there is no evidence to read —
        # and no number is better than the session's wrong one.
        self.agent("only", [json.dumps({"type": "attachment"})], age_seconds=30)
        row = self.rows([self.task("only")])[0]
        self.assertNotIn("warm", row)
        self.assertNotIn("cold", row)
        self.assertNotIn("idle", row)

    def test_an_agent_on_a_provider_without_ttl_buckets_shows_idle_time(self):
        zero = json.dumps({"type": "assistant", "message": {"usage": {
            "cache_read_input_tokens": 184768,
            "cache_creation": {"ephemeral_1h_input_tokens": 0,
                               "ephemeral_5m_input_tokens": 0}}}})
        write(self.transcript, [zero], age_seconds=120)   # the session cannot say either
        self.agent("a1", [zero], age_seconds=740)
        row = self.rows([self.task("a1")])[0]
        self.assertIn("idle 12m", row)

    def test_the_column_disappears_when_no_agent_has_a_transcript(self):
        rows = self.rows([self.task("a1", name="one"), self.task("a2", name="two")])
        for row in rows:
            self.assertNotIn("warm", row)
            self.assertNotIn("cold", row)
            self.assertFalse(row.rstrip().endswith("  "), row)

    def test_agents_stay_column_aligned_when_only_some_have_a_transcript(self):
        self.agent("a1", [usage_line(ephemeral_1h=800)], age_seconds=130)
        rows = self.rows([self.task("a1", name="one"), self.task("a2", name="two")])
        offsets = [row.index("do the thing") for row in rows]
        self.assertEqual(offsets[0], offsets[1])

    def test_durations_line_up_under_each_other(self):
        self.agent("a1", [usage_line(ephemeral_1h=800)], age_seconds=130)      # warm 57m
        self.agent("a2", [usage_line(ephemeral_1h=800)], age_seconds=8100)     # cold 2h15m
        rows = self.rows([self.task("a1", name="one"), self.task("a2", name="two")])
        self.assertEqual(rows[0].index("57m") + 3, rows[1].index("2h15m") + 5)

    def test_warm_is_green_and_cold_is_dim(self):
        self.agent("a1", [usage_line(ephemeral_1h=800)], age_seconds=130)
        self.agent("a2", [usage_line(ephemeral_1h=800)], age_seconds=8100)
        rows = self.rows([self.task("a1", name="one"), self.task("a2", name="two")],
                         no_color="")
        self.assertIn("\x1b[32mwarm", rows[0])
        self.assertIn("\x1b[2mcold", rows[1])

    def test_the_row_still_fits_its_column_budget(self):
        self.agent("a1", [usage_line(ephemeral_1h=800)], age_seconds=130)
        for columns in (10, 20, 30, 40, 60, 100, 200):
            row = self.rows([self.task("a1", name="Explore-2", description="x" * 300)],
                            columns=columns)[0]
            self.assertLessEqual(len(row), columns, f"overflowed at columns={columns}")

    def test_a_payload_without_a_transcript_path_renders_without_the_cell(self):
        payload = {"session_id": SESSION, "columns": 120, "tasks": [self.task("a1")]}
        result = subprocess.run(
            [sys.executable, SUBAGENT], input=json.dumps(payload),
            env={"PATH": "", "HOME": self.home, "CC_STATUS_NO_COLOR": "1"},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("warm", result.stdout)
        self.assertNotIn("cold", result.stdout)


class MainStatusLineTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.project = os.path.join(self.home, ".claude", "projects", "-repo")
        self.transcript = os.path.join(self.project, f"{SESSION}.jsonl")

    def render(self, status_input: dict) -> str:
        result = subprocess.run(
            [sys.executable, STATUSLINE], input=json.dumps(status_input),
            env={"PATH": "", "HOME": self.home}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def payload(self, **overrides):
        base = {"session_id": SESSION, "transcript_path": self.transcript,
                "model": {"id": "claude-opus-5", "display_name": "Opus"},
                "context_window": {"used_percentage": 61}}
        base.update(overrides)
        return base

    def test_the_segment_follows_the_context_segment(self):
        write(self.transcript, [usage_line(ephemeral_1h=954)], age_seconds=130)
        self.assertEqual(self.render(self.payload()), "Opus | ctx 61% | warm 57m")

    def test_an_abandoned_session_reads_cold(self):
        write(self.transcript, [usage_line(ephemeral_1h=954)], age_seconds=9000)
        self.assertEqual(self.render(self.payload()), "Opus | ctx 61% | cold 2h30m")

    def test_no_segment_without_a_transcript(self):
        self.assertEqual(self.render(self.payload()), "Opus | ctx 61%")
        self.assertEqual(self.render(self.payload(transcript_path=None)), "Opus | ctx 61%")
        self.assertEqual(self.render({"model": {"id": "claude-opus-5",
                                                "display_name": "Opus"}}), "Opus | ctx ?")

    def test_a_provider_without_ttl_buckets_still_reports_when_it_last_moved(self):
        zero = json.dumps({"type": "assistant", "message": {"usage": {
            "cache_read_input_tokens": 184768,
            "cache_creation": {"ephemeral_1h_input_tokens": 0,
                               "ephemeral_5m_input_tokens": 0}}}})
        write(self.transcript, [zero], age_seconds=740)
        self.assertEqual(self.render(self.payload()), "Opus | ctx 61% | idle 12m")

    def test_a_session_that_has_not_called_the_api_has_nothing_to_report(self):
        # Transcript open, no request made: there is no cache yet, and `idle 0s`
        # would only flip to a real reading seconds later.
        write(self.transcript, [json.dumps({"type": "attachment"})], age_seconds=5)
        self.assertEqual(self.render(self.payload()), "Opus | ctx 61%")

    def test_the_segment_makes_no_network_calls(self):
        """Block the network, then render: the cache segment is a local read."""
        write(self.transcript, [usage_line(ephemeral_1h=954)], age_seconds=130)
        helper = r"""
import sys, json, os
sys.path.insert(0, %r)
import statusline
import socket, urllib.request
def _block(*a, **kw): raise RuntimeError("network not allowed in the cache path")
socket.create_connection = _block
urllib.request.urlopen = _block
print(statusline.format_cache_segment(json.loads(%r)))
"""
        proc = subprocess.run(
            [sys.executable, "-c", helper % (HERE, json.dumps(self.payload()))],
            env={"PATH": "", "HOME": self.home}, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warm 57m", proc.stdout)

    def test_a_path_the_os_rejects_costs_the_segment_not_the_line(self):
        # An uncaught ValueError here would drop the whole line to its fallback.
        self.assertEqual(self.render(self.payload(transcript_path="x\x00y.jsonl")),
                         "Opus | ctx 61%")


if __name__ == "__main__":
    unittest.main(verbosity=2)
