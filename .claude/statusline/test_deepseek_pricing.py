#!/usr/bin/env python3
"""
DeepSeek pricing integration tests for the cost engine in statusline.py.
Run: python3 .claude/statusline/test_deepseek_pricing.py

Each test runs the script in a subprocess with a fresh temp HOME so session
state files do not leak between cases. No real credentials are required.
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


_PRO_ENV = {
    "CC_STATUS_PROVIDER": "deepseek",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "deepseek-v4-pro[500k]",
}

_FLASH_ENV = {
    "CC_STATUS_PROVIDER": "deepseek",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "deepseek-v4-flash",
}


class DeepSeekPricingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    # ------------------------------------------------------------------
    # Model normalization
    # ------------------------------------------------------------------

    def test_500k_suffix_normalizes_to_pro_pricing(self):
        """deepseek-v4-pro[500k] strips suffix and resolves to deepseek-v4-pro pricing."""
        out = run(read_fixture("deepseek-int-call-1.json"), _PRO_ENV, self.tmp)
        # 220 000 tokens × $0.435/M = $0.0957 → $0.10
        self.assertIn("$0.10", out)

    def test_200k_suffix_normalizes_to_pro_pricing(self):
        """deepseek-v4-pro[200k] strips suffix and resolves to the same deepseek-v4-pro pricing."""
        env = dict(_PRO_ENV)
        env["ANTHROPIC_MODEL"] = "deepseek-v4-pro[200k]"
        out = run(read_fixture("deepseek-int-call-1.json"), env, self.tmp)
        self.assertIn("$0.10", out)

    # ------------------------------------------------------------------
    # Integer current_usage path (real claude-ds shape)
    # ------------------------------------------------------------------

    def test_integer_usage_renders_calculated_cost(self):
        """Integer current_usage prices via blended_per_million — the real claude-ds path."""
        out = run(read_fixture("deepseek-int-call-1.json"), _PRO_ENV, self.tmp)
        self.assertIn("$0.10", out)
        self.assertNotIn("cost ?", out)

    def test_integer_usage_repeated_render_no_double_count(self):
        """Same integer current_usage rendered twice must not double-count."""
        first = run(read_fixture("deepseek-int-call-1.json"), _PRO_ENV, self.tmp)
        second = run(read_fixture("deepseek-int-call-1.json"), _PRO_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.10", second)

    def test_integer_usage_second_call_increments_cost(self):
        """A higher integer current_usage in a subsequent render adds the delta."""
        run(read_fixture("deepseek-int-call-1.json"), _PRO_ENV, self.tmp)
        out2 = run(read_fixture("deepseek-int-call-2.json"), _PRO_ENV, self.tmp)
        # 440 000 total × $0.435/M = $0.1914 → $0.19
        self.assertIn("$0.19", out2)

    # ------------------------------------------------------------------
    # Dict current_usage path (forward-compatibility — bucket shape)
    # ------------------------------------------------------------------

    def test_bucket_usage_renders_calculated_cost(self):
        """Dict current_usage with per-field buckets prices via bucket rates."""
        out = run(read_fixture("deepseek-buckets-call-1.json"), _PRO_ENV, self.tmp)
        # 100 000 input × $0.435/M + 5 000 output × $0.87/M = $0.04785 → $0.05
        self.assertIn("$0.05", out)
        self.assertNotIn("cost ?", out)

    def test_bucket_usage_repeated_render_no_double_count(self):
        """Same bucket payload rendered twice must not double-count."""
        first = run(read_fixture("deepseek-buckets-call-1.json"), _PRO_ENV, self.tmp)
        second = run(read_fixture("deepseek-buckets-call-1.json"), _PRO_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.05", second)

    # ------------------------------------------------------------------
    # deepseek-v4-flash
    # ------------------------------------------------------------------

    def test_flash_resolves_separately(self):
        """deepseek-v4-flash uses its own pricing entry, not the pro entry."""
        out = run(read_fixture("deepseek-flash-call-1.json"), _FLASH_ENV, self.tmp)
        # 100 000 tokens × $0.14/M = $0.014 → $0.01
        self.assertIn("$0.01", out)
        self.assertNotIn("cost ?", out)

    def test_unknown_deepseek_model_renders_question(self):
        """A deepseek model with no pricing entry renders cost ?."""
        env = dict(_PRO_ENV)
        env["ANTHROPIC_MODEL"] = "deepseek-unknown-future-model"
        payload = json.dumps({
            "model": {"display_name": "deepseek-unknown-future-model"},
            "context_window": {"used_percentage": 10, "current_usage": 50000},
            "session_id": "ds-unknown-test",
        })
        out = run(payload, env, self.tmp)
        self.assertIn("cost ?", out)

    # ------------------------------------------------------------------
    # Suppression regression: non-API setups must not show cost
    # ------------------------------------------------------------------

    def test_claude_subscription_suppresses_cost(self):
        """Claude Max subscription must not show any cost segment."""
        env = {}  # no ANTHROPIC_BASE_URL → claude / subscription
        payload = json.dumps({
            "model": {"display_name": "claude-sonnet"},
            "context_window": {"used_percentage": 50, "current_usage": 220000},
            "session_id": "sub-claude-regression",
        })
        out = run(payload, env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    def test_glm_subscription_suppresses_cost(self):
        """Z.ai / GLM Coding Plan subscription must not show any cost segment."""
        env = {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_MODEL": "glm-4.7",
        }
        payload = json.dumps({
            "model": {"display_name": "GLM-4.7"},
            "context_window": {"used_percentage": 50, "current_usage": 220000},
            "session_id": "sub-zai-regression",
        })
        out = run(payload, env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost ", out)

    def test_local_gemma_suppresses_cost(self):
        """Local Gemma must not show any cost segment."""
        env = {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:18080",
            "ANTHROPIC_MODEL": "gemma",
        }
        payload = json.dumps({
            "model": {"display_name": "gemma"},
            "context_window": {"used_percentage": 32, "current_usage": 220000},
            "session_id": "local-gemma-regression",
        })
        out = run(payload, env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)


if __name__ == "__main__":
    unittest.main()
