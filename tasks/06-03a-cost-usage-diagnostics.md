# Task 06-03a: Cost Usage Diagnostics

## Objective

Determine exactly how Claude Code status line usage data behaves for metered API providers before implementing cost calculation.

This is a diagnostic task. It should produce fixtures and a written conclusion, not a full cost engine.

## Context

Existing status line implementation:

```text
.claude/statusline/statusline.py
.claude/statusline/README.md
.claude/statusline/fixtures/
```

Earlier phases are complete:

- Claude Max status line works.
- GLM Coding Plan quota works.

Cost display will be implemented in later tasks. The main risk is double-counting if `context_window.current_usage` is repeated across status line refreshes.

Relevant Claude Code docs:

- https://code.claude.com/docs/en/statusline

Relevant fields:

- `context_window.current_usage`
- `context_window.used_percentage`
- `context_window.context_window_size`
- `session_id`
- `model.display_name`
- `cost.total_cost_usd`

Known doc behavior:

- `context_window.current_usage` is `null` before the first API call.
- `context_window.used_percentage` is context occupancy, not billable cost.
- Status line commands may run on events and may also run repeatedly if `refreshInterval` is configured.

## Provider To Test First

Use DeepSeek first if a real diagnostic run is possible:

```text
launcher: claude-ds
provider: deepseek
billing: api
models: deepseek-v4-pro[500k], deepseek-v4-pro[200k], deepseek-v4-flash
```

Do not copy tokens from `/data/sync/Config/bash/claude.bashrc` into output files.

If a real provider run is not possible, use any existing statusline fixtures and document the limitation clearly.

## Required Questions To Answer

1. Is `context_window.current_usage` per-call usage, cumulative usage, or something else?
2. Does the same `current_usage` object repeat across multiple status line renders?
3. Which token bucket fields are present?
4. Are cache token fields present?
5. Does `cost.total_cost_usd` appear for custom Anthropic-compatible API providers?
6. Does `session_id` remain stable across repeated renders in one Claude session?
7. Are there fields that can identify a single API response uniquely?

## Diagnostic Implementation

Add a temporary/debug diagnostic mode to the existing status line implementation, gated by an env var:

```bash
CC_STATUS_DIAGNOSTIC=1
```

When enabled, append sanitized status input snapshots to:

```text
~/.cache/claude-statusline/diagnostics/{safe_session_key}.jsonl
```

Requirements:

- Create the diagnostics directory if needed.
- Append one JSON object per status line invocation.
- Include a timestamp.
- Include only safe fields required for usage analysis.
- Use a filesystem-safe session key in filenames. A short hash of `session_id` is acceptable.
- Do not store prompts, completions, transcripts, cwd paths, workspace paths, private command text, or API tokens.
- Do not change normal status line output except possibly adding a tiny debug marker if already supported by the implementation.
- Keep normal status line behavior unchanged when `CC_STATUS_DIAGNOSTIC` is unset.
- Do not modify Claude Code settings to enable diagnostics permanently.

Suggested recorded fields:

```json
{
  "timestamp": "2026-05-03T00:00:00Z",
  "session_key": "short-session-hash",
  "provider": "deepseek",
  "billing": "api",
  "model": "deepseek-v4-pro[500k]",
  "context_window": {
    "used_percentage": 44,
    "context_window_size": 500000,
    "current_usage": {
      "input_tokens": 1000,
      "output_tokens": 100,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0
    }
  },
  "cost": {
    "total_cost_usd": null
  }
}
```

If the actual field names differ, preserve the actual names in fixtures and document them.

The raw diagnostic JSONL under `~/.cache` may contain a little more detail during local investigation, but committed/repo fixtures must be sanitized. Before copying any diagnostic output into `.claude/statusline/fixtures/`, inspect it for secrets and private content.

## Fixture Output

Create sanitized fixtures under:

```text
.claude/statusline/fixtures/cost-diagnostics/
```

Suggested files:

```text
deepseek-before-first-call.json
deepseek-after-call-1.json
deepseek-repeated-render-same-call.json
deepseek-after-call-2.json
diagnostic-summary.md
```

The fixture JSON files should be safe to commit: no tokens, prompts, completions, or private transcript paths.
They should also omit private workspace paths and command text.

## Diagnostic Summary

Write the conclusion in:

```text
.claude/statusline/fixtures/cost-diagnostics/diagnostic-summary.md
```

The summary must state:

- Observed `current_usage` semantics.
- Observed token bucket field names.
- Whether repeated renders duplicate usage.
- Recommended dedupe strategy for task `06-03b`.
- Any limitations of the diagnostic.

## Done Criteria

- [ ] Diagnostic mode exists and is gated behind `CC_STATUS_DIAGNOSTIC=1`.
- [ ] Normal status line output remains unchanged when diagnostics are disabled.
- [ ] Sanitized diagnostic fixtures are created.
- [ ] `diagnostic-summary.md` answers all required questions.
- [ ] No secrets, prompts, completions, or transcripts are stored.
- [ ] No private workspace paths or command text are stored in repo fixtures.
- [ ] The task does not implement vendor pricing or cost display.

## Validation

Run the existing status line fixture commands to confirm no normal behavior regressed.

If possible, run a short `claude-ds` session with diagnostics enabled:

```bash
export CC_STATUS_DIAGNOSTIC=1
claude-ds
```

During the session, cause at least two API calls and allow at least one repeated status line render between calls. Then inspect the diagnostic JSONL file and create sanitized fixtures.
