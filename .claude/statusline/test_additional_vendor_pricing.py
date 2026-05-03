#!/usr/bin/env python3
"""
Fireworks, MiniMax, and Kimi pricing integration tests for the cost engine.
Run: python3 .claude/statusline/test_additional_vendor_pricing.py

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


def buckets_payload(session_id: str, input_tokens: int = 100000, output_tokens: int = 5000) -> str:
    return json.dumps({
        "model": {"display_name": "model"},
        "context_window": {
            "used_percentage": 50,
            "current_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
        "session_id": session_id,
    })


_FW_GLM_ENV = {
    "CC_STATUS_PROVIDER": "fireworks",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "glm-5.1",
}

_FW_M2P5_ENV = {
    "CC_STATUS_PROVIDER": "fireworks",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "minimax-m2p5",
}

_MM_M27_ENV = {
    "CC_STATUS_PROVIDER": "minimax",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "MiniMax-M2.7",
}

_KIMI_K25_ENV = {
    "CC_STATUS_PROVIDER": "kimi",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "kimi-k2.5",
}

_KIMI_0905_ENV = {
    "CC_STATUS_PROVIDER": "kimi",
    "CC_STATUS_BILLING": "api",
    "ANTHROPIC_MODEL": "kimi-k2-0905-preview",
}


class FireworksPricingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    # ------------------------------------------------------------------
    # GLM-5.1 on Fireworks
    # ------------------------------------------------------------------

    def test_glm51_renders_calculated_cost(self):
        """GLM-5.1 on Fireworks uses blended fallback (no cache_write published).

        100k input + 5k output → blended sum 105k × $1.40/M = $0.147 → $0.15
        """
        out = run(read_fixture("fireworks-glm51-buckets.json"), _FW_GLM_ENV, self.tmp)
        self.assertIn("$0.15", out)
        self.assertNotIn("cost ?", out)

    def test_glm51_repeated_render_no_double_count(self):
        """Same GLM-5.1 payload rendered twice must not double-count."""
        first = run(read_fixture("fireworks-glm51-buckets.json"), _FW_GLM_ENV, self.tmp)
        second = run(read_fixture("fireworks-glm51-buckets.json"), _FW_GLM_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.15", second)

    def test_fireworks_unknown_model_renders_question(self):
        """A Fireworks model with no pricing entry renders cost ?."""
        env = dict(_FW_GLM_ENV)
        env["ANTHROPIC_MODEL"] = "fireworks-unknown-future-model"
        out = run(buckets_payload("fw-unknown"), env, self.tmp)
        self.assertIn("cost ?", out)

    # ------------------------------------------------------------------
    # MiniMax-M2.5 on Fireworks
    # ------------------------------------------------------------------

    def test_m2p5_on_fireworks_renders_calculated_cost(self):
        """minimax-m2p5 on Fireworks uses blended fallback (no cache_write published).

        105k tokens × $0.30/M = $0.0315 → $0.03
        """
        out = run(read_fixture("fireworks-m2p5-buckets.json"), _FW_M2P5_ENV, self.tmp)
        self.assertIn("$0.03", out)
        self.assertNotIn("cost ?", out)

    def test_m2p5_repeated_render_no_double_count(self):
        """Same minimax-m2p5 payload rendered twice must not double-count."""
        first = run(read_fixture("fireworks-m2p5-buckets.json"), _FW_M2P5_ENV, self.tmp)
        second = run(read_fixture("fireworks-m2p5-buckets.json"), _FW_M2P5_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.03", second)


class MiniMaxPricingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_m27_renders_calculated_cost(self):
        """MiniMax-M2.7 direct API uses per-bucket rates (all four published).

        100k input × $0.30/M + 5k output × $1.20/M = $0.030 + $0.006 = $0.036 → $0.04
        """
        out = run(read_fixture("minimax-m27-buckets.json"), _MM_M27_ENV, self.tmp)
        self.assertIn("$0.04", out)
        self.assertNotIn("cost ?", out)

    def test_m27_repeated_render_no_double_count(self):
        """Same MiniMax-M2.7 payload rendered twice must not double-count."""
        first = run(read_fixture("minimax-m27-buckets.json"), _MM_M27_ENV, self.tmp)
        second = run(read_fixture("minimax-m27-buckets.json"), _MM_M27_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.04", second)

    def test_minimax_unknown_model_renders_question(self):
        """A MiniMax model with no pricing entry renders cost ?."""
        env = dict(_MM_M27_ENV)
        env["ANTHROPIC_MODEL"] = "MiniMax-Unknown-Future"
        out = run(buckets_payload("mm-unknown"), env, self.tmp)
        self.assertIn("cost ?", out)


class KimiPricingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    # ------------------------------------------------------------------
    # kimi-k2.5
    # ------------------------------------------------------------------

    def test_kimi_k25_renders_calculated_cost(self):
        """kimi-k2.5 uses per-bucket rates (all four published via hit/miss pricing).

        200k input × $0.60/M = $0.12 → $0.12
        """
        out = run(read_fixture("kimi-k25-buckets.json"), _KIMI_K25_ENV, self.tmp)
        self.assertIn("$0.12", out)
        self.assertNotIn("cost ?", out)

    def test_kimi_k25_repeated_render_no_double_count(self):
        """Same kimi-k2.5 payload rendered twice must not double-count."""
        first = run(read_fixture("kimi-k25-buckets.json"), _KIMI_K25_ENV, self.tmp)
        second = run(read_fixture("kimi-k25-buckets.json"), _KIMI_K25_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.12", second)

    # ------------------------------------------------------------------
    # kimi-k2-0905-preview
    # ------------------------------------------------------------------

    def test_kimi_0905_renders_calculated_cost(self):
        """kimi-k2-0905-preview uses per-bucket rates.

        100k input × $0.60/M + 5k output × $2.50/M = $0.060 + $0.0125 = $0.0725 → $0.07
        """
        out = run(read_fixture("kimi-k2-0905-buckets.json"), _KIMI_0905_ENV, self.tmp)
        self.assertIn("$0.07", out)
        self.assertNotIn("cost ?", out)

    def test_kimi_0905_repeated_render_no_double_count(self):
        """Same kimi-k2-0905-preview payload rendered twice must not double-count."""
        first = run(read_fixture("kimi-k2-0905-buckets.json"), _KIMI_0905_ENV, self.tmp)
        second = run(read_fixture("kimi-k2-0905-buckets.json"), _KIMI_0905_ENV, self.tmp)
        self.assertEqual(first, second)
        self.assertIn("$0.07", second)

    def test_kimi_unknown_model_renders_question(self):
        """A Kimi model with no pricing entry renders cost ?."""
        env = dict(_KIMI_K25_ENV)
        env["ANTHROPIC_MODEL"] = "kimi-unknown-future-model"
        out = run(buckets_payload("kimi-unknown"), env, self.tmp)
        self.assertIn("cost ?", out)


class SuppressionRegressionTest(unittest.TestCase):
    """Verify that subscription and local suppression still works after adding new providers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_claude_subscription_suppresses_cost(self):
        """Claude Max subscription must not show any cost segment."""
        out = run(buckets_payload("sub-claude-regression"), {}, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    def test_glm_subscription_suppresses_cost(self):
        """Z.ai / GLM Coding Plan subscription must not show any cost segment."""
        env = {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_MODEL": "glm-5.1",
        }
        out = run(buckets_payload("sub-zai-regression"), env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost ", out)

    def test_local_gemma_suppresses_cost(self):
        """Local Gemma must not show any cost segment."""
        env = {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:18080",
            "ANTHROPIC_MODEL": "gemma",
        }
        out = run(buckets_payload("local-gemma-regression"), env, self.tmp)
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    def test_deepseek_still_works(self):
        """DeepSeek pricing from task 06-03c must still resolve correctly."""
        payload = json.dumps({
            "model": {"display_name": "deepseek-v4-pro"},
            "context_window": {
                "used_percentage": 44,
                "current_usage": 220000,
                "context_window_size": 500000,
            },
            "session_id": "deepseek-regression-from-06d",
        })
        env = {
            "CC_STATUS_PROVIDER": "deepseek",
            "CC_STATUS_BILLING": "api",
            "ANTHROPIC_MODEL": "deepseek-v4-pro[500k]",
        }
        out = run(payload, env, self.tmp)
        # 220k × $0.435/M = $0.0957 → $0.10
        self.assertIn("$0.10", out)
        self.assertNotIn("cost ?", out)


if __name__ == "__main__":
    unittest.main()
