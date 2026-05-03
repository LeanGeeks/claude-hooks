#!/usr/bin/env python3
"""
Claude Code status line script.
Reads Claude Code session JSON from stdin, prints a compact one-line status.

Set CC_STATUS_DEBUG=1 to emit diagnostic output on stderr.
"""

import datetime
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
    pricing_key: str = ""  # raw model identifier used for pricing lookup (e.g. "deepseek-v4-pro")


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

_KNOWN_PROVIDERS = {"claude", "zai", "local", "deepseek", "fireworks", "minimax", "kimi", "mock", "unknown"}
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
        pricing_key = _pricing_key(raw_model)
    else:
        json_name = (status_input.get("model") or {}).get("display_name", "")
        model = _normalize_model_name(json_name, provider) if json_name else "Claude"
        pricing_key = _pricing_key(json_name) if json_name else ""

    return StatusEnvironment(
        provider=provider, billing=billing, profile=profile, model=model, pricing_key=pricing_key
    )


def _pricing_key(raw_model: str) -> str:
    """Strip context suffix (e.g. '[500k]') and lowercase, for pricing config lookup."""
    cleaned = re.sub(r'\[.*?\]', '', raw_model).strip().lower()
    return cleaned


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


_BUCKET_KEYS = ("input", "output", "cache_write", "cache_read")
_BUCKET_FIELD_MAP = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_write",
    "cache_read_input_tokens": "cache_read",
}
_PRICE_FIELD_MAP = {
    "input": "input_per_million",
    "output": "output_per_million",
    "cache_write": "cache_write_per_million",
    "cache_read": "cache_read_per_million",
}


def _safe_session_key(session_id: Optional[str]) -> str:
    if not session_id:
        return "no-session"
    return hashlib.sha256(str(session_id).encode()).hexdigest()[:16]


def _atomic_write_json(path: str, data: dict) -> None:
    cache_dir = os.path.dirname(path)
    os.makedirs(cache_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_pricing_config() -> dict:
    """Load repo default pricing, then merge user override on top."""
    here = os.path.dirname(os.path.abspath(__file__))
    default_path = os.path.join(here, "pricing.default.json")
    user_path = os.path.expanduser("~/.config/claude-statusline/pricing.json")

    config: dict = {"version": 1, "currency": "USD", "providers": {}}
    for path in (default_path, user_path):
        try:
            with open(path) as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        _merge_pricing(config, loaded)
    return config


def _merge_pricing(base: dict, overlay: dict) -> None:
    """Deep-merge overlay providers/models into base; overlay wins per-model."""
    for key in ("version", "currency"):
        if key in overlay:
            base[key] = overlay[key]
    overlay_providers = overlay.get("providers") or {}
    base_providers = base.setdefault("providers", {})
    for provider, pdata in overlay_providers.items():
        if not isinstance(pdata, dict):
            continue
        bp = base_providers.setdefault(provider, {"models": {}})
        ov_models = pdata.get("models") or {}
        bp_models = bp.setdefault("models", {})
        for model_key, model_price in ov_models.items():
            if isinstance(model_price, dict):
                bp_models[model_key] = dict(model_price)


def lookup_model_pricing(config: dict, provider: str, pricing_key: str) -> Optional[dict]:
    """Return the pricing dict for (provider, pricing_key) or None."""
    if not provider or not pricing_key:
        return None
    providers = config.get("providers") or {}
    pdata = providers.get(provider) or {}
    models = pdata.get("models") or {}
    return models.get(pricing_key)


def extract_usage(status_input: dict):
    """
    Return (kind, value) where kind is one of:
      - "none": no usage available
      - "int": value is an int total token count
      - "buckets": value is a dict with bucket keys (input/output/cache_write/cache_read)
    """
    ctx = status_input.get("context_window") or {}
    raw = ctx.get("current_usage")
    if raw is None:
        return "none", None
    if isinstance(raw, int):
        return "int", raw
    if isinstance(raw, dict):
        buckets = {k: 0 for k in _BUCKET_KEYS}
        any_field = False
        for src, dest in _BUCKET_FIELD_MAP.items():
            v = raw.get(src)
            if isinstance(v, int):
                buckets[dest] = v
                any_field = True
        if any_field:
            return "buckets", buckets
        return "none", None
    return "none", None


def _cost_state_path(session_key: str) -> str:
    cache_dir = os.path.expanduser("~/.cache/claude-statusline")
    return os.path.join(cache_dir, f"cost-{session_key}.json")


def _read_cost_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _format_cost(amount: float) -> str:
    if amount <= 0:
        return "$0.00"
    if amount >= 0.01:
        return f"${amount:.2f}"
    # compact precision below 1 cent
    return f"${amount:.3f}" if amount >= 0.001 else f"${amount:.4f}"


def _has_all_bucket_prices(price: dict) -> bool:
    return all(isinstance(price.get(_PRICE_FIELD_MAP[b]), (int, float)) for b in _BUCKET_KEYS)


def compute_api_cost(status_input: dict, env: StatusEnvironment) -> list:
    """
    Run the cost engine for an API-billed render.
    Returns a list of segments (typically one element).
    """
    kind, value = extract_usage(status_input)
    if kind == "none":
        # No usage yet — emit nothing rather than a placeholder.
        return []

    pricing = load_pricing_config()
    price = lookup_model_pricing(pricing, env.provider, env.pricing_key)
    if not isinstance(price, dict):
        return ["cost ?"]

    blended_rate = price.get("blended_per_million")
    has_blended = isinstance(blended_rate, (int, float))
    has_buckets = _has_all_bucket_prices(price)

    # Determine whether we can price this render.
    if kind == "int" and not has_blended:
        return ["cost ?"]
    if kind == "buckets" and not has_buckets:
        # Fall back to blended on the sum, if available.
        if not has_blended:
            return ["cost ?"]

    session_id = status_input.get("session_id")
    session_key = _safe_session_key(session_id)
    state_path = _cost_state_path(session_key)
    state = _read_cost_state(state_path)

    total_cost = float(state.get("total_cost_usd") or 0.0)
    total_tokens = state.get("total_tokens") or {k: 0 for k in _BUCKET_KEYS}
    last_total = int(state.get("last_usage_total") or 0)
    last_buckets = state.get("last_buckets") or {k: 0 for k in _BUCKET_KEYS}
    fingerprints = list(state.get("seen_usage_fingerprints") or [])

    delta_cost = 0.0
    new_last_total = last_total
    new_last_buckets = dict(last_buckets)
    new_total_tokens = dict(total_tokens)

    if kind == "int":
        current_total = int(value)
        fp = hashlib.sha256(f"{env.provider}|{env.pricing_key}|{session_id}|int:{current_total}"
                            .encode()).hexdigest()[:16]
        if current_total > last_total and fp not in fingerprints:
            delta = current_total - last_total
            delta_cost = (delta / 1_000_000.0) * float(blended_rate)
            new_last_total = current_total
            new_total_tokens["input"] = int(new_total_tokens.get("input", 0)) + delta
            fingerprints.append(fp)
        else:
            # Repeated render or non-monotonic value: skip accumulation.
            pass
    else:  # buckets
        deltas = {}
        any_increase = False
        for b in _BUCKET_KEYS:
            cur = int(value.get(b, 0))
            prev = int(last_buckets.get(b, 0))
            d = cur - prev if cur >= prev else 0
            if d > 0:
                any_increase = True
            deltas[b] = d
        canon = json.dumps({k: int(value.get(k, 0)) for k in _BUCKET_KEYS}, sort_keys=True)
        fp = hashlib.sha256(f"{env.provider}|{env.pricing_key}|{session_id}|buckets:{canon}"
                            .encode()).hexdigest()[:16]
        if any_increase and fp not in fingerprints:
            if has_buckets:
                for b in _BUCKET_KEYS:
                    rate = float(price[_PRICE_FIELD_MAP[b]])
                    delta_cost += (deltas[b] / 1_000_000.0) * rate
            else:
                # blended fallback on summed delta
                summed = sum(deltas.values())
                delta_cost = (summed / 1_000_000.0) * float(blended_rate)
            for b in _BUCKET_KEYS:
                new_last_buckets[b] = int(value.get(b, 0))
                new_total_tokens[b] = int(new_total_tokens.get(b, 0)) + deltas[b]
            fingerprints.append(fp)

    total_cost += delta_cost

    # Persist updated state. Cap fingerprint history.
    fingerprints = fingerprints[-100:]
    new_state = {
        "version": 1,
        "session_id": session_id,
        "provider": env.provider,
        "model": env.model,
        "pricing_key": env.pricing_key,
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": new_total_tokens,
        "last_usage_total": new_last_total,
        "last_buckets": new_last_buckets,
        "seen_usage_fingerprints": fingerprints,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        _atomic_write_json(state_path, new_state)
    except Exception:
        pass

    return [_format_cost(total_cost)]


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
        parts.extend(compute_api_cost(status_input, env))
    # local: no extras

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Diagnostics  (CC_STATUS_DIAGNOSTIC=1)
# ---------------------------------------------------------------------------

def _build_diagnostic_record(status_input: dict, env: StatusEnvironment) -> dict:
    """
    Build a sanitized snapshot safe for appending to the diagnostic JSONL.
    Excluded: workspace paths, cwd, prompts, completions, command text, tokens.
    """
    ctx = status_input.get("context_window") or {}
    raw_cost = status_input.get("cost")
    cost_usd = None
    if isinstance(raw_cost, dict):
        cost_usd = raw_cost.get("total_cost_usd")

    session_id = status_input.get("session_id")
    if session_id:
        session_key = hashlib.sha256(str(session_id).encode()).hexdigest()[:12]
    else:
        session_key = "no-session"

    # current_usage may be null, int, or dict depending on Claude Code version
    current_usage = ctx.get("current_usage")

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_key": session_key,
        "provider": env.provider,
        "billing": env.billing,
        "model": env.model,
        "context_window": {
            "used_percentage": ctx.get("used_percentage"),
            "context_window_size": ctx.get("context_window_size"),
            "current_usage": current_usage,
        },
        "cost": {
            "total_cost_usd": cost_usd,
        },
    }


def maybe_write_diagnostic(status_input: dict, env: StatusEnvironment) -> None:
    """Append one diagnostic record if CC_STATUS_DIAGNOSTIC=1. Never raises."""
    if os.environ.get("CC_STATUS_DIAGNOSTIC", "") != "1":
        return
    try:
        record = _build_diagnostic_record(status_input, env)
        diag_dir = os.path.expanduser("~/.cache/claude-statusline/diagnostics")
        os.makedirs(diag_dir, exist_ok=True)
        diag_file = os.path.join(diag_dir, f"{record['session_key']}.jsonl")
        with open(diag_file, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


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

        maybe_write_diagnostic(status_input, env)
        print(render_status_line(status_input, env))
    except Exception as exc:
        if os.environ.get("CC_STATUS_DEBUG", "") == "1":
            print(f"[CC_STATUS_DEBUG] error: {exc}", file=sys.stderr)
        print("Claude | ctx ?")
    sys.exit(0)


if __name__ == "__main__":
    main()
