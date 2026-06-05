# Task 06-03d: Additional Vendor Pricing

## Objective

Add pricing for the remaining metered API providers after the generic cost engine and DeepSeek integration are proven.

Providers:

- Fireworks
- MiniMax
- Kimi/Moonshot

## Prerequisites

Read:

- `tasks/06-03-api-cost-pricing.md`
- `tasks/06-03b-cost-engine.md`
- `tasks/06-03c-deepseek-pricing.md`
- `.claude/statusline/statusline.py`
- `.claude/statusline/pricing.default.json`
- `.claude/statusline/README.md`

Task `06-03c` should already be complete. Do not add vendor-specific cost logic that bypasses the generic engine.

## Scope Boundary

This task should only add pricing entries, alias mappings, fixtures, and docs for the remaining providers.

Do not change the dedupe algorithm unless a real bug is found. If a bug is found, document it and keep the fix generic.

## Provider Context

From `/data/sync/Config/bash/claude.bashrc`:

| Launcher | Base URL | Provider | Billing | Models |
|---|---|---|---|---|
| `claude-glm5-fw` | `https://api.fireworks.ai/inference` | `fireworks` | `api` | `glm-5.1`, `minimax-m2p5` |
| `claude-minimax` | `https://api.minimax.io/anthropic` | `minimax` | `api` | `MiniMax-M2.7` |
| `claude-kimi` | `https://api.moonshot.ai/anthropic` | `kimi` | `api` | `kimi-k2.5`, `kimi-k2-0905-preview` |

Do not copy API tokens from the bashrc file.

## Pricing Requirements

Use official pricing sources only.

For each provider/model, record:

- source URL
- retrieval date
- input price per 1M tokens
- output price per 1M tokens
- cache write price per 1M tokens
- cache read price per 1M tokens

If a provider does not publish cache-specific pricing, do not infer it unless the provider officially documents that cache tokens are billed as normal input or at a specific multiplier.

If pricing cannot be verified:

- leave that model out of `pricing.default.json`, or mark it incomplete in a way the engine treats as unknown
- render `cost ?`
- document the missing official source in the README

## Fireworks Caution

`claude-glm5-fw` uses Fireworks billing, even when the model is a GLM model.

Do not use Z.ai Coding Plan pricing for Fireworks. Use Fireworks pricing for the exact hosted model/alias. If Fireworks pricing cannot be verified for `glm-5.1` or `minimax-m2p5`, render `cost ?`.

## MiniMax And Kimi Caution

The launcher model names may not match pricing-page model names exactly:

- `MiniMax-M2.7`
- `kimi-k2.5`
- `kimi-k2-0905-preview`

Add explicit aliases only when the mapping is clear from official docs or from the provider API documentation. Otherwise leave the model unknown.

## Validation

Required fixture tests:

- Fireworks known-price model displays cost, or unknown model displays `cost ?`.
- MiniMax known-price model displays cost, or unknown model displays `cost ?`.
- Kimi known-price model displays cost, or unknown model displays `cost ?`.
- DeepSeek behavior from task `06-03c` still works.
- Claude Max, GLM Coding Plan, and local Gemma still suppress cost.
- Repeated render still does not double-count.

Example fixture commands:

```bash
echo '{"model":{"display_name":"GLM"},"context_window":{"used_percentage":51,"current_usage":{"input_tokens":100000,"output_tokens":5000,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},"session_id":"fireworks-cost-test"}' | CC_STATUS_PROVIDER=fireworks CC_STATUS_BILLING=api ANTHROPIC_MODEL='glm-5.1' ./.claude/statusline/statusline.py
echo '{"model":{"display_name":"MiniMax"},"context_window":{"used_percentage":38,"current_usage":{"input_tokens":100000,"output_tokens":5000,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},"session_id":"minimax-cost-test"}' | CC_STATUS_PROVIDER=minimax CC_STATUS_BILLING=api ANTHROPIC_MODEL='MiniMax-M2.7' ./.claude/statusline/statusline.py
echo '{"model":{"display_name":"Kimi"},"context_window":{"used_percentage":49,"current_usage":{"input_tokens":100000,"output_tokens":5000,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},"session_id":"kimi-cost-test"}' | CC_STATUS_PROVIDER=kimi CC_STATUS_BILLING=api ANTHROPIC_MODEL='kimi-k2.5' ./.claude/statusline/statusline.py
```

## Done Criteria

- [ ] Fireworks pricing is added when official pricing is verified, otherwise documented as unknown.
- [ ] MiniMax pricing is added when official pricing is verified, otherwise documented as unknown.
- [ ] Kimi/Moonshot pricing is added when official pricing is verified, otherwise documented as unknown.
- [ ] Source URLs and retrieval dates are documented for every real price.
- [ ] Provider/model aliases are explicit and conservative.
- [ ] Unknown pricing renders `cost ?`.
- [ ] DeepSeek behavior remains intact.
- [ ] Subscription/local cost suppression remains intact.
- [ ] No secrets are copied into code, config, tests, fixtures, or docs.
