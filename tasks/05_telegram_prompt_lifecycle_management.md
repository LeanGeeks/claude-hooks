# Task 05: Telegram Prompt Lifecycle Management

## Objective

Enhance the Telegram permission integration to:
1. Keep Telegram prompts functional indefinitely (no 60-second timeout)
2. Automatically revoke/update Telegram messages when permission is granted via terminal

## Context

Current limitation:
- `permission_request_hook.py` waits synchronously for 60 seconds (`MAX_WAIT_FOR_RESPONSE`)
- After timeout, hook exits and Telegram buttons become "dead"
- No coordination between Telegram and terminal prompt resolution

Relevant files:
- `.claude/hooks/permission_request_hook.py`
- `.claude/hooks/telegram_permission_router.py`
- `.claude/hooks/permission_state_store.py`
- `.claude/settings.json` (hook configuration)

## Architecture Decisions

1. **Background Daemon Pattern**
   - Extract polling logic into a long-running daemon process
   - Daemon runs independently of individual hook invocations
   - Hook's responsibility: send message, ensure daemon is running, exit immediately
   - Daemon's responsibility: poll for callbacks, update state store, manage message lifecycle

2. **PostToolUse Hook for Terminal Resolution Detection**
   - Claude Code's terminal prompt is outside our control
   - Use `PostToolUse` hook to detect when a tool was actually executed
   - If tool execution matches a pending Telegram request → revoke the Telegram message

3. **Resolution Source Tracking**
   - Add `resolution_source` field to state store: `telegram` | `terminal` | `timeout`
   - Track which channel resolved each permission request
   - Enables audit trail and prevents double-resolution

4. **Message Lifecycle States**
   - `pending` → message has active buttons
   - `resolved_telegram` → button was clicked, message updated
   - `resolved_terminal` → tool executed via terminal, message revoked
   - `expired` → TTL reached, message updated with expiry notice

## Implementation Notes

### New Files

1. **`.claude/hooks/telegram_daemon.py`**
   - Long-running process that polls for Telegram updates
   - Manages lifecycle of all pending permission messages
   - Uses PID file to ensure single instance
   - Communicates via state store (no IPC needed)

2. **`.claude/hooks/posttool_hook.py`**
   - Triggered after tool execution
   - Checks state store for pending requests matching the tool
   - Revokes corresponding Telegram messages (remove buttons, add reaction)

### Modified Files

1. **`permission_request_hook.py`**
   - Remove synchronous waiting (`wait_for_response`)
   - Add daemon startup/healthcheck
   - Exit immediately after sending message

2. **`permission_state_store.py`**
   - Add `resolution_source` field
   - Add `resolved_at` timestamp
   - Add methods for terminal-resolution detection

3. **`telegram_permission_router.py`**
   - Add `revoke_message()` function (update with expiry/terminal notice)
   - Extract daemon-compatible polling logic

4. **`.claude/settings.json`**
   - Add `PostToolUse` hook configuration

### Daemon Management

- PID file: `~/.claude/telegram_daemon.pid`
- Log file: `~/.claude/telegram_daemon.log`
- Auto-start from PermissionRequest hook if not running
- Graceful shutdown on SIGTERM
- Self-healing: restart on unexpected exit

### Revocation Message Format

When terminal is used:
```
*Permission Request* `abc123`

✅ Resolved via terminal
```

When expired:
```
*Permission Request* `abc123`

⏰ Expired (no response)
```

## Done Criteria

- [ ] Telegram daemon runs continuously and polls for callbacks
- [ ] PermissionRequest hook exits immediately after sending message
- [ ] Telegram buttons remain functional indefinitely (until resolved)
- [ ] PostToolUse hook detects terminal resolution and revokes Telegram message
- [ ] State store tracks resolution source correctly
- [ ] Only one daemon instance runs at a time (PID file locking)
- [ ] Daemon auto-starts when needed
- [ ] All error paths handled gracefully

## Validation

### Test Scenarios

1. **Long-running Telegram prompt**
   - Trigger permission request
   - Wait 5+ minutes
   - Click button → action should work

2. **Terminal resolution revokes Telegram**
   - Trigger permission request
   - Respond via terminal (allow/deny)
   - Telegram message should update within 2 seconds

3. **Concurrent requests**
   - Trigger multiple permission requests
   - Resolve some via Telegram, some via terminal
   - Each should be tracked correctly

4. **Daemon resilience**
   - Kill daemon process
   - Trigger new permission request
   - Daemon should auto-restart

5. **Double-resolution protection**
   - Click Telegram button after terminal already resolved
   - Should show "already resolved" message

## Estimated Effort

- Daemon implementation: ~2-3 hours
- PostToolUse hook: ~1 hour
- State store updates: ~1 hour
- Testing and refinement: ~1-2 hours

**Total: ~5-7 hours**
