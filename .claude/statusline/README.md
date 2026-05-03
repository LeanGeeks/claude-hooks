# Claude Code Status Line

Provider-aware one-line status showing model, context usage, rate limits, and cost.

## Quick setup

Add to your Claude Code settings (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /data/sync/work/leangeeks-ai/ai-playground/.claude/statusline/statusline.py",
    "refreshInterval": 30
  }
}
```

The script requires Python 3 and uses only the standard library.

## Sample output

| Scenario | Output |
|---|---|
| Claude Max (with rate limits) | `Opus \| ctx 61% \| 5h 43% reset 1:12 \| 7d 18%` |
| Claude (before first API call) | `Sonnet \| ctx ?` |
| GLM Coding Plan (live quota) | `GLM-4.7 plan \| ctx 58% \| 5h 5% reset 3:29 \| 7d 27% \| MCP 1%` |
| GLM Coding Plan (stale cache) | `GLM-5.1 plan \| ctx 72% \| 5h 81% reset 0:12 stale` |
| GLM Coding Plan (no quota) | `GLM plan \| ctx 58% \| quota ?` |
| DeepSeek API | `DeepSeek \| ctx 44% \| cost pending` |
| Gemma local | `Gemma local \| ctx 32%` |
| Fireworks GLM | `Fireworks GLM-5.1 \| ctx 51% \| cost pending` |

## Provider inference

Provider and billing mode are inferred automatically from `ANTHROPIC_BASE_URL`:

| Base URL | Provider | Billing |
|---|---|---|
| (none / default) | `claude` | `subscription` |
| `127.0.0.1`, `localhost` | `local` | `local` |
| `api.z.ai`, `open.bigmodel.cn` | `zai` | `subscription` |
| `api.deepseek.com` | `deepseek` | `api` |
| `fireworks.ai` | `fireworks` | `api` |
| `minimax.io` | `minimax` | `api` |
| `moonshot.ai` | `kimi` | `api` |

## Override env vars

Set these in your launcher function to override inference:

| Variable | Values | Purpose |
|---|---|---|
| `CC_STATUS_PROVIDER` | `claude`, `zai`, `local`, `deepseek`, `fireworks`, `minimax`, `kimi`, `unknown` | Force provider |
| `CC_STATUS_BILLING` | `subscription`, `api`, `local` | Force billing mode |
| `CC_STATUS_PROFILE` | free-form | Custom profile label |
| `CC_STATUS_MODEL` | free-form | Override model display name |

## Behavior by billing type

- **subscription/claude** — shows `rate_limits` from Claude Code (5-hour and 7-day windows)
- **subscription/zai** — fetches GLM Coding Plan quota from Z.ai monitor API; falls back to stale cache or `quota ?`
- **api** — shows `cost pending` (pricing added in task 06-03)
- **local** — no rate limits or cost shown

## GLM Coding Plan quota

When launched with `ANTHROPIC_BASE_URL` pointing to `api.z.ai`, `open.bigmodel.cn`, or `dev.bigmodel.cn`, the status line fetches live quota from:

```
GET {baseDomain}/api/monitor/usage/quota/limit
Authorization: {ANTHROPIC_AUTH_TOKEN}
```

Fields displayed:

| Field | Source |
|---|---|
| `5h N% reset H:MM` | `TOKENS_LIMIT` with the soonest `nextResetTime` (5-hour token quota) |
| `7d N%` | `TOKENS_LIMIT` with the later `nextResetTime` (weekly token quota) |
| `MCP N%` | `TIME_LIMIT` percentage (monthly MCP/tool usage) |

When two `TOKENS_LIMIT` entries are present, they are distinguished by `nextResetTime`: the one that resets sooner is the 5-hour window. This avoids depending on the undocumented `unit` enum field.

The raw auth token is never printed or persisted. The cache key is a short SHA-256 hash of the token.

### Cache

Quota responses are cached at:

```
~/.cache/claude-statusline/glm-quota-{token-hash}.json
```

- TTL: 60 seconds. Fresh cache is used as-is without a network call.
- On TTL expiry: attempts a live fetch. On success, cache is updated atomically.
- On network failure (URLError / timeout / 5xx): serves stale cache with `stale` marker.
- On auth failure (HTTP 401/403): shows `quota ?`; stale data is not used.
- No cache and no network: shows `quota ?`.

### Validation without network

Write a fake cache fixture and run:

```bash
SCRIPT=.claude/statusline/statusline.py
FAKE_TOKEN=test-token
HASH=$(python3 -c "import hashlib; print(hashlib.sha256(b'$FAKE_TOKEN').hexdigest()[:16])")
CACHE=~/.cache/claude-statusline/glm-quota-$HASH.json
mkdir -p ~/.cache/claude-statusline
python3 -c "
import json, time
print(json.dumps({'_cached_at': time.time(), 'data': {'limits': [
  {'type': 'TOKENS_LIMIT', 'percentage': 37},
  {'type': 'TIME_LIMIT', 'percentage': 12}
]}}))" > "$CACHE"

echo '{"model":{"display_name":"GLM-4.7"},"context_window":{"used_percentage":58}}' | \
  ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic \
  ANTHROPIC_MODEL=glm-4.7 \
  ANTHROPIC_AUTH_TOKEN="$FAKE_TOKEN" \
  python3 $SCRIPT
# Expected: GLM-4.7 plan | ctx 58% | 5h 37% | MCP 12%
```

## Debug mode

```bash
CC_STATUS_DEBUG=1 echo '{"context_window":{"used_percentage":42}}' | python3 statusline.py
```

## Manual testing

```bash
SCRIPT=.claude/statusline/statusline.py

# Claude Max with rate limits
cat .claude/statusline/fixtures/claude-max.json | python3 $SCRIPT

# GLM Coding Plan
cat .claude/statusline/fixtures/glm-plan.json | \
  ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic \
  ANTHROPIC_MODEL=glm-4.7 python3 $SCRIPT

# Local Gemma
cat .claude/statusline/fixtures/gemma-local.json | \
  ANTHROPIC_BASE_URL=http://127.0.0.1:18080 \
  ANTHROPIC_MODEL='gemma[128k]' python3 $SCRIPT

# DeepSeek API
cat .claude/statusline/fixtures/deepseek-api.json | \
  ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
  ANTHROPIC_MODEL='deepseek-v4-pro[500k]' python3 $SCRIPT
```

## Extension points

Later tasks should extend `statusline.py` without replacing it:

- **Task 06-02** ✓ — GLM quota polling implemented via `format_glm_subscription_quota()`
- **Task 06-03** — replace `format_api_cost_placeholder()` with per-provider cost calculation

The `detect_environment()` / `render_status_line()` split keeps provider detection stable while allowing segment formatters to evolve independently.
