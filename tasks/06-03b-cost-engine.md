# Task 06-03b: Vendor-Agnostic Cost Engine

## Objective

Implement the provider-agnostic cost calculation engine for the Claude Code status line using mock pricing first.

This task should not research or add real vendor prices. It should define the durable pricing format, usage extraction, model normalization, session state, dedupe strategy, and output formatting.

## Prerequisites

Read the diagnostic output from:

```text
.claude/statusline/fixtures/cost-diagnostics/diagnostic-summary.md
```

Also read:

- `tasks/06-03-api-cost-pricing.md`
- `tasks/06-03a-cost-usage-diagnostics.md`
- `.claude/statusline/statusline.py`
- `.claude/statusline/README.md`

Do not proceed by guessing `current_usage` semantics if the diagnostic summary is missing or inconclusive. In that case, add a clear blocker note to the README or task output and implement only the parts that are independent of dedupe, such as pricing config loading and model normalization. Do not enable accumulating cost until a dedupe strategy is justified.

## Scope Boundary

Implement the generic engine only:

- usage bucket extraction
- pricing config loading
- model alias normalization
- session state storage
- dedupe
- cost formatting
- subscription/local suppression

Do not add real prices for DeepSeek, Fireworks, MiniMax, or Kimi in this task. Use mock/test pricing so the engine can be verified deterministically.

## Existing Implementation

Extend the existing status line entrypoint:

```text
.claude/statusline/statusline.py
```

Do not create a second status line command.

Current expected behavior must remain intact:

- Claude Max shows context and built-in Claude rate limits.
- GLM Coding Plan shows context and GLM quota.
- Local Gemma hides quota and cost.

## Pricing Config Contract

Create repository default pricing:

```text
.claude/statusline/pricing.default.json
```

Allow user override pricing:

```text
~/.config/claude-statusline/pricing.json
```

The user override should override or extend the repo default.

Use this schema:

```json
{
  "version": 1,
  "currency": "USD",
  "providers": {
    "mock": {
      "models": {
        "mock-model": {
          "input_per_million": 1.0,
          "output_per_million": 2.0,
          "cache_write_per_million": 1.25,
          "cache_read_per_million": 0.1,
          "source": "test fixture",
          "retrieved_at": "fixture"
        }
      }
    }
  }
}
```

Rules:

- Prices are USD per 1 million tokens.
- All four price fields are required for a model to be considered priced.
- If a real provider has no cache-specific pricing, later tasks must either configure an explicit documented fallback or leave the price unknown.
- Unknown or incomplete pricing renders `cost ?`.
- Do not use Claude Code's `cost.total_cost_usd` for custom API providers as a fallback.

## Usage Bucket Contract

Extract these token buckets when present:

| Status line field | Bucket |
|---|---|
| `input_tokens` | input |
| `output_tokens` | output |
| `cache_creation_input_tokens` | cache write |
| `cache_read_input_tokens` | cache read |

If the diagnostic task found different field names, support those names too and document the mapping in `.claude/statusline/README.md`.

If `current_usage` is `null`, render no cost yet or render `cost ?` only if the provider is API and the renderer already needs a cost placeholder. Prefer compact output.

## Session State Contract

Store session cost state under:

```text
~/.cache/claude-statusline/cost-{safe_session_key}.json
```

Requirements:

- `safe_session_key` must be filesystem-safe.
- State must not contain prompts, completions, transcripts, cwd paths, workspace paths, private command text, or raw API tokens.
- State may contain provider, normalized model, token counters, cost totals, timestamps, and usage fingerprints.
- Writes should be atomic where practical.

Suggested state:

```json
{
  "version": 1,
  "session_id": "session-id",
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "total_cost_usd": 0.1234,
  "total_tokens": {
    "input": 1000,
    "output": 100,
    "cache_write": 0,
    "cache_read": 0
  },
  "seen_usage_fingerprints": ["..."],
  "updated_at": "2026-05-03T00:00:00Z"
}
```

Limit fingerprint history to a reasonable number, for example the last 100 fingerprints.

## Dedupe Contract

Use the recommendation from `diagnostic-summary.md`.

If `current_usage` is per-call and repeated across renders, dedupe by a stable fingerprint of:

- provider
- normalized model
- session id
- current usage object with sorted keys

If diagnostics reveal cumulative counters, calculate deltas instead and store last counters.

If semantics remain unknown, do not accumulate cost silently. Render `cost ?` and document the blocker.

If using fingerprints, hash the canonicalized usage object rather than storing the full raw object when practical. If storing raw usage is simpler, store only numeric token fields.

## Model Normalization

Implement model normalization for current launchers:

- Strip bracketed context suffixes like `[500k]` and `[200k]`.
- Normalize provider/model case where appropriate.
- Prefer `CC_STATUS_MODEL` if set.
- Then prefer `ANTHROPIC_MODEL`.
- Then use `model.display_name` from status JSON.

Do not over-normalize across providers. For example, `glm-5.1` under Fireworks should be looked up under provider `fireworks`, not under provider `zai`.

## Output Contract

Display cost only when billing is `api`.

Do not display cost for:

- `provider=claude`, `billing=subscription`
- `provider=zai`, `billing=subscription`
- `provider=local`, `billing=local`

Formatting:

- If known and `>= 0.01`, use two decimals: `$0.18`.
- If known and `< 0.01`, use compact precision such as `$0.004`.
- If unknown pricing for an API provider, render `cost ?`.
- Do not include token counts by default unless the existing status line has room and the README documents the choice.

## Tests

Add tests or fixture commands that run without real credentials.

Required coverage:

- Mock API provider with known pricing displays a calculated cost.
- Re-running the exact same fixture with the same `session_id` does not double-count.
- A second distinct usage fixture with the same `session_id` increments cost.
- Unknown pricing renders `cost ?`.
- Subscription providers suppress cost.
- Local provider suppresses cost.
- Invalid or missing `current_usage` does not crash.

## Done Criteria

- [ ] Cost engine is implemented in the existing status line code path.
- [ ] Pricing config schema exists and is documented.
- [ ] User override pricing path is supported.
- [ ] Session state is persisted safely under `~/.cache/claude-statusline/`.
- [ ] Dedupe follows the diagnostic conclusion.
- [ ] Mock pricing tests pass.
- [ ] Claude Max, GLM Coding Plan, and local Gemma behavior are not regressed.
- [ ] No real vendor prices are added in this task except mock/test pricing.
- [ ] No secrets or prompt content are persisted.
