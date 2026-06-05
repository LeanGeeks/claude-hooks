# Task 06-03e: Cost Display Review And Hardening

## Objective

Review and harden the full metered API cost display implementation before handing it to the final integration task.

This is a code-review and stabilization task. Prioritize bugs, edge cases, double-counting, secret safety, and behavior regressions.

## Prerequisites

Read:

- `tasks/06-03-api-cost-pricing.md`
- `tasks/06-03a-cost-usage-diagnostics.md`
- `tasks/06-03b-cost-engine.md`
- `tasks/06-03c-deepseek-pricing.md`
- `tasks/06-03d-additional-vendor-pricing.md`
- `.claude/statusline/statusline.py`
- `.claude/statusline/pricing.default.json`
- `.claude/statusline/README.md`
- cost diagnostic fixtures under `.claude/statusline/fixtures/cost-diagnostics/`

## Review Priorities

1. Double-counting risk across repeated status line renders.
2. Correct suppression for subscriptions and local models.
3. Correct provider/model normalization.
4. Unknown/incomplete pricing behavior.
5. Cache and state file safety.
6. Secret leakage.
7. Runtime performance.
8. Regressions to Claude Max and GLM Coding Plan status output.

## Required Checks

### Dedupe

- Re-run the same API usage fixture with the same `session_id` multiple times.
- Confirm cost does not increase on identical repeated renders.
- Run a second distinct usage fixture with the same `session_id`.
- Confirm cost increases once.
- Confirm changing `session_id` starts a separate cost state.

### Suppression

Confirm no cost segment appears for:

- `CC_STATUS_PROVIDER=claude`, `CC_STATUS_BILLING=subscription`
- `CC_STATUS_PROVIDER=zai`, `CC_STATUS_BILLING=subscription`
- `CC_STATUS_PROVIDER=local`, `CC_STATUS_BILLING=local`

### Unknown Pricing

Confirm API providers with unknown or incomplete prices display:

```text
cost ?
```

They must not:

- crash
- display `$0.00` for unknown pricing
- fall back to Claude Code `cost.total_cost_usd` for custom providers

### State Files

Inspect state files under:

```text
~/.cache/claude-statusline/
```

Confirm they contain only safe data:

- numeric counters
- provider/model names
- timestamps
- usage fingerprints

They must not contain:

- API tokens
- prompts
- completions
- transcript content
- private command text

### Runtime

Status line rendering should normally be fast:

- under 200 ms for cached/local paths
- under 2 seconds only when GLM quota network timeout is involved

Cost calculation should not make network calls.

## Test Harness

If tests do not already exist, add a small dependency-free test harness, for example:

```text
.claude/statusline/test_statusline.py
```

or documented fixture commands in the README.

Tests must run without real provider credentials.

Minimum coverage:

- Claude subscription fixture.
- GLM subscription fixture.
- local Gemma fixture.
- DeepSeek priced fixture.
- Fireworks/MiniMax/Kimi priced or unknown fixtures.
- repeated-render dedupe.
- unknown pricing.
- invalid JSON or missing `current_usage`.

## Documentation

Update `.claude/statusline/README.md` with:

- pricing config path
- user override path
- cache/state path
- supported providers
- source URLs/retrieval dates for built-in prices
- meaning of `cost ?`
- explanation that subscription and local setups hide cost

## Done Criteria

- [ ] All review checks are complete.
- [ ] Any double-counting issue is fixed.
- [ ] Subscription/local cost suppression is verified.
- [ ] Unknown pricing behavior is verified.
- [ ] State/cache files are safe.
- [ ] Tests or documented fixture commands run without secrets.
- [ ] README describes pricing, cache/state paths, and `cost ?`.
- [ ] Claude Max and GLM Coding Plan status outputs still work.
- [ ] No git information is displayed.
