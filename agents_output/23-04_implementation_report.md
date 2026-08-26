# Implementation Report — 23-04 Questions MCP Server

## Summary

Implemented the `questions` MCP server with two tools (`ask` and `notify`) and the shared routing index module (`questions_listen_lib.py`). The file write is guaranteed to precede the relay call (invariant 1), role resolution and cap checks refuse before any write, and 23-05 can import the index module directly without restating its shape.

## Files Created

- `questions-mcp/server.py` — Thin MCP registration shell (uv single-file script, `MCPServer("questions")`). Follows the `permissions-mcp/server.py` pattern exactly: all logic in the lib, this file only registers tools and calls `mcp.run()`. Identity and config resolved per call in the lib.
- `questions-mcp/questions_mcp_lib.py` — All tool logic: per-call workspace resolution with a 5 s TTL cache keyed by workspace_dir path; `ask` and `notify` implementations; relay send helpers; keyboard builder; HTML body renderer.
- `.claude/hooks/questions_listen_lib.py` — The index module (invariant 5 applied to the index): defines `IndexEntry`, `PendingApply`, `Index` dataclasses; provides `read_index`, `write_index`, `add_message_entry`, `count_pending`; `flock` + tmp + `os.replace` write discipline. **23-05 must import this and never hand-parse the JSON.**
- `tests/test_unit_questions_mcp.py` — 36 tests (1 skipped), covering all required scenarios.

## Files Modified

- `relay-server/relay_server/client.py` — Added `never_expires: bool`, `nudge_schedule: str | None`, `escalate_after_sec: int | None`, `escalate_to_token: str | None` to `RelayClient.send_message`. These are passed conditionally in the request body so existing callers that omit them produce byte-identical relay calls (invariant 9).
- `tests/run_all_tests.py` — Registered `unit_questions_mcp` → `test_unit_questions_mcp`.
- `install-claude-config.sh` — Added `questions_listen_lib.py` to `REQUIRED_HOOKS` so the listener (23-05) can `import questions_listen_lib` from the installed hooks directory.

**`install-claude-config.sh` was NOT re-run.** 23-06 owns installation. Re-running it in a shared checkout would clobber another session's live config.

## Verification

- **Compile/import:** PASS — `python3 -m py_compile` on all five Python files succeeds; `bash -n install-claude-config.sh` passes.
- **Tests (full suite):** 1156 passed, 2 skipped (was 1120 passed, 1 skipped — delta is the 36 new tests plus 1 new skip from `test_min_interval_allows_after_cooldown`'s `skipTest` guard).
- **Relay suite:** 315 passed (`/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q`).
- **Installed:** no — not applicable (23-06 owns this).

## Decisions

### Write-before-send ordering guarantee (invariant 1)

`create_entry` (which locks, allocates the id, composes, appends, and fsyncs) runs **before** any relay call. The relay call is step 3; the index write and `mark_dispatched` are steps 4 and 5. A crash at any point in steps 3–5 leaves an entry in the queue file with status `[open]` and no dispatch marker — the "accepted failure" (brd D4). The reverse (Telegram message with no queue entry) cannot happen because the write is fsynced before the relay call is made. Tested with a relay mock that raises on `send_message`, confirming the entry exists and `dispatched: false` is returned with the id.

### How `ask` surfaces errors

| Situation | Behavior |
|---|---|
| No `[questions]` section | Returns `{"error": "...actionable message..."}` — no `id` key, no write |
| No role catalog | Returns `{"error": "..."}` — no write |
| `dest.token is None` (unreachable role) | Returns `{"error": "..."}` — no write (invariant 1: refuse before write) |
| Explicitly requested chat-only role | Returns `{"error": "..."}` — no write (checks the requested role's queue file before the destination fallback's) |
| `QuestionsStoreError` during `create_entry` | Returns `{"error": "Could not write question entry: ..."}` — nothing was written (error occurred during or before the atomic write) |
| `QuestionsStoreError` during `mark_dispatched` | Appended to `result["error"]` — entry and message both exist; id and message_id are still returned |
| `RelayError` / `NotBoundError` | `dispatched: false` with `id` and `error` — entry written, relay message NOT sent (brd D4) |
| Index write failure | Appended to `result["error"]` — entry and message both exist |

The `QuestionsStoreError` from `create_entry` is surfaced as a clean tool error (no stack trace, just the store's message). The entry does not exist in this case so there is no orphan.

### Index module shape and lock protocol (for 23-05)

`questions_listen_lib.py` is the sole parser/writer of `~/.claude/async_questions.json`. Its shapes:
- `IndexEntry`: `workspace_id`, `anchor`, `root`, `rel_path`, `qid` (None for ack-notifications), `role`, `created_at`
- `PendingApply`: `message_id`, `answer`, `attempts`, `first_failed_at`, `last_error`
- `Index`: `watermark`, `messages` (dict[str, IndexEntry]), `pending` (list[PendingApply])

23-04 only writes to `messages`; it never touches `watermark` or `pending`. 23-05 advances `watermark` and manages `pending` — both fields are present in the shape and survive round-trips.

Lock protocol: `flock` on `~/.claude/async_questions.json.lock` (a sibling file), then tmp + `os.replace` on the index file itself. `add_message_entry` is the safe concurrent writer: read → update → write under the lock.

### Escalation target resolution

`escalate_after_sec` comes from `[questions].escalate_after` (the questions-level config, defaulting to 24 h). The escalation target token: if the destination is a non-default role and escalation is configured, the default role's token is resolved via `resolve_destination(catalog, bindings, None)` and passed as `escalate_to_token`. If the destination IS the default role, no escalation token is set (no one to escalate to). If the default role also has no token, `escalate_to_token` is None and the field is omitted from the relay request (safe — relay skips escalation when the field is absent).

### Per-call workspace resolution and TTL cache

`_load_workspace(workspace_dir)` caches `(QuestionsConfig | None, RoleCatalog | None, Bindings)` keyed by `workspace_dir` string with a 5 s TTL under a `threading.Lock`. The cache is keyed by workspace_dir, not global, so two concurrent sessions with different workspace dirs each see their own config. Tested explicitly in `TestTwoWorkspaces`.

### Rate-limit state (min_interval_s)

The `max_open` counter is durable (reads the queue file). `min_interval_s` uses an in-memory dict keyed by `workspace_id` — a server restart resets it (fine, the default is 30 s and restarts are rare). This matches the brd's note that the durable requirement applies only to `max_open`.

### MCP server registration

The installer block for `questions-mcp/server.py` is **left to 23-06** (the task that owns installation). The server follows the `permissions-mcp` pattern exactly (same uv script preamble, same path-in-place registration approach) so 23-06 can copy the installer block verbatim and substitute `"questions"` for `"permissions"`.

## Blockers

None. All requirements fulfilled in implementation and tests. Live verification (real relay, real Telegram, real answer after 24+ hours, machine sleep) is 23-07's scope.

## Questions for User

None.
