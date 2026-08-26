# 23-04 — Questions MCP server (`ask` + `notify`)

**Status:** todo · **Depends on:** 23-01 (`never_expires`), 23-02 (escalation
send-time fields), 23-03 (the store)
**Read first:** [brd.md](./brd.md) §4, D1–D4, D7 · [architecture.md](./architecture.md)
§4 · [state.md](./state.md) invariants 1, 5, 6, 10 · `context-mcp/server.py` (the
pattern to copy) · `tasks/22_agent_permission_flows/22-03-permissions-mcp.md`
(the same pattern, one epic over) · `.claude/hooks/roles_config.py`,
`.claude/hooks/telegram_permission_router.py` (send path, keyboard building)

## Goal

The agent-facing surface: one call that records a question in the workspace and
tells a human, then returns.

## Scope

### 1. Server skeleton — `questions-mcp/server.py`

Copy the **live** precedent, not the doc: epic 22 shipped `permissions-mcp/` with
a `server.py` + `permissions_mcp_lib.py` split and its own registration block in
`install-claude-config.sh` (uv gate, in-place, `~/.claude.json`). Mirror that
split as `questions-mcp/server.py` + `questions_mcp_lib.py`.

**Coordinate with epic 22**: at the time of writing its work is uncommitted and
touches the same installer regions, `REQUIRED_HOOKS` and `tests/run_all_tests.py`.
Rebase onto it rather than racing it.

**Resolve identity and config per call, not at startup.** The server is
long-lived and may serve sessions in different workspaces; read
`CLAUDE_PROJECT_DIR`/`PWD` per invocation and load that workspace's `roles.toml`
through the existing loader with a short TTL cache. (Epic 22 notes the same
`SettingsLoader` caching hazard for a long-lived server — inherit the lesson.)

### 2. `ask(title, body, options?, role?, tags?)`

Order is load-bearing (invariant 1):

1. Resolve anchor, role and queue file. **An unresolvable role refuses here**,
   before anything is written (brd §4.1).
2. `questions_store` allocates the id and appends the entry; fsync.
3. Send: `kind=question`, `never_expires: true`, one button per option, the workspace's
   nudge ladder and `escalate_after`, and the escalation **token** resolved
   locally from `roles_config` (invariant 4 — the relay never learns the role).
4. Record `message_id → {workspace_id, anchor, root, rel_path, qid, role}` in the
   shared index (`~/.claude/async_questions.json`, shape in architecture §5).
5. `mark_dispatched`.
6. Return `{id, file, dispatched, message_id, pending_answers}`.

A relay failure at step 3 returns `dispatched: false` **with the id** — the entry
stands and a human finds it by reading the queue (brd D4). Never unwind the write.

`pending_answers` is the count of unapplied answers on this machine, so a backlog
is visible on every ask rather than only in a log.

### 2a. Caps and single-select (brd D12)

Refuse loudly, never queue: `max_open` unanswered entries per workspace
(default 20) and `min_interval_s` between asks (default 30), both configurable in
`[questions]`. The counter is the queue file's open entries, so it is durable and
needs no extra store.

**Options are single-select in v1.** The relay's `multi_select` is documented as
meaningful only for grouped question messages, and an async ask is deliberately
ungrouped (one `Q-NNN` is one entry). Reject a multi-select request with a clear
message rather than sending something the relay will ignore.

### 3. `notify(text, role?, ack?)`

`ack` defaults true: one `[ Acknowledge ]` button, sent as `kind=question` so the
`#unanswered` tag and the nudge ladder work with no relay change (brd D7,
invariant 10). Index entry carries **no `qid`**, which is how the listener knows
to finalize without touching a file. `ack: false` sends a plain notification and
records nothing.

### 4. Index writes

The index is shared with the listener, so use the same `flock` + atomic-replace
discipline. Define the shape once, here or in 23-05, and have the other import it
— never two readers with two hand-rolled parsers.

### 5. Agent-facing docs

A `docs/` section (23-06 owns the file) plus tool descriptions written for the
calling agent: when to use `ask` over `AskUserQuestion` (you have other work; the
answer is not needed to continue), what goes in `body` versus `options`, and that
the returned id is what to cite when halting.

## Done when

- `ask` in the reference workspace produces an entry indistinguishable in shape
  from a hand-written one, and a normal Telegram question.
- An unresolvable role writes nothing.
- Relay down: entry present, `dispatched: false`, id returned.
- `notify` with ack nudges; without ack it is silent and unrecorded.
- A workspace with no `[questions]` section gets a clear, actionable error.

## Tests

Patched `RelayClient`, `FakeTelegramBackend`, no network: ordering (write before
send, and a simulated crash between them leaves an entry, never an orphan
message); role refusal; relay-failure return shape; option→keyboard mapping;
index round-trip with the listener's reader; per-call workspace resolution with
two workspaces in one server process; `notify` both modes.
