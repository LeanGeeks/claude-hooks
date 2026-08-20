#!/usr/bin/env python3
"""
Tests for GLM Coding Plan quota parsing and caching (Z.ai credit system).

Covers the 2026-08 credit-system migration: CREDIT_LIMIT entries with
unit/number-encoded windows alongside the legacy TOKENS_LIMIT/TIME_LIMIT
format, 5h/weekly window classification, cache hygiene for HTTP-200 error
bodies, and the stale-cache fallback.

Offline by design: network fetches are monkeypatched (in-process tests) or
pointed at an unresolvable host (subprocess tests).

Run: python3 .claude/statusline/test_glm_quota.py -v
"""

import hashlib
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
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


def credit_payload(five_h_pct=1, seven_d_pct=36, five_reset_s=None, week_reset_s=None,
                   with_time_limit=False):
    """Build a credit-era payload mirroring the live API response."""
    now = time.time()
    five_reset_ms = int((five_reset_s if five_reset_s is not None else now + 4.8 * 3600) * 1000)
    week_reset_ms = int((week_reset_s if week_reset_s is not None else now + 99 * 3600) * 1000)
    limits = [
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 12000,
         "currentValue": 208, "remaining": 11791, "percentage": five_h_pct,
         "nextResetTime": five_reset_ms},
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 60000,
         "currentValue": 22027, "remaining": 37972, "percentage": seven_d_pct,
         "nextResetTime": week_reset_ms},
    ]
    if with_time_limit:
        limits.append({"type": "TIME_LIMIT", "percentage": 12})
    return {
        "code": 200, "msg": "Operation successful", "success": True,
        "data": {"limits": limits, "level": "pro"},
    }


def cache_path_for(home: str, token: str) -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
    return os.path.join(home, ".cache", "claude-statusline", f"glm-quota-{token_hash}.json")


class ParseCreditEraTest(unittest.TestCase):
    """parse_glm_quota over the credit-era payload shape."""

    def test_credit_windows_parsed(self):
        summary = statusline.parse_glm_quota(credit_payload())
        self.assertEqual(summary.five_hour_pct, 1.0)
        self.assertEqual(summary.seven_day_pct, 36.0)
        self.assertIsNone(summary.mcp_pct)
        self.assertIsNotNone(summary.five_hour_reset_at)
        self.assertIsNotNone(summary.seven_day_reset_at)
        segments = statusline.format_glm_quota_segment(summary)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].startswith("5h 1% reset at "))
        self.assertTrue(segments[1].startswith("7d 36% resets in 4d"))

    def test_classification_beats_reset_order(self):
        """Weekly window in its final hours resets sooner than the 5h one —
        unit/number decoding must win over the reset-time heuristic."""
        now = time.time()
        payload = credit_payload(
            five_h_pct=80, seven_d_pct=95,
            five_reset_s=now + 4.9 * 3600, week_reset_s=now + 0.5 * 3600,
        )
        summary = statusline.parse_glm_quota(payload)
        self.assertEqual(summary.five_hour_pct, 80.0)
        self.assertEqual(summary.seven_day_pct, 95.0)

    def test_single_short_window_is_five_hour(self):
        payload = {"data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 42},
        ]}}
        summary = statusline.parse_glm_quota(payload)
        self.assertEqual(summary.five_hour_pct, 42.0)
        self.assertIsNone(summary.seven_day_pct)

    def test_single_long_window_is_weekly(self):
        payload = {"data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percentage": 42},
        ]}}
        summary = statusline.parse_glm_quota(payload)
        self.assertIsNone(summary.five_hour_pct)
        self.assertEqual(summary.seven_day_pct, 42.0)

    def test_credit_with_time_limit_keeps_mcp(self):
        summary = statusline.parse_glm_quota(credit_payload(with_time_limit=True))
        self.assertEqual(summary.mcp_pct, 12.0)
        segments = statusline.format_glm_quota_segment(summary)
        self.assertEqual(segments[-1], "MCP 12%")

    def test_percentage_fallback_from_current_and_usage(self):
        payload = {"data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "usage": 12000, "currentValue": 3000, "nextResetTime": 1},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
             "usage": 60000, "currentValue": 15000, "nextResetTime": 2},
        ]}}
        summary = statusline.parse_glm_quota(payload)
        self.assertAlmostEqual(summary.five_hour_pct, 25.0)
        self.assertAlmostEqual(summary.seven_day_pct, 25.0)

    def test_unknown_unit_falls_back_to_reset_order(self):
        payload = {"data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 9, "number": 5, "percentage": 10,
             "nextResetTime": 200},
            {"type": "CREDIT_LIMIT", "unit": 9, "number": 1, "percentage": 70,
             "nextResetTime": 100},
        ]}}
        summary = statusline.parse_glm_quota(payload)
        # Soonest reset (100) is treated as the 5-hour window
        self.assertEqual(summary.five_hour_pct, 70.0)
        self.assertEqual(summary.seven_day_pct, 10.0)

    def test_empty_limits_render_quota_question(self):
        summary = statusline.parse_glm_quota({"data": {"limits": []}})
        self.assertIsNone(summary.five_hour_pct)
        self.assertEqual(statusline.format_glm_quota_segment(summary), ["quota ?"])


class ParseLegacyTest(unittest.TestCase):
    """Pre-credit TOKENS_LIMIT/TIME_LIMIT payloads keep working."""

    LEGACY = {"data": {"limits": [
        {"type": "TOKENS_LIMIT", "percentage": 5, "nextResetTime": 100},
        {"type": "TOKENS_LIMIT", "percentage": 27, "nextResetTime": 200},
        {"type": "TIME_LIMIT", "percentage": 1, "nextResetTime": 300},
    ]}}

    def test_legacy_payload(self):
        summary = statusline.parse_glm_quota(self.LEGACY)
        self.assertEqual(summary.five_hour_pct, 5.0)
        self.assertEqual(summary.seven_day_pct, 27.0)
        self.assertEqual(summary.mcp_pct, 1.0)

    def test_legacy_payload_is_usable_and_not_auth_failure(self):
        self.assertTrue(statusline._quota_payload_usable(self.LEGACY))
        self.assertFalse(statusline._quota_auth_failure(self.LEGACY))


class PayloadHygieneTest(unittest.TestCase):
    """Validation of HTTP-200 error bodies (observed in the wild)."""

    AUTH_BODY = {"code": 1000, "msg": "Authentication Failed", "success": False}

    def test_auth_body_detected(self):
        self.assertFalse(statusline._quota_payload_usable(self.AUTH_BODY))
        self.assertTrue(statusline._quota_auth_failure(self.AUTH_BODY))

    def test_auth_message_without_code(self):
        body = {"code": 42, "msg": "invalid token", "success": False}
        self.assertTrue(statusline._quota_auth_failure(body))

    def test_non_auth_error_is_not_auth_failure(self):
        body = {"code": 500, "msg": "busy", "success": False}
        self.assertFalse(statusline._quota_payload_usable(body))
        self.assertFalse(statusline._quota_auth_failure(body))

    def test_garbage_payloads(self):
        for payload in (None, "x", [], {}, {"data": None}, {"data": {"limits": None}}):
            self.assertFalse(statusline._quota_payload_usable(payload))
            self.assertFalse(statusline._quota_auth_failure(payload))


class OrchestrationTest(unittest.TestCase):
    """
    format_glm_subscription_quota flow with a monkeypatched fetch:
    cache reads/writes, error-body handling, stale fallback.
    """

    TOKEN = "fake-token"
    BASE = "https://api.z.ai/api/anthropic"

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
            provider="zai", billing="subscription", profile="glm-plan",
            model="GLM-4.7", pricing_key="glm-4.7",
        )

    @property
    def cache_path(self):
        return cache_path_for(self.tmp, self.TOKEN)

    def write_cache(self, payload, age_seconds=0):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        data = dict(payload)
        data["_cached_at"] = time.time() - age_seconds
        with open(self.cache_path, "w") as f:
            json.dump(data, f)

    def read_cache_raw(self):
        with open(self.cache_path) as f:
            return f.read()

    def test_live_fetch_cached_and_rendered(self):
        payload = credit_payload(five_h_pct=7, seven_d_pct=44)
        with mock.patch.object(statusline, "fetch_glm_quota", return_value=payload):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 7%"))
        self.assertTrue(any(s.startswith("7d 44%") for s in segments))
        # Payload was cached with limits intact
        cached = json.loads(self.read_cache_raw())
        self.assertEqual(len(cached["data"]["limits"]), 2)

        # Second render within TTL must not hit the network at all
        def boom(*a, **kw):
            raise AssertionError("network fetch during TTL cache hit")
        with mock.patch.object(statusline, "fetch_glm_quota", side_effect=boom):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 7%"))

    def test_auth_failure_body_renders_quota_question_and_keeps_cache(self):
        good = credit_payload(five_h_pct=9, seven_d_pct=51)
        self.write_cache(good, age_seconds=3600)  # expired → live fetch attempted
        before = self.read_cache_raw()
        with mock.patch.object(statusline, "fetch_glm_quota",
                               return_value=dict(PayloadHygieneTest.AUTH_BODY)):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertEqual(segments, ["quota ?"])
        # Good stale data was not clobbered by the error body
        self.assertEqual(self.read_cache_raw(), before)

    def test_transient_error_body_falls_back_to_stale(self):
        good = credit_payload(five_h_pct=9, seven_d_pct=51)
        self.write_cache(good, age_seconds=3600)
        before = self.read_cache_raw()
        with mock.patch.object(statusline, "fetch_glm_quota",
                               return_value={"code": 500, "msg": "busy", "success": False}):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 9%"))
        self.assertIn("stale", segments[0])
        self.assertEqual(self.read_cache_raw(), before)

    def test_http_401_renders_quota_question(self):
        err = urllib.error.HTTPError(
            self.BASE, 401, "Unauthorized", email_headers(), io.BytesIO(b"{}"))
        with mock.patch.object(statusline, "fetch_glm_quota", side_effect=err):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertEqual(segments, ["quota ?"])

    def test_urlerror_serves_stale(self):
        self.write_cache(credit_payload(five_h_pct=30), age_seconds=3600)
        err = urllib.error.URLError("connection refused")
        with mock.patch.object(statusline, "fetch_glm_quota", side_effect=err):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertTrue(segments[0].startswith("5h 30%"))
        self.assertIn("stale", segments[0])

    def test_missing_token_renders_quota_question(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": ""}):
            segments = statusline.format_glm_subscription_quota(self.status_env)
        self.assertEqual(segments, ["quota ?"])


def email_headers():
    import email.message
    return email.message.Message()


class ResetFormatTest(unittest.TestCase):
    """Reset rendering: wall clock + countdown inside 24h, floored days beyond."""

    def tail(self, offset_s):
        return statusline._format_reset_tail(time.time() + offset_s)

    def test_no_epoch_is_empty(self):
        self.assertEqual(statusline._format_reset_tail(None), "")
        self.assertEqual(statusline._format_reset_tail(0), "")

    def test_past_reset(self):
        self.assertEqual(self.tail(-10), "reset now")

    def test_inside_24h_wall_clock_and_countdown(self):
        epoch = time.time() + 4 * 3600 + 29 * 60 + 30
        wall = datetime.datetime.fromtimestamp(epoch).strftime("%H:%M")
        self.assertEqual(
            statusline._format_reset_tail(epoch),
            f"reset at {wall} (in 4:29)")

    def test_beyond_24h_whole_days_floored(self):
        self.assertEqual(self.tail(3.4 * 86400), "resets in 3d")
        self.assertEqual(self.tail(1.4 * 86400), "resets in 1d")
        # just past the 24h boundary (exact-24h would race below it)
        self.assertEqual(self.tail(24 * 3600 + 30), "resets in 1d")

    def test_just_under_24h_uses_clock(self):
        epoch = time.time() + 24 * 3600 - 90
        wall = datetime.datetime.fromtimestamp(epoch).strftime("%H:%M")
        self.assertEqual(
            statusline._format_reset_tail(epoch),
            f"reset at {wall} (in 23:58)")

    def test_segments_render_full_format(self):
        now = time.time()
        summary = statusline.QuotaSummary(
            five_hour_pct=7, five_hour_reset_at=now + 4 * 3600 + 29 * 60 + 30,
            seven_day_pct=37, seven_day_reset_at=now + 99 * 3600,
            mcp_pct=None)
        segments = statusline.format_glm_quota_segment(summary)
        wall = datetime.datetime.fromtimestamp(
            summary.five_hour_reset_at).strftime("%H:%M")
        self.assertEqual(segments[0], f"5h 7% reset at {wall} (in 4:29)")
        self.assertEqual(segments[1], "7d 37% resets in 4d")


class PeakHoursTest(unittest.TestCase):
    """
    Peak hours = Mon-Fri 14:00-18:00 UTC+8 (06:00-10:00 UTC), weekends
    off-peak all day. 2026-08-19 is a Wednesday; 2026-08-21 Friday;
    2026-08-22 Saturday; 2026-08-24 the next Monday.
    """

    def utc(self, y, mo, d, h, mi=0, s=0):
        return datetime.datetime(y, mo, d, h, mi, s,
                                  tzinfo=datetime.timezone.utc).timestamp()

    def env(self, provider="zai", billing="subscription"):
        return statusline.StatusEnvironment(
            provider=provider, billing=billing, profile="glm-plan",
            model="GLM-5.3", pricing_key="glm-5.3",
        )

    # --- clock state ------------------------------------------------------

    def test_state_during_peak(self):
        state = statusline.zai_peak_state(self.utc(2026, 8, 19, 7, 30))
        self.assertTrue(state["is_peak"])
        self.assertEqual(state["kind"], "end")
        self.assertEqual(state["boundary"], self.utc(2026, 8, 19, 10))

    def test_state_just_inside_end_boundary(self):
        state = statusline.zai_peak_state(self.utc(2026, 8, 19, 9, 59, 59))
        self.assertTrue(state["is_peak"])
        self.assertEqual(state["boundary"], self.utc(2026, 8, 19, 10))

    def test_state_before_peak_same_day(self):
        state = statusline.zai_peak_state(self.utc(2026, 8, 19, 4))
        self.assertFalse(state["is_peak"])
        self.assertEqual(state["kind"], "start")
        self.assertEqual(state["boundary"], self.utc(2026, 8, 19, 6))

    def test_state_after_peak_next_day(self):
        state = statusline.zai_peak_state(self.utc(2026, 8, 19, 11))
        self.assertEqual(state["boundary"], self.utc(2026, 8, 20, 6))

    def test_state_friday_after_peak_skips_weekend(self):
        state = statusline.zai_peak_state(self.utc(2026, 8, 21, 11))
        self.assertEqual(state["boundary"], self.utc(2026, 8, 24, 6))

    def test_state_weekend_off_peak(self):
        for hour in (3, 8, 15):
            state = statusline.zai_peak_state(self.utc(2026, 8, 22, hour))
            self.assertFalse(state["is_peak"], msg=f"Sat {hour}:00 UTC")
            self.assertEqual(state["boundary"], self.utc(2026, 8, 24, 6))

    # --- segment rendering ------------------------------------------------

    def test_segments_during_peak(self):
        now = self.utc(2026, 8, 19, 7, 30)
        prefix, suffix = statusline.format_zai_peak_segments(self.env(), now)
        wall = datetime.datetime.fromtimestamp(
            self.utc(2026, 8, 19, 10)).strftime("%H:%M")
        self.assertEqual(prefix, ["🔥 PEAK HOURS"])
        self.assertEqual(suffix, [f"Peak hours end at {wall} (in 2:30)"])

    def test_segments_approaching_within_1h(self):
        start = self.utc(2026, 8, 19, 6)
        now = start - (13 * 60 + 25)
        prefix, suffix = statusline.format_zai_peak_segments(self.env(), now)
        wall = datetime.datetime.fromtimestamp(start).strftime("%H:%M")
        self.assertEqual(prefix, [])
        self.assertEqual(suffix, [f"⚠️ Peak hours start at {wall} (in 13:25)"])

    def test_segments_more_than_1h_out_hidden(self):
        # Wednesday 20:00 UTC — next start is Thursday 06:00, 10h away
        prefix, suffix = statusline.format_zai_peak_segments(
            self.env(), self.utc(2026, 8, 19, 20))
        self.assertEqual((prefix, suffix), ([], []))

    def test_segments_hidden_for_other_providers(self):
        now = self.utc(2026, 8, 19, 7, 30)  # mid-peak
        self.assertEqual(statusline.format_zai_peak_segments(
            self.env(provider="claude"), now), ([], []))
        self.assertEqual(statusline.format_zai_peak_segments(
            self.env(billing="api"), now), ([], []))

    def test_countdown_short_format(self):
        fmt = statusline._format_countdown_short
        self.assertEqual(fmt(3 * 3600 + 50 * 60), "3:50")
        self.assertEqual(fmt(3600), "1:00")
        self.assertEqual(fmt(13 * 60 + 25), "13:25")
        self.assertEqual(fmt(45), "0:45")

    # --- render wiring ----------------------------------------------------

    def test_render_places_marker_first_and_note_last(self):
        now = time.time()
        with mock.patch.object(statusline, "zai_peak_state",
                               return_value={"is_peak": True,
                                             "boundary": now + 2.5 * 3600 + 30,
                                             "kind": "end"}), \
             mock.patch.object(statusline, "format_glm_subscription_quota",
                               return_value=["5h 1% reset at 20:13 (in 4:08)",
                                             "7d 37% resets in 4d"]):
            line = statusline.render_status_line(
                {"context_window": {"used_percentage": 18}}, self.env())
        parts = line.split(" | ")
        self.assertEqual(parts[0], "🔥 PEAK HOURS")
        self.assertEqual(parts[1], "GLM-5.3 plan")
        self.assertEqual(parts[2], "ctx 18%")
        self.assertTrue(parts[-1].startswith("Peak hours end at "),
                        msg=f"last segment: {parts[-1]!r}")
        self.assertIn("(in 2:30)", parts[-1])


class EndToEndTest(unittest.TestCase):
    """Subprocess runs with a pre-seeded cache (no network)."""

    TOKEN = "e2e-token"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def run_script(self, base_url):
        stdin_json = json.dumps({
            "model": {"display_name": "glm-4.7"},
            "context_window": {"used_percentage": 58},
            "session_id": "glm-e2e",
        })
        env = {
            "PATH": "",
            "HOME": self.tmp,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": self.TOKEN,
        }
        proc = subprocess.run(
            [sys.executable, SCRIPT],
            input=stdin_json, env=env,
            capture_output=True, text=True, timeout=15,
        )
        return proc.stdout.strip()

    def seed_cache(self, age_seconds):
        payload = credit_payload(
            five_h_pct=1, seven_d_pct=36,
            five_reset_s=time.time() + 2 * 3600 + 5 * 60 + 30,
        )
        path = cache_path_for(self.tmp, self.TOKEN)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload["_cached_at"] = time.time() - age_seconds
        with open(path, "w") as f:
            json.dump(payload, f)

    def test_fresh_cache_renders_credit_quota(self):
        self.seed_cache(age_seconds=0)
        out = self.run_script("https://api.z.ai/api/anthropic")
        self.assertTrue(
            out.startswith("GLM-4.7 plan | ctx 58% | 5h 1% reset at "),
            msg=f"unexpected output: {out!r}")
        self.assertIn("(in 2:05)", out)
        self.assertIn("7d 36% resets in 4d", out)
        self.assertNotIn("stale", out)
        self.assertNotIn("quota ?", out)

    def test_expired_cache_offline_serves_stale(self):
        # Host contains 'api.z.ai' (provider inference) but does not resolve,
        # so the live fetch fails and the stale cache is served.
        self.seed_cache(age_seconds=3600)
        out = self.run_script("https://api.z.ai.unreachable.test/api/anthropic")
        self.assertTrue(
            out.startswith("GLM-4.7 plan | ctx 58% | 5h 1%"),
            msg=f"unexpected output: {out!r}")
        self.assertIn("stale", out)
        self.assertIn("7d 36%", out)


if __name__ == "__main__":
    unittest.main()
