# 22-02 — External decisions reach the wait loop

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §1 finding 2, §2 D5, §4 H2/H3/H7 ·
[state.md](./state.md) invariants 3, 5, 6

## Goal

Make an `ALLOW`/`DENY`/`STOP` written into the state store by an external actor
actually resolve a parked permission request on the **relay path** — today only
the relay-less path honors it — and finalize the Telegram message with agent
attribution so the human can always see, post-hoc, that an agent decided.

This task ships **no agent-facing surface**. It is inert until 22-03 writes
decisions; the only observable change before then is the schema.

## Scope

### 1. State store schema — actor attribution

`.claude/hooks/permission_state_store.py`:

- `PermissionRequest` (`:77-103`): add `actor_agent: Optional[str] = None` —
  a human-readable actor string (22-03 composes it; shape
  `"<session_id[:12]> @ <cwd basename>"`). `from_dict` already filters unknown
  keys, so old rows load fine.
- New constant `RESOLUTION_SOURCE_AGENT = "agent"` beside the existing three
  (`:71-73`). The `resolution_source is None` inference in
  `update_request_state` (`:377-383`) must **not** silently claim `telegram`
  for agent writes — the caller passes the source explicitly; add a comment
  saying so.
- `update_request_state` (`:338`): accept and persist `actor_agent` the same
  guarded way `actor_user_id` is handled (write only when a value is passed).
  The `AuditEntry` (`:117-125`) gains `actor_agent` too.

All writes stay inside `update_request_state` — no new writer functions
(invariant 6).

### 2. The relay-path wait loop honors external terminal states

`.claude/hooks/permission_request_hook.py`, the loop at `:352-387`. Today the
per-chunk store check is `RESOLVED_TERMINAL`-only (`:355-359`). Widen it:

```python
current = get_request(request_id)
if current and current.state == RequestState.RESOLVED_TERMINAL.value:
    ... unchanged ...
if (current and current.decision
        and current.resolution_source == RESOLUTION_SOURCE_AGENT
        and current.state in _EXTERNALLY_DECIDABLE_STATES):
    _finalize_agent_decision(message_id, current)
    return current.decision
```

with `_EXTERNALLY_DECIDABLE_STATES = {ALLOW, DENY, STOP}` (values). Notes that
are load-bearing:

- **Gate on `resolution_source == "agent"`, not on any-terminal-with-decision.**
  The hook's own `_mark_relay_resolved` (`:402-420`) writes terminal states
  *after* the loop exits, so it cannot race itself — but expiry writes
  `EXPIRED` and 22-05's compaction touches old rows; the source gate keeps the
  loop from adopting anything an agent did not write.
- `REPLY`/`WHITELIST` are deliberately **not** externally decidable in v1:
  reply carries injection semantics and whitelist has its own writer path
  (22-04 goes through settings files instead). `decide` in 22-03 only ever
  writes allow/deny/stop.
- The decision dict shape is the existing one (`{"action": "allow"}` etc.,
  matching `_ACTION_TO_STATE` at `:393-399` and what
  `build_output_decision` consumes). 22-03 must write exactly this shape;
  assert it in tests here.
- Latency: the check runs once per long-poll chunk (≤25 s) — brd H2, accepted.
  Do not shrink the chunk in this task.

`_wait_state_store_only` (`:490-519`) already returns any terminal decision —
verify with a test that an agent-written allow (with the new source) passes
through unchanged.

### 3. Telegram finalization with attribution

`_finalize_agent_decision(message_id, row)`: patch the message text, then
cancel — the same order `finalize_message` uses
(`telegram_permission_router.py:806`, "patch then cancel"). Reuse the existing
answer-baking helper if its shape fits (the `patch + cancel` helper at
`telegram_permission_router.py:820-832` takes a body + prefix + answer text);
the appended line should read like:

```
🤖 <action> by agent <actor_agent>
```

Best-effort, non-fatal on failure (the reaper's cleanup sweep is the backstop,
as with every other finalization path). On success the relay PATCH re-renders
via `render_body`, which strips `#unanswered` and kills any live nudge — that
is the epic-19 machinery doing its job; no extra work here.

### 4. AskUserQuestion group loop — explicitly untouched

The group wait (`:640` onward) is **out of scope** (brd §3.2). Add no external-
decision check there; a comment at the loop head pointing at this task's
decision is enough for the next reader.

## Testing

Existing integration suites patch `RelayClient` and drive the wait loop
(`tests/` permission-request integration; find the harness with
`grep -rn "wait_for_relay_answer" tests/`). New cases:

| # | Path | Setup | Expect |
|---|---|---|---|
| 1 | relay path | pending row + live message; external write ALLOW w/ source `agent` + decision dict | loop returns the decision within one chunk; patch + cancel called; attribution text contains `actor_agent` |
| 2 | relay path | external write DENY w/ source `agent` | decision returned; message finalized |
| 3 | relay path | row flipped to EXPIRED (source `timeout`) | loop does **not** adopt it (source gate) |
| 4 | relay path | human answers via relay first, agent write races second | `update_request_state` returns None for the loser; winner's decision returned once |
| 5 | store-only path | external ALLOW w/ source `agent` | `_wait_state_store_only` returns it |
| 6 | schema | old-format row without `actor_agent` | loads; audit entry for a new write carries `actor_agent` |

Case 4 is the race invariant: terminal-state idempotency in
`update_request_state` (`:411-414`) is what makes agent-vs-human races safe —
prove it stays that way.

## Done criteria

1. Suites green; before/after counts reported.
2. A manual walkthrough, reported with literal output: create a pending row via
   the state-store `__main__` harness (or a scratch script driving
   `create_request`), run the wait loop against a fake relay, write an agent
   allow from a second process, observe the returned decision and the patched
   text.
3. Reinstall note (standing rule) — the hook copy changes only on
   `./install-claude-config.sh`.
4. No edits outside `permission_state_store.py`, `permission_request_hook.py`,
   `telegram_permission_router.py`, and tests.
