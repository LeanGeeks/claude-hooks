---
description: Disable YOLO mode for this session — resume normal permission prompting. Re-enable with /yolo.
allowed-tools: Bash(python3:*)
---
!`python3 ~/.claude/hooks/session_yolo_store.py disable "${CLAUDE_SESSION_ID}"`

Relay the command output above to the user in one short line (it confirms YOLO is disabled, or reports an error), then add one short line: if the session was switched into bypassPermissions by a YOLO tap, this only clears the flag — the mode itself has to be changed at the keyboard with Shift+Tab (or by ending the session). Take no other action.
