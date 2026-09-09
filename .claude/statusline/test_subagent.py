#!/usr/bin/env python3
"""
Tests for the per-subagent status line (subagent.py) and the main status line's
effort/agent segments.

Covers:

- Effort chain: agent-pinned → session (cached by statusline.py) → model
  catalogue default → "high", with inherited levels marked "~".
- Models without the effort capability show no effort at all.
- Names are skipped when absent; the column collapses when nobody has one.
- Settled agents show their outcome, not a clock that keeps ticking.
- The description is the only cell allowed to truncate, and rows fit `columns`.
- Malformed input emits nothing and still exits 0 (a bad line would make Claude
  Code log a schema error every tick).
- statusline.py publishes the session effort and renders effort/agent segments.

Run: python3 .claude/statusline/test_subagent.py -v
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

import subagent  # noqa: E402


def task(**overrides) -> dict:
    base = {
        "id": "t1",
        "type": "local_agent",
        "status": "running",
        "description": "do the thing",
        "startTime": int(time.time() * 1000),
        "model": "claude-opus-5",
        "contextWindowSize": 1000000,
        "tokenCount": 100000,
    }
    base.update(overrides)
    return base


def run(payload: dict, home: str, env_overrides: dict = None) -> subprocess.CompletedProcess:
    env = {"PATH": "", "HOME": home, "CC_STATUS_NO_COLOR": "1"}
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, SUBAGENT],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def rows(payload: dict, home: str, env_overrides: dict = None) -> list:
    result = run(payload, home, env_overrides)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


class EffortChainTest(unittest.TestCase):
    """resolve_effort mirrors the harness: agent → session → catalogue → high."""

    def test_agent_pinned_effort_is_not_marked_inherited(self):
        self.assertEqual(subagent.resolve_effort("max", "low", "claude-opus-5"), ("max", False))

    def test_session_effort_wins_when_agent_pins_nothing(self):
        self.assertEqual(subagent.resolve_effort(None, "low", "claude-opus-5"), ("low", True))

    def test_catalogue_default_used_without_session_effort(self):
        self.assertEqual(subagent.resolve_effort(None, None, "claude-opus-4-7"), ("xhigh", True))

    def test_falls_back_to_high_when_catalogue_has_no_default(self):
        # claude-opus-4-6 is effort-capable but carries no default_effort.
        self.assertEqual(subagent.resolve_effort(None, None, "claude-opus-4-6"), ("high", True))

    def test_invalid_agent_effort_is_ignored(self):
        self.assertEqual(subagent.resolve_effort("turbo", None, "claude-opus-5"), ("high", True))

    def test_models_without_effort_capability_get_none(self):
        self.assertIsNone(subagent.resolve_effort(None, "high", "claude-haiku-4-5"))
        self.assertIsNone(subagent.resolve_effort(None, "high", "glm-4.7"))

    def test_unknown_claude_model_assumed_effort_capable(self):
        self.assertTrue(subagent.supports_effort("claude-opus-9"))

    def test_context_suffix_does_not_defeat_lookup(self):
        self.assertEqual(subagent.resolve_effort(None, None, "claude-opus-4-7[1m]"), ("xhigh", True))


class SessionEffortCacheTest(unittest.TestCase):
    """subagent.py inherits the effort statusline.py resolved for the session."""

    def setUp(self):
        self.home = tempfile.mkdtemp()

    def _write_main_statusline_state(self, session_id: str, effort: str):
        env = {"PATH": "", "HOME": self.home}
        payload = {
            "session_id": session_id,
            "model": {"id": "claude-opus-5", "display_name": "Opus"},
            "effort": {"level": effort},
            "context_window": {"used_percentage": 12},
        }
        subprocess.run([sys.executable, STATUSLINE], input=json.dumps(payload),
                       env=env, capture_output=True, text=True, timeout=10)

    def test_effort_is_inherited_from_the_main_status_line(self):
        self._write_main_statusline_state("sess-a", "xhigh")
        out = rows({"session_id": "sess-a", "columns": 120, "tasks": [task()]}, self.home)
        self.assertIn("Opus · ~xhigh", out[0]["content"])

    def test_cache_is_scoped_per_session(self):
        self._write_main_statusline_state("sess-a", "low")
        out = rows({"session_id": "sess-b", "columns": 120, "tasks": [task()]}, self.home)
        # sess-b has no cache, so the catalogue default for opus-5 applies.
        self.assertIn("Opus · ~high", out[0]["content"])

    def test_agent_pinned_effort_beats_the_cache(self):
        self._write_main_statusline_state("sess-a", "low")
        out = rows({"session_id": "sess-a", "columns": 120,
                    "tasks": [task(effort="max")]}, self.home)
        self.assertIn("Opus · max", out[0]["content"])
        self.assertNotIn("~", out[0]["content"])


class LayoutTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_named_and_unnamed_agents_stay_column_aligned(self):
        out = rows({"columns": 120, "tasks": [
            task(id="a", name="Explore-2"),
            task(id="b"),
        ]}, self.home)
        offsets = [row["content"].index("Opus") for row in out]
        self.assertEqual(offsets[0], offsets[1])
        self.assertTrue(out[1]["content"].startswith(" "))

    def test_name_column_disappears_when_no_agent_has_a_name(self):
        out = rows({"columns": 120, "tasks": [task(id="a"), task(id="b")]}, self.home)
        for row in out:
            self.assertTrue(row["content"].startswith("Opus"), row["content"])

    def test_settled_agents_show_outcome_not_a_running_clock(self):
        started = int((time.time() - 600) * 1000)
        out = rows({"columns": 120, "tasks": [
            task(id="a", status="completed", startTime=started),
            task(id="b", status="failed", startTime=started),
            task(id="c", status="killed", startTime=started),
        ]}, self.home)
        contents = [row["content"] for row in out]
        self.assertIn("done", contents[0])
        self.assertIn("failed", contents[1])
        self.assertIn("killed", contents[2])
        for content in contents:
            self.assertNotIn("10m", content)

    def test_running_agent_shows_elapsed(self):
        out = rows({"columns": 120, "tasks": [
            task(startTime=int((time.time() - 72) * 1000)),
        ]}, self.home)
        self.assertIn("1m12s", out[0]["content"])

    def test_row_never_exceeds_the_column_budget(self):
        # Narrow widths overrun the fixed cells alone, so this also exercises
        # the hard cap, not just the description fitting.
        long_description = "x" * 400
        for columns in (10, 20, 30, 40, 60, 100, 200):
            out = rows({"columns": columns, "tasks": [
                task(name="Explore-2", description=long_description),
            ]}, self.home)
            self.assertLessEqual(len(out[0]["content"]), columns,
                                 f"overflowed at columns={columns}")

    def test_hard_cap_never_slices_an_escape_sequence(self):
        for columns in range(4, 46):
            out = rows({"columns": columns, "tasks": [
                task(name="Explore-2", status="failed", description="y" * 100),
            ]}, self.home, {"CC_STATUS_NO_COLOR": ""})
            content = out[0]["content"]
            stripped = subagent._ANSI_RE.sub("", content)
            self.assertNotIn("\x1b", stripped,
                             f"broken escape at columns={columns}: {content!r}")
            self.assertLessEqual(len(stripped), columns)

    def test_only_the_description_truncates(self):
        out = rows({"columns": 60, "tasks": [
            task(name="Explore-2", description="y" * 200),
        ]}, self.home)
        content = out[0]["content"]
        self.assertIn("Explore-2", content)
        self.assertIn("Opus · ~high", content)
        self.assertIn("ctx 10%", content)
        self.assertTrue(content.endswith("…"))

    def test_label_preferred_over_description(self):
        out = rows({"columns": 120, "tasks": [
            task(description="stale description", label="live progress summary"),
        ]}, self.home)
        self.assertIn("live progress summary", out[0]["content"])
        self.assertNotIn("stale description", out[0]["content"])

    def test_missing_context_window_renders_a_question_mark(self):
        out = rows({"columns": 120, "tasks": [task(contextWindowSize=None)]}, self.home)
        self.assertIn("ctx ?%", out[0]["content"])

    def test_colour_is_emitted_unless_disabled(self):
        # Escape codes travel escaped inside the JSON line, so assert on the
        # decoded content rather than on raw stdout.
        payload = {"columns": 120, "tasks": [task(name="Explore-2")]}
        self.assertNotIn("\x1b[", rows(payload, self.home)[0]["content"])

        colored = rows(payload, self.home, {"CC_STATUS_NO_COLOR": ""})[0]["content"]
        self.assertIn("\x1b[36m", colored)  # name
        self.assertIn("\x1b[33m", colored)  # high effort

    def test_colour_never_emits_a_blanket_reset(self):
        # A \x1b[0m would drop the row-level dim/bold Claude Code applies to
        # everything after it; each attribute must close specifically.
        out = rows({"columns": 120, "tasks": [
            task(name="Explore-2", status="failed"),
        ]}, self.home, {"CC_STATUS_NO_COLOR": ""})
        self.assertNotIn("\x1b[0m", out[0]["content"])
        self.assertIn("\x1b[39m", out[0]["content"])  # colour closer
        self.assertIn("\x1b[22m", out[0]["content"])  # dim closer

    def test_state_column_collapses_when_no_task_has_a_start_time(self):
        out = rows({"columns": 120, "tasks": [
            task(id="a", startTime=None), task(id="b", startTime=None),
        ]}, self.home)
        for row in out:
            self.assertNotIn("      ", row["content"])  # no doubled gap

    def test_no_color_env_var_is_honoured(self):
        out = rows({"columns": 120, "tasks": [task()]}, self.home,
                   {"CC_STATUS_NO_COLOR": "", "NO_COLOR": "1"})
        self.assertNotIn("\x1b[", out[0]["content"])


class RobustnessTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_every_row_matches_the_required_schema(self):
        out = rows({"columns": 120, "tasks": [task(id="a"), task(id="b")]}, self.home)
        self.assertEqual([row["id"] for row in out], ["a", "b"])
        for row in out:
            self.assertEqual(set(row), {"id", "content"})
            self.assertIsInstance(row["content"], str)

    def test_malformed_input_emits_nothing_and_exits_zero(self):
        for raw in ("", "not json", "[]", "null", '{"tasks": "nope"}'):
            result = subprocess.run(
                [sys.executable, SUBAGENT], input=raw,
                env={"PATH": "", "HOME": self.home}, capture_output=True,
                text=True, timeout=10)
            self.assertEqual(result.returncode, 0, raw)
            self.assertEqual(result.stdout.strip(), "", raw)

    def test_tasks_without_an_id_are_skipped(self):
        out = rows({"columns": 120, "tasks": [
            {"type": "local_agent", "status": "running"},
            task(id="keeper"),
        ]}, self.home)
        self.assertEqual([row["id"] for row in out], ["keeper"])

    def test_missing_fields_do_not_crash(self):
        out = rows({"columns": 120, "tasks": [{"id": "bare"}]}, self.home)
        self.assertEqual(len(out), 1)

    def test_absent_columns_falls_back_to_a_sane_width(self):
        out = rows({"tasks": [task(description="z" * 300)]}, self.home)
        self.assertLessEqual(len(out[0]["content"]), 80)

    def test_makes_no_network_calls(self):
        import socket
        original = socket.socket
        socket.socket = lambda *a, **k: self.fail("subagent.py opened a socket")
        try:
            subagent.build_rows({"columns": 120, "tasks": [task()]},
                                {"HOME": self.home, "CC_STATUS_NO_COLOR": "1"})
        finally:
            socket.socket = original


class MainStatusLineTest(unittest.TestCase):
    """statusline.py gains effort + agent segments and publishes session effort."""

    def setUp(self):
        self.home = tempfile.mkdtemp()

    def render(self, payload: dict) -> str:
        result = subprocess.run(
            [sys.executable, STATUSLINE], input=json.dumps(payload),
            env={"PATH": "", "HOME": self.home},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_effort_appended_to_the_model_segment(self):
        line = self.render({
            "session_id": "s1",
            "model": {"id": "claude-opus-5", "display_name": "Opus"},
            "effort": {"level": "xhigh"},
            "context_window": {"used_percentage": 61},
        })
        self.assertTrue(line.startswith("Opus · xhigh | ctx 61%"), line)

    def test_agent_segment_shown_when_the_main_thread_runs_an_agent(self):
        line = self.render({
            "session_id": "s1",
            "model": {"id": "claude-opus-5", "display_name": "Opus"},
            "agent": {"name": "reviewer"},
            "context_window": {"used_percentage": 12},
        })
        self.assertIn("| agent reviewer |", line)

    def test_unchanged_when_neither_effort_nor_agent_present(self):
        line = self.render({
            "session_id": "s1",
            "model": {"id": "claude-opus-5", "display_name": "Opus"},
            "context_window": {"used_percentage": 61},
        })
        self.assertEqual(line, "Opus | ctx 61%")

    def test_session_effort_state_is_written(self):
        self.render({
            "session_id": "s1",
            "model": {"id": "claude-opus-5", "display_name": "Opus"},
            "effort": {"level": "max"},
            "context_window": {"used_percentage": 5},
        })
        cache = os.path.join(self.home, ".cache", "claude-statusline")
        state_files = [f for f in os.listdir(cache) if f.startswith("session-")]
        self.assertEqual(len(state_files), 1)
        with open(os.path.join(cache, state_files[0])) as f:
            self.assertEqual(json.load(f)["effort"], "max")

    def test_no_state_written_without_an_effort_level(self):
        self.render({
            "session_id": "s1",
            "model": {"id": "claude-haiku-4-5", "display_name": "Haiku"},
            "context_window": {"used_percentage": 5},
        })
        cache = os.path.join(self.home, ".cache", "claude-statusline")
        files = os.listdir(cache) if os.path.isdir(cache) else []
        self.assertEqual([f for f in files if f.startswith("session-")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
