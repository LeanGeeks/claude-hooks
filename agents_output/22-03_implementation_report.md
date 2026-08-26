# Implementation Report - 22-03 Permissions MCP: read + decide

## Summary

Added a `permissions` MCP server (context-mcp pattern: uv single-file script,
registered user-scoped by the installer) exposing three read tools and one
guarded, tiered decide tool. The tier comes from one shared `classify(row)`
helper that re-runs the *imported* `BashPermissionValidator` against the row's
`cwd` settings (invariant 2 / H5); the D5 guard is row-shaped
(`row.session_id == caller AND row.agent_id is None` is the only refusal); the
decide write is exactly `decision={"action": ...}` + `resolution_source="agent"`
+ `actor_agent`, the shape 22-02's wait loop adopts. A general
`get_requests(states=None, since=None)` reader was added to
`permission_state_store.py` so nothing outside that module parses the JSONL.

Tests: **942 → 984** (42 new), all green. The installer was **not** run (see
Blockers); in its place the real MCP server was driven over stdio by an MCP
client and the literal output is reproduced below.

## Files Created

- `permissions-mcp/server.py` — the MCP server. uv script header
  (`mcp>=2.0,<3`), `MCPServer("permissions", version="1.0.0")`, four
  `@mcp.tool()` registrations, caller identity resolved **once at startup** from
  the process env. Tool descriptions carry the H3 wording ("records a decision",
  not "runs the tool"), the H4 note (classification may lag a settings edit by
  ≤60 s), and both guard rules.
- `permissions-mcp/permissions_mcp_lib.py` — all logic: caller identity,
  `classify`, `guard_row`, the read tools, `decide_permission_request`.
  Imports the hook modules from `<repo>/.claude/hooks` (`CLAUDE_HOOKS_REPO`
  first, path-relative fallback).
- `tests/test_unit_permissions_mcp.py` — 42 tests (all 10 required cases plus
  the guard/tier/reader edges).

## Files Modified

- `.claude/hooks/permission_state_store.py` — **only** the §2 reader:
  `get_requests(states=None, since=None)`, under the module's existing
  `_acquire_lock`/`_release_lock` protocol. Accepts `RequestState` members or
  raw values; `since` filters on the row's latest activity
  (`resolved_at` → `updated_at` → `created_at`). Pure reader — it deliberately
  does **not** mark lapsed pending rows expired the way `get_request` does, and
  says so in the docstring. Nothing else in the file was touched.
- `install-claude-config.sh` — registers `permissions` in `~/.claude.json`
  beside `context-usage` (`:762-784` pattern, same `UV_AVAILABLE` gate, same
  `.tmp` + `jq empty` validation + rollback), with
  `env: {"CLAUDE_HOOKS_REPO": "$SCRIPT_DIR"}`. Also: the "uv not found" warning
  now names both servers, and the final summary prints a
  `MCP server (permissions): installed / not installed` line.
- `.claude/settings.json` — four `mcp__permissions__*` entries added to
  `permissions.allow`, right after `mcp__context-usage__get_context_usage`
  (D4: the decide grant is deliberate and global; the tier is the cap).
  Step 5's jq lifts `.permissions.allow` wholesale into the global file, so they
  propagate on the next installer run — verified by reading `:639-656`.
- `tests/run_all_tests.py` — registered the new module as
  `unit_permissions_mcp`.

No other files were edited (done criterion 4).

## Verification

- **Compile/import: PASS.**
  `python3 -m py_compile permissions-mcp/server.py permissions-mcp/permissions_mcp_lib.py .claude/hooks/permission_state_store.py tests/test_unit_permissions_mcp.py tests/run_all_tests.py` → clean.
  `bash -n install-claude-config.sh` → clean.
  `permissions_mcp_lib` imports cleanly under plain `python3` (it has no `mcp`
  dependency); `server.py` imports and serves under `uv run --script` (proven
  below).
- **Tests: 984 passed, 0 failed, 1 skipped** (`python3 tests/run_all_tests.py`,
  31.4 s). Baseline before this task was 942 passing; 942 + 42 new = 984. The
  one skip is pre-existing. New-module-only run:
  `python3 tests/run_all_tests.py --module unit_permissions_mcp` → `Ran 42 tests … OK`.
- **Installed (`install-claude-config.sh` re-run): NO — deliberately not run**
  (the manager is getting separate approval, because that run also makes
  22-01's deny flip live machine-wide). Consequently the MCP is **not** yet
  registered in `~/.claude.json`, and the `mcp__permissions__*` allow entries
  are **not** yet in the global `~/.claude/settings.json` (invariant 8 / H6).

### Test coverage of the required table

| # | Case | Test |
|---|------|------|
| 1 | own session, `agent_id: None` → refused | `TestDecideGuard.test_own_session_main_agent_row_is_refused` |
| 2 | own session, `agent_id: "abc"` → through | `TestDecideGuard.test_own_session_subagent_row_passes_the_guard` |
| 3 | other session, main-agent row → through | `TestDecideGuard.test_other_session_main_agent_row_passes_the_guard` |
| 4 | ask pattern in the row's cwd | `TestDecideTier.test_ask_pattern_in_the_rows_cwd_refuses_and_names_the_pattern` |
| 5 | redirect escape | `TestDecideTier.test_redirect_escape_refuses_as_human_only` |
| 6 | now deny-matched | `TestDecideTier.test_now_deny_matched_row_refuses_with_predates_wording` |
| 7 | plain not-in-allowlist → written with attribution | `TestDecideWrite.test_plain_not_in_allowlist_row_is_written_with_full_attribution` |
| 8 | already-terminal row | `TestDecideWrite.test_already_terminal_row_is_refused_with_the_resolver_named` |
| 9 | `AskUserQuestion` row | `TestQuestionRows.test_question_row_is_listed_as_a_question_and_never_decidable` |
| 10 | end-to-end with 22-02's loop harness | `TestEndToEndWithWaitLoop.test_mcp_allow_unblocks_a_parked_wait_loop` (+ deny, + a refused-decide-leaves-it-parked negative) |

Case 10 is not a shape assertion: it creates a pending row, parks the real
`permission_request_hook.wait_for_response` on the relay path with
`wait_for_relay_answer` patched to call the **MCP's own `decide` function**
mid-chunk, and asserts the loop returns `{"action": "allow"}` verbatim, that
`build_output_decision` turns it into `{"behavior": "allow"}`, and that the
Telegram message was PATCHed with `🤖 allow by agent <actor>` and then cancelled.

Beyond the table: fail-closed identity (read works / decide refuses), reason
required, unknown action, unknown id, lost-the-race, tier-uses-the-row's-cwd-
not-the-caller's, list-and-decide-agree (the §4 one-classifier property),
whole-tool ask patterns for non-Bash rows, bare `Bash` vs `Bash(...)` ask
entries, workspace prefix filter, explicit state filter, history join +
`include_auto_denied`, and five `get_requests` reader cases.

### Substitute evidence for the blocked criteria

Since I could not run the installer (and so could not do criterion 2's "with the
server registered, `claude` in this workspace …"), I drove **the real
`permissions-mcp/server.py`** over stdio with a real MCP client
(`uv run --script`, `stdio_client` + `ClientSession`), against a scratch state
store, with three fabricated pending rows. This exercises the actual uv script,
the actual `mcp` dependency resolution, the actual tool registration and the
actual startup identity resolution — everything except Claude Code being the
client. Literal output:

```
SEEDED: {"worker_row": "c168adf8d679", "own_row": "30ef7173acef", "question_row": "b27f30940df4"}
== tools ==
 - list_permission_requests
 - get_permission_request
 - permission_history
 - decide_permission_request

== list_permission_requests({"state": "pending", "workspace": "/data/sync/work/leangeeks-ai/claude-hooks"}) ==
{
  "state_filter": "pending",
  "workspace_filter": "/data/sync/work/leangeeks-ai/claude-hooks",
  "count": 3,
  "requests": [
    {
      "request_id": "b27f30940df4",
      "session_id": "worker-session-aaaa",
      "agent_id": null,
      "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
      "tool_name": "AskUserQuestion",
      "kind": "question",
      "state": "pending",
      "created_at": "2026-08-26T11:51:38.214190+00:00",
      "expires_at": "2026-08-26T12:01:38.214198+00:00",
      "decidable": false,
      "decidable_reason": "questions are answered by humans; see brd §3.2",
      "matched_patterns": [],
      "caller_guard": "ok"
    },
    {
      "request_id": "30ef7173acef",
      "session_id": "demo-session-1234",
      "agent_id": null,
      "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
      "tool_name": "Bash",
      "kind": "permission",
      "state": "pending",
      "created_at": "2026-08-26T11:51:38.214097+00:00",
      "expires_at": "2026-08-26T12:01:38.214112+00:00",
      "decidable": true,
      "decidable_reason": "agent-decidable: Not in allowlist — review before approving: `deploy-thing --now`",
      "matched_patterns": [],
      "command": "deploy-thing --now",
      "caller_guard": "refused: you may not decide your own session's main-agent request (session demo-session-1234, agent_id null). A parent may decide a subagent's row (one that carries an agent_id), and any row from another session."
    },
    {
      "request_id": "c168adf8d679",
      "session_id": "worker-session-aaaa",
      "agent_id": null,
      "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
      "tool_name": "Bash",
      "kind": "permission",
      "state": "pending",
      "created_at": "2026-08-26T11:51:38.213906+00:00",
      "expires_at": "2026-08-26T12:01:38.213940+00:00",
      "decidable": true,
      "decidable_reason": "agent-decidable: Not in allowlist — review before approving: `deploy-thing --now`",
      "matched_patterns": [],
      "command": "deploy-thing --now",
      "caller_guard": "ok"
    }
  ]
}

== decide_permission_request({"request_id": "30ef7173acef", "action": "allow", "reason": "trying to approve my own request"}) ==
{
  "ok": false,
  "refused": true,
  "reason": "refused: you may not decide your own session's main-agent request (session demo-session-1234, agent_id null). A parent may decide a subagent's row (one that carries an agent_id), and any row from another session.",
  "request_id": "30ef7173acef"
}

== decide_permission_request({"request_id": "b27f30940df4", "action": "allow", "reason": "answering a question"}) ==
{
  "ok": false,
  "refused": true,
  "reason": "refused: questions are answered by humans; see brd §3.2",
  "request_id": "b27f30940df4",
  "matched_patterns": [],
  "tier": "human-only"
}

== decide_permission_request({"request_id": "c168adf8d679", "action": "allow", "reason": "the deploy worker is blocked; command is a workspace script"}) ==
{
  "ok": true,
  "request_id": "c168adf8d679",
  "action": "allow",
  "state": "allow",
  "decision": {
    "action": "allow"
  },
  "resolution_source": "agent",
  "actor_agent": "demo-session @ claude-hooks",
  "reason": "the deploy worker is blocked; command is a workspace script",
  "tier": "agent-decidable: Not in allowlist — review before approving: `deploy-thing --now`",
  "status": "decision recorded; the waiting hook applies it within ~30 s. This tool does not run the tool call — if the waiting hook is gone (session killed, machine slept) nothing runs and the row just reads as decided."
}

== decide_permission_request({"request_id": "c168adf8d679", "action": "deny", "reason": "second attempt on an already-decided row"}) ==
{
  "ok": false,
  "refused": true,
  "reason": "refused: request c168adf8d679 is no longer pending — state 'allow' via agent (agent demo-session @ claude-hooks) at 2026-08-26T11:51:40.811258+00:00",
  "request_id": "c168adf8d679",
  "state": "allow",
  "resolution_source": "agent",
  "actor_agent": "demo-session @ claude-hooks",
  "actor_user_id": null
}

== get_permission_request({"request_id": "c168adf8d679"}) ==
{
  "request": {
    "request_id": "c168adf8d679",
    "session_id": "worker-session-aaaa",
    "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
    "tool_name": "Bash",
    "tool_input": { "command": "deploy-thing --now" },
    "permission_suggestions": [ "Bash(deploy-thing:*)" ],
    "state": "allow",
    "created_at": "2026-08-26T11:51:38.213906+00:00",
    "updated_at": "2026-08-26T11:51:40.811258+00:00",
    "expires_at": "2026-08-26T12:01:38.213940+00:00",
    "telegram_message_id": null,
    "decision": { "action": "allow" },
    "reply_text": null,
    "actor_user_id": null,
    "actor_agent": "demo-session @ claude-hooks",
    "resolution_source": "agent",
    "resolved_at": "2026-08-26T11:51:40.811258+00:00",
    "expired_notified_at": null,
    "agent_id": null,
    "role": null,
    "terminal_answers": null
  },
  "classification": {
    "decidable": true,
    "tier_reason": "agent-decidable: Not in allowlist — review before approving: `deploy-thing --now`",
    "matched_patterns": []
  },
  "caller_guard": "ok"
}

== permission_history({"days": 1, "workspace": "/data/sync/work/leangeeks-ai/claude-hooks"}) ==
{
  "since": "2026-08-25T11:51:40.816115+00:00",
  "days": 1.0,
  "workspace_filter": "/data/sync/work/leangeeks-ai/claude-hooks",
  "include_auto_denied": true,
  "requests": [
    {
      "request_id": "c168adf8d679",
      "session_id": "worker-session-aaaa",
      "agent_id": null,
      "cwd": "/data/sync/work/leangeeks-ai/claude-hooks",
      "tool_name": "Bash",
      "command": "deploy-thing --now",
      "state": "allow",
      "decision": { "action": "allow" },
      "resolution_source": "agent",
      "actor_agent": "demo-session @ claude-hooks",
      "actor_user_id": null,
      "created_at": "2026-08-26T11:51:38.213906+00:00",
      "resolved_at": "2026-08-26T11:51:40.811258+00:00"
    }
  ],
  "request_count": 1,
  "confirm_log": [],
  "confirm_log_count": 0
}
```

The audit log the same run produced (`permission_actions.jsonl`) — the store's
own transition entry plus the reason entry:

```json
{"timestamp":"2026-08-26T11:51:40.811396+00:00","request_id":"c168adf8d679","action":"allow","actor_user_id":null,"previous_state":"pending","new_state":"allow","details":{"decision":{"action":"allow"}},"actor_agent":"demo-session @ claude-hooks"}
{"timestamp":"2026-08-26T11:51:40.811497+00:00","request_id":"c168adf8d679","action":"agent_decision","actor_user_id":null,"previous_state":"pending","new_state":"allow","details":{"decision":{"action":"allow"},"reason":"the deploy worker is blocked; command is a workspace script","resolution_source":"agent","tier":"agent-decidable: Not in allowlist — review before approving: `deploy-thing --now`","matched_patterns":[],"caller_session_id":"demo-session-1234"},"actor_agent":"demo-session @ claude-hooks"}
```

Fail-closed identity, same server, started **without** `CLAUDE_CODE_SESSION_ID`:

```
== list (no session id) ==
count: 2

== decide (no session id) ==
{
  "ok": false,
  "refused": true,
  "reason": "refused: this MCP server has no CLAUDE_CODE_SESSION_ID, so it cannot identify its caller and cannot enforce the self-decision guard (epic 22 invariant 3). Read tools still work; decisions do not.",
  "request_id": "30ef7173acef"
}
```

And the installer's registration jq, dry-run against a scratch `~/.claude.json`
containing an existing `context-usage` entry and an unrelated key — both
preserved, `permissions` added with `CLAUDE_HOOKS_REPO`:

```json
{
  "mcpServers": {
    "context-usage": { "type": "stdio", "command": "uv", "args": ["run","--script","/x/context-mcp/server.py"], "env": {} },
    "permissions": {
      "type": "stdio",
      "command": "uv",
      "args": ["run","--script","/data/sync/work/leangeeks-ai/claude-hooks/permissions-mcp/server.py"],
      "env": { "CLAUDE_HOOKS_REPO": "/data/sync/work/leangeeks-ai/claude-hooks" }
    }
  },
  "other": "preserved"
}
```

Driver scripts live in the session scratchpad
(`…/scratchpad/mcpdemo/{seed.py,drive.py,drive_noid.py}`), not in the repo.

## Decisions

1. **Two files, not one: `server.py` + `permissions_mcp_lib.py`.** The task
   allowed either. `mcp` is not installed in the system interpreter (only inside
   uv's environment), so a single-file server could not be imported by
   `tests/run_all_tests.py` at all. Splitting keeps every behaviour under the
   repo's plain-`python3` suite while `server.py` stays a registration shell —
   each tool is a one-line delegation, so what is tested is what runs. The
   stdio round-trip above is what covers the shell itself.

2. **The tier keys off the fresh validator's *structured* output, not its
   prose.** `classify` calls `validator.validate_bash_command(command)` and
   branches on that run's `decision` plus the per-sub-command
   `matched_deny_patterns` / `matched_ask_patterns`. That is strictly stronger
   than string-matching 22-01's `"Matches a denied pattern: "` /
   `"Matches an ask pattern: "` prefixes, and it is why the refusals can name
   the exact patterns. The **one** branch with no structured marker is the
   redirect escape (the validator reports it as an `ask` with no matched
   patterns), so that single case is told apart by the fresh reason's
   `"Redirects output outside the workspace"` prefix. Nothing anywhere reads the
   row's *stored* reason. A test (`test_classifier_is_the_imported_validator`)
   patches `BashPermissionValidator.validate_bash_command` and asserts the tier
   moves with it — which only holds if there is exactly one classifier.

3. **Whole-tool ask patterns are resolved by pattern-head parsing, not a second
   matcher.** `_matches_pattern` only understands `Bash(...)` forms matched
   against a command string, so two forms are outside its reach: the bare `Tool`
   form (`"Bash"`, `"WebFetch"` — "always prompt for this tool") and, for
   non-command tools, `Tool(arg…)` whose argument only Claude Code can evaluate
   natively. `_tool_level_ask_matches` reads the pattern's head and refuses
   whenever an ask entry targets the row's tool. Both go in the *safe*
   direction (human-only), and for Bash/Monitor rows the parenthesised forms are
   deliberately left to the validator — tested in both directions
   (`test_bare_bash_ask_pattern_refuses_every_command_row` and
   `test_parenthesised_bash_ask_pattern_is_left_to_the_validator`).

4. **The reason reaches the audit log as its own `agent_decision` entry.**
   `update_request_state` builds its own audit entry with
   `details={'decision': decision}` and has no channel for free text, and the
   decision dict itself must stay exactly `{"action": ...}` (that is 22-02's
   contract — extra keys were not risked). Widening `update_request_state`'s
   signature was out of scope (the store may gain "the §2 reader only"). So
   after a successful write, `decide` appends one extra entry through the store
   module's own audit writer (`AuditEntry` + `_append_audit_log`, invariant 6 —
   no bespoke JSONL writer) carrying `reason`, the tier verdict, the matched
   patterns and the caller's session id. Both entries carry `actor_agent`.
   If a public audit helper is wanted later, this is the one line to move.

5. **`decidable` is the tier; the guard is reported separately as
   `caller_guard`.** §4 defines `classify(row)` as tier-only (it cannot depend on
   who is asking), so `list`'s `decidable` is exactly that. But an orchestrator
   listing its own main-agent rows would otherwise see `decidable: true` and get
   refused, so each row also carries a `caller_guard` field: `"ok"` or the
   guard's refusal text. No extra classification work; pure UX.

6. **`include_auto_denied` covers both "nobody decided this" classes**: store
   rows with `resolution_source == "timeout"` (the TTL auto-deny / expiry) and
   `bash_manual_confirm.log` entries with `decision == "deny"` (D1 hard-denies
   that never became a prompt). Default `True` — the reviewer wants exactly
   those, they are the H1 false-positive signal.

7. **`get_requests` is a pure reader.** Unlike `get_request` /
   `get_all_pending_requests` it does not mark lapsed pending rows expired as a
   side effect — a review feed should not mutate the store it is reading, and
   the reaper already owns expiry. Documented in the docstring; `list`'s default
   `state="pending"` still goes through `get_all_pending_requests`, so the
   expiry side effect is preserved on the path that had it. A row whose
   timestamp is unparseable is **kept** by `since`, not silently dropped
   (dropping would hide exactly the anomalies a reviewer is hunting).

8. **Fail-closed on missing identity — the one deliberate exception to this
   repo's fail-open convention** (task §1 mandates it). No
   `CLAUDE_CODE_SESSION_ID` ⇒ the three read tools work normally and `decide`
   refuses every call, because a guard that cannot identify its caller cannot
   enforce invariant 3. Everything else in the module stays fail-open in the
   usual direction, and note the two *classifier* failure paths also fail
   closed on purpose: if the row's settings cannot be loaded, or the validator
   raises on the command, the row is classified **human-only** rather than
   agent-decidable.

9. **`CLAUDE_HOOKS_REPO` resolves the hooks directory when set**, with the
   path-relative fallback (`<this file>/../.claude/hooks`) otherwise. The task
   only required the installer to export it for 22-04; using it here also makes
   a relocated checkout work, and costs one function.

10. **Workspace filter is a `cwd` prefix match**, as the task specifies until
    22-04's `resolve_project_key` lands (paths normalized with `abspath` +
    `expanduser`, matching the directory itself or anything under it).

## Blockers

Two done criteria could not be fulfilled, both for the same reason, and both
were explicitly deferred by the manager in my brief.

1. **Done criterion 3 — "Registration + allowlist entries land via
   `./install-claude-config.sh`; name the run."**
   - *Missing:* permission to run the installer. I was instructed: "DO NOT RUN
     `./install-claude-config.sh`" — the manager is asking the user separately,
     because that run also makes 22-01's deny flip live machine-wide.
   - *State:* the installer edits and the `.claude/settings.json` entries are
     written and verified statically (bash syntax check; the registration jq
     dry-run above against a scratch `~/.claude.json`; Step 5's permissions jq
     read at `:639-656` to confirm the four `mcp__permissions__*` entries
     propagate to the global file). Nothing is live: `~/.claude.json` has no
     `permissions` server and `~/.claude/settings.json` has no
     `mcp__permissions__*` entries (invariant 8 / H6).
   - *To resolve:* one `./install-claude-config.sh` run, once the user approves
     the coupled 22-01 deny flip. Expect two new log lines:
     `MCP server registered in ~/.claude.json: permissions (…)` and
     `- MCP server (permissions): installed`.

2. **Done criterion 2 — "Manual: with the server registered, `claude` in this
   workspace lists a fabricated pending request, decides it, and the row + audit
   log show the attribution. Report literal tool output."**
   - *Missing:* the registration from blocker 1 — Claude Code only sees the
     server after `~/.claude.json` carries it, and a running session needs a
     restart to pick it up.
   - *Substitute performed (not a downgrade claim — the criterion is still
     open):* the same four tools were driven against **the real server process**
     over stdio by a real MCP client, and the literal output, the resulting
     store row and both audit-log lines are reproduced in Verification above.
     What that does **not** cover is Claude Code as the client: the
     `mcp__permissions__*` allowlist entries actually suppressing prompts, and
     the real `CLAUDE_CODE_SESSION_ID`/`CLAUDE_PROJECT_DIR` values Claude Code
     injects.
   - *To resolve:* after the installer run, restart a session in this workspace
     and repeat the sequence through Claude Code's own tool calls. Note that
     criterion 2's "decides it" needs a row from *another* session (or one with
     an `agent_id`) — the guard correctly refuses a session deciding its own
     main-agent row, as the demo output shows.

Everything else in the task file is implemented and tested.

## Questions for User

None.
