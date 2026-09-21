---
description: Enable YOLO mode for this session — auto-allow every permission request without prompting (Telegram or terminal) until the session ends. AskUserQuestion prompts are still forwarded. Disable with /yolo-off — note that a Telegram YOLO tap also moves the session into bypassPermissions, which only Shift+Tab at the keyboard (or ending the session) can leave.
allowed-tools: Bash(python3:*)
---
!`python3 ~/.claude/hooks/session_yolo_store.py enable "${CLAUDE_SESSION_ID}"`

Relay the command output above to the user in one short line (it confirms YOLO is enabled, or reports an error). Take no other action.
