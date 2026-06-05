# Task 06-01: Claude Code Status Line Foundation

## Objective

Create the foundation for a provider-aware Claude Code status line that restores useful context visibility and adapts output based on how Claude Code was launched.

This task should implement the core script, configuration shape, provider detection, and compact rendering. Later tasks will add GLM Coding Plan quota polling and API cost calculation.

## Scope Boundary

This task is intentionally the foundation only. Do not implement GLM network quota calls or vendor pricing in this phase. Instead, create stable extension points so later agents can add those features without replacing the entrypoint.

## Background

Claude Code removed built-in context usage information and replaced it with a customizable status line. The status line is configured through `statusLine` in Claude Code settings and runs a local command/script. Claude Code pipes session JSON into the script through stdin, and displays whatever the script prints.

Relevant Claude Code docs:
- https://code.claude.com/docs/en/statusline

Important status line input fields from the current Claude Code docs:
- `model.display_name`
- `workspace.current_dir`
- `workspace.project_dir`
- `context_window.used_percentage`
- `context_window.remaining_percentage`
- `context_window.current_usage`
- `context_window.context_window_size`
- `rate_limits.five_hour.used_percentage`
- `rate_limits.five_hour.resets_at`
- `rate_limits.seven_day.used_percentage`
- `rate_limits.seven_day.resets_at`
- `cost.total_cost_usd`
- `cost.total_duration_ms`
- `effort.level`
- `thinking.enabled`
- `session_id`
- `version`

Notes from the docs that matter for implementation:

- `rate_limits` appears only for Claude.ai subscribers, and only after the first API response in a session.
- Each rate limit window may be independently absent.
- `context_window.current_usage` is `null` before the first API call.
- `context_window.used_percentage` and `remaining_percentage` may be `null` early in the session.
- `refreshInterval` is optional. If configured, Claude Code re-runs the command on that interval in addition to event-driven updates.
- The status line command runs locally and should print to stdout only.

## User Preferences

- Do not display git branch or git status.
- Always display context usage when available.
- Display subscription rate limits only for:
  - Claude Code Max subscription
  - GLM Coding Plan subscription
- Do not display subscription-style rate limits for:
  - local Gemma
  - DeepSeek API
  - GLM via Fireworks
  - MiniMax API
  - Kimi API
- Display cost only for metered API setups, not for subscriptions or local models.
- Keep the status line compact and operational rather than decorative.

## Existing Launch Setup

Review provider launch functions in:

`/data/sync/Config/bash/claude.bashrc`

Do not copy secrets from this file into code or task notes.

Current provider mapping, based on that file:

| Launcher | Base URL | Billing Type | Notes |
|---|---|---|---|
| `claude` | default Anthropic/Claude Code | subscription | Claude Code Max; use built-in Claude Code rate limit fields |
| `claude-gemma` | `http://127.0.0.1:18080` | local | no rate limit, no cost |
| `claude-ds` | `https://api.deepseek.com/anthropic` | api | show calculated API cost later |
| `claude-glm` | `https://api.z.ai/api/anthropic` | subscription | GLM Coding Plan; quota polling added in task 06-02 |
| `claude-glm5` | `https://api.z.ai/api/anthropic` | subscription | GLM Coding Plan; quota polling added in task 06-02 |
| `claude-glm5-fw` | `https://api.fireworks.ai/inference` | api | GLM through Fireworks; show calculated API cost later |
| `claude-minimax` | `https://api.minimax.io/anthropic` | api | show calculated API cost later |
| `claude-kimi` | `https://api.moonshot.ai/anthropic` | api | show calculated API cost later |

## Design Requirements

Implement a status line script that:

1. Reads Claude Code status line JSON from stdin.
2. Detects provider and billing mode from environment variables.
3. Allows explicit override env vars, because inference can be wrong:
   - `CC_STATUS_PROVIDER`
   - `CC_STATUS_BILLING`
   - `CC_STATUS_PROFILE`
   - `CC_STATUS_MODEL`
4. Falls back to inference from:
   - `ANTHROPIC_BASE_URL`
   - `ANTHROPIC_MODEL`
   - `ANTHROPIC_DEFAULT_OPUS_MODEL`
   - `ANTHROPIC_DEFAULT_SONNET_MODEL`
5. Renders a short one-line status.
6. Does not perform network calls in this task.
7. Does not display git information.
8. Handles null/missing JSON fields gracefully.

Expected env var values:

| Env var | Expected values |
|---|---|
| `CC_STATUS_PROVIDER` | `claude`, `zai`, `local`, `deepseek`, `fireworks`, `minimax`, `kimi`, `unknown` |
| `CC_STATUS_BILLING` | `subscription`, `api`, `local` |
| `CC_STATUS_PROFILE` | free-form label such as `claude-max`, `glm-plan`, `deepseek-api` |
| `CC_STATUS_MODEL` | explicit model override, free-form |

Unknown env values should not crash the script. Treat unknown provider as `unknown`; treat unknown billing as inferred billing if possible, otherwise `api`.

## Target File Layout

Use this shared file layout unless the repository already contains a better equivalent by the time this task is started:

```text
.claude/statusline/
├── statusline.py          # single executable entrypoint used by Claude Code
├── README.md              # setup notes and example statusLine config
└── fixtures/              # optional mock stdin payloads for manual testing
```

Preferred entrypoint:

```text
/data/sync/work/leangeeks-ai/ai-playground/.claude/statusline/statusline.py
```

Use Python 3 and only the standard library for the foundation. This repository already uses Python for Claude hooks, and avoiding external runtime dependencies makes the status line more reliable.

Later tasks should extend this entrypoint instead of creating separate scripts. If helper modules are introduced, keep the executable wrapper stable.

## Internal Contracts

Structure the script so later tasks can add features without rewriting provider detection or rendering. Suggested internal functions:

- `load_status_input(stdin_text) -> dict`
- `detect_environment(env, status_input) -> StatusEnvironment`
- `format_context_segment(status_input) -> str`
- `format_claude_rate_limits(status_input) -> list[str]`
- `format_subscription_quota_placeholder(env) -> list[str]`
- `format_api_cost_placeholder(env) -> list[str]`
- `render_status_line(status_input, env) -> str`

The exact names can differ, but preserve the separation between input parsing, provider detection, feature segments, and final rendering.

## Suggested Provider Detection

Explicit env vars should win over inference.

Suggested inferred provider/billing:

| Condition | Provider | Billing |
|---|---|---|
| no custom `ANTHROPIC_BASE_URL` or default Claude Code environment | `claude` | `subscription` |
| base URL contains `api.z.ai`, `open.bigmodel.cn`, or `dev.bigmodel.cn` | `zai` | `subscription` |
| base URL contains `127.0.0.1`, `localhost`, or local LAN host | `local` | `local` |
| base URL contains `api.deepseek.com` | `deepseek` | `api` |
| base URL contains `fireworks.ai` | `fireworks` | `api` |
| base URL contains `minimax.io` | `minimax` | `api` |
| base URL contains `moonshot.ai` | `kimi` | `api` |
| unknown custom base URL | `unknown` | `api` unless explicitly overridden |

Also normalize model names for display:

- Prefer `CC_STATUS_MODEL` if set.
- Then prefer `ANTHROPIC_MODEL`.
- Then prefer `model.display_name` from status JSON.
- Strip bracketed context suffixes such as `[500k]` for compact display unless the suffix is useful for local context visibility.
- Use display names like `Claude`, `GLM-4.7`, `GLM-5.1`, `DeepSeek`, `Fireworks GLM-5.1`, `MiniMax`, `Kimi`, `Gemma local`.

## Suggested Output

Claude Max:

```text
Opus | ctx 61% | 5h 43% reset 1:12 | 7d 18%
```

If Claude has not supplied rate limits yet:

```text
Opus | ctx 61%
```

GLM Coding Plan before task 06-02:

```text
GLM-4.7 plan | ctx 58% | quota pending
```

Metered API before task 06-03:

```text
DeepSeek | ctx 44% | cost pending
```

Local:

```text
Gemma local | ctx 32%
```

## Implementation Notes

- Prefer a direct executable Python script with a shebang such as `#!/usr/bin/env python3`.
- Use only Python standard library modules.
- Keep slow work out of the render path. Later tasks will add caching for network calls.
- If adding files under `.claude/`, keep them executable where needed.
- Do not update global `~/.claude/settings.json` in this task unless explicitly requested during implementation. Document the config snippet instead.
- If updating project `.claude/settings.json`, preserve unrelated settings and existing hooks.
- On invalid JSON, print a minimal fallback such as `Claude | ctx ?` and exit 0.
- Do not print stack traces in normal status line execution. If debug logging is added, gate it behind an env var such as `CC_STATUS_DEBUG=1`.

## Done Criteria

- [ ] A status line script exists and can be invoked with JSON on stdin.
- [ ] The script displays model/provider and context percentage.
- [ ] Claude subscription rate limits are displayed from built-in `rate_limits` fields when provider is Claude subscription and those fields are present.
- [ ] GLM subscription displays a placeholder segment for quota until task 06-02 implements polling.
- [ ] API providers display a placeholder segment for cost until task 06-03 implements pricing.
- [ ] Local providers hide rate limits and cost.
- [ ] Git branch/status is not displayed.
- [ ] Missing fields do not crash the script.
- [ ] Include a short README or inline usage notes showing how to configure `statusLine`.
- [ ] The executable entrypoint is stable for tasks 06-02 through 06-04 to extend.
- [ ] The implementation uses no non-standard Python dependencies.

## Validation

Test with mock stdin:

```bash
echo '{"model":{"display_name":"Sonnet"},"workspace":{"current_dir":"/tmp/project"},"context_window":{"used_percentage":42},"rate_limits":{"five_hour":{"used_percentage":33,"resets_at":1770000000},"seven_day":{"used_percentage":12,"resets_at":1770500000}},"session_id":"test"}' | ./path/to/statusline-script
```

Also test with representative env vars:

```bash
echo '{"model":{"display_name":"Gemma"},"context_window":{"used_percentage":32},"session_id":"local-test"}' | CC_STATUS_PROVIDER=local CC_STATUS_BILLING=local ANTHROPIC_MODEL='gemma[128k]' ./path/to/statusline-script
echo '{"model":{"display_name":"GLM"},"context_window":{"used_percentage":58},"session_id":"glm-test"}' | CC_STATUS_PROVIDER=zai CC_STATUS_BILLING=subscription ANTHROPIC_MODEL='glm-4.7' ./path/to/statusline-script
echo '{"model":{"display_name":"DeepSeek"},"context_window":{"used_percentage":44},"session_id":"api-test"}' | CC_STATUS_PROVIDER=deepseek CC_STATUS_BILLING=api ANTHROPIC_MODEL='deepseek-v4-pro[500k]' ./path/to/statusline-script
```

Validation commands should pipe JSON into stdin. Do not rely on interactive Claude Code for the first pass.
