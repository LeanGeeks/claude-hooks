# Epic 14 — Context usage MCP server

**Status:** done · **Owner:** Anton · **Created:** 2026-07-19 · **Rev:** 2

## 1. Problem & thesis

Claude Code has no awareness of its own context window consumption during a
session. It cannot pace itself, warn the user, or adjust strategy (e.g.,
summarize early, avoid large tool outputs) based on remaining capacity. The
information exists in session transcript JSONL files but is not surfaced.

**Thesis:** a lightweight MCP server exposes a single tool (`get_context_usage`)
that reads the current session's transcript JSONL, computes token totals, and
returns a structured summary. Claude can call it on demand — at the start of
complex tasks, before large reads, or whenever strategy depends on remaining
capacity.

## 2. Data source

Session transcripts live at:
```
~/.claude/projects/<encoded-project-path>/<session-id>.jsonl
```

Each `type: "assistant"` entry carries a `message.usage` block:
```json
{
  "input_tokens": 3,
  "cache_creation_input_tokens": 9436,
  "cache_read_input_tokens": 21380,
  "output_tokens": 1535
}
```

**Effective context fill** for the latest API call =
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
(this is what was sent to the model on that turn — the full conversation size).

**Critical:** a single API response is split across multiple JSONL lines (one
per content block), all sharing the same `message.id`. Must deduplicate by
`message.id` to avoid double-counting.

The model name (e.g., `claude-opus-4-6`) is in `message.model` — no context
window field exists, so a lookup table maps model → max tokens.

## 3. Identifying the current session

**Resolved.** Claude Code sets these env vars in all child processes (including
stdio MCP servers):

| Variable | Example value |
|----------|--------------|
| `CLAUDE_CODE_SESSION_ID` | `cd679f39-8b4c-41b5-abf3-87c552253b1a` |
| `CLAUDECODE` | `1` |
| `CLAUDE_CODE_ENTRYPOINT` | `cli` |
| `CLAUDE_CODE_EXECPATH` | `/home/anton/.local/share/claude/versions/2.1.214` |
| `CLAUDE_EFFORT` | `high` |
| `CLAUDE_PID` | `1968622` |
| `PWD` | `/data/sync/work/leangeeks-ai/claude-hooks` |

Additionally, the MCP docs mention `CLAUDE_PROJECT_DIR` for MCP servers specifically
(not observed in Bash tool env, but may be MCP-only).

**JSONL path formula** (verified working):
```
~/.claude/projects/{encode(PWD)}/{CLAUDE_CODE_SESSION_ID}.jsonl
```
where `encode()` replaces `/` with `-` (leading slash becomes leading `-`).

**Fallback** if env vars are missing: most recently modified `.jsonl` in the
project directory.

## 4. Tool interface

### `get_context_usage`

**Parameters:** none (the server auto-detects the current session)

**Returns:**
```json
{
  "model": "claude-opus-4-6",
  "context_window": 1000000,
  "latest_turn": {
    "input_tokens": 3,
    "cache_creation_input_tokens": 42000,
    "cache_read_input_tokens": 115000,
    "effective_context_tokens": 157003,
    "output_tokens": 2100
  },
  "cumulative_output_tokens": 48500,
  "fill_percent": 15.7,
  "remaining_tokens": 842997,
  "turns_count": 14,
  "session_id": "cd679f39-..."
}
```

Key fields:
- `context_window` — resolved via §5 chain (env var → bundled map → fallback)
- `fill_percent` — percentage of context window used on the most recent turn
  (the number Claude cares about for pacing decisions)
- `remaining_tokens` — headroom before the window is full
- `cumulative_output_tokens` — total output generated this session (useful for
  cost awareness)
- `latest_turn` — raw token breakdown for the most recent API call

## 5. Model → context window mapping

Resolution order (first match wins):

### 5a. Env var `CONTEXT_WINDOW_MAP`

Comma-separated `pattern=tokens` pairs. Supports glob patterns (`*`).
Set per-profile (epic 13) to cover third-party models without code changes.

```bash
CONTEXT_WINDOW_MAP="glm-*=131072,kimi-*=131072,deepseek-*=131072"
```

### 5b. Bundled `models.json`

Ships with the MCP server. Covers Claude models and any known third-party
models. Easy to update without code changes.

```json
{
  "claude-opus-4-*":   1000000,
  "claude-sonnet-4-*": 1000000,
  "claude-sonnet-5*":  1000000,
  "claude-fable-5*":   1000000,
  "claude-haiku-4-*":  200000
}
```

### 5c. Fallback

If the model matches nothing: **1,000,000** (conservative — underreporting
fill% is safer than overreporting and triggering premature summarization).

### Notes

- The `message.model` field on each JSONL entry identifies the model used for
  that turn. Context window is resolved for the model on the **latest** turn
  (model overrides can change mid-session).
- Known third-party models at time of writing: GLM 4.7, GLM 5.1, GLM 5.2,
  Kimi 2.7 Code, Kimi 3. Their context windows should be added to
  `models.json` once confirmed.

## 6. Implementation plan

| # | Task | Details |
|---|------|---------|
| 1 | Env discovery | Verify what env vars Claude Code passes to MCP servers (session ID, project path, etc.) |
| 2 | JSONL parser | Read transcript, deduplicate by `message.id`, extract latest usage |
| 3 | MCP server scaffold | Stdio-based MCP server (Python, using `mcp` SDK), single tool |
| 4 | Session detection | Find current session JSONL (env var → fallback to mtime) |
| 5 | Tool implementation | Wire parser + session detection → structured response |
| 6 | Registration | Add to `.claude/settings.json` mcpServers config |
| 7 | Testing | Manual verification in a live session |

## 7. Technology choices

- **Language:** Python (consistent with relay-server, `mcp` SDK available)
- **MCP transport:** stdio (simplest, no daemon needed)
- **Dependencies:** `mcp` Python SDK, stdlib (`json`, `pathlib`, `os`)
- **Location:** `context-mcp/` at repo root (or `mcp-servers/context-usage/`)

## 8. Open questions

1. ~~Does Claude Code set session/project env vars for MCP servers?~~
   **Resolved:** yes — `CLAUDE_CODE_SESSION_ID` + `PWD` (and possibly
   `CLAUDE_PROJECT_DIR`). See §3.
2. Should the tool also expose per-turn history (token growth over time) or just
   the latest snapshot? Start with latest-only, extend if useful.
3. Should there be a companion hook (PostToolUse) that auto-calls the tool at
   thresholds — or leave that as a future enhancement? (Start with MCP-only.)
4. Naming: `context-usage-mcp` vs embedding in an existing server?

## 9. Out of scope (for now)

- Automatic threshold injection via hooks (future enhancement)
- Cost calculation (exists in task 06-03 cost engine)
- Subagent token tracking (only the main session)
- Historical session comparison
