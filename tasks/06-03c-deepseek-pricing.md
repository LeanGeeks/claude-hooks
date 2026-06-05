# Task 06-03c: DeepSeek Pricing Integration

## Objective

Add verified DeepSeek pricing to the vendor-agnostic cost engine and validate end-to-end cost display with the `claude-ds` setup.

This is the first real vendor integration. Use it to prove the cost engine before adding Fireworks, MiniMax, or Kimi.

## Prerequisites

Read:

- `tasks/06-03-api-cost-pricing.md`
- `tasks/06-03a-cost-usage-diagnostics.md`
- `tasks/06-03b-cost-engine.md`
- `.claude/statusline/statusline.py`
- `.claude/statusline/pricing.default.json`
- `.claude/statusline/README.md`

Task `06-03b` must already be complete. If the generic cost engine or pricing schema is missing, stop and document the blocker instead of implementing a separate DeepSeek-specific path.

## Scope Boundary

Add DeepSeek pricing only.

Do not add Fireworks, MiniMax, or Kimi pricing in this task. Do not change GLM Coding Plan quota behavior.

## DeepSeek Launch Context

From `/data/sync/Config/bash/claude.bashrc`:

| Launcher | Base URL | Provider | Billing |
|---|---|---|---|
| `claude-ds` | `https://api.deepseek.com/anthropic` | `deepseek` | `api` |

Typical models:

- `deepseek-v4-pro[500k]`
- `deepseek-v4-pro[200k]`
- `deepseek-v4-flash`

Do not copy API tokens from the bashrc file.

## Pricing Requirements

Use official DeepSeek pricing sources only. Pricing is temporally unstable, so verify it during implementation.

Record for every configured DeepSeek model:

- source URL
- retrieval date
- input price per 1M tokens
- output price per 1M tokens
- cache write price per 1M tokens
- cache read price per 1M tokens
- **blended price per 1M tokens** (`blended_per_million`)

The diagnostic in `06-03a` established that real `claude-ds` emits `context_window.current_usage` as a **plain integer** (cumulative total context tokens), not a per-bucket object. The cost engine from `06-03b` requires `blended_per_million` to price integer payloads — without it the engine renders `cost ?` for live `claude-ds` sessions even if all four bucket prices are configured. Configure both:

- The four bucket rates for forward compatibility (in case a future Claude Code version exposes per-bucket fields).
- `blended_per_million` so the present-day integer payload prices correctly. Document how the blended rate was derived (e.g. equal to input rate, or a weighted estimate) in the `source` field.

If a cache bucket price cannot be verified officially, leave that model incomplete and render `cost ?` rather than guessing. The same applies to `blended_per_million`.

If DeepSeek's official pricing uses a different model name than the launcher alias, add an alias mapping in code or config and document it. The current schema in `pricing.default.json` does not have first-class alias support — either duplicate the model entry under each alias, or extend the schema with an `aliases` map and update the lookup in `lookup_model_pricing()`.

## Pricing Config

Update:

```text
.claude/statusline/pricing.default.json
```

Use the schema from `06-03b`. Example shape only:

```json
{
  "providers": {
    "deepseek": {
      "models": {
        "deepseek-v4-pro": {
          "input_per_million": 0.0,
          "output_per_million": 0.0,
          "cache_write_per_million": 0.0,
          "cache_read_per_million": 0.0,
          "blended_per_million": 0.0,
          "source": "official URL (note how blended rate was derived)",
          "retrieved_at": "2026-05-03"
        }
      }
    }
  }
}
```

Do not put placeholder zero prices into the final config unless they are clearly marked as test-only and not used for real DeepSeek models.

## Validation

Use fixtures first. Then, if practical, validate with a real `claude-ds` session.

Required fixture tests — **must include both payload shapes**:

- `deepseek-v4-pro[500k]` normalizes to the configured pricing key.
- `deepseek-v4-pro[200k]` normalizes to the configured pricing key if appropriate.
- `deepseek-v4-flash` resolves separately or renders `cost ?` if pricing is unknown.
- **Integer `current_usage` fixture (matches real `claude-ds` output)** — see `.claude/statusline/fixtures/cost-diagnostics/deepseek-after-call-1.json` for shape. This is the realistic path and must price correctly via `blended_per_million`.
- **Dict `current_usage` fixture (forward-compatibility path)** — uses the four bucket rates.
- Repeated render with same `session_id` does not double-count.
- Subscription and local fixtures still suppress cost.

Add tests to `.claude/statusline/test_cost_engine.py` (or a sibling `test_deepseek_pricing.py` that follows the same subprocess-with-temp-`HOME` pattern) so they run without real credentials.

Example fixture commands:

Realistic integer-shape payload (matches live `claude-ds`):

```bash
echo '{"model":{"display_name":"deepseek-v4-pro"},"context_window":{"used_percentage":44,"current_usage":220000,"context_window_size":500000},"session_id":"deepseek-cost-test"}' | CC_STATUS_PROVIDER=deepseek CC_STATUS_BILLING=api ANTHROPIC_MODEL='deepseek-v4-pro[500k]' python3 .claude/statusline/statusline.py
```

Forward-compatible bucket-shape payload (per-bucket pricing path):

```bash
echo '{"model":{"display_name":"DeepSeek"},"context_window":{"used_percentage":44,"current_usage":{"input_tokens":100000,"output_tokens":5000,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},"session_id":"deepseek-cost-test"}' | CC_STATUS_PROVIDER=deepseek CC_STATUS_BILLING=api ANTHROPIC_MODEL='deepseek-v4-pro[500k]' python3 .claude/statusline/statusline.py
```

If only the bucket fixture passes but the integer fixture renders `cost ?`, `blended_per_million` is missing or zero — fix the config rather than the engine.

## Done Criteria

- [ ] DeepSeek prices are added from official sources.
- [ ] Source URL and retrieval date are documented.
- [ ] `blended_per_million` is configured and its derivation is documented in the `source` field.
- [ ] DeepSeek launcher model names normalize correctly (suffix-stripped + lowercased pricing key).
- [ ] Integer `current_usage` payload (real `claude-ds` shape) renders a calculated cost, not `cost ?`.
- [ ] Dict `current_usage` payload (forward-compat shape) renders a calculated cost.
- [ ] Unknown/incomplete DeepSeek pricing renders `cost ?`.
- [ ] Repeated renders do not double-count.
- [ ] Claude Max, GLM Coding Plan, and local Gemma cost suppression still works.
- [ ] No secrets are copied into code, config, tests, fixtures, or docs.
