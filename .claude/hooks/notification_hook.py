#!/usr/bin/env python3
"""
Claude Code Notification Hook - Escalate to Mobile

Sends desktop notifications that escalate to mobile if not dismissed.
Triggers on:
- idle_prompt: When Claude is waiting for user input

Usage (configured in .claude/settings.json):
{
  "hooks": {
    "Notification": [
      {
        "matcher": "idle_prompt",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/notification_hook.py"
          }
        ]
      }
    ]
  }
}
"""

import json
import os
import sys
import subprocess
import re
from pathlib import Path

# Configuration
NOTIFY_ESCALATE_BIN = os.path.expanduser("~/.bin/notify_escalate")
TIMEOUT_SECONDS = 60
CLAUDE_DIR = Path.home() / ".claude"
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "canceled", "error"}


def debug_log(message: str):
    """Log debug message if debug mode is enabled"""
    if os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1':
        debug_log_path = CLAUDE_DIR / "notification_hook_debug.log"
        try:
            with open(debug_log_path, 'a') as f:
                f.write(f"[DEBUG] {message}\n")
        except Exception:
            pass


def get_session_name(session_id: str, cwd: str) -> str | None:
    """
    Get session name (slug) from Claude's session storage.

    Args:
        session_id: UUID of the current session
        cwd: Current working directory

    Returns:
        Session slug/name if found, None otherwise
    """
    if not session_id:
        return None

    # Sanitize cwd to match Claude's project directory naming
    # e.g., /data/sync/work/leangeeks-ai/ai-playground
    #   → -data-sync-work-leangeeks-ai-ai-playground
    # Remove leading slash first, then add the prefix
    normalized_path = cwd.lstrip("/")
    project_dir_name = "-" + normalized_path.replace("/", "-")

    # Path to session file
    session_file = CLAUDE_DIR / "projects" / project_dir_name / f"{session_id}.jsonl"

    debug_log(f"Looking for session file: {session_file}")

    if not session_file.exists():
        debug_log(f"Session file not found: {session_file}")
        return None

    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Look for the slug field
                    if 'slug' in entry and entry['slug']:
                        debug_log(f"Found session slug: {entry['slug']}")
                        return entry['slug']
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        debug_log(f"Error reading session file: {e}")

    debug_log("No slug found in session file")
    return None


def get_workspace_name(cwd: str) -> str:
    """Extract workspace name (last segment of cwd)"""
    return Path(cwd).name


def get_title(cwd: str, session_id: str) -> str:
    """
    Generate notification title with workspace and optionally session name.

    Args:
        cwd: Current working directory
        session_id: Session UUID

    Returns:
        Notification title string
    """
    workspace_name = get_workspace_name(cwd)
    session_name = get_session_name(session_id, cwd)

    if session_name:
        return f"{workspace_name} · {session_name}"
    else:
        return workspace_name


def _extract_task_notification(content: str) -> tuple[str, str] | None:
    """
    Extract task ID and status from task-notification XML-like payload.

    Returns:
        (task_id, status) if found, None otherwise.
    """
    if not content or "<task-notification>" not in content:
        return None

    task_id_match = re.search(r"<task-id>\s*([^<\s]+)\s*</task-id>", content)
    status_match = re.search(r"<status>\s*([^<\s]+)\s*</status>", content)

    if not task_id_match or not status_match:
        return None

    return task_id_match.group(1), status_match.group(1).strip().lower()


def has_active_background_agents(transcript_path: str) -> bool:
    """
    Detect if the current session has async background agents still running.

    We infer this by scanning the session transcript:
    - async launches: toolUseResult.isAsync=true with status=async_launched + agentId
    - terminal notifications: task-notification with status in TERMINAL_TASK_STATUSES
    """
    if not transcript_path:
        debug_log("No transcript_path provided; cannot detect background agents")
        return False

    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        debug_log(f"Transcript file not found: {transcript_file}")
        return False

    active_agent_ids: set[str] = set()

    try:
        with open(transcript_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Async launch event
                tool_use_result = entry.get("toolUseResult")
                if (
                    isinstance(tool_use_result, dict)
                    and tool_use_result.get("isAsync") is True
                    and tool_use_result.get("status") == "async_launched"
                ):
                    agent_id = tool_use_result.get("agentId")
                    if agent_id:
                        active_agent_ids.add(str(agent_id))
                    continue

                # Completion/failure/cancel notification event
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue

                content = message.get("content")
                if isinstance(content, str):
                    task_notification = _extract_task_notification(content)
                    if task_notification:
                        task_id, status = task_notification
                        if status in TERMINAL_TASK_STATUSES:
                            active_agent_ids.discard(task_id)

    except Exception as e:
        debug_log(f"Error scanning transcript for background agents: {e}")
        # Fail open: if detection errors, do not suppress notification
        return False

    if active_agent_ids:
        debug_log(
            "Background agents still active: "
            + ", ".join(sorted(active_agent_ids))
        )
        return True

    debug_log("No active background agents detected")
    return False


def send_notification(title: str, message: str) -> bool:
    """
    Send notification using notify_escalate.

    Args:
        title: Notification title
        message: Notification message

    Returns:
        True if successful, False otherwise
    """
    if not os.path.exists(NOTIFY_ESCALATE_BIN):
        debug_log(f"notify_escalate not found at: {NOTIFY_ESCALATE_BIN}")
        return False

    try:
        # Run in background - don't block the hook
        subprocess.Popen(
            [NOTIFY_ESCALATE_BIN, title, message, str(TIMEOUT_SECONDS)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        debug_log(f"Notification sent: {title}")
        return True
    except Exception as e:
        debug_log(f"Failed to send notification: {e}")
        return False


def main():
    """Main hook entry point"""
    try:
        # Read hook input from stdin
        raw_input = sys.stdin.read()
        debug_log(f"=== Notification hook called ===")
        debug_log(f"Raw input: {raw_input[:500]}...")

        input_data = json.loads(raw_input)

        # Extract notification info
        notification_type = input_data.get('notification_type', '')
        message = input_data.get('message', '')
        session_id = input_data.get('session_id', '')
        cwd = input_data.get('cwd', '')
        transcript_path = input_data.get('transcript_path', '')

        debug_log(f"Notification type: {notification_type}")
        debug_log(f"Message: {message[:100]}...")
        debug_log(f"Session ID: {session_id}")
        debug_log(f"CWD: {cwd}")
        debug_log(f"Transcript path: {transcript_path}")

        # Permission requests are forwarded through the PermissionRequest
        # Telegram flow; keep this hook limited to idle notifications.
        if notification_type != 'idle_prompt':
            debug_log(f"Skipping notification type: {notification_type}")
            sys.exit(0)

        # Suppress idle notifications while async background agents are running.
        # Parent sessions can be "idle" while they wait for child Task completions.
        if notification_type == 'idle_prompt' and has_active_background_agents(transcript_path):
            debug_log("Skipping idle notification: background agents are still running")
            sys.exit(0)

        # Build title
        title = get_title(cwd, session_id)

        # Send notification
        send_notification(title, message)

        # Always exit 0 - notification failure shouldn't block Claude
        sys.exit(0)

    except Exception as e:
        debug_log(f"ERROR: {type(e).__name__}: {str(e)}")
        # Don't fail the hook - notification issues shouldn't block Claude
        sys.exit(0)


if __name__ == '__main__':
    main()
