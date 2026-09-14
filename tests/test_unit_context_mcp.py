#!/usr/bin/env python3
"""
Unit Tests: context-usage MCP

Covers the agent-facing surface:
- context-window resolution and, critically, its PROVENANCE (env / models.json
  / fallback constant) — an unmarked fallback is how "W = 1M" became doctrine
- session-transcript resolution, including the most-recent-``*.jsonl`` guess
- transcript folding (message-id dedupe, latest usage wins)
- the 2026-09-14 unit-034 regression: a subagent calling this tool gets its
  PARENT's numbers, and the payload must say so instead of implying otherwise

Every case runs against a scratch projects root via ``CLAUDE_PROJECTS_ROOT``;
nothing here touches the developer's real ~/.claude.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "context-mcp"))

import context_mcp_lib as lib  # noqa: E402


def _usage_line(msg_id, model, cache_read, *, inp=1, cache_creation=0, out=100):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": msg_id,
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": out,
                },
            },
        }
    )


class ContextMCPTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="context-mcp-test-")
        self.root = Path(self._tmp.name)
        self.projects = self.root / "projects"
        self.project_dir = "/work/demo"
        self.encoded = self.project_dir.replace("/", "-")
        (self.projects / self.encoded).mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _env(self, **overrides):
        env = {
            "CLAUDE_PROJECTS_ROOT": str(self.projects),
            "CLAUDE_PROJECT_DIR": self.project_dir,
        }
        env.update(overrides)
        return patch.dict(os.environ, env, clear=False)

    def _write_session(self, session_id, lines):
        p = self.projects / self.encoded / f"{session_id}.jsonl"
        p.write_text("\n".join(lines) + "\n")
        return p

    def _write_subagent(self, session_id, agent_id, lines, meta=None):
        d = self.projects / self.encoded / session_id / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{agent_id}.jsonl").write_text("\n".join(lines) + "\n")
        if meta is not None:
            (d / f"{agent_id}.meta.json").write_text(json.dumps(meta))


class TestResolveContextWindow(ContextMCPTestBase):
    def test_env_map_wins_over_bundled_table(self):
        with patch.dict(os.environ, {"CONTEXT_WINDOW_MAP": "claude-opus-4-*=123456"}, clear=False):
            tokens, source = lib.resolve_context_window("claude-opus-4-6")
        self.assertEqual(tokens, 123456)
        self.assertEqual(source, "env")

    def test_explicit_1m_variant_beats_the_family_entry(self):
        """``models.json`` lists ``*[1m]*`` first on purpose — a 1M build of a
        200K family must not be read down to 200K."""
        with patch.dict(os.environ, {"CONTEXT_WINDOW_MAP": ""}, clear=False):
            tokens, source = lib.resolve_context_window("claude-opus-5[1m]")
        self.assertEqual(tokens, 1_000_000)
        self.assertEqual(source, "models.json")

    def test_opus_4_and_sonnet_4_are_200k_not_1m(self):
        """The shipped table must reflect what was measured on 2026-09-14:
        a sonnet-4-6 chain auto-compacted at 166K/167K/167K/173K. Declaring
        those families 1M is what hid four compactions from their supervisor."""
        with patch.dict(os.environ, {"CONTEXT_WINDOW_MAP": ""}, clear=False):
            for model in ("claude-opus-4-6", "claude-sonnet-4-6"):
                tokens, source = lib.resolve_context_window(model)
                self.assertEqual(tokens, 200_000, model)
                self.assertEqual(source, "models.json", model)

    def test_the_1m_pattern_is_bracket_escaped_and_does_not_over_match(self):
        """``[`` opens an fnmatch character class. The obvious spelling of the
        long-context pattern, ``*[1m]*``, means "contains a 1 or an m" — it
        matches ``claude-haiku-4-5-20251001`` and would size a 200K model at
        1M. The shipped pattern must be ``*[[]1m]*``."""
        with patch.dict(os.environ, {"CONTEXT_WINDOW_MAP": ""}, clear=False):
            for model in ("claude-opus-5[1m]", "claude-sonnet-4-6[1m]"):
                self.assertEqual(lib.resolve_context_window(model)[0], 1_000_000, model)
            for model in ("claude-haiku-4-5-20251001", "some-unlisted-model-9"):
                self.assertNotEqual(
                    lib.resolve_context_window(model),
                    (1_000_000, "models.json"),
                    f"{model} must not match the long-context pattern",
                )

    def test_unknown_model_is_reported_as_a_fallback_not_a_fact(self):
        with patch.dict(os.environ, {"CONTEXT_WINDOW_MAP": ""}, clear=False):
            tokens, source = lib.resolve_context_window("some-unlisted-model-9")
        self.assertEqual(tokens, lib.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(source, "default-fallback")


class TestFindSessionJsonl(ContextMCPTestBase):
    def test_exact_resolution_by_session_id(self):
        self._write_session("sess-a", [_usage_line("m1", "claude-opus-4-6", 1000)])
        with self._env(CLAUDE_CODE_SESSION_ID="sess-a"):
            path, resolution = lib.find_session_jsonl()
        self.assertEqual(resolution, "session-id")
        self.assertEqual(path.stem, "sess-a")

    def test_missing_session_id_falls_back_to_most_recent_and_says_so(self):
        old = self._write_session("sess-old", [_usage_line("m1", "claude-opus-4-6", 1000)])
        new = self._write_session("sess-new", [_usage_line("m2", "claude-opus-4-6", 2000)])
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        env = {"CLAUDE_PROJECTS_ROOT": str(self.projects), "CLAUDE_PROJECT_DIR": self.project_dir}
        with patch.dict(os.environ, env, clear=True):
            path, resolution = lib.find_session_jsonl()
        self.assertEqual(path.stem, "sess-new")
        self.assertEqual(resolution, "fallback-most-recent")

    def test_no_project_dir_is_not_found(self):
        with patch.dict(os.environ, {"CLAUDE_PROJECTS_ROOT": str(self.projects)}, clear=True):
            path, resolution = lib.find_session_jsonl()
        self.assertIsNone(path)
        self.assertEqual(resolution, "not-found")


class TestParseSession(ContextMCPTestBase):
    def test_dedupes_by_message_id_and_takes_the_latest_usage(self):
        p = self._write_session(
            "sess-a",
            [
                _usage_line("m1", "claude-opus-4-6", 1000, out=10),
                _usage_line("m1", "claude-opus-4-6", 1000, out=10),  # streamed twice
                _usage_line("m2", "claude-opus-4-6", 5000, out=20),
                json.dumps({"type": "user", "message": {"content": "hi"}}),
                "not json at all",
                "",
            ],
        )
        parsed = lib.parse_session(p)
        self.assertEqual(parsed["turns_count"], 2)
        self.assertEqual(parsed["cumulative_output"], 30)
        self.assertEqual(parsed["latest_usage"]["cache_read_input_tokens"], 5000)


class TestGetContextUsageSubagentRegression(ContextMCPTestBase):
    """The 2026-09-14 unit-034 incident, reproduced in miniature.

    A parent session sits at ~129 K while its chain subagent sits at ~173 K.
    The old server answered a subagent's question with the parent's 12.9 % and
    no caveat, so two managers reported themselves "well within budget" while
    their chain was auto-compacting.
    """

    def setUp(self):
        super().setUp()
        self._write_session(
            "sess-parent",
            [_usage_line("p1", "claude-opus-4-6", 128_981, inp=3, cache_creation=416, out=32)],
        )
        self._write_subagent(
            "sess-parent",
            "agent-aef47be13e466a7fd",
            [_usage_line("s1", "claude-sonnet-4-6", 172_500, out=200)],
            meta={
                "agentType": "implementer-complex",
                "description": "Implement 049-01 partner send call-time EM",
                "name": "049-01-implementer",
            },
        )

    def _payload(self):
        with self._env(CLAUDE_CODE_SESSION_ID="sess-parent", CONTEXT_WINDOW_MAP=""):
            return json.loads(lib.get_context_usage())

    def test_scope_and_caller_identification_are_explicit(self):
        d = self._payload()
        self.assertEqual(d["scope"], "main-session")
        self.assertIs(d["caller_identified"], False)
        self.assertEqual(d["session_resolution"], "session-id")

    def test_parent_numbers_are_the_parents(self):
        d = self._payload()
        self.assertEqual(d["latest_turn"]["effective_context_tokens"], 129_400)
        self.assertEqual(d["context_window"], 200_000)
        self.assertEqual(d["context_window_source"], "models.json")

    def test_a_warning_names_the_subagent_trap(self):
        d = self._payload()
        joined = " ".join(d["warnings"])
        self.assertIn("MAIN SESSION", joined)
        self.assertIn("PARENT", joined)
        self.assertIn("subagents", joined)

    def test_subagent_row_carries_its_own_measured_usage(self):
        d = self._payload()
        self.assertEqual(len(d["subagents"]), 1)
        row = d["subagents"][0]
        self.assertEqual(row["agent_id"], "agent-aef47be13e466a7fd")
        self.assertEqual(row["model"], "claude-sonnet-4-6")
        self.assertEqual(row["effective_context_tokens"], 172_501)
        self.assertEqual(row["context_window"], 200_000)
        # The number that matters: 86 %, not the parent's 65 %.
        self.assertGreater(row["fill_percent"], 75)
        self.assertEqual(row["agent_type"], "implementer-complex")
        self.assertEqual(row["name"], "049-01-implementer")

    def test_subagents_are_ordered_fullest_first(self):
        self._write_subagent(
            "sess-parent",
            "agent-bsmall",
            [_usage_line("s2", "claude-sonnet-4-6", 20_000)],
        )
        d = self._payload()
        fills = [r["effective_context_tokens"] for r in d["subagents"]]
        self.assertEqual(fills, sorted(fills, reverse=True))

    def test_no_subagents_means_no_subagent_warning(self):
        self._write_session(
            "sess-solo", [_usage_line("x1", "claude-opus-4-6", 1000)]
        )
        with self._env(CLAUDE_CODE_SESSION_ID="sess-solo", CONTEXT_WINDOW_MAP=""):
            d = json.loads(lib.get_context_usage())
        self.assertNotIn("subagents", d)
        self.assertNotIn("warnings", d)


class TestGetContextUsageWarnings(ContextMCPTestBase):
    def test_fallback_window_is_flagged_as_unreliable(self):
        self._write_session("sess-a", [_usage_line("m1", "totally-unknown-model", 50_000)])
        with self._env(CLAUDE_CODE_SESSION_ID="sess-a", CONTEXT_WINDOW_MAP=""):
            d = json.loads(lib.get_context_usage())
        self.assertEqual(d["context_window_source"], "default-fallback")
        joined = " ".join(d["warnings"])
        self.assertIn("fallback constant", joined)
        self.assertIn("unreliable", joined)

    def test_guessed_session_is_flagged(self):
        self._write_session("sess-a", [_usage_line("m1", "claude-opus-4-6", 50_000)])
        env = {"CLAUDE_PROJECTS_ROOT": str(self.projects), "CLAUDE_PROJECT_DIR": self.project_dir}
        with patch.dict(os.environ, env, clear=True):
            d = json.loads(lib.get_context_usage())
        self.assertEqual(d["session_resolution"], "fallback-most-recent")
        self.assertIn("may belong to a different session", " ".join(d["warnings"]))

    def test_missing_transcript_errors(self):
        env = {"CLAUDE_PROJECTS_ROOT": str(self.projects)}
        with patch.dict(os.environ, env, clear=True):
            d = json.loads(lib.get_context_usage())
        self.assertIn("error", d)

    def test_transcript_without_usage_errors(self):
        self._write_session("sess-a", [json.dumps({"type": "user", "message": {"content": "hi"}})])
        with self._env(CLAUDE_CODE_SESSION_ID="sess-a"):
            d = json.loads(lib.get_context_usage())
        self.assertIn("error", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
