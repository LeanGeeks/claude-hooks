#!/usr/bin/env python3
"""
Unit Tests: epic 50-03 measurement helpers

- ``context_mcp_lib.measure_transcript`` (context, bounded tail + fallback)
- ``statusline.cache_seconds_left`` (TTL bucket + last assistant timestamp)
- ``amux_spawn_lib.last_assistant_entry`` / ``compaction_boundaries``

Fixtures are small synthetic transcripts under ``tests/fixtures/measurement/``.
Nothing here reads a real ~/.claude transcript or touches the network.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
FIX = _REPO / "tests" / "fixtures" / "measurement"
sys.path.insert(0, str(_REPO / "context-mcp"))
sys.path.insert(0, str(_REPO / ".claude" / "hooks"))
sys.path.insert(0, str(_REPO / ".claude" / "statusline"))

import amux_spawn_lib as asl  # noqa: E402
import context_mcp_lib as cml  # noqa: E402
import statusline  # noqa: E402


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()


class TestMeasureTranscript(unittest.TestCase):
    def test_normal_transcript_matches_measure_of_parse_session(self):
        p = FIX / "measure_normal.jsonl"
        got = cml.measure_transcript(p)
        self.assertEqual(got, cml._measure(cml.parse_session(p)))
        self.assertEqual(got["latest_turn"]["effective_context_tokens"], 7 + 500 + 4000)
        self.assertEqual(got["cumulative_output_tokens"], 100)
        self.assertEqual(got["turns_count"], 2)
        self.assertEqual(got["model"], "claude-test-model")
        self.assertNotIn("approximate", got)

    def test_accepts_str_path(self):
        self.assertIsNotNone(cml.measure_transcript(str(FIX / "measure_normal.jsonl")))

    def test_duplicated_streamed_message_id_counted_once(self):
        got = cml.measure_transcript(FIX / "measure_dup_streamed.jsonl")
        self.assertEqual(got["turns_count"], 2)
        self.assertEqual(got["cumulative_output_tokens"], 30)
        self.assertEqual(got["latest_turn"]["effective_context_tokens"], 6 + 2000)

    def test_tail_only_read_is_marked_approximate(self):
        # Large file, small tail: the newest usage record is inside the tail.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            src = (FIX / "measure_normal.jsonl").read_text().splitlines()
            pad = ['{"type":"user","message":{"role":"user","content":"' + "x" * 80 + '"}}'] * 30
            p.write_text("\n".join(pad + src) + "\n")
            size = p.stat().st_size
            got = cml.measure_transcript(p, tail_bytes=size // 2)
        self.assertTrue(got["approximate"])
        self.assertEqual(got["latest_turn"]["effective_context_tokens"], 7 + 500 + 4000)

    def test_small_file_read_whole_is_not_approximate(self):
        got = cml.measure_transcript(FIX / "measure_normal.jsonl", tail_bytes=10**6)
        self.assertNotIn("approximate", got)

    def test_usage_beyond_tail_falls_back_to_full_parse(self):
        p = FIX / "measure_usage_early.jsonl"
        self.assertGreater(p.stat().st_size, 600)
        got = cml.measure_transcript(p, tail_bytes=300)
        self.assertEqual(got["latest_turn"]["effective_context_tokens"], 9 + 100 + 900)
        self.assertEqual(got["turns_count"], 1)
        self.assertNotIn("approximate", got)

    def test_empty_missing_and_malformed_return_none(self):
        self.assertIsNone(cml.measure_transcript(FIX / "measure_empty.jsonl"))
        self.assertIsNone(cml.measure_transcript(FIX / "measure_malformed.jsonl"))
        self.assertIsNone(cml.measure_transcript(FIX / "does_not_exist.jsonl"))
        self.assertIsNone(cml.measure_transcript(None))
        self.assertIsNone(cml.measure_transcript(FIX))  # a directory

    def test_import_does_not_pull_in_mcp(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import context_mcp_lib; "
            "bad = [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')]; "
            "sys.exit(1 if bad else 0)"
        )
        r = subprocess.run([sys.executable, "-I", "-c", code, str(_REPO / "context-mcp")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestCacheSecondsLeft(unittest.TestCase):
    T0 = epoch("2026-01-01T10:00:00Z")

    def test_warm_one_hour(self):
        left, ttl = statusline.cache_seconds_left(str(FIX / "cache_warm_1h.jsonl"), self.T0 + 600)
        self.assertEqual(ttl, 3600)
        self.assertAlmostEqual(left, 3000)

    def test_warm_five_minutes_overage(self):
        left, ttl = statusline.cache_seconds_left(str(FIX / "cache_warm_5m.jsonl"), self.T0 + 100)
        self.assertEqual(ttl, 300)
        self.assertAlmostEqual(left, 200)

    def test_cold_is_negative(self):
        left, ttl = statusline.cache_seconds_left(str(FIX / "cache_warm_5m.jsonl"), self.T0 + 400)
        self.assertEqual(ttl, 300)
        self.assertAlmostEqual(left, -100)

    def test_timestamp_wins_over_newer_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            shutil.copy(FIX / "cache_warm_1h.jsonl", p)
            os.utime(p, (self.T0 + 7000, self.T0 + 7000))
            left, ttl = statusline.cache_seconds_left(str(p), self.T0 + 7200)
            self.assertEqual(ttl, 3600)
            self.assertAlmostEqual(left, -3600)  # mtime would have said +3400

    def test_no_assistant_entry(self):
        self.assertEqual(statusline.cache_seconds_left(str(FIX / "cache_no_assistant.jsonl"), self.T0), (None, None))

    def test_missing_empty_and_blank_path(self):
        self.assertEqual(statusline.cache_seconds_left(str(FIX / "nope.jsonl"), self.T0), (None, None))
        self.assertEqual(statusline.cache_seconds_left(str(FIX / "measure_empty.jsonl"), self.T0), (None, None))
        self.assertEqual(statusline.cache_seconds_left("", self.T0), (None, None))

    def test_status_line_behaviour_unchanged(self):
        p = str(FIX / "cache_warm_1h.jsonl")
        self.assertEqual(statusline.read_cache_ttl(p), (3600, True))
        self.assertEqual(statusline.last_activity_epoch(p), os.stat(p).st_mtime)


class TestLastAssistantEntry(unittest.TestCase):
    P = str(FIX / "assistant_entries.jsonl")

    def test_newest_is_the_rate_limit_entry(self):
        e = asl.last_assistant_entry(self.P)
        self.assertEqual(e["error"], "rate_limit")
        self.assertTrue(e["is_api_error"])
        self.assertEqual(e["timestamp"], "2026-01-01T10:20:00.000Z")
        self.assertEqual(e["text"], "synthetic limit notice")
        self.assertEqual(e["model"], "<synthetic>")
        self.assertEqual(set(e), {"timestamp", "model", "error", "is_api_error", "text"})

    def test_since_picks_first_entry_at_or_after(self):
        e = asl.last_assistant_entry(self.P, since="2026-01-01T10:05:00Z")
        self.assertEqual(e["model"], "claude-test-model-b")
        self.assertEqual(e["text"], "second answer")
        self.assertIsNone(e["error"])
        self.assertFalse(e["is_api_error"])

    def test_since_accepts_epoch_and_is_inclusive(self):
        e = asl.last_assistant_entry(self.P, since=epoch("2026-01-01T10:00:05Z"))
        self.assertEqual(e["model"], "claude-test-model-a")

    def test_since_after_everything_is_none(self):
        self.assertIsNone(asl.last_assistant_entry(self.P, since="2026-01-01T11:00:00Z"))

    def test_unparseable_since_is_none(self):
        self.assertIsNone(asl.last_assistant_entry(self.P, since="garbage"))

    def test_fail_soft(self):
        self.assertIsNone(asl.last_assistant_entry(str(FIX / "nope.jsonl")))
        self.assertIsNone(asl.last_assistant_entry(None))
        self.assertIsNone(asl.last_assistant_entry(str(FIX / "measure_empty.jsonl")))
        self.assertIsNone(asl.last_assistant_entry(str(FIX / "measure_malformed.jsonl")))
        self.assertIsNone(asl.last_assistant_entry(str(FIX / "compaction_none.jsonl"), since="2026-01-01T11:00:00Z"))

    def test_read_is_bounded_to_the_tail(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            pad = '{"type":"user","message":{"role":"user","content":"' + "x" * 80 + '"}}\n'
            p.write_text((FIX / "assistant_entries.jsonl").read_text() + pad * 50)
            # Only the padding fits in the last 1000 bytes.
            tail = asl._tail_entries(str(p), limit=1000)
            self.assertTrue(tail)
            self.assertTrue(all(e["type"] == "user" for e in tail))


class TestCompactionBoundaries(unittest.TestCase):
    def test_none(self):
        self.assertEqual(asl.compaction_boundaries(str(FIX / "compaction_none.jsonl")), [])

    def test_auto_and_manual(self):
        got = asl.compaction_boundaries(str(FIX / "compaction_two.jsonl"))
        self.assertEqual(got, [
            {"timestamp": "2026-01-01T10:00:00.000Z", "trigger": "auto", "pre_tokens": 160000, "post_tokens": 12000},
            {"timestamp": "2026-01-01T11:00:00.000Z", "trigger": "manual", "pre_tokens": 80000, "post_tokens": 9000},
        ])

    def test_since_filters(self):
        p = str(FIX / "compaction_two.jsonl")
        got = asl.compaction_boundaries(p, since="2026-01-01T10:30:00Z")
        self.assertEqual([b["trigger"] for b in got], ["manual"])
        self.assertEqual(asl.compaction_boundaries(p, since=epoch("2026-01-01T10:00:00Z"))[0]["trigger"], "auto")
        self.assertEqual(asl.compaction_boundaries(p, since="2026-01-01T12:00:00Z"), [])

    def test_fail_soft(self):
        self.assertEqual(asl.compaction_boundaries(str(FIX / "nope.jsonl")), [])
        self.assertEqual(asl.compaction_boundaries(None), [])
        self.assertEqual(asl.compaction_boundaries(str(FIX / "measure_malformed.jsonl")), [])
        self.assertEqual(asl.compaction_boundaries(str(FIX / "compaction_two.jsonl"), since="garbage"), [])


class TestTimestampOffsetsAndLocalTz(unittest.TestCase):
    """``Z`` and ``+02:00`` spellings of one instant agree, whatever the local TZ.

    ``amux_spawn_lib._epoch_of`` and ``statusline._parse_iso_epoch`` are two
    deliberate copies (the status line is installed separately). A naive
    timestamp (no offset) is read as UTC by both, never as local time.
    """

    Z = "2026-01-01T10:00:00.000Z"
    PLUS2 = "2026-01-01T12:00:00.000+02:00"
    NAIVE = "2026-01-01T10:00:00"
    T0 = epoch("2026-01-01T10:00:00Z")
    TZS = ("America/Los_Angeles", "Asia/Kolkata")

    def setUp(self):
        self._tz = os.environ.get("TZ")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def _each_tz(self):
        for tz in self.TZS:
            os.environ["TZ"] = tz
            time.tzset()
            yield tz

    def _variant(self, fixture, old, new):
        out = Path(self._tmp.name) / ("v%d.jsonl" % len(list(Path(self._tmp.name).iterdir())))
        out.write_text((FIX / fixture).read_text().replace(old, new))
        return str(out)

    def test_parsers_agree_across_offsets(self):
        for tz in self._each_tz():
            for parse in (asl._epoch_of, statusline._parse_iso_epoch):
                with self.subTest(tz=tz, parser=parse.__name__):
                    self.assertEqual(parse(self.Z), self.T0)
                    self.assertEqual(parse(self.PLUS2), self.T0)
                    self.assertEqual(parse("2026-01-01T02:00:00-08:00"), self.T0)

    def test_naive_timestamp_is_utc_not_local(self):
        for tz in self._each_tz():
            for parse in (asl._epoch_of, statusline._parse_iso_epoch):
                with self.subTest(tz=tz, parser=parse.__name__):
                    self.assertEqual(parse(self.NAIVE), self.T0)

    def test_cache_seconds_left_offset_equals_z(self):
        now = self.T0 + 600
        for tz in self._each_tz():
            for ts in (self.Z, self.PLUS2, self.NAIVE):
                p = self._variant("cache_warm_1h.jsonl", self.Z, ts)
                with self.subTest(tz=tz, ts=ts):
                    left, ttl = statusline.cache_seconds_left(p, now)
                    self.assertEqual(ttl, 3600)
                    self.assertAlmostEqual(left, 3000)

    def test_last_assistant_entry_since_offset_equals_z(self):
        for tz in self._each_tz():
            for since in ("2026-01-01T10:05:00Z", "2026-01-01T12:05:00+02:00", "2026-01-01T02:05:00-08:00"):
                with self.subTest(tz=tz, since=since):
                    e = asl.last_assistant_entry(str(FIX / "assistant_entries.jsonl"), since=since)
                    self.assertEqual(e["model"], "claude-test-model-b")
            # Entry timestamps written with an offset are compared by instant too.
            p = self._variant("assistant_entries.jsonl", "2026-01-01T10:10:00.000Z", "2026-01-01T12:10:00.000+02:00")
            with self.subTest(tz=tz, where="entry"):
                e = asl.last_assistant_entry(p, since="2026-01-01T10:05:00Z")
                self.assertEqual(e["model"], "claude-test-model-b")
                self.assertEqual(asl.last_assistant_entry(p, since="2026-01-01T10:10:01Z")["error"], "rate_limit")

    def test_compaction_boundaries_since_offset_equals_z(self):
        for tz in self._each_tz():
            for since in ("2026-01-01T10:30:00Z", "2026-01-01T12:30:00+02:00", "2026-01-01T02:30:00-08:00"):
                with self.subTest(tz=tz, since=since):
                    got = asl.compaction_boundaries(str(FIX / "compaction_two.jsonl"), since=since)
                    self.assertEqual([b["trigger"] for b in got], ["manual"])
            p = self._variant("compaction_two.jsonl", "2026-01-01T11:00:00.000Z", "2026-01-01T13:00:00.000+02:00")
            with self.subTest(tz=tz, where="entry"):
                got = asl.compaction_boundaries(p, since="2026-01-01T11:00:00Z")
                self.assertEqual([b["trigger"] for b in got], ["manual"])
                self.assertEqual(asl.compaction_boundaries(p, since="2026-01-01T11:00:01Z"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
