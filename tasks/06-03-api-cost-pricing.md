# Task 06-03: Metered API Cost Display Overview

## Objective

Add cost display for metered API providers in the Claude Code status line, without affecting Claude Max, GLM Coding Plan, or local model behavior.

This file is an overview for the cost work. Implement the work through the smaller handoff tasks:

1. `tasks/06-03a-cost-usage-diagnostics.md`
2. `tasks/06-03b-cost-engine.md`
3. `tasks/06-03c-deepseek-pricing.md`
4. `tasks/06-03d-additional-vendor-pricing.md`
5. `tasks/06-03e-cost-review-hardening.md`

Do not assign this overview file directly to an implementation agent unless the subtask files are also included.

## Current Status

The earlier status line phases are complete:

- Task 06-01: Claude Max status line foundation works.
- Task 06-02: GLM Coding Plan status line quota works.

Current implementation path:

```text
.claude/statusline/statusline.py
.claude/statusline/README.md
.claude/statusline/fixtures/
```

The cost phases must extend the existing entrypoint. Do not create a second status line command.

## User Preferences

- Show cost only for metered API setups.
- Hide cost for:
  - Claude Code Max subscription
  - GLM Coding Plan subscription
  - local Gemma
- Use proper pricing per vendor/model.
- Use official pricing sources when adding real prices.
- Do not display git branch or git status.
- Keep the output compact.
- Unknown or unverified prices should render `cost ?`.

## Provider Scope

Metered API providers from `/data/sync/Config/bash/claude.bashrc`:

| Launcher | Provider | Billing | Typical Models |
|---|---|---|---|
| `claude-ds` | DeepSeek | api | `deepseek-v4-pro[500k]`, `deepseek-v4-pro[200k]`, `deepseek-v4-flash` |
| `claude-glm5-fw` | Fireworks | api | `glm-5.1`, `minimax-m2p5` |
| `claude-minimax` | MiniMax | api | `MiniMax-M2.7` |
| `claude-kimi` | Kimi/Moonshot | api | `kimi-k2.5`, `kimi-k2-0905-preview` |

Non-metered setups where cost must remain hidden:

| Launcher | Provider | Billing |
|---|---|---|
| `claude` | Claude | subscription |
| `claude-glm` | Z.ai GLM Coding Plan | subscription |
| `claude-glm5` | Z.ai GLM Coding Plan | subscription |
| `claude-gemma` | local Gemma | local |

Do not copy secrets from `/data/sync/Config/bash/claude.bashrc` into task notes, fixtures, tests, code comments, or README files.

## Implementation Strategy

Build the feature in this order:

1. Diagnose actual `context_window.current_usage` behavior before writing cost logic.
2. Implement a vendor-agnostic cost engine with fake pricing.
3. Add DeepSeek as the first real provider.
4. Add Fireworks, MiniMax, and Kimi only after the engine is proven.
5. Run a final review focused on double-counting, subscription suppression, and degraded behavior.

This split is intentional. Cost display has several independent risks:

- Claude Code may repeat the same `current_usage` object across status line renders.
- Usage fields may be per-call rather than cumulative.
- Provider pricing changes over time.
- Hosted/proxy providers such as Fireworks bill by their own pricing, not by upstream model vendor pricing.
- Model names in launchers may include aliases or context suffixes such as `[500k]`.

## Shared Design Contract

All cost subtasks should preserve these decisions unless an earlier task documents a better one:

- Use Python 3 standard library only.
- Keep `.claude/statusline/statusline.py` as the single executable entrypoint.
- Put repository default pricing in `.claude/statusline/pricing.default.json`.
- Allow user overrides from `~/.config/claude-statusline/pricing.json`.
- Put cost session state under `~/.cache/claude-statusline/`.
- Store only numeric counters, provider/model names, timestamps, and fingerprints.
- Never store prompts, completions, transcripts, or raw API tokens.
- Cost display appears only when `CC_STATUS_BILLING=api` or inferred billing is `api`.

## Final Desired Output Examples

DeepSeek:

```text
DeepSeek | ctx 44% | $0.18
```

Fireworks:

```text
Fireworks GLM-5.1 | ctx 51% | $0.27
```

Unknown price:

```text
Kimi | ctx 49% | cost ?
```

Subscription suppression:

```text
GLM-5.1 plan | ctx 58% | 5h 37%
```

Local suppression:

```text
Gemma local | ctx 32%
```

## Handoff Order

Recommended model capability by subtask:

| Task | Complexity | Suggested model |
|---|---:|---|
| `06-03a` usage diagnostics | 4/5 | strong |
| `06-03b` cost engine | 5/5 | maximum |
| `06-03c` DeepSeek pricing | 3/5 | medium/strong |
| `06-03d` additional vendors | 3/5 | medium/strong |
| `06-03e` review/hardening | 5/5 | maximum |

Run `06-03a`, `06-03b`, and `06-03c` sequentially. `06-03d` can be split further by vendor only after `06-03b` defines the final pricing format.
