#!/usr/bin/env python3
"""
Tests for the claude-code-router /v1/quota path (ccr provider).

Covers: router detection (ccr- token prefix, claude-router host, precedence
over the localhost heuristic), zai-/ocg- alias stripping in model naming and
pricing keys, /v1/quota payload parsing (display-name provider entries, ISO
resetAt), and the cache/TTL/stale orchestration in format_ccr_quota.

Offline by design: fetch_ccr_quota is monkeypatched.

Run: python3 .claude/statusline/test_ccr_quota.py -v
"""

import datetime
import hashlib
import importlib.util
import json
import os
import tempfile
import time
import unittest
import urllib.error
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "statusline.py")

_spec = importlib.util.spec_from_file_location("statusline_under_test", SCRIPT)
statusline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(statusline)


def iso_utc(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")


def quota_payload(five_h_pct=26, seven_d_pct=53, zai_status="ok",
                  with_zai=True, zai_name="Z.ai (Global) - Coding Plan"):
    """Build a /v1/quota response mirroring the gateway's account snapshots."""
    now = time.time()
    providers = []
    if with_zai:
        providers.append({
            "provider": zai_name,
            "status": zai_status,
            "meters": [
                {"id": "five_hour_quota", "kind": "quota", "label": "5h quota",
                 "limit": 100, "remaining": 100 - five_h_pct, "used": five_h_pct,
                 "unit": "%", "window": "5h",
                 "resetAt": iso_utc(now + 4.8 * 3600)},
                {"id": "weekly_quota", "kind": "quota", "label": "Weekly quota",
                 "limit": 100, "remaining": 100 - seven_d_pct, "used": seven_d_pct,
                 "unit": "%", "window": "weekly",
                 "resetAt": iso_utc(now + 99 * 3600)},
            ],
            "source": "http-json",
            "updatedAt": iso_utc(now),
        })
    providers.append({
        "provider": "OpenCode Go",
        "status": "ok",
        "meters": [],
        "source": "http-json",
        "updatedAt": iso_utc(now),
    })
    return {"ok": all(p["status"] == "ok" for p in providers), "providers": providers}


def ccr_cache_path_for(home: str, token: str) -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
    return os.path.join(home, ".cache", "claude-statusline", f"ccr-quota-{token_hash}.json")


class DetectionTest(unittest.TestCase):
    def test_router_host_detected(self):
        self.assertEqual(
            statusline._infer_provider_billing("https://claude-router.h02.activecdn.net"),
            ("ccr", "subscription"),
        )

    def test_router_localhost_not_local(self):
        provider, billing = statusline._infer_provider_billing("http://claude-router.localhost")
        self.assertEqual((provider, billing), ("ccr", "subscription"))

    def test_ccr_token_wins_over_localhost(self):
        provider, billing = statusline._infer_provider_billing(
            "http://127.0.0.1:3456", "ccr-a0ca9441"
        )
        self.assertEqual((provider, billing), ("ccr", "subscription"))

    def test_ccr_token_without_base_url(self):
        provider, billing = statusline._infer_provider_billing(None, "ccr-a0ca9441")
        self.assertEqual((provider, billing), ("ccr", "subscription"))

    def test_plain_localhost_still_local(self):
        self.assertEqual(
            statusline._infer_provider_billing("http://127.0.0.1:18083"),
            ("local", "local"),
        )

    def test_zai_url_still_zai(self):
        self.assertEqual(
            statusline._infer_provider_billing("https://api.z.ai/api/anthropic", "e468-not-ccr"),
            ("zai", "subscription"),
        )

    def test_detect_environment_claude_lg(self):
        with mock.patch.dict(os.environ, {
            "ANTHROPIC_BASE_URL": "https://claude-router.h02.activecdn.net",
            "ANTHROPIC_AUTH_TOKEN": "ccr-a0ca9441",
            "ANTHROPIC_MODEL": "zai-glm-5.3[1m]",
        }, clear=False):
            env = statusline.detect_environment(dict(os.environ), {})
        self.assertEqual(env.provider, "ccr")
        self.assertEqual(env.billing, "subscription")
        self.assertEqual(env.model, "GLM-5.3")
        self.assertEqual(env.pricing_key, "glm-5.3")

    def test_explicit_provider_override_still_wins(self):
        with mock.patch.dict(os.environ, {
            "CC_STATUS_PROVIDER": "zai",
            "ANTHROPIC_BASE_URL": "https://claude-router.h02.activecdn.net",
            "ANTHROPIC_AUTH_TOKEN": "ccr-a0ca9441",
        }, clear=False):
            env = statusline.detect_environment(dict(os.environ), {})
        self.assertEqual(env.provider, "zai")


class AliasPrefixTest(unittest.TestCase):
    def test_normalize_strips_zai_prefix(self):
        self.assertEqual(statusline._normalize_model_name("zai-glm-5.3[1m]", "ccr"), "GLM-5.3")
        self.assertEqual(statusline._normalize_model_name("zai-glm-5.3-flash[1m]", "ccr"), "GLM-5.3-flash")

    def test_normalize_strips_ocg_prefix(self):
        self.assertEqual(
            statusline._normalize_model_name("ocg-deepseek-v4-flash-haiku", "ccr"), "DeepSeek"
        )

    def test_pricing_keys_strip_prefixes(self):
        self.assertEqual(statusline._pricing_key("zai-glm-5.3[1m]"), "glm-5.3")
        self.assertEqual(statusline._pricing_key("ocg-deepseek-v4-flash-haiku"), "deepseek-v4-flash-haiku")

    def test_unprefixed_names_unchanged(self):
        self.assertEqual(statusline._pricing_key("glm-5.3[1m]"), "glm-5.3")
        self.assertEqual(statusline._normalize_model_name("claude-opus-5", "claude"), "Opus")


class ParseCcrQuotaTest(unittest.TestCase):
    def test_meters_mapped_to_summary(self):
        payload = quota_payload(five_h_pct=26, seven_d_pct=53)
        summary = statusline.parse_ccr_quota(payload)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.five_hour_pct, 26)
        self.assertEqual(summary.seven_day_pct, 53)
        self.assertAlmostEqual(summary.five_hour_reset_at, time.time() + 4.8 * 3600, delta=5)
        self.assertAlmostEqual(summary.seven_day_reset_at, time.time() + 99 * 3600, delta=5)
        self.assertIsNone(summary.mcp_pct)

    def test_missing_zai_entry_is_none(self):
        self.assertIsNone(statusline.parse_ccr_quota(quota_payload(with_zai=False)))
        self.assertIsNone(statusline.parse_ccr_quota({}))
        self.assertIsNone(statusline.parse_ccr_quota(None))
        self.assertIsNone(statusline.parse_ccr_quota({"providers": "nope"}))

    def test_error_status_entry_is_none(self):
        self.assertIsNone(statusline.parse_ccr_quota(quota_payload(zai_status="error")))

    def test_unsupported_status_entry_is_none(self):
        self.assertIsNone(statusline.parse_ccr_quota(quota_payload(zai_status="unsupported")))

    def test_provider_match_is_case_insensitive(self):
        payload = quota_payload(zai_name="z.ai coding plan")
        self.assertIsNotNone(statusline.parse_ccr_quota(payload))


class CcrOrchestrationTest(unittest.TestCase):
    """
    format_ccr_quota flow with a monkeypatched fetch:
    cache reads/writes, auth failure, stale fallback.
    """

    TOKEN = "ccr-a0ca9441"
    BASE = "https://claude-router.h02.activecdn.net"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env_patch = mock.patch.dict(os.environ, {
            "HOME": self.tmp,
            "ANTHROPIC_BASE_URL": self.BASE,
            "ANTHROPIC_AUTH_TOKEN": self.TOKEN,
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.status_env = statusline.StatusEnvironment(
            provider="ccr", billing="subscription", profile="ccr-plan",
            model="GLM-5.3", pricing_key="glm-5.3",
        )

    @property
    def cache_path(self):
        return ccr_cache_path_for(self.tmp, self.TOKEN)

    def write_cache(self, payload, age_seconds=0):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        data = dict(payload)
        data["_cached_at"] = time.time() - age_seconds
        with open(self.cache_path, "w") as f:
            json.dump(data, f)

    def test_live_fetch_cached_and_rendered(self):
        payload = quota_payload(five_h_pct=26, seven_d_pct=53)
        with mock.patch.object(statusline, "fetch_ccr_quota", return_value=payload):
            segments = statusline.format_ccr_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 26%"), segments)
        self.assertTrue(any(s.startswith("7d 53%") for s in segments), segments)
        # Payload was cached under the ccr key
        with open(self.cache_path) as f:
            cached = json.load(f)
        self.assertEqual(cached["providers"][0]["provider"], "Z.ai (Global) - Coding Plan")

        # Second render within TTL must not hit the network at all
        def boom(*a, **kw):
            raise AssertionError("network fetch during TTL cache hit")
        with mock.patch.object(statusline, "fetch_ccr_quota", side_effect=boom):
            segments = statusline.format_ccr_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 26%"), segments)

    def test_http_401_renders_quota_question_without_caching(self):
        with mock.patch.object(
            statusline, "fetch_ccr_quota",
            side_effect=urllib.error.HTTPError(
                "url", 401, "Unauthorized", {}, None),
        ):
            segments = statusline.format_ccr_quota(self.status_env)
        self.assertEqual(segments, ["quota ?"])
        self.assertFalse(os.path.exists(self.cache_path))

    def test_transient_error_falls_back_to_stale(self):
        self.write_cache(quota_payload(five_h_pct=9, seven_d_pct=51), age_seconds=3600)
        with mock.patch.object(
            statusline, "fetch_ccr_quota",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            segments = statusline.format_ccr_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 9%"), segments)
        self.assertIn("stale", segments[0])

    def test_payload_without_zai_does_not_clobber_cache(self):
        self.write_cache(quota_payload(five_h_pct=9, seven_d_pct=51), age_seconds=3600)
        before = open(self.cache_path).read()
        with mock.patch.object(statusline, "fetch_ccr_quota",
                               return_value=quota_payload(with_zai=False)):
            segments = statusline.format_ccr_quota(self.status_env)
        # Stale data still rendered, not overwritten by the zai-less payload
        self.assertTrue(segments[0].startswith("5h 9%"), segments)
        self.assertIn("stale", segments[0])
        self.assertEqual(open(self.cache_path).read(), before)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env_patch = mock.patch.dict(os.environ, {
            "HOME": self.tmp,
            "ANTHROPIC_BASE_URL": "https://claude-router.h02.activecdn.net",
            "ANTHROPIC_AUTH_TOKEN": "ccr-a0ca9441",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        with open(os.path.join(HERE, "fixtures", "ccr-plan.json")) as f:
            self.status_input = json.load(f)

    def ccr_env(self):
        return statusline.StatusEnvironment(
            provider="ccr", billing="subscription", profile="ccr-plan",
            model="GLM-5.3", pricing_key="glm-5.3",
        )

    def test_render_claude_lg_line(self):
        with mock.patch.object(statusline, "fetch_ccr_quota",
                               return_value=quota_payload(five_h_pct=26, seven_d_pct=53)):
            line = statusline.render_status_line(self.status_input, self.ccr_env())
        self.assertTrue(line.startswith("GLM-5.3 plan | ctx 41%"), line)
        self.assertIn("5h 26%", line)
        self.assertIn("7d 53%", line)

    def test_render_without_zai_data_shows_quota_question(self):
        with mock.patch.object(statusline, "fetch_ccr_quota",
                               return_value=quota_payload(with_zai=False)):
            line = statusline.render_status_line(self.status_input, self.ccr_env())
        self.assertIn("quota ?", line)

    def test_peak_hours_render_behind_ccr_with_zai(self):
        # 2026-09-07 is a Monday; 07:00 UTC is inside the 06:00-10:00 peak window
        monday_peak = datetime.datetime(2026, 9, 7, 7, 0, tzinfo=datetime.timezone.utc).timestamp()
        with mock.patch.object(statusline, "fetch_ccr_quota",
                               return_value=quota_payload(five_h_pct=26, seven_d_pct=53)):
            # render warms the ccr cache; the peak check then finds the zai entry
            statusline.render_status_line(self.status_input, self.ccr_env())
        prefix, suffix = statusline.format_zai_peak_segments(self.ccr_env(), now_epoch=monday_peak)
        self.assertEqual(prefix, ["🔥 PEAK HOURS"])
        self.assertTrue(suffix[0].startswith("Peak hours end at"), suffix)

    def test_peak_hours_silent_without_zai_cache(self):
        # 2026-09-07 07:00 UTC — inside peak, but no cached /v1/quota payload
        monday_peak = datetime.datetime(2026, 9, 7, 7, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertFalse(os.path.exists(
            ccr_cache_path_for(self.tmp, "ccr-a0ca9441")))
        prefix, suffix = statusline.format_zai_peak_segments(self.ccr_env(), now_epoch=monday_peak)
        self.assertEqual((prefix, suffix), ([], []))

    def test_peak_hours_silent_when_cache_has_no_zai(self):
        monday_peak = datetime.datetime(2026, 9, 7, 7, 0, tzinfo=datetime.timezone.utc).timestamp()
        path = ccr_cache_path_for(self.tmp, "ccr-a0ca9441")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(quota_payload(with_zai=False), f)
        prefix, suffix = statusline.format_zai_peak_segments(self.ccr_env(), now_epoch=monday_peak)
        self.assertEqual((prefix, suffix), ([], []))


if __name__ == "__main__":
    unittest.main()
