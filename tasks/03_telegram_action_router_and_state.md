# Task 03: Telegram Action Router, Request State, and Whitelist Updates

## Objective

Implement the Telegram-side control plane used by the `PermissionRequest` hook:
- send message with action buttons
- store pending request state
- consume button callbacks/replies
- return actionable result for the waiting hook process

## Context

This task is intentionally separate from hook parsing logic.

Suggested new module(s):
- `.claude/hooks/telegram_permission_router.py`
- `.claude/hooks/permission_state_store.py`

State location (local, simple):
- `~/.claude/permission_requests.jsonl` or `~/.claude/permission_requests/`

## Architecture Decisions

1. Use request correlation key:
- `request_id` (UUID)
- include `session_id`, `cwd`, timestamp.

2. State machine:
- `pending` -> `allow|deny|stop|whitelist|reply|expired`
- transitions must be idempotent.

3. Reply text handling:
- classify as `reply` action and preserve raw text.
- do not attempt to inject a synthetic user message via CLI restart.
- upstream hook (Task 02) maps reply to deny-with-message (or deny+interrupt if configured).

4. Whitelist policy:
- prefer `permission_suggestions` from hook input.
- if unavailable, fallback to conservative generated pattern.
- write updates to `.claude/settings.local.json` (workspace-local), preserving JSON validity and deduplicating entries.

## Implementation Notes

1. Telegram transport:
- Support either webhook or long-poll; choose one and document.
- Inline buttons payload includes `request_id` + action.

2. Concurrency:
- lock state file before update (file lock).
- ensure duplicate callback taps are harmless.

3. TTL and cleanup:
- expire stale pending requests after configured timeout.
- cleanup on read/write operations.

4. Auditability:
- append action log with actor (`user_id`), action, timestamp, request_id.

## Done Criteria

- New request can be created/read/updated atomically.
- Buttons `allow`, `deny`, `stop`, `whitelist` are persisted as final states.
- Telegram plain text reply is captured and linked to correct pending request.
- Whitelist action safely updates `.claude/settings.local.json` without corrupting file.
- Duplicate/late callbacks are ignored with informative log.

## Validation

Test matrix:
1. Concurrent two pending requests, each resolved independently.
2. Double-click same button -> one final decision only.
3. Reply text arrives after request expired -> rejected safely.
4. Whitelist writes valid JSON and dedupes existing rule.

