# Implementation Report - 22-02 External decisions reach the wait loop

## Summary

The state store now carries agent attribution (`actor_agent` on the row and on
the audit entry, plus a `RESOLUTION_SOURCE_AGENT = "agent"` constant), and the
relay-path wait loop adopts a terminal `ALLOW`/`DENY`/`STOP` written into the
store by an external actor — gated strictly on `resolution_source == "agent"`.
When it adopts one, it finalizes the live Telegram message with
`🤖 <action> by agent <actor_agent>` (patch, then cancel) and returns the row's
decision dict unchanged. No agent-facing surface ships here; the change is inert
until 22-03 writes decisions.

## Files Created

None (scratch walkthrough scripts live in the session scratchpad, not the repo).

## Files Modified

- `.claude/hooks/permission_state_store.py`
  - `RESOLUTION_SOURCE_AGENT = "agent"` beside the existing three sources.
  - `PermissionRequest.actor_agent: Optional[str] = None` (placed before
    `resolution_source`; every construction site in repo + tests is
    keyword-based, and `from_dict` filters unknown keys, so old rows load).
  - `AuditEntry.actor_agent: Optional[str] = None`.
  - `update_request_state(..., actor_agent=None)`: persisted with the same
    guarded `is not None` shape as `actor_user_id`, and forwarded to the audit
    entry. No new writer, no second lock — the write happens inside the existing
    `LOCK_EX` read-modify-write block (invariant 6 / H7).
  - Comment on the `resolution_source is None` inference block stating that it
    must never claim an agent decision, and that agent writers always pass the
    source explicitly (otherwise the row would be labelled `telegram` and lose
    its attribution).

- `.claude/hooks/permission_request_hook.py`
  - `wait_for_response`'s per-chunk store check now has a second arm, after the
    unchanged `RESOLVED_TERMINAL` arm:
    `current.decision and current.resolution_source == RESOLUTION_SOURCE_AGENT
    and current.state in _EXTERNALLY_DECIDABLE_STATES` → `_finalize_agent_decision`
    → `return current.decision`.
  - `_EXTERNALLY_DECIDABLE_STATES = {allow, deny, stop}` (raw state values), with
    the comment explaining why `REPLY`/`WHITELIST` are excluded and why the
    source gate (not "any terminal + decision") is the load-bearing part.
  - `_finalize_agent_decision(message_id, row)`: rebuilds the body via
    `render_permission_body` and calls the existing `finalize_message`
    (patch-then-cancel) with `prefix="🤖 "` and answer text
    `"<action> by agent <actor_agent>"`. Wrapped in `try/except` + `debug_log` —
    best-effort and non-fatal, so a dead chat or unreachable relay never costs
    the caller its decision.
  - Long-poll chunk untouched (still `min(25, …)`) — H2 latency accepted.
  - `_wait_state_store_only` untouched: it already returns any terminal decision,
    including an agent-sourced one (proved by test case 5).
  - `_wait_for_group_answers` (AskUserQuestion): docstring-level pointer comment
    only — "no external-decision check here, deliberately" with the brd §3.2 /
    22-02 §4 reference. No behavior change.

- `.claude/hooks/telegram_permission_router.py`
  - Extracted the body composition of `send_permission_message` into a pure
    `render_permission_body(request, workspace_name, session_name) -> str`
    (mirroring the existing `render_question_body`, which exists for exactly this
    reason: "callers that later need to PATCH that message call it again to
    reconstruct the exact body they sent"). `send_permission_message` now calls
    it; the composed text is byte-identical to before. No other change — in
    particular `finalize_message`'s patch-then-cancel order is reused as-is
    rather than duplicated.

- `tests/test_integration_permission_request.py` — new `TestAgentWrittenDecisions`
  (6 tests, real state store, patched relay).
- `tests/test_unit_state_store.py` — new `TestActorAgentField` (4 tests).

## Test cases (the Testing table, all 6 + 2 extra)

| # | Test | Where |
|---|------|-------|
| 1 | `test_relay_loop_adopts_agent_allow_and_finalizes_with_attribution` — agent writes ALLOW *during* a long-poll chunk; loop returns `{"action": "allow"}` on the next chunk; `edit_message_text` then `remove_inline_buttons` recorded in that order on the same message id; patched text contains `🤖 allow by agent sess-abc123 @ workspace`, the request id and the command | integration |
| 2 | `test_relay_loop_adopts_agent_deny_and_finalizes` — DENY returned, `finalize_message` called once with `prefix="🤖 "` and `"deny by agent sess-deadbe @ ws"` | integration |
| 3 | `test_relay_loop_ignores_a_terminal_row_written_by_another_source` — row flipped to `EXPIRED` **carrying a decision** with source `timeout`; the loop does not adopt it, returns `None` at TTL, `finalize_message` never called (the source gate) | integration |
| 4 | `test_human_and_agent_race_resolve_the_request_exactly_once` — real store, both orderings: (a) relay answer wins → the agent's later `update_request_state` returns `None`, row stays `allow`/`telegram`/`actor_agent=None`; (b) agent wins → the loop adopts it once and the human's later `_mark_relay_resolved` is refused, row stays `allow`/`agent` with the agent's decision | integration |
| 5 | `test_state_store_only_path_returns_an_agent_written_allow` — `_wait_state_store_only` returns the agent-written decision unchanged | integration |
| 6 | `test_row_written_before_this_change_loads_with_actor_agent_none` (+ `test_audit_entry_carries_actor_agent`) — a legacy row without `actor_agent` loads via `from_dict` and through the JSONL store; a new agent write onto that same old row attributes correctly; the audit entry for an agent write carries `actor_agent` and a `None` `actor_user_id` | unit_state |
| + | `test_reply_and_whitelist_are_not_externally_decidable` — asserts `_EXTERNALLY_DECIDABLE_STATES == {"allow","deny","stop"}` | integration |
| + | `test_human_write_leaves_actor_agent_untouched` — guarded write: a telegram write passing no `actor_agent` neither invents nor blanks one | unit_state |

The fixed decision shape `{"action": "allow"}` / `{"action": "deny"}` — what 22-03
must write, what `_ACTION_TO_STATE` maps and what `build_output_decision`
consumes — is asserted in cases 1, 2, 4 and 5; case 1 additionally round-trips the
returned decision through `build_output_decision`.

## Verification

- **Compile/import: PASS**
  ```
  $ python3 -m py_compile .claude/hooks/permission_state_store.py .claude/hooks/permission_request_hook.py .claude/hooks/telegram_permission_router.py tests/test_integration_permission_request.py tests/test_unit_state_store.py
  py_compile: PASS
  $ python3 -c "import permission_state_store, permission_request_hook, telegram_permission_router"
  imports: PASS
  agent {'allow', 'deny', 'stop'}
  ```

- **Tests: 941 passed, 0 failed** (baseline before this task: 931).
  ```
  # before
  $ python3 tests/run_all_tests.py
  Ran 931 tests in 29.704s
  OK (skipped=1)

  # after
  $ python3 tests/run_all_tests.py
  Ran 941 tests in 30.495s
  OK (skipped=1)
  ```
  The one skip is pre-existing. No pre-existing failures observed.

- **Installed (`install-claude-config.sh` re-run): no.** Per instruction 9 the
  installer was not run: the hook copy under `~/.claude/hooks/` still carries the
  pre-22-02 code, so nothing in this task is live in any workspace (including
  this one for the installed path) until `./install-claude-config.sh` re-runs
  (standing rule / invariant 8). Nothing here changes user-visible behavior
  before 22-03 in any case.

## Done criterion 2 — manual walkthrough, literal output

Two real processes, an isolated state store, a fake relay. The pending row is
created in the hook process; a **second process** (`agent_writer.py`, its own
PID) writes the agent ALLOW two seconds later via `update_request_state`; the
hook process is parked in `wait_for_response` against a fake 1-second long-poll
that always 204s.

```
$ CLAUDE_PERMISSION_STATE_FILE=$PWD/store.jsonl \
  CLAUDE_PERMISSION_AUDIT_FILE=$PWD/audit.jsonl \
  CLAUDE_PERMISSION_DEBUG_LOG=$PWD/debug.log \
  python3 walkthrough.py

[writer pid=890713] wrote state=allow source=agent actor_agent='a1b2c3d4e5f6 @ claude-hooks' decision={'action': 'allow'}
[hook   pid=890656] state file: /tmp/claude-1000/.../22-02/store.jsonl
[hook   pid=890656] pending row created: 993b40606da6 state=pending
[relay ] long-poll chunk on message 31337 -> 204 (no answer)
[relay ] long-poll chunk on message 31337 -> 204 (no answer)
[relay ] long-poll chunk on message 31337 -> 204 (no answer)
[relay ] PATCH message 31337 text:
--------------------------------------------------
<b>claude-hooks</b>

<b>Permission Request</b> <code>993b40606da6</code>

<pre>
rm -rf /tmp/scratch-dir
</pre>

Approve this command?

🤖 allow by agent a1b2c3d4e5f6 @ claude-hooks
--------------------------------------------------
[relay ] CANCEL message 31337 (keyboard stripped)
[hook   pid=890656] wait_for_response returned {'action': 'allow'} after 3.0s
[hook   pid=890656] hook output: {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}
[store ] row: state=allow resolution_source=agent actor_agent='a1b2c3d4e5f6 @ claude-hooks' decision={'action': 'allow'}
```

(The writer's line appears first only because the two processes' stdout buffers
interleave on flush; the timeline is visible in the hook's `after 3.0s` — three
1-second fake chunks, the write landing during the third.)

Observed: PATCH strictly before CANCEL on the same message id; attribution line
present; decision returned to the caller and mapped to a `behavior: allow` hook
output; the row carries `resolution_source=agent` and `actor_agent`.

## Decisions

1. **Body reconstruction via an extracted `render_permission_body`.** The relay
   client has no "get message" call and the permission body was composed inline
   in `send_permission_message`, so finalizing needed the body from somewhere.
   Rather than inventing a second, divergent body format for the finalized
   message, I extracted the existing composition into a pure function — the same
   pattern `render_question_body` already establishes for AskUserQuestion
   messages, whose docstring names this exact use case. `send_permission_message`
   now delegates to it, so the sent text is unchanged.
   `_finalize_agent_decision` re-derives `workspace_name`/`session_name` from the
   row (`get_workspace_name(row.cwd)`, `get_session_name(row.session_id, row.cwd)`)
   — the same functions and inputs the send path used.
2. **`finalize_message` reused, not duplicated.** It already does patch-then-
   cancel with a body + prefix + answer text; the attribution is passed as the
   answer text with `prefix="🤖 "`, matching the `✍️ ` / `✅ ` marker convention.
3. **`_EXTERNALLY_DECIDABLE_STATES` holds raw state *values*** (`"allow"`, …)
   because the row's `state` is a string; comparing against enum members would
   silently never match.
4. **Fallbacks in the finalizer** (`action` → `"decided"`, `actor` → `"unknown"`)
   keep a malformed row from raising inside the best-effort path; the decision is
   returned either way.
5. **No new store reader/writer.** The loop uses the existing `get_request`; the
   only write path remains `update_request_state`.
6. Tests drive the **real** state store (isolated by the runner's env vars)
   rather than mocking `update_request_state`, so case 4's terminal-state
   idempotency is proved by the store itself, not by a mock's return value.

## Blockers

None.

## Questions for User

None.
