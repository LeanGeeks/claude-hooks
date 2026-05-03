# Status Line Usage Diagnostic Summary

**Date:** 2026-05-03  
**Provider tested:** DeepSeek (api billing, `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`)  
**Limitation:** Fixtures are constructed from existing hand-crafted `fixtures/deepseek-api.json` and
`claude-early.json` — a real `claude-ds` session was not run. The conclusions in questions 1–3 are
based on observed fixture data and are treated as **working assumptions**, not live measurements.
Enable `CC_STATUS_DIAGNOSTIC=1` during a real session to verify and upgrade these conclusions.

---

## Required Questions

### 1. Is `context_window.current_usage` per-call, cumulative, or something else?

**Working assumption: cumulative integer.**

Both existing fixtures use a plain integer for `current_usage` (`null` before first call, `220000`
after). This is consistent with total context occupancy that grows as the conversation accumulates
turns. It is not a per-call delta.

**For task 06-03b:** treat `current_usage` as cumulative. Compute cost from the delta
`current_usage_new − current_usage_prev` rather than summing raw values across renders.

**Upgrade path:** if a live diagnostic JSONL shows `current_usage` as a nested object (see Q3), the
dedupe strategy changes — see the Deduplication section below.

### 2. Does the same `current_usage` repeat across multiple status line renders?

**Working assumption: yes, identical values repeat between API calls.**

The status line receives a frozen snapshot; `current_usage` does not change until a new API call
is made. Confirmed by the `deepseek-repeated-render-same-call.json` fixture (values identical to
`deepseek-after-call-1.json`).

**Implication for task 06-03b:** summing raw `current_usage` across renders double-counts. The
engine must skip a render whose `current_usage` equals the previously stored value.

### 3. Which token bucket fields are present in `current_usage`?

**Working assumption: single integer only — no per-bucket breakdown.**

Both fixtures store `current_usage` as a plain integer (total context tokens). The nested object
form `{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` was not
observed in any existing fixture.

**Consequence for task 06-03b:** per-bucket pricing (separate input/output/cache rates) cannot be
applied from `current_usage` alone. Two viable paths:

| Path | What to implement |
|---|---|
| **A — blended rate** | Price the integer delta at a single blended per-token rate. Supported now. Requires a `blended_per_million` field in pricing config alongside (or instead of) the four bucket rates. |
| **B — block and wait** | Render `cost ?` for all API providers until a live diagnostic confirms per-bucket fields exist. Safe but shows nothing useful. |

**Recommendation: implement Path A** with a `blended_per_million` fallback in the pricing schema.
When per-bucket fields are confirmed live, add exact pricing on top without breaking Path A.

### 4. Are cache token fields present?

**Working assumption: not present** in any fixture inspected.

Unknown until a live run with a provider that reports prompt-caching usage. The diagnostic mode
records the full `current_usage` value regardless of shape, so a live run will surface any cache
fields automatically.

**For task 06-03b:** leave `cache_write_per_million` and `cache_read_per_million` in the pricing
schema for forward compatibility, but do not block cost display on their presence. If only a
blended integer is available, use `blended_per_million`.

### 5. Does `cost.total_cost_usd` appear for custom Anthropic-compatible API providers?

**Observed: absent.** The `cost` key is missing from all existing fixtures; the diagnostic records
it as `null`.

`cost.total_cost_usd` is `null` (or absent) for custom API providers. Claude Code has no access to
per-provider pricing, so it cannot compute cost autonomously.

**For task 06-03b:** do not use `cost.total_cost_usd` as a fallback. Compute cost entirely within
the status line script from token deltas × pricing config rates.

### 6. Does `session_id` remain stable across repeated renders in one Claude session?

**Working assumption: yes.** `session_id` is present as a string in all fixtures (including before
the first API call, per `claude-early.json`). It identifies the session and should be constant until
the session ends.

The diagnostic JSONL file is keyed on a short SHA-256 hash of `session_id`, so all renders within
one session append to the same file. The cost state file should use the same key scheme.

Fallback `"no-session"` key fires only if `session_id` is completely absent from the payload.

### 7. Are there fields that can identify a single API response uniquely?

**Observed: none.** No `request_id`, `turn_id`, or equivalent was found.

The only change-detection proxy is `context_window.current_usage`: if its value increases, a new
API call occurred. Deduplication must rely on this value-level change.

---

## Observed Field Names

All `context_window` fields seen across existing fixtures:

| Field | Type | Notes |
|---|---|---|
| `context_window.used_percentage` | number \| null | Context occupancy 0–100 |
| `context_window.remaining_percentage` | number \| null | 100 minus used |
| `context_window.current_usage` | integer \| null | Total context tokens, cumulative |
| `context_window.context_window_size` | integer | Model's max context window |

No nested `current_usage` object was observed. No per-bucket token fields were observed.

---

## Deduplication Strategy for Task 06-03b

`current_usage` is a cumulative integer and repeated renders emit the same value. The recommended
strategy:

1. On each render, read `current_usage` from the status payload.
2. Read last-seen `current_usage` from the session state file
   (`~/.cache/claude-statusline/cost-{session_key}.json`).
3. If `current_usage` is null or equals last-seen → **skip**; display accumulated cost unchanged.
4. If `current_usage` has increased → **delta = new − prev**; apply blended rate; add to
   accumulated cost; write new state.
5. If no last-seen state exists (first render with non-null usage) → treat prev as 0.
6. Write state atomically.

**If a live run later reveals a per-bucket object:** switch to bucket-level deltas per bucket,
accumulate each bucket separately, apply per-bucket prices. The state file already has per-bucket
fields (`input`, `output`, `cache_write`, `cache_read`) ready for this path.

---

## Diagnostic Mode

Enable with:

```bash
CC_STATUS_DIAGNOSTIC=1 claude-ds
```

Records are appended to:

```
~/.cache/claude-statusline/diagnostics/{session_key}.jsonl
```

Each record contains: `timestamp`, `session_key`, `provider`, `billing`, `model`,
`context_window`, `cost`. No workspace paths, prompts, completions, or tokens are stored.

To run against an existing fixture:

```bash
CC_STATUS_DIAGNOSTIC=1 \
  ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
  ANTHROPIC_MODEL='deepseek-v4-pro[500k]' \
  cat .claude/statusline/fixtures/deepseek-api.json | python3 .claude/statusline/statusline.py
```
