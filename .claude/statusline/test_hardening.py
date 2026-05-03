#!/usr/bin/env python3
"""
Hardening tests for the cost display (task 06-03e).

Covers the required checks from the task brief that aren't already in
test_cost_engine / test_deepseek_pricing / test_additional_vendor_pricing:

- Distinct session_id starts a separate cost state.
- Same session_id with two different usages accumulates once each.
- Unknown provider with billing=api renders 'cost ?' (no crash, no $0.00).
- State files contain only safe data (no tokens, prompts, transcripts).
- No git information is rendered.
- Cost calculation makes no network calls (offline-only env).
- Cached/local renders complete under a reasonable time budget.
- Missing/null/invalid current_usage does not crash.

Run: python3 .claude/statusline/test_hardening.py -v
"""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "statusline.py")
FIX_COST = os.path.join(HERE, "fixtures", "cost-engine")


def run(stdin_json: str, env_overrides: dict, home: str, extra_path: bool = False) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "") if extra_path else "",
        "HOME": home,
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, SCRIPT],
        input=stdin_json,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def read_fixture(name: str) -> str:
    with open(os.path.join(FIX_COST, name)) as f:
        return f.read()


class HardeningTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mock_env = {
            "ANTHROPIC_BASE_URL": "https://api.example-mock.com/anthropic",
            "CC_STATUS_PROVIDER": "mock",
            "CC_STATUS_BILLING": "api",
            "CC_STATUS_MODEL": "mock-model",
        }

    # --- dedupe ----------------------------------------------------------

    def test_distinct_session_id_separate_state(self):
        """Same fixture content under two different session_ids → two
        independent cost trackers, each at its own total."""
        payload_a = json.dumps({
            "model": {"display_name": "mock-model"},
            "context_window": {"used_percentage": 10, "current_usage": 1_000_000},
            "session_id": "alpha",
        })
        payload_b = json.dumps({
            "model": {"display_name": "mock-model"},
            "context_window": {"used_percentage": 10, "current_usage": 2_000_000},
            "session_id": "beta",
        })
        out_a = run(payload_a, self.mock_env, self.tmp).stdout.strip()
        out_b = run(payload_b, self.mock_env, self.tmp).stdout.strip()
        self.assertIn("$1.00", out_a)
        self.assertIn("$2.00", out_b)
        # Two separate state files exist.
        cache_dir = os.path.join(self.tmp, ".cache", "claude-statusline")
        files = [f for f in os.listdir(cache_dir) if f.startswith("cost-")]
        self.assertEqual(len(files), 2)

    def test_repeated_render_same_session_same_usage(self):
        env = self.mock_env
        out1 = run(read_fixture("mock-call-1.json"), env, self.tmp).stdout.strip()
        out2 = run(read_fixture("mock-call-1.json"), env, self.tmp).stdout.strip()
        out3 = run(read_fixture("mock-call-1.json"), env, self.tmp).stdout.strip()
        self.assertEqual(out1, out2)
        self.assertEqual(out2, out3)
        self.assertIn("$1.00", out3)

    def test_second_distinct_usage_increments_once_per_render(self):
        env = self.mock_env
        run(read_fixture("mock-call-1.json"), env, self.tmp)
        # Repeat call-2 a few times — must only increment once.
        out_a = run(read_fixture("mock-call-2.json"), env, self.tmp).stdout.strip()
        out_b = run(read_fixture("mock-call-2.json"), env, self.tmp).stdout.strip()
        self.assertIn("$3.00", out_a)
        self.assertEqual(out_a, out_b)

    # --- unknown / missing pricing --------------------------------------

    def test_unknown_provider_billing_api_shows_cost_question(self):
        """An API base_url we don't recognize → provider=unknown,
        billing=api, no pricing entry → 'cost ?', not crash, not $0.00."""
        env = {"ANTHROPIC_BASE_URL": "https://example.unknown-vendor.test/anthropic",
               "ANTHROPIC_MODEL": "mystery-9000"}
        payload = json.dumps({
            "model": {"display_name": "mystery-9000"},
            "context_window": {"used_percentage": 22, "current_usage": 12345},
            "session_id": "unknown-vendor",
        })
        proc = run(payload, env, self.tmp)
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout.strip()
        self.assertIn("cost ?", out)
        self.assertNotIn("$0.00", out)

    def test_unknown_pricing_does_not_use_cost_total_cost_usd(self):
        """Even when stdin includes a non-null cost.total_cost_usd, custom
        providers must not display it as cost."""
        env = dict(self.mock_env)
        env["CC_STATUS_MODEL"] = "mock-unpriced"
        payload = json.dumps({
            "model": {"display_name": "mock-unpriced"},
            "context_window": {"used_percentage": 10, "current_usage": 999_999},
            "cost": {"total_cost_usd": 4.20},
            "session_id": "fallback-trap",
        })
        out = run(payload, env, self.tmp).stdout.strip()
        self.assertIn("cost ?", out)
        self.assertNotIn("$4.20", out)
        self.assertNotIn("$4", out)

    # --- suppression -----------------------------------------------------

    def test_explicit_subscription_overrides_suppress_cost(self):
        for provider, base_url in (
            ("claude", ""),
            ("zai", "https://api.z.ai/api/anthropic"),
        ):
            env = {
                "CC_STATUS_PROVIDER": provider,
                "CC_STATUS_BILLING": "subscription",
            }
            if base_url:
                env["ANTHROPIC_BASE_URL"] = base_url
            payload = json.dumps({
                "model": {"display_name": "x"},
                "context_window": {"used_percentage": 10, "current_usage": 1000},
                "session_id": f"sub-{provider}",
            })
            out = run(payload, env, self.tmp).stdout.strip()
            self.assertNotIn("$", out, f"provider={provider}: {out}")
            self.assertNotIn("cost ", out, f"provider={provider}: {out}")

    def test_explicit_local_billing_suppresses_cost(self):
        env = {
            "CC_STATUS_PROVIDER": "local",
            "CC_STATUS_BILLING": "local",
            "ANTHROPIC_MODEL": "gemma",
        }
        payload = json.dumps({
            "model": {"display_name": "gemma"},
            "context_window": {"used_percentage": 31, "current_usage": 5000},
            "session_id": "local-cost-suppress",
        })
        out = run(payload, env, self.tmp).stdout.strip()
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    # --- state file safety ----------------------------------------------

    def test_state_file_contains_only_safe_fields(self):
        """Run a render and confirm content from prompt/completion/
        transcript-shaped stdin fields never reaches the state file."""
        env = self.mock_env
        payload = json.dumps({
            "model": {"display_name": "mock-model"},
            "context_window": {"used_percentage": 5, "current_usage": 100},
            "session_id": "11111111-2222-3333-4444-555555555555",
            "transcript": [{"role": "user", "content": "DO_NOT_LEAK_TRANSCRIPT"}],
            "prompt": "DO_NOT_LEAK_PROMPT",
            "completion": "DO_NOT_LEAK_COMPLETION",
            "command": "DO_NOT_LEAK_COMMAND",
        })
        run(payload, env, self.tmp)

        cache_dir = os.path.join(self.tmp, ".cache", "claude-statusline")
        files = [f for f in os.listdir(cache_dir) if f.startswith("cost-")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(cache_dir, files[0])) as f:
            blob = f.read()

        for forbidden in (
            "DO_NOT_LEAK_TRANSCRIPT", "DO_NOT_LEAK_PROMPT",
            "DO_NOT_LEAK_COMPLETION", "DO_NOT_LEAK_COMMAND",
        ):
            self.assertNotIn(forbidden, blob,
                             f"state file leaked content '{forbidden}': {blob}")

        state = json.loads(blob)
        for forbidden_key in ("transcript", "prompt", "completion", "command"):
            self.assertNotIn(forbidden_key, state)
        allowed_keys = {
            "version", "session_id", "provider", "model", "pricing_key",
            "total_cost_usd", "total_tokens", "last_usage_total",
            "last_buckets", "seen_usage_fingerprints", "updated_at",
        }
        self.assertEqual(set(state.keys()) - allowed_keys, set(),
                         f"state file has unexpected keys: {set(state.keys())}")

    def test_state_file_does_not_include_api_token_value(self):
        env = dict(self.mock_env)
        env["ANTHROPIC_AUTH_TOKEN"] = "tok-do-not-persist-12345"
        env["ANTHROPIC_API_KEY"]   = "key-do-not-persist-67890"
        run(read_fixture("mock-call-1.json"), env, self.tmp)
        cache_dir = os.path.join(self.tmp, ".cache", "claude-statusline")
        for name in os.listdir(cache_dir):
            with open(os.path.join(cache_dir, name)) as f:
                blob = f.read()
            self.assertNotIn("tok-do-not-persist-12345", blob)
            self.assertNotIn("key-do-not-persist-67890", blob)

    # --- input robustness -----------------------------------------------

    def test_invalid_stdin_does_not_crash(self):
        proc = run("not json {{}", self.mock_env, self.tmp)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.strip())

    def test_missing_current_usage_renders_no_cost_segment(self):
        payload = json.dumps({
            "model": {"display_name": "mock-model"},
            "context_window": {"used_percentage": 10},
            "session_id": "no-usage",
        })
        out = run(payload, self.mock_env, self.tmp).stdout.strip()
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    def test_string_current_usage_treated_as_missing(self):
        payload = json.dumps({
            "model": {"display_name": "mock-model"},
            "context_window": {"used_percentage": 10, "current_usage": "garbage"},
            "session_id": "weird-usage",
        })
        proc = run(payload, self.mock_env, self.tmp)
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout.strip()
        self.assertNotIn("$", out)
        self.assertNotIn("cost", out)

    # --- output cleanliness ---------------------------------------------

    def test_no_git_information_in_output(self):
        out = run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp).stdout.strip()
        # No git-ish words anywhere in the rendered status line.
        for token in ("git", "branch", "commit", "main", "HEAD"):
            self.assertNotIn(token, out.lower() if token == "git" else out,
                             f"unexpected git-related token '{token}' in {out!r}")

    # --- runtime / network ----------------------------------------------

    def test_cost_path_makes_no_network_calls(self):
        """Import the engine, then patch socket.create_connection and
        urllib.request.urlopen to raise. compute_api_cost must still run."""
        helper = r'''
import sys, json, os
sys.path.insert(0, %r)
import statusline
import socket, urllib.request
def _block(*a, **kw): raise RuntimeError("network not allowed in cost path")
socket.create_connection = _block
urllib.request.urlopen = _block
status = json.loads(%r)
env = statusline.detect_environment(dict(os.environ), status)
out = statusline.compute_api_cost(status, env)
print(out)
'''
        code = helper % (HERE, read_fixture("mock-call-1.json"))
        env = dict(os.environ)
        env.update(self.mock_env)
        env["HOME"] = self.tmp
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("$1.00", proc.stdout)

    def test_runtime_under_budget_for_cached_path(self):
        """Mock/cost render should be well under 2s; in practice << 200ms."""
        # Warm up state once.
        run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp)
        start = time.monotonic()
        run(read_fixture("mock-call-1.json"), self.mock_env, self.tmp)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0,
                        f"cached cost render took {elapsed:.3f}s, expected < 2s")


if __name__ == "__main__":
    unittest.main()
