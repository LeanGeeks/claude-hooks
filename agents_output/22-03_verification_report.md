# 22-03 Verification Report

**Date:** 2026-08-26  
**Agent role:** Verification only — no source files changed.

---

## Criterion 3 — Registration + allowlist landed via the installer

**Verdict: MET**

### `~/.claude.json` MCP entry

```json
{
  "type": "stdio",
  "command": "uv",
  "args": [
    "run",
    "--script",
    "/data/sync/work/leangeeks-ai/claude-hooks/permissions-mcp/server.py"
  ],
  "env": {
    "CLAUDE_HOOKS_REPO": "/data/sync/work/leangeeks-ai/claude-hooks"
  }
}
```

Retrieved by: `python3 -c "import json; d=json.load(open('/home/anton/.claude.json')); print(json.dumps(d['mcpServers']['permissions'], indent=2))"`.

### Four `mcp__permissions__*` entries in `~/.claude/settings.json`

```json
[
  "mcp__permissions__list_permission_requests",
  "mcp__permissions__get_permission_request",
  "mcp__permissions__permission_history",
  "mcp__permissions__decide_permission_request"
]
```

The `permissions.ask` key is also present (empty list: `[]`), indicating the
`install-claude-config.sh` merge ran correctly.

### Installed hook copies match the repo

```
diff ~/.claude/hooks/pretool_hook.py   .claude/hooks/pretool_hook.py          → IDENTICAL
diff ~/.claude/hooks/permission_request_hook.py .claude/hooks/permission_request_hook.py → IDENTICAL
```

Spot grep confirms 22-01's `BashPermissionValidator` is present in
`pretool_hook.py` (lines 343, 419, 1391) and 22-02's agent-decision path
(`wait_for_response`, `_finalize_agent_decision`) is present in
`permission_request_hook.py` (lines 344, 373, 415, 418).

### Server boots in its registered form

```
$ echo '<initialize request>' | CLAUDE_CODE_SESSION_ID=verify-test \
    uv run --script permissions-mcp/server.py
{"jsonrpc":"2.0","id":1,"result":{"capabilities":{...},"protocolVersion":"2024-11-05","serverInfo":{"name":"permissions","version":"1.0.0"}}}
```

The registration is live and the server process starts correctly.

---

## Criterion 2 — Manual end-to-end through a registered `claude` session

**Verdict: MET**

### Setup

Scratch store files were used (isolated from the developer's real store):

- `CLAUDE_PERMISSION_STATE_FILE` → `$SCRATCHPAD/c2_state.jsonl`
- `CLAUDE_PERMISSION_AUDIT_FILE` → `$SCRATCHPAD/c2_audit.jsonl`
- `CLAUDE_PERMISSION_DEBUG_LOG`  → `$SCRATCHPAD/c2_debug.log`

A fabricated pending row was seeded via `permission_state_store.create_request`:

```
session_id: "other-session-xxxx"   ← different from the claude -p session
tool_name:  "Bash"
command:    "frobnicate --dangerously"   ← not in any allowlist
cwd:        /data/sync/work/leangeeks-ai/claude-hooks
request_id: c2040c02b10c
```

The MCP server inherits the env vars from the `claude` process, so the overrides
were visible to the server as spawned by Claude Code.

### `claude -p` invocation and literal tool output

```
claude -p "... Call mcp__permissions__list_permission_requests ... then mcp__permissions__decide_permission_request ..." \
  CLAUDE_PERMISSION_STATE_FILE=… CLAUDE_PERMISSION_AUDIT_FILE=… …
```

**list_permission_requests result (literal):**

```json
{"state_filter": "pending", "workspace_filter": null, "count": 1, "requests": [
  {
    "request_id": "c2040c02b10c",
    "session_id": "other-session-xxxx",
    "agent_id": null,
    "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
    "tool_name": "Bash",
    "kind": "permission",
    "state": "pending",
    "created_at": "2026-08-26T13:12:00.027860+00:00",
    "expires_at": "2026-08-26T14:12:00.027896+00:00",
    "decidable": true,
    "decidable_reason": "agent-decidable: Not in allowlist — review before approving: `frobnicate --dangerously`",
    "matched_patterns": [],
    "command": "frobnicate --dangerously",
    "caller_guard": "ok"
  }
]}
```

**decide_permission_request result (literal):**

```json
{
  "ok": true,
  "request_id": "c2040c02b10c",
  "action": "allow",
  "state": "allow",
  "decision": {"action": "allow"},
  "resolution_source": "agent",
  "actor_agent": "348f1c82-70d @ claude-hooks",
  "reason": "verified by 22-03 verification agent; command is a test stub",
  "tier": "agent-decidable: Not in allowlist — review before approving: `frobnicate --dangerously`",
  "status": "decision recorded; the waiting hook applies it within ~30 s. ..."
}
```

### Resulting state-store row

```json
{
  "request_id": "c2040c02b10c",
  "session_id": "other-session-xxxx",
  "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
  "tool_name": "Bash",
  "tool_input": {"command": "frobnicate --dangerously"},
  "state": "allow",
  "decision": {"action": "allow"},
  "actor_agent": "348f1c82-70d @ claude-hooks",
  "resolution_source": "agent",
  "resolved_at": "2026-08-26T13:12:22.830090+00:00",
  ...
}
```

### Audit log entries

Two entries written (the state-transition entry + the agent-decision-reason entry):

```json
{
  "timestamp": "2026-08-26T13:12:22.830219+00:00",
  "request_id": "c2040c02b10c",
  "action": "allow",
  "actor_user_id": null,
  "previous_state": "pending",
  "new_state": "allow",
  "details": {"decision": {"action": "allow"}},
  "actor_agent": "348f1c82-70d @ claude-hooks"
}
{
  "timestamp": "2026-08-26T13:12:22.830352+00:00",
  "request_id": "c2040c02b10c",
  "action": "agent_decision",
  "actor_user_id": null,
  "previous_state": "pending",
  "new_state": "allow",
  "details": {
    "decision": {"action": "allow"},
    "reason": "verified by 22-03 verification agent; command is a test stub",
    "resolution_source": "agent",
    "tier": "agent-decidable: Not in allowlist — review before approving: `frobnicate --dangerously`",
    "matched_patterns": [],
    "caller_session_id": "348f1c82-70d1-4366-ad6e-480dcbddbbc1"
  },
  "actor_agent": "348f1c82-70d @ claude-hooks"
}
```

**All three required attribution fields are present:** `actor_agent`, `resolution_source: "agent"`, and `reason` in the `agent_decision` audit entry.

### Fabricated data

The scratch files (`c2_state.jsonl`, `c2_audit.jsonl`, `c2_debug.log`) were
deleted after evidence was captured. The developer's real store was not touched.

---

## Defects observed

None. The env-var override path works correctly: the MCP server (spawned by
Claude Code) inherited `CLAUDE_PERMISSION_STATE_FILE` / `CLAUDE_PERMISSION_AUDIT_FILE`
from the `claude` process env and read/wrote the scratch files, not the
developer's real `~/.claude/permission_requests.jsonl`.

---

## Blockers

None.

---

## Working-tree check

`git status` at the end of this run shows only the pre-existing 22-03 uncommitted
changes (modifications to `.claude/hooks/permission_state_store.py`,
`.claude/settings.json`, `install-claude-config.sh`, `tests/run_all_tests.py`,
and untracked `permissions-mcp/`, `tests/test_unit_permissions_mcp.py`,
`tasks/23_async_questions/`). Nothing was added by this verification agent.
