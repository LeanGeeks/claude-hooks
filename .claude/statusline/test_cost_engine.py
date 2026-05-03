#!/usr/bin/env python3
"""
Tests for the cost engine in statusline.py.
Run: python3 .claude/statusline/test_cost_engine.py
Each test runs the script in a subprocess with a fresh temp HOME so session
state files do not leak between cases.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "statusline.py")
FIX = os.path.join(HERE, "fixtures", "cost-engine")


def run(stdin_json: str, env_overrides: dict, home: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
    }
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, SCRIPT],
        input=stdin_json,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip()


def read_fixture(name: str) -> str:
    with open(os.path.join(FIX, name)) as f:
        return f.read()


class CostEngineTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mock_env = {
            "ANTHROPIC_BASE_URL": "https://api.example-mock.com/anthropic",
            "CC_STATUS_PROVIDER": "mock",
            "CC_STATUS_BILLING": "api",
            "CC_STATUS_MODEL": "mock-model",
        }

    def test_known_pricing_renders_cost(self):
        out = run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp)
        # 1,000,000 tokens * $1/M = $1.00
        self.assertIn("$1.00", out)

    def test_repeated_render_does_not_double_count(self):
        first = run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp)
        second = run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$1.00", second)

    def test_second_distinct_usage_increments(self):
        run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp)
        out2 = run(read_fixture("mock-call-2.json"), self.mock_env, self.tmp)
        # 3,000,000 total tokens * $1/M = $3.00
        self.assertIn("$3.00", out2)

    def test_unknown_model_renders_question(self):
        env = dict(self.mock_env)
        env["CC_STATUS_MODEL"] = "mock-unpriced"
        out = run(read_fixture("mock-unknown-model.json"), env, self.tmp)
        self.assertIn("cost ?", out)

    def test_subscription_claude_suppresses_cost(self):
        env = {}  # no ANTHROPIC_BASE_URL → claude/subscription
        payload = json.dumps({
            "model": {"display_name": "claude-opus"},
            "context_window": {"used_percentage": 50, "current_usage": 1000},
            "session_id": "sub-claude",
        })
        out = run(payload, env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    def test_subscription_zai_suppresses_cost(self):
        env = {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_MODEL": "glm-4.7",
        }
        payload = json.dumps({
            "model": {"display_name": "GLM-4.7"},
            "context_window": {"used_percentage": 50, "current_usage": 1000},
            "session_id": "sub-zai",
        })
        out = run(payload, env, self.tmp)
        self.assertNotIn("$", out)
        # zai may show "quota ?" but never "cost"
        self.assertNotIn("cost ", out)

    def test_local_provider_suppresses_cost(self):
        env = {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:18080",
            "ANTHROPIC_MODEL": "gemma",
        }
        payload = json.dumps({
            "model": {"display_name": "gemma"},
            "context_window": {"used_percentage": 32, "current_usage": 1000},
            "session_id": "local-1",
        })
        out = run(payload, env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    def test_null_current_usage_no_crash_no_segment(self):
        out = run(read_fixture("mock-null-usage.json"), self.mock_env, self.tmp)
        # Should still render the model + ctx, no cost segment yet
        self.assertNotIn("$", out)
        self.assertNotIn("cost ?", out)

    def test_invalid_stdin_does_not_crash(self):
        out = run("not json at all", self.mock_env, self.tmp)
        self.assertTrue(len(out) > 0)

    def test_buckets_priced_with_bucket_rates(self):
        env = dict(self.mock_env)
        out = run(read_fixture("mock-buckets.json"), env, self.tmp)
        # 2M input * $1 + 0.5M output * $2 = $2.00 + $1.00 = $3.00
        self.assertIn("$3.00", out)


if __name__ == "__main__":
    unittest.main()
