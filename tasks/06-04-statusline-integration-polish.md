# Task 06-04: Status Line Integration, Polish, and Operational Hardening

## Objective

Finalize the provider-aware Claude Code status line so it is reliable enough for daily use across Claude Max, GLM Coding Plan, local Gemma, and metered API providers.

This task integrates and hardens work from:

- `tasks/06-01-statusline-foundation.md`
- `tasks/06-02-glm-coding-plan-quota.md`
- `tasks/06-03-api-cost-pricing.md`
- `tasks/06-03a-cost-usage-diagnostics.md`
- `tasks/06-03b-cost-engine.md`
- `tasks/06-03c-deepseek-pricing.md`
- `tasks/06-03d-additional-vendor-pricing.md`
- `tasks/06-03e-cost-review-hardening.md`

## Scope Boundary

This is the final integration task. It should consolidate and harden earlier work, not introduce a second implementation path. If prior agents created multiple status line entrypoints or conflicting helpers, choose one cohesive implementation and clearly deprecate the others in documentation; only remove files when they are definitely part of the status line work and no longer referenced.

## User Preferences

- Do not display git branch or git status.
- Always display context usage when available.
- Display subscription rate limits only for Claude Code Max and GLM Coding Plan.
- Display cost only for metered API providers.
- Keep output compact.
- Prefer useful degraded output over blank/error output.

## Existing Launch Setup

Review provider launch functions in:

`/data/sync/Config/bash/claude.bashrc`

Do not copy secrets from this file.

The official Z.ai coding plugin repository is available locally for GLM quota behavior comparison:

```text
/data/sync/work/leangeeks-ai/ai-playground/temp/zai-coding-plugins/
```

Use it only as a reference/validation source. The final status line should not depend on files under `temp/`.

Known launchers:

| Launcher | Billing | Expected Display |
|---|---|---|
| `claude` | subscription | model, context, Claude 5h/7d limits |
| `claude-glm` | subscription | GLM model, context, GLM 5h quota |
| `claude-glm5` | subscription | GLM model, context, GLM 5h quota |
| `claude-gemma` | local | model, context only |
| `claude-ds` | api | model/provider, context, calculated cost |
| `claude-glm5-fw` | api | model/provider, context, calculated cost |
| `claude-minimax` | api | model/provider, context, calculated cost |
| `claude-kimi` | api | model/provider, context, calculated cost |

## Final Desired Behavior

Claude Max:

```text
Opus | ctx 61% | 5h 43% reset 1:12 | 7d 18%
```

GLM Coding Plan:

```text
GLM-4.7 plan | ctx 58% | 5h 37%
```

GLM Coding Plan with stale quota cache:

```text
GLM-5.1 plan | ctx 72% | 5h 81% stale
```

Metered API:

```text
DeepSeek | ctx 44% | $0.18
```

Local:

```text
Gemma local | ctx 32%
```

Unknown/custom:

```text
Custom API | ctx 41% | cost ?
```

## Integration Requirements

1. Provide one stable status line entrypoint.
2. Document all supported env vars:
   - `CC_STATUS_PROVIDER`
   - `CC_STATUS_BILLING`
   - `CC_STATUS_PROFILE`
   - `CC_STATUS_MODEL`
   - any config/cache/pricing path overrides
3. Provide recommended exports to add to each launcher in `/data/sync/Config/bash/claude.bashrc`.
4. Preserve secrets. Do not edit or print API tokens.
5. Ensure the script exits quickly.
6. Ensure failures do not make the status line blank.
7. Ensure narrow terminals do not produce awkward wrapping where reasonably avoidable.
8. Keep the status line free of git information.

Document these allowed values:

| Env var | Expected values |
|---|---|
| `CC_STATUS_PROVIDER` | `claude`, `zai`, `local`, `deepseek`, `fireworks`, `minimax`, `kimi`, `unknown` |
| `CC_STATUS_BILLING` | `subscription`, `api`, `local` |
| `CC_STATUS_PROFILE` | free-form label such as `claude-max`, `glm-plan`, `fireworks-api` |
| `CC_STATUS_MODEL` | explicit model override, free-form |

## Expected Final File Layout

Prefer this final shape:

```text
.claude/statusline/
├── statusline.py
├── pricing.default.json
├── README.md
├── fixtures/
│   ├── claude-subscription.json
│   ├── glm-subscription.json
│   ├── api-usage.json
│   ├── local.json
│   └── cost-diagnostics/
└── tests/ or test_statusline.py
```

If the previous tasks used a slightly different layout, keep it only if it is simpler and equally documented. The important requirement is one executable entrypoint referenced by Claude Code.

## Suggested Launcher Env Additions

These are examples for documentation. If editing the actual bashrc, preserve existing user content and do not expose secrets.

```bash
# Claude Max
export CC_STATUS_PROVIDER=claude
export CC_STATUS_BILLING=subscription
export CC_STATUS_PROFILE=claude-max
```

```bash
# GLM Coding Plan
export CC_STATUS_PROVIDER=zai
export CC_STATUS_BILLING=subscription
export CC_STATUS_PROFILE=glm-plan
```

```bash
# Local Gemma
export CC_STATUS_PROVIDER=local
export CC_STATUS_BILLING=local
export CC_STATUS_PROFILE=gemma-local
```

```bash
# Metered API examples
export CC_STATUS_PROVIDER=deepseek
export CC_STATUS_BILLING=api
export CC_STATUS_PROFILE=deepseek-api
```

Use analogous values for:

- `fireworks-api`
- `minimax-api`
- `kimi-api`

## Claude Code Settings

The status line is configured in Claude Code settings. Example:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/absolute/path/to/statusline-script",
    "padding": 0,
    "refreshInterval": 30
  }
}
```

Use `refreshInterval` only if useful for quota reset times or stale cache refresh. Otherwise event-driven updates may be enough.

Recommended default for this project: include `refreshInterval: 30` only if GLM quota reset/stale markers need to update while Claude is idle. Otherwise leave it unset to avoid unnecessary script executions.

## Robustness Requirements

- Missing `context_window.used_percentage` -> display `ctx ?`.
- Missing Claude rate limits -> omit the rate limit segment, do not show fake values.
- Missing GLM token/base URL -> display `quota ?`, no crash.
- GLM API timeout -> use cache if available.
- Missing pricing -> display `cost ?` only for API billing.
- Unknown provider -> infer best effort and remain compact.
- Invalid JSON on stdin -> exit cleanly with minimal fallback output.
- Any unexpected exception -> print a minimal fallback, optionally log details only when `CC_STATUS_DEBUG=1`.
- Status line execution target: normally under 200 ms with cache hits, and under 2 seconds on a GLM cache miss/network timeout.
- No raw tokens in stdout, stderr, cache files, README, fixtures, or tests.

## Output Precedence

Use this precedence when deciding which optional segments to render:

1. Context is always attempted.
2. Claude built-in 5h/7d rate limits only for `provider=claude`, `billing=subscription`.
3. GLM quota only for `provider=zai`, `billing=subscription`.
4. Cost only for `billing=api`.
5. Local billing hides both quota and cost.

Never display both subscription quota and API cost for the same run unless the user explicitly overrides behavior in a future task.

## Testing Requirements

Add a small test harness or documented test commands using mock JSON. Cover:

1. Claude subscription with built-in rate limits.
2. GLM subscription with mocked cached quota.
3. GLM subscription with no token.
4. Local Gemma.
5. DeepSeek API with known pricing.
6. Fireworks API with known or unknown pricing.
7. MiniMax API with known or unknown pricing.
8. Kimi API with known or unknown pricing.
9. Invalid/missing fields.
10. Repeated renders do not double-count cost.

The test harness can be plain Python standard library tests or a shell script with fixtures. It should run without real provider credentials. Network-dependent GLM behavior should be tested through cache fixtures or mocked fetch helpers.

## Done Criteria

- [ ] One documented status line command is ready for Claude Code settings.
- [ ] Provider env overrides are documented.
- [ ] Recommended `claude.bashrc` additions are documented or applied safely.
- [ ] Claude Max displays context and built-in rate limits.
- [ ] GLM Coding Plan displays context and GLM quota.
- [ ] Local Gemma displays context only.
- [ ] Metered APIs display context and calculated cost when pricing is known.
- [ ] Unknown prices and failed quota calls degrade gracefully.
- [ ] No git information is displayed.
- [ ] No secrets are logged, copied, or persisted.
- [ ] Validation commands are documented.
- [ ] Tests or documented fixture commands run without real secrets.
- [ ] Cache/config paths are documented.
- [ ] Final output examples match the user preferences in this task.

## Notes for Implementing Agent

Each previous phase may have been implemented by a separate agent. Read the current code before changing it. Preserve working behavior from earlier phases and make the final entrypoint cohesive rather than introducing parallel scripts with divergent behavior.
