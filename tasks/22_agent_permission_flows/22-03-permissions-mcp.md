# 22-03 — Permissions MCP: read + decide

**Status:** todo · **Depends on:** 22-01 (tier vocabulary), 22-02 (decide
becomes effective)
**Read first:** [brd.md](./brd.md) §2 D3/D4/D5, §4 H3/H4/H5 ·
[state.md](./state.md) invariants 2, 3, 5 · `context-mcp/server.py` (the
pattern to copy) · `install-claude-config.sh:762-784` (registration pattern)

## Goal

The agent-facing surface: a `permissions` MCP server exposing read access to
current and historical permission requests, and a guarded, tiered decide tool.
Covers vision items 1–3; the write tools (items 4–6) are 22-04 and extend this
same server.

## Scope

### 1. Server skeleton — `permissions-mcp/server.py`

Copy the context-mcp shape: a uv single-file script (`# /// script` header,
`mcp>=2,<3`), `MCPServer("permissions", ...)`. It imports the hook modules —
add `.claude/hooks` to `sys.path` relative to the repo root (the server runs
from the checkout, like context-mcp; the installer registers it in place, so
imports track the repo, while on-disk stores are shared with the *installed*
hooks — same JSONL formats by construction since the modules are the same).

Caller identity, resolved once at startup from the server process env (the
context-mcp precedent, `server.py:47-48`): `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_PROJECT_DIR`/`PWD`. Compose
`actor_agent = f"{session_id[:12]} @ {basename(project_dir)}"`.
**Fail closed:** if `CLAUDE_CODE_SESSION_ID` is absent (server run outside a
Claude Code session), the read tools still work but `decide` refuses every
call — a guard that cannot identify its caller cannot enforce invariant 3.

### 2. Read tools

- `list_permission_requests(state="pending", workspace=None)` — rows from the
  state store (`get_all_pending_requests` for the default; for explicit states,
  add a general reader `get_requests(states=None, since=None)` **to
  `permission_state_store.py` itself**, under its lock protocol, rather than
  parsing the JSONL in the server — readers follow the same discipline as
  writers, and `permission_history` reuses it). Optional workspace filter matches on
  `resolve_project_key(row.cwd)` once 22-04 lands; until then, prefix-match on
  `cwd`. Each row returned as compact JSON: `request_id`, `session_id`,
  `agent_id`, `cwd`, `tool_name`, the command (for Bash), `created_at`,
  `expires_at`, and a `decidable` verdict with a reason (see §4 — computed
  here so an orchestrator need not call decide to learn "human-only").
  `AskUserQuestion` rows are listed with `kind: "question"` and
  `decidable: false` ("questions are answered by humans; see brd §3.2").
- `get_permission_request(request_id)` — the full row, plus the live
  classification (§4) with the matched patterns spelled out.
- `permission_history(days=1, workspace=None, include_auto_denied=True)` —
  joins the state store (terminal rows in range) with
  `bash_manual_confirm.log` entries (timestamp-ranged). This is the daily
  reviewer's feed; keep the output structured (JSON lines), not prose.

### 3. Decide tool

`decide_permission_request(request_id, action, reason)` with
`action ∈ {allow, deny, stop}`. Flow:

1. Load the row; refuse non-pending (report who resolved it and how — the
   H3-aware wording: on success say "decision recorded; the waiting hook
   applies it within ~30 s", never "the tool ran").
2. **Guard (invariant 3):** refuse when
   `row.session_id == caller_session and row.agent_id is None` — "you may not
   decide your own session's main-agent request." Same-session rows **with**
   an `agent_id` are decidable (parent→subagent, brd D5). Different sessions
   are decidable.
3. **Tier (invariant 2, brd D3):** re-run `BashPermissionValidator` for the
   row's `cwd` on `tool_input.command` (Bash/Monitor rows). Refuse as
   human-only when the fresh result matches an **ask** pattern or flags a
   **redirect escape**. A fresh **deny** verdict also refuses ("now matches a
   deny pattern — the request predates a settings change; let it expire or
   have a human act"). Non-Bash rows: decidable unless an ask pattern matches
   the tool name (reuse `_matches_pattern` semantics with the `Tool` /
   `Tool(...)` forms); `AskUserQuestion` rows always refuse.
   Note H4 in the tool description: classification may lag a settings edit by
   ≤60 s (SettingsLoader class cache in this long-lived process).
4. Write via `update_request_state(request_id, <state>,
   decision={"action": action}, actor_agent=..., resolution_source="agent")` —
   exactly the dict shape 22-02's loop adopts. `None` return ⇒ lost the race;
   report it honestly.
5. Append the caller's `reason` into the audit entry details. The reason is
   required — an unexplained approval is not auditable.

### 4. Shared classification helper

One function (in the server or a small `permissions_mcp_lib.py` beside it)
that both `list`'s `decidable` field and `decide`'s tier check call:
`classify(row) -> {decidable, tier_reason, matched_patterns}`. It must call the
imported validator — never duplicate matching logic (invariant 2 / brd H5).

### 5. Registration + global allowlist (brd D4)

`install-claude-config.sh`:

- Register `permissions` in `~/.claude.json` beside `context-usage`
  (`:762-784` pattern; same uv-availability gate). Add
  `env: {"CLAUDE_HOOKS_REPO": "$SCRIPT_DIR"}` — 22-04 needs the repo path for
  user-scope targets; harmless now.
- Add to **this repo's** `.claude/settings.json` allow list:
  `mcp__permissions__list_permission_requests`,
  `mcp__permissions__get_permission_request`,
  `mcp__permissions__permission_history`,
  `mcp__permissions__decide_permission_request` (D4: the decide grant is
  deliberate and global; the tier is the cap). The installer's existing
  permissions merge carries them to the global file.

## Testing

Unit-test the guard and tier as pure functions against fabricated rows +
scratch settings dirs (env-var store overrides exist:
`CLAUDE_PERMISSION_STATE_FILE` etc.). Minimum:

| # | Case | Expect |
|---|---|---|
| 1 | caller's own session, `agent_id: None` | refused (guard) |
| 2 | caller's own session, `agent_id: "abc"` | allowed through guard |
| 3 | other session, main-agent row | allowed through guard |
| 4 | row's command matches ask pattern in row's cwd settings | refused human-only, pattern named |
| 5 | row's command has redirect escape | refused human-only |
| 6 | row's command now deny-matched | refused, "predates settings change" wording |
| 7 | plain not-in-allowlist row | decision written; row carries `actor_agent`, source `agent`, dict `{"action": "allow"}` |
| 8 | already-terminal row | refused with resolver named |
| 9 | `AskUserQuestion` row | listed as question, decide refuses |
| 10 | end-to-end with 22-02's loop harness | decide → parked wait loop returns the decision |

## Done criteria

1. Suites green; counts reported.
2. Manual: with the server registered, `claude` in this workspace lists a
   fabricated pending request, decides it, and the row + audit log show the
   attribution. Report literal tool output.
3. Registration + allowlist entries land via `./install-claude-config.sh`;
   name the run (invariant 8 analog: MCP registration is also not live until
   the installer runs).
4. No edits outside `permissions-mcp/`, `permission_state_store.py` (the §2
   reader only), `install-claude-config.sh`, `.claude/settings.json`, and
   tests.
