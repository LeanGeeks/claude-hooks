# Task 06-02: GLM Coding Plan Quota Integration

## Objective

Extend the provider-aware Claude Code status line with real GLM Coding Plan quota usage, fetched from Z.ai/Zhipu usage APIs and displayed only for GLM Coding Plan subscription setups.

This task depends on the foundation from `tasks/06-01-statusline-foundation.md`.

## Scope Boundary

This task should only add GLM Coding Plan quota display. Do not implement API cost pricing here. Do not replace the status line entrypoint created in task 06-01.

## Background

Claude Code status line scripts receive session JSON on stdin and print status text. The foundation task detects provider/billing mode and renders context usage. This task fills in the GLM Coding Plan quota segment.

Relevant docs and source:
- Claude Code status line docs: https://code.claude.com/docs/en/statusline
- Z.ai GLM Coding Plan overview: https://docs.z.ai/devpack/overview
- Z.ai GLM usage query plugin docs: https://docs.z.ai/devpack/extension/usage-query-plugin
- Z.ai plugin source: https://github.com/zai-org/zai-coding-plugins

The official plugin repository has also been cloned locally for implementation reference:

```text
/data/sync/work/leangeeks-ai/ai-playground/temp/zai-coding-plugins/
```

Use this local clone first when inspecting the plugin implementation. Treat it as a reference source only; the status line must not depend on the plugin being installed or on files under `temp/` at runtime.

Z.ai documents GLM Coding Plan usage limits on 5-hour and weekly cycles. Their official usage plugin queries quota and usage statistics from the current Claude Code environment.

The current Claude Code built-in `rate_limits` fields are for Claude.ai subscriber limits. Do not use those fields for GLM Coding Plan. GLM quota must come from Z.ai/Zhipu usage APIs or cached data from those APIs.

## User Preferences

- Display rate limit/quota usage for GLM Coding Plan.
- Do not display cost for GLM Coding Plan; it is subscription billing.
- Do not display git branch/status.
- Keep output compact.
- Avoid excessive API calls from the status line.

## Existing Launch Setup

Review provider launch functions in:

`/data/sync/Config/bash/claude.bashrc`

Do not copy secrets from this file.

Relevant GLM launchers:

| Launcher | Base URL | Billing Type | Typical Models |
|---|---|---|---|
| `claude-glm` | `https://api.z.ai/api/anthropic` | subscription | `glm-4.7`, `glm-5`, `glm-4.7-flashx` |
| `claude-glm5` | `https://api.z.ai/api/anthropic` | subscription | `glm-5.1`, `glm-4.7`, `glm-4.7-flashx` |

Provider detection should treat base URLs containing any of these as GLM Coding Plan subscription unless explicitly overridden:

- `api.z.ai`
- `open.bigmodel.cn`
- `dev.bigmodel.cn`

## API Endpoints

The official Z.ai plugin queries these endpoints, selected from `ANTHROPIC_BASE_URL`:

```text
/api/monitor/usage/model-usage
/api/monitor/usage/tool-usage
/api/monitor/usage/quota/limit
```

For status line purposes, the most important endpoint is currently:

```text
GET {baseDomain}/api/monitor/usage/quota/limit
Authorization: {ANTHROPIC_AUTH_TOKEN}
Accept-Language: en-US,en
Content-Type: application/json
```

Where `baseDomain` is derived from `ANTHROPIC_BASE_URL`:

- `https://api.z.ai/api/anthropic` -> `https://api.z.ai`
- `https://open.bigmodel.cn/api/anthropic` -> `https://open.bigmodel.cn`

The official plugin maps quota items like:

- `TOKENS_LIMIT` -> 5-hour token quota
- `TIME_LIMIT` -> monthly MCP/tool usage

The exact response shape and auth header format may change. The header above is a starting point, not a final contract. During implementation, inspect the local official plugin clone and mirror its request behavior. If the official source and this task disagree, prefer the official source and update local comments/docs accordingly.

Do not assume the response is a flat object. Implement parsing that can handle common shapes such as:

- `{ "data": [...] }`
- `{ "data": { "items": [...] } }`
- named quota entries containing `type`, `code`, `name`, `used`, `limit`, `usage`, `usedPercentage`, `used_percentage`, or similar fields

The display should prefer server-provided percentages when present. If only used/limit values are available, calculate percentage as `used / limit * 100`.

## Design Requirements

1. Fetch GLM quota only when provider is `zai` and billing is `subscription`.
2. Use `ANTHROPIC_AUTH_TOKEN` from the current Claude Code process environment.
3. Never print or log the auth token.
4. Cache quota responses to avoid calling the Z.ai API on every status line refresh.
5. Use stale cache data if the network/API call fails.
6. Render a compact quota segment.
7. Handle missing/changed response fields without crashing.
8. Keep API polling out of non-GLM providers.

## Target Integration

Extend the existing task 06-01 files:

```text
.claude/statusline/statusline.py
.claude/statusline/README.md
```

Keep the same Claude Code `statusLine.command`. Add helper functions/modules only if they are imported by the existing entrypoint.

Suggested helpers:

- `derive_zai_usage_base_url(anthropic_base_url) -> str`
- `fetch_glm_quota(base_url, token, timeout_seconds) -> dict`
- `read_glm_quota_cache(cache_key) -> dict | None`
- `write_glm_quota_cache(cache_key, payload) -> None`
- `parse_glm_quota(payload) -> QuotaSummary`
- `format_glm_quota_segment(summary) -> list[str]`

## Caching Requirements

Claude Code may run the status line script frequently. Network calls must be throttled.

Suggested cache:

```text
~/.cache/claude-statusline/glm-quota-{account-or-token-hash}.json
```

Requirements:

- Cache key must not expose the raw token. Use a short hash if needed.
- Suggested TTL: 60 seconds.
- If cache is younger than TTL, use it.
- If cache is expired, attempt refresh.
- If refresh fails and stale cache exists, render stale values with a small marker such as `stale`.
- If no cache and refresh fails, render `quota ?`.
- Writes should be atomic where practical: write to a temporary file then replace.
- Cache files must not contain the raw `ANTHROPIC_AUTH_TOKEN`.

## Suggested Output

Normal:

```text
GLM-4.7 plan | ctx 58% | 5h 37% | MCP 12%
```

With stale cache:

```text
GLM-5.1 plan | ctx 72% | 5h 81% stale
```

Failure with no cache:

```text
GLM plan | ctx 58% | quota ?
```

## Implementation Notes

- Reuse the foundation script from task 06-01 rather than creating a separate status line entrypoint.
- Inspect `/data/sync/work/leangeeks-ai/ai-playground/temp/zai-coding-plugins/` before implementing the HTTP request and quota parser.
- Keep network timeout short, for example 1-2 seconds.
- The status line should still render context usage if quota lookup fails.
- Do not add model usage/tool usage to the status line unless it is needed to parse quota. Keep the default display focused on 5-hour quota.
- MCP/tool monthly usage can be displayed if present and compact, but it should not crowd out 5-hour quota.
- Use Python standard library networking (`urllib.request`) unless the foundation already introduced a different dependency-free approach.
- Treat HTTP 401/403 as an authentication failure and render `quota ?`; do not retry aggressively.
- Treat HTTP 429/5xx/timeouts as temporary failures and use stale cache if available.

## Done Criteria

- [ ] GLM Coding Plan status line fetches real quota from Z.ai/Zhipu when launched with GLM subscription env vars.
- [ ] Quota polling uses `ANTHROPIC_AUTH_TOKEN` and derived base domain correctly.
- [ ] Token is never printed or persisted in raw form.
- [ ] Quota response is cached.
- [ ] Stale cache fallback works.
- [ ] Non-GLM providers do not call GLM quota APIs.
- [ ] Cost remains hidden for GLM Coding Plan.
- [ ] Git remains omitted.
- [ ] The main entrypoint from task 06-01 remains the only configured status line command.
- [ ] README/setup notes document GLM behavior and cache location.

## Validation

With real GLM env vars loaded, run the script manually with representative Claude Code stdin JSON.

Also validate without a token:

```bash
echo '{"model":{"display_name":"GLM"},"context_window":{"used_percentage":58},"session_id":"glm-no-token"}' | ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic ANTHROPIC_AUTH_TOKEN= ./path/to/statusline-script
```

Expected behavior: no crash, compact `quota ?` or equivalent.

Validate provider isolation:

```bash
echo '{"model":{"display_name":"DeepSeek"},"context_window":{"used_percentage":44},"session_id":"deepseek-test"}' | ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic ./path/to/statusline-script
```

Expected behavior: no GLM API call.

Add a fixture or test command using a fake cached GLM quota response so the formatting path can be validated without network access.
