---
description: Disable YOLO mode for this session — resume normal permission prompting. Re-enable with /yolo.
allowed-tools: Bash(python3:*)
---
!`python3 ~/.claude/hooks/session_yolo_store.py disable "${CLAUDE_SESSION_ID}"`

Relay the command output above to the user in one short line (it confirms YOLO is disabled, or reports an error). Take no other action.
