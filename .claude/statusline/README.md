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
| GLM Coding Plan (live quota, credit era) | `GLM-4.7 plan \| ctx 58% \| 5h 1% reset 3:29 \| 7d 36%` |
| GLM Coding Plan (stale cache) | `GLM-5.1 plan \| ctx 72% \| 5h 81% reset 0:12 stale` |
| GLM Coding Plan (no quota) | `GLM plan \| ctx 58% \| quota ?` |
| DeepSeek API | `DeepSeek \| ctx 44% \| $0.10` |
| Fireworks GLM-5.1 | `Fireworks GLM-5.1 \| ctx 51% \| $0.15` |
| MiniMax API | `MiniMax \| ctx 38% \| $0.04` |
| Kimi API | `Kimi \| ctx 49% \| $0.07` |
| Unknown API model | `Kimi \| ctx 49% \| cost ?` |
| Gemma local | `Gemma local \| ctx 32%` |

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
- **api** — shows accumulated cost (e.g. `$0.18`) when pricing is configured for the model, otherwise `cost ?`. No segment is shown until the first non-null `current_usage`.
- **local** — no rate limits or cost shown

## GLM Coding Plan quota

When launched with `ANTHROPIC_BASE_URL` pointing to `api.z.ai`, `open.bigmodel.cn`, or `dev.bigmodel.cn`, the status line fetches live quota from:

```
GET {baseDomain}/api/monitor/usage/quota/limit
Authorization: {ANTHROPIC_AUTH_TOKEN}
```

The API has had two payload eras; both are supported:

**Credit era** (since the 2026-08 credit-system migration) — windows arrive as `CREDIT_LIMIT` entries with a `unit`/`number`-encoded window and credit amounts (`usage` = window allowance, `currentValue` = spent, `percentage` = used share):

```json
{ "data": { "limits": [
    { "type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 12000,
      "currentValue": 208, "remaining": 11791, "percentage": 1,
      "nextResetTime": 1787242384006 },
    { "type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 60000,
      "currentValue": 22027, "remaining": 37972, "percentage": 36,
      "nextResetTime": 1787581390997 } ], "level": "pro" } }
```

**Token era** (legacy) — `TOKENS_LIMIT` entries (5h + weekly token quota) plus a `TIME_LIMIT` entry (monthly MCP/tool usage).

The `unit` enum on window entries is undocumented officially; it is decoded as `5=minute, 3=hour, 1=day, 6=week` (cross-checked against multiple community quota monitors), so `unit=3, number=5` → 5-hour window and `unit=6, number=1` → weekly window.

Fields displayed:

| Field | Source |
|---|---|
| `5h N% reset H:MM` | Window limit with duration ≤ 6h (5-hour quota/credits) |
| `7d N%` | Longest window limit (weekly quota/credits) |
| `MCP N%` | `TIME_LIMIT` percentage (monthly MCP/tool usage), when present |

Window classification prefers the decoded duration: the shortest window ≤ 6h is the 5-hour bucket, the longest is weekly, and a single window lands in whichever bucket its duration matches. When no duration decodes (unknown `unit`), entries fall back to `nextResetTime` ordering — soonest reset = 5-hour window. That fallback mislabels only while the weekly window is in its final hours, which is why duration decoding takes priority.

The raw auth token is never printed or persisted. The cache key is a short SHA-256 hash of the token.

### Cache

Quota responses are cached at:

```
~/.cache/claude-statusline/glm-quota-{token-hash}.json
```

- TTL: 60 seconds. Fresh cache is used as-is without a network call.
- On TTL expiry: attempts a live fetch. On success, cache is updated atomically.
- Only payloads that actually carry limit entries are cached. Z.ai signals failures (e.g. expired tokens) inside HTTP 200 bodies (`{"code": 1000, "msg": "Authentication Failed", "success": false}`); those are never written over previously good cache.
- On network failure (URLError / timeout / 5xx): serves stale cache with `stale` marker.
- On auth failure (HTTP 401/403, or in-body `code 1000`/auth message): shows `quota ?`; stale data is not used.
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
  {'type': 'CREDIT_LIMIT', 'unit': 3, 'number': 5, 'usage': 12000,
   'currentValue': 4440, 'percentage': 37, 'nextResetTime': (time.time()+11130)*1000},
  {'type': 'CREDIT_LIMIT', 'unit': 6, 'number': 1, 'usage': 60000,
   'currentValue': 15000, 'percentage': 25, 'nextResetTime': (time.time()+300000)*1000}
]}}))" > "$CACHE"

echo '{"model":{"display_name":"GLM-4.7"},"context_window":{"used_percentage":58}}' | \
  ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic \
  ANTHROPIC_MODEL=glm-4.7 \
  ANTHROPIC_AUTH_TOKEN="$FAKE_TOKEN" \
  python3 $SCRIPT
# Expected: GLM-4.7 plan | ctx 58% | 5h 37% reset 3:05 | 7d 25%
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

## Usage field structure (diagnostic findings)

Task 06-03a ran diagnostics on the `context_window` payload. Full findings are in
`.claude/statusline/fixtures/cost-diagnostics/diagnostic-summary.md`.

Key results for the cost engine (task 06-03b):

| Field | Observed type | Notes |
|---|---|---|
| `context_window.current_usage` | integer \| null | **Plain integer** — total cumulative context tokens. Null before first API call. No per-bucket breakdown observed. |
| `context_window.used_percentage` | number \| null | Context occupancy 0–100 |
| `context_window.context_window_size` | integer | Model's max context window |
| `cost.total_cost_usd` | null | Always null for custom API providers — not usable for cost calculation |

**No per-bucket token fields** (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`) were found inside `current_usage`. The nested-object form described in
the task spec was not observed.

**Consequence for cost engine:** use a `blended_per_million` rate applied to the integer delta
`current_usage_new − current_usage_prev`. Per-bucket pricing can be added later if a live
diagnostic run confirms the bucket fields exist. See the diagnostic summary for the full dedupe
strategy.

## Cost engine

When billing is `api`, the status line accumulates per-session cost from `context_window.current_usage` and pricing config.

### Pricing config

Repo default lives at `.claude/statusline/pricing.default.json`. User overrides at `~/.config/claude-statusline/pricing.json` are deep-merged on top (per-model entries fully replace the default).

Schema:

```json
{
  "version": 1,
  "currency": "USD",
  "providers": {
    "<provider>": {
      "models": {
        "<pricing_key>": {
          "input_per_million": 1.0,
          "output_per_million": 2.0,
          "cache_write_per_million": 1.25,
          "cache_read_per_million": 0.1,
          "blended_per_million": 1.0,
          "source": "...",
          "retrieved_at": "..."
        }
      }
    }
  }
}
```

- All four bucket fields are required to price a per-bucket usage object.
- `blended_per_million` is required to price an integer `current_usage` (the form observed in the diagnostic). It is also used as a fallback for partially-bucketed payloads.
- Unknown or incomplete pricing → `cost ?`.
- `cost.total_cost_usd` from Claude Code is **never** used as a fallback for custom API providers (always null in observed payloads).

### Usage bucket mapping

When `context_window.current_usage` is a dict, the engine maps:

| Source field | Bucket |
|---|---|
| `input_tokens` | `input` |
| `output_tokens` | `output` |
| `cache_creation_input_tokens` | `cache_write` |
| `cache_read_input_tokens` | `cache_read` |

When `current_usage` is a plain integer (the form observed in diagnostics), the engine treats deltas of that integer as `input` tokens priced at `blended_per_million`.

### Pricing key lookup

The pricing key is derived from the raw model identifier (preferring `CC_STATUS_MODEL`, then `ANTHROPIC_MODEL`, then `model.display_name`):

1. Strip any bracketed context suffix (e.g. `[500k]`).
2. Lowercase.

So `deepseek-v4-pro[500k]` looks up `providers.deepseek.models.deepseek-v4-pro`. The same model under a different provider (e.g. `glm-5.1` on Fireworks vs Z.ai) is intentionally looked up under the matching provider — there is no cross-provider aliasing.

### Session state

State is persisted at `~/.cache/claude-statusline/cost-{session_key}.json`, where `session_key` is a 16-char SHA-256 prefix of `session_id` (or `no-session` when absent). Writes are atomic (`tmp + os.replace`). Stored fields: provider, normalized model, pricing key, total cost, per-bucket totals, last-seen counters, and a capped (last 100) list of usage fingerprints. Prompts, completions, transcripts, paths, and raw tokens are never written.

### Dedupe strategy

Per the diagnostic conclusion (`fixtures/cost-diagnostics/diagnostic-summary.md`): `current_usage` is cumulative and identical between renders within the same API call. The engine:

1. Reads the previous total/buckets from session state.
2. Computes a positive delta (`new - prev`); zero or negative deltas are skipped.
3. Hashes a fingerprint (provider + pricing_key + session_id + canonical usage) to guard against accidental replay; fingerprints already seen are skipped.
4. Multiplies the delta by the appropriate rate(s) and adds to the running total.
5. Writes new state atomically.

### Cost format

- `>= $0.01` → two decimals, e.g. `$0.18`.
- `>= $0.001 and < $0.01` → three decimals, e.g. `$0.004`.
- `< $0.001` → four decimals.
- Unpriced API provider → `cost ?`.
- Subscription/local → no cost segment at all.

### Tests

All test suites are dependency-free and run without provider credentials. Each test uses a temporary `HOME` so cache/state files do not leak between cases.

```bash
python3 .claude/statusline/test_cost_engine.py -v          # generic engine
python3 .claude/statusline/test_deepseek_pricing.py -v     # DeepSeek integration
python3 .claude/statusline/test_additional_vendor_pricing.py -v  # Fireworks, MiniMax, Kimi
python3 .claude/statusline/test_hardening.py -v            # review checks (06-03e)
python3 .claude/statusline/test_glm_quota.py -v            # GLM quota parse/cache (credit + token eras)
```

`test_hardening.py` covers: dedupe across renders, distinct `session_id` isolation, unknown-provider behavior, state-file content safety (no tokens / prompts / transcripts / commands), suppression for subscription and local billing, no-network guarantee for the cost path, runtime budget, and absence of git information in the rendered line.

### Cost display contract

Summary of guarantees verified by `test_hardening.py`:

- `cost ?` means the provider/model is API-billed but pricing is **unknown or incomplete**. It is never shown for subscriptions or local models, and never shown when cumulative usage is unavailable (no segment is rendered in that case).
- `$0.00` is only displayed for **known** pricing where the priced delta evaluates to zero. Unknown pricing always renders `cost ?`, never `$0.00`.
- `cost.total_cost_usd` from Claude Code is **never** consulted for custom API providers. The diagnostic record may capture it for analysis, but the rendered status line and the cost engine ignore it.
- Subscription billing (`claude`, `zai`) and local billing suppress all cost segments unconditionally.
- Cost calculation is purely local: pricing config is loaded from disk, and accumulated state is read/written under `~/.cache/claude-statusline/`. No network calls are made on the cost path.
- State files (`cost-{session_key}.json`) contain only: schema version, session_id, provider, normalized model name, pricing key, total cost, per-bucket totals, last-seen counters, capped fingerprint list, and an ISO timestamp. No prompts, completions, transcript content, command text, or auth tokens are ever persisted.

### Per-provider pricing notes

#### Fireworks (`provider=fireworks`)

Source: https://fireworks.ai/pricing — retrieved 2026-05-03

| Model key | Input | Output | Cache read | Cache write |
|---|---|---|---|---|
| `glm-5.1` | $1.40/M | $4.40/M | $0.26/M | not published |
| `minimax-m2p5` | $0.30/M | $1.20/M | $0.03/M | not published |

Fireworks publishes cached-input (read) pricing but not cache-write pricing. The engine uses `blended_per_million` (= input rate) as the fallback for both integer `current_usage` payloads and bucket payloads missing `cache_write_per_million`. The general platform policy states "cached input tokens are priced at 50% for all text and vision language models, unless otherwise specified"; per-model exceptions (like GLM-5.1) are listed on the pricing page.

#### MiniMax (`provider=minimax`)

Source: https://platform.minimax.io/docs/guides/pricing-paygo — retrieved 2026-05-03

| Model key | Input | Output | Cache write | Cache read |
|---|---|---|---|---|
| `minimax-m2.7` | $0.30/M | $1.20/M | $0.375/M | $0.06/M |

MiniMax publishes all four bucket rates. The pricing key for `ANTHROPIC_MODEL=MiniMax-M2.7` is `minimax-m2.7` (lowercased).

Note: `minimax-m2p5` on Fireworks is a separately hosted model billed by Fireworks at their rates, not by MiniMax directly.

#### Kimi / Moonshot (`provider=kimi`)

Source: https://platform.kimi.ai/docs/pricing/chat-k25 and /chat-k2 — retrieved 2026-05-03

| Model key | Input / cache miss | Output | Cache write | Cache hit |
|---|---|---|---|---|
| `kimi-k2.5` | $0.60/M | $3.00/M | $0.60/M | $0.10/M |
| `kimi-k2-0905-preview` | $0.60/M | $2.50/M | $0.60/M | $0.15/M |

Kimi's pricing page defines two input tiers: cache-miss (token not in cache, billed at standard input rate) and cache-hit (token served from cache at discounted rate). There is no separate cache-write fee — writing tokens to the cache is billed at the cache-miss (input) rate. `cache_write_per_million` is set to the input rate per this official two-tier structure.

## Extension points

Later tasks should extend `statusline.py` without replacing it:

- **Task 06-02** ✓ — GLM quota polling implemented via `format_glm_subscription_quota()`
- **Task 06-03a** ✓ — diagnostic mode added (`CC_STATUS_DIAGNOSTIC=1`), findings in `fixtures/cost-diagnostics/`
- **Task 06-03b** ✓ — cost engine implemented in `compute_api_cost()` with mock pricing (`pricing.default.json`)
- **Task 06-03c** ✓ — DeepSeek pricing added (`deepseek-v4-pro`, `deepseek-v4-flash`) with `blended_per_million`; tests in `test_deepseek_pricing.py`
- **Task 06-03d** ✓ — Fireworks (`glm-5.1`, `minimax-m2p5`), MiniMax (`minimax-m2.7`), Kimi (`kimi-k2.5`, `kimi-k2-0905-preview`) pricing added; tests in `test_additional_vendor_pricing.py`
- **Task 06-03e** ✓ — review and hardening pass: dedupe / suppression / unknown-pricing / state-file-safety / no-network / no-git-output verified in `test_hardening.py`; cost display contract documented above

The `detect_environment()` / `render_status_line()` split keeps provider detection stable while allowing segment formatters to evolve independently.
