# Task 01: Split Responsibilities Between `PreToolUse` and `PermissionRequest`

## Objective

Refactor Claude hook flow so:
- `PreToolUse` only auto-allows clearly safe commands.
- All non-auto-approved commands go through native Claude permission flow (`ask`), handled by `PermissionRequest`.

## Context

Current files:
- `.claude/hooks/pretool_hook.py`
- `.claude/hooks/bash_command_parser.py`
- `.claude/hooks/settings_loader.py`
- `.claude/settings.json`

Current behavior in `pretool_hook.py`:
- `allow` for fully matched commands.
- otherwise "defer" (no explicit `ask`).
- logs to `~/.claude/bash_manual_confirm.log`.

Target behavior:
- explicit `ask` for non-auto-approved commands, so `PermissionRequest` hook becomes deterministic.

## Architecture Decisions

1. `PreToolUse` remains a fast classifier.
2. Never block in `PreToolUse` waiting for Telegram/user input.
3. Decision mapping:
- safe -> `permissionDecision: "allow"`
- non-safe -> `permissionDecision: "ask"`
- hard deny in policy (optional, if matched deny rules) -> either `deny` or `ask` based on local policy; prefer `ask` for now unless command is explicitly dangerous.
4. Keep logging, but enrich log entries with correlation-friendly metadata (`session_id`, timestamp, reason, parsed subcommands).

## Implementation Notes

1. Update `.claude/settings.json` hooks:
- keep `PreToolUse` matcher `Bash`.
- add new `PermissionRequest` hook command entry (placeholder for Task 02 script).

2. Update `.claude/hooks/pretool_hook.py`:
- when command is not auto-allow: emit JSON with `permissionDecision: "ask"` instead of silent defer.
- keep fail-open on exceptions (`exit 0`).

3. Do not remove existing parser/settings logic.

4. Add/adjust developer docs:
- short note in `.claude/README.md` on hook lifecycle and why `ask` is required.

## Done Criteria

- For a known allowed composite command (all subcommands allowed), Claude receives `allow`.
- For unknown command (`npx ...` example), Claude receives `ask` (verified with debug logs).
- Hook exits quickly (< 200ms typical, no sleeps).
- Existing behavior for non-Bash tools remains unchanged.
- `.claude/settings.json` includes a valid `PermissionRequest` hook stanza.

## Validation

Manual checks:
1. Run an allowed command and confirm no prompt.
2. Run an unknown command and confirm Claude permission prompt appears.
3. Confirm debug log entry includes decision `ask`.

