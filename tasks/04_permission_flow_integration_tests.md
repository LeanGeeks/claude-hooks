# Task 04: End-to-End Tests for Claude Permission + Telegram Flow

## Objective

Create repeatable tests to validate the full permission path:
- `PreToolUse` classifier
- `PermissionRequest` Telegram gate
- action routing/state
- fallback and timeout behavior

## Context

System under test:
- `.claude/hooks/pretool_hook.py`
- `.claude/hooks/permission_request_hook.py` (Task 02)
- Telegram router/state modules (Task 03)
- `.claude/settings.json` hook wiring

Current known fragility:
- multiline/heredoc parsing can over-split commands in `bash_command_parser.py`.

## Test Plan

1. Unit tests
- decision mapper (`allow|deny|stop|whitelist|reply`)
- timeout fallback
- whitelist update logic and dedupe
- state store locking/idempotency

2. Integration tests (local harness)
- simulate hook stdin payloads for `PreToolUse` and `PermissionRequest`
- mock Telegram API responses and callback delivery

3. Manual smoke tests in real Claude session
- unknown command triggers Telegram after 15s
- each button path returns expected effect in Claude
- ignore path falls back to terminal prompt

## Implementation Notes

1. Add test fixtures:
- sample `PermissionRequest` payload JSON
- sample `permission_suggestions`
- stale and duplicate callback samples

2. Add scripts:
- `tasks`-style executable script for quick scenario checks.
- include expected outputs and exit conditions.

3. Logging checks:
- verify actionable logs in `~/.claude/*debug*.log` and state/audit logs.

## Done Criteria

- Automated tests cover all action branches and timeout path.
- At least one real manual E2E run per action (`allow`, `deny`, `stop`, `whitelist`, ignore, reply).
- No uncaught exceptions in hooks during test runs.
- Documented known limitation(s), especially heredoc/multiline parsing false positives.

## Deliverables

- Test files/scripts committed under project test location (choose and document).
- Short test report markdown with pass/fail and reproduction commands.

