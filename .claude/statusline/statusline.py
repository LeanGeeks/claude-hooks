#!/usr/bin/env python3
"""
Claude Code status line script.
Reads Claude Code session JSON from stdin, prints a compact one-line status.

Set CC_STATUS_DEBUG=1 to emit diagnostic output on stderr.
"""

import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class StatusEnvironment:
    provider: str  # claude | zai | local | deepseek | fireworks | minimax | kimi | unknown
    billing: str   # subscription | api | local
    profile: str   # free-form label, e.g. claude-max / glm-plan / deepseek-api
    model: str     # normalized display name


@dataclass
class QuotaSummary:
    five_hour_pct: Optional[float]
    five_hour_reset_at: Optional[float]   # epoch seconds
    seven_day_pct: Optional[float]
    seven_day_reset_at: Optional[float]   # epoch seconds
    mcp_pct: Optional[float]
    stale: bool = field(default=False)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def load_status_input(stdin_text: str) -> dict:
    try:
        return json.loads(stdin_text)
    except (json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Provider / billing detection
# ---------------------------------------------------------------------------

_KNOWN_PROVIDERS = {"claude", "zai", "local", "deepseek", "fireworks", "minimax", "kimi", "unknown"}
_KNOWN_BILLINGS = {"subscription", "api", "local"}

_PROFILE_DEFAULTS = {
    "claude":    "claude-max",
    "zai":       "glm-plan",
    "local":     "local",
    "deepseek":  "deepseek-api",
    "fireworks": "fireworks-api",
    "minimax":   "minimax-api",
    "kimi":      "kimi-api",
    "unknown":   "unknown",
}


def _infer_provider_billing(base_url: Optional[str]) -> tuple:
    """Return (provider, billing) inferred from ANTHROPIC_BASE_URL."""
    if not base_url:
        return "claude", "subscription"

    url = base_url.lower()

    if "127.0.0.1" in url or "localhost" in url:
        return "local", "local"
    # Private RFC-1918 ranges (LAN)
    if re.search(r'192\.168\.|^https?://10\.\d|172\.(1[6-9]|2\d|3[01])\.', url):
        return "local", "local"
    if "api.z.ai" in url or "open.bigmodel.cn" in url or "dev.bigmodel.cn" in url:
        return "zai", "subscription"
    if "api.deepseek.com" in url:
        return "deepseek", "api"
    if "fireworks.ai" in url:
        return "fireworks", "api"
    if "minimax.io" in url:
        return "minimax", "api"
    if "moonshot.ai" in url:
        return "kimi", "api"

    return "unknown", "api"


def _normalize_model_name(raw: str, provider: str) -> str:
    """Strip context-window suffixes and return a compact display name."""
    clean = re.sub(r'\[.*?\]', '', raw).strip()
    lower = clean.lower()

    # Claude family — match by tier keyword present in the name
    if "opus" in lower:
        return "Opus"
    if "sonnet" in lower:
        return "Sonnet"
    if "haiku" in lower:
        return "Haiku"

    # Local models — append " local" if not already present
    if provider == "local":
        base = re.sub(r'[^a-zA-Z0-9]', '', clean.split()[0]).capitalize() or "Local"
        if "local" in lower:
            return clean
        return f"{base} local"

    # GLM family
    if lower.startswith("glm"):
        suffix = clean[3:]  # e.g. "-4.7", "-5.1"
        if provider == "fireworks":
            return f"Fireworks GLM{suffix}"
        return f"GLM{suffix}"

    if lower.startswith("deepseek"):
        return "DeepSeek"
    if lower.startswith("minimax"):
        return "MiniMax"
    if lower.startswith("kimi"):
        return "Kimi"

    return clean or "Claude"


def detect_environment(env: dict, status_input: dict) -> StatusEnvironment:
    """Build StatusEnvironment from env vars and status JSON."""
    # Explicit overrides
    provider_ov = env.get("CC_STATUS_PROVIDER", "").strip()
    billing_ov  = env.get("CC_STATUS_BILLING",  "").strip()
    profile_ov  = env.get("CC_STATUS_PROFILE",  "").strip()
    model_ov    = env.get("CC_STATUS_MODEL",    "").strip()

    base_url = env.get("ANTHROPIC_BASE_URL", "").strip()
    inferred_provider, inferred_billing = _infer_provider_billing(base_url or None)

    provider = provider_ov if provider_ov in _KNOWN_PROVIDERS else inferred_provider
    billing  = billing_ov  if billing_ov  in _KNOWN_BILLINGS  else inferred_billing

    profile  = profile_ov if profile_ov else _PROFILE_DEFAULTS.get(provider, "unknown")

    # Model name: explicit override → ANTHROPIC_MODEL → JSON display_name
    raw_model = (
        model_ov
        or env.get("ANTHROPIC_MODEL", "")
        or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
    )
    if raw_model:
        model = _normalize_model_name(raw_model, provider)
    else:
        json_name = (status_input.get("model") or {}).get("display_name", "")
        model = _normalize_model_name(json_name, provider) if json_name else "Claude"

    return StatusEnvironment(provider=provider, billing=billing, profile=profile, model=model)


# ---------------------------------------------------------------------------
# Segment formatters
# ---------------------------------------------------------------------------

def format_context_segment(status_input: dict) -> str:
    ctx = status_input.get("context_window") or {}
    pct = ctx.get("used_percentage")
    return f"ctx {int(pct)}%" if pct is not None else "ctx ?"


def _reset_countdown(epoch: Optional[float]) -> str:
    if not epoch:
        return ""
    remaining = epoch - time.time()
    if remaining <= 0:
        return "now"
    h = int(remaining // 3600)
    m = int((remaining % 3600) // 60)
    return f"{h}:{m:02d}"


def format_claude_rate_limits(status_input: dict) -> list:
    """Rate-limit segments from built-in Claude Code fields (subscription only)."""
    rl = status_input.get("rate_limits") or {}
    if not rl:
        return []

    segments = []

    five_h = rl.get("five_hour") or {}
    pct_5h = five_h.get("used_percentage")
    if pct_5h is not None:
        seg = f"5h {int(pct_5h)}%"
        cd = _reset_countdown(five_h.get("resets_at"))
        if cd:
            seg += f" reset {cd}"
        segments.append(seg)

    seven_d = rl.get("seven_day") or {}
    pct_7d = seven_d.get("used_percentage")
    if pct_7d is not None:
        segments.append(f"7d {int(pct_7d)}%")

    return segments


def format_api_cost_placeholder(env: StatusEnvironment) -> list:
    """Placeholder until task 06-03 adds per-provider pricing."""
    return ["cost pending"]


# ---------------------------------------------------------------------------
# GLM Coding Plan quota (Z.ai / Zhipu subscription)
# ---------------------------------------------------------------------------

_GLM_QUOTA_TTL = 60  # seconds


def derive_zai_usage_base_url(anthropic_base_url: str) -> str:
    """Extract scheme+host from ANTHROPIC_BASE_URL for Z.ai monitor API calls."""
    # urllib.parse available but keep dependencies minimal; simple split is reliable here
    # e.g. 'https://api.z.ai/api/anthropic' -> 'https://api.z.ai'
    for prefix in ("https://", "http://"):
        if anthropic_base_url.startswith(prefix):
            host_and_path = anthropic_base_url[len(prefix):]
            host = host_and_path.split("/")[0]
            return f"{prefix}{host}"
    return anthropic_base_url


def _glm_cache_path(token: str) -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
    cache_dir = os.path.expanduser("~/.cache/claude-statusline")
    return os.path.join(cache_dir, f"glm-quota-{token_hash}.json")


def read_glm_quota_cache(cache_path: str) -> Optional[dict]:
    """Return cached payload if it exists and is within TTL, else None."""
    try:
        with open(cache_path) as f:
            data = json.load(f)
        age = time.time() - data.get("_cached_at", 0)
        return data if age <= _GLM_QUOTA_TTL else None
    except Exception:
        return None


def _read_glm_quota_cache_any_age(cache_path: str) -> Optional[dict]:
    """Return cached payload regardless of age, for stale fallback."""
    try:
        with open(cache_path) as f:
            return json.load(f)
    except Exception:
        return None


def write_glm_quota_cache(cache_path: str, payload: dict) -> None:
    """Atomically write quota payload to cache (never stores raw token)."""
    cache_dir = os.path.dirname(cache_path)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        data = dict(payload)
        data["_cached_at"] = time.time()
        fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, cache_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def fetch_glm_quota(base_url: str, token: str, timeout_seconds: float = 2.0) -> dict:
    """
    Fetch quota/limit from Z.ai monitor API.
    Auth header mirrors the official plugin: raw token, no 'Bearer' prefix.
    Raises urllib.error.HTTPError / urllib.error.URLError / socket.timeout on failure.
    """
    url = f"{base_url}/api/monitor/usage/quota/limit"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": token,
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        return json.loads(resp.read())


def _item_pct(item: dict) -> Optional[float]:
    """Extract usage percentage from a quota item, with used/limit fallback."""
    pct = (
        item.get("percentage")
        or item.get("usedPercentage")
        or item.get("used_percentage")
    )
    if pct is None:
        used = item.get("used") or item.get("currentValue")
        limit = item.get("limit") or item.get("usage")
        if used is not None and limit:
            try:
                pct = float(used) / float(limit) * 100
            except (TypeError, ZeroDivisionError):
                return None
    if pct is not None:
        try:
            return float(pct)
        except (TypeError, ValueError):
            pass
    return None


def _item_reset_epoch(item: dict) -> Optional[float]:
    """Return nextResetTime converted from milliseconds to epoch seconds."""
    ms = item.get("nextResetTime")
    if ms is not None:
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            pass
    return None


def parse_glm_quota(payload: dict) -> QuotaSummary:
    """
    Parse raw quota API response into QuotaSummary.

    Actual response shape (observed from Z.ai web API):
      { "data": { "limits": [
          { "type": "TOKENS_LIMIT", "percentage": 5,  "nextResetTime": <ms>, ... },  # 5h window
          { "type": "TOKENS_LIMIT", "percentage": 27, "nextResetTime": <ms>, ... },  # 7d window
          { "type": "TIME_LIMIT",   "percentage": 1,  "nextResetTime": <ms>, ... }   # monthly MCP
      ] } }

    Multiple TOKENS_LIMIT entries are distinguished by nextResetTime:
    the one that resets sooner is the 5-hour window.
    """
    data = payload.get("data") or {}
    if isinstance(data, dict):
        limits = data.get("limits") or []
    elif isinstance(data, list):
        limits = data
    else:
        limits = []

    token_items = []
    mcp_pct: Optional[float] = None

    for item in limits:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "TOKENS_LIMIT":
            token_items.append(item)
        elif item_type == "TIME_LIMIT":
            mcp_pct = _item_pct(item)

    # Sort TOKENS_LIMIT by nextResetTime ascending: soonest reset = 5h window
    token_items.sort(key=lambda x: x.get("nextResetTime") or float("inf"))

    five_h_pct = _item_pct(token_items[0]) if len(token_items) >= 1 else None
    five_h_reset = _item_reset_epoch(token_items[0]) if len(token_items) >= 1 else None
    seven_d_pct = _item_pct(token_items[1]) if len(token_items) >= 2 else None
    seven_d_reset = _item_reset_epoch(token_items[1]) if len(token_items) >= 2 else None

    return QuotaSummary(
        five_hour_pct=five_h_pct,
        five_hour_reset_at=five_h_reset,
        seven_day_pct=seven_d_pct,
        seven_day_reset_at=seven_d_reset,
        mcp_pct=mcp_pct,
    )


def format_glm_quota_segment(summary: QuotaSummary) -> list:
    segments = []

    if summary.five_hour_pct is not None:
        seg = f"5h {int(summary.five_hour_pct)}%"
        cd = _reset_countdown(summary.five_hour_reset_at)
        if cd:
            seg += f" reset {cd}"
        if summary.stale:
            seg += " stale"
        segments.append(seg)

    if summary.seven_day_pct is not None:
        seg = f"7d {int(summary.seven_day_pct)}%"
        segments.append(seg)

    if summary.mcp_pct is not None:
        segments.append(f"MCP {int(summary.mcp_pct)}%")

    return segments if segments else ["quota ?"]


def format_glm_subscription_quota(env: StatusEnvironment) -> list:
    """
    Fetch and format GLM Coding Plan quota from Z.ai monitor API.
    Uses cached data within TTL; falls back to stale cache on network failure.
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()

    if not base_url or not token:
        return ["quota ?"]

    usage_base = derive_zai_usage_base_url(base_url)
    cache_path = _glm_cache_path(token)

    cached = read_glm_quota_cache(cache_path)
    if cached is not None:
        return format_glm_quota_segment(parse_glm_quota(cached))

    try:
        raw = fetch_glm_quota(usage_base, token, timeout_seconds=2.0)
        write_glm_quota_cache(cache_path, raw)
        return format_glm_quota_segment(parse_glm_quota(raw))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Auth failure — stale data is equally untrustworthy
            return ["quota ?"]
        # 429 / 5xx — fall through to stale cache
    except (urllib.error.URLError, socket.timeout, OSError, json.JSONDecodeError):
        pass
    except Exception:
        pass

    stale = _read_glm_quota_cache_any_age(cache_path)
    if stale is not None:
        summary = parse_glm_quota(stale)
        summary.stale = True
        return format_glm_quota_segment(summary)

    return ["quota ?"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_status_line(status_input: dict, env: StatusEnvironment) -> str:
    # First segment: model + billing hint for plan/subscription providers
    model_label = env.model
    if env.provider == "zai" and env.billing == "subscription":
        model_label = f"{model_label} plan"

    parts = [model_label, format_context_segment(status_input)]

    if env.billing == "subscription":
        if env.provider == "claude":
            parts.extend(format_claude_rate_limits(status_input))
        elif env.provider == "zai":
            parts.extend(format_glm_subscription_quota(env))
    elif env.billing == "api":
        parts.extend(format_api_cost_placeholder(env))
    # local: no extras

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    debug = os.environ.get("CC_STATUS_DEBUG", "").strip() == "1"

    try:
        raw = sys.stdin.read()
        if debug:
            print(f"[CC_STATUS_DEBUG] stdin: {raw[:300]}", file=sys.stderr)

        status_input = load_status_input(raw)
        env = detect_environment(dict(os.environ), status_input)

        if debug:
            print(f"[CC_STATUS_DEBUG] env: {env}", file=sys.stderr)

        print(render_status_line(status_input, env))
    except Exception as exc:
        if os.environ.get("CC_STATUS_DEBUG", "") == "1":
            print(f"[CC_STATUS_DEBUG] error: {exc}", file=sys.stderr)
        print("Claude | ctx ?")
    sys.exit(0)


if __name__ == "__main__":
    main()
