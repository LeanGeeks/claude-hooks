# Task 02: Implement Telegram-Gated `PermissionRequest` Hook

## Objective

Implement a new Claude `PermissionRequest` hook that waits 15 seconds, sends a Telegram approval message, and returns a native hook decision based on user action.

## Context

Relevant files:
- `.claude/settings.json`
- `.claude/hooks/notification_hook.py` (existing notification patterns)
- new file to create: `.claude/hooks/permission_request_hook.py`

Claude hook behavior (target):
- Hook may block briefly and return structured decision JSON.
- We should avoid process restarts or terminal automation.

## Architecture Decisions

1. Use native `PermissionRequest` hook only for final permission decisions.
2. Wait `15s` before sending Telegram (as requested).
3. Use explicit action mapping:
- `allow` -> `behavior: "allow"`
- `deny` -> `behavior: "deny"`
- `stop` -> `behavior: "deny"` + `"interrupt": true`
- `whitelist` -> `behavior: "allow"` + `updatedPermissions`
4. If no Telegram response before hook deadline:
- return no decision (fall back to normal in-terminal prompt), or
- explicitly return `"ask"` style fallback if supported by payload shape.
Choose fallback-to-terminal as default.

## Required Inputs / Outputs

Input from Claude hook payload (must parse and log):
- `session_id`
- `cwd`
- permission request metadata
- `tool_name`, `tool_input` if present
- `permission_suggestions` (if available)

Output payload:
- `hookSpecificOutput.hookEventName = "PermissionRequest"`
- `decision` object with behavior fields above.

## Implementation Notes

1. Build concise Telegram message content:
- workspace name
- session slug/name (reuse logic from `notification_hook.py` if possible)
- tool + command summary
- risk hint (unknown vs denied pattern)

2. Keep hook robust:
- fail-open to terminal prompt if Telegram API errors.
- enforce strict max wait less than Claude hook timeout.

3. Minimize duplication:
- share helper functions with `notification_hook.py` where practical.

4. Security:
- only accept callbacks from configured chat/user IDs.
- reject stale/unknown request IDs.

## Done Criteria

- `PermissionRequest` hook is wired in `.claude/settings.json`.
- On permission request, hook waits 15s then sends Telegram message with inline buttons.
- Button actions produce correct Claude-native decision payloads (`allow`, `deny`, `stop`, `whitelist`).
- Timeout/no-response path cleanly falls back to terminal prompt.
- Hook never crashes Claude session; all errors handled with safe fallback.

## Validation

Run 4 manual scenarios:
1. Click `allow` -> command proceeds.
2. Click `deny` -> command denied, session continues.
3. Click `stop` -> command denied and session interrupted.
4. Ignore message -> terminal prompt appears normally.

