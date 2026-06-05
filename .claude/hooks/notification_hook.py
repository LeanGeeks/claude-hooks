#!/usr/bin/env python3
"""
Claude Code Notification Hook - Forward idle sessions to Telegram

When a session goes idle (Claude is waiting for the user), this hook posts a
notification to Telegram via the central relay server — the same transport the
permission/question hooks already use. The notification carries the agent's
*last message* (summary, question, or whatever it ended on) so you can tell at a
glance what the session is waiting on, without switching back to the terminal.

Triggers on:
- idle_prompt: When Claude is waiting for user input

Behaviour notes:
- Transport is the relay (``relay_server.client.RelayClient``), reusing the
  config at ``~/.config/claude-tg-relay/config.toml``. The legacy
  ``notify_escalate`` desktop→mobile path is gone.
- The message is fire-and-forget: no buttons, no reply expected (the
  Notification hook can't inject a reply back into the running session — making
  it answerable is a future iteration).
- Idle notifications are suppressed while async background agents are still
  running, since a parent session looks "idle" while it waits on child Tasks.

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

import hashlib
import html
import json
import os
import sys
import re
from pathlib import Path

import telegram_permission_router as telegram_router
from telegram_permission_router import (
    load_telegram_config,
    send_idle_notification,
)

# Configuration
CLAUDE_DIR = Path.home() / ".claude"
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "canceled", "error"}

# Telegram caps a message at 4096 chars. We forward the *tail* of the agent's
# last message (the end is where the question / conclusion lives), HTML-escaped,
# leaving headroom for the title and markers. Budget is measured on the escaped
# string so we never overflow even when the text is escape-heavy.
MAX_ESCAPED_BODY = 3800


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


def _message_text_blocks(content) -> str:
    """Join the ``text`` blocks of an assistant message's content.

    Content may be a plain string (older transcript shape) or a list of typed
    blocks (``text`` / ``thinking`` / ``tool_use`` / ...). We only want the
    natural-language ``text`` blocks; thinking and tool calls are dropped.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_last_agent_message(transcript_path: str) -> str | None:
    """Return the last *main-agent* assistant text from the transcript.

    Scans the JSONL transcript from the end, skipping subagent/sidechain
    entries (``isSidechain: true``) and assistant turns that carried only
    tool calls or thinking, until it finds an assistant message with real text.
    Returns None if the transcript is missing/unreadable or has no such message.
    """
    if not transcript_path:
        return None
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        debug_log(f"Transcript not found for last-message extraction: {transcript_file}")
        return None

    try:
        with open(transcript_file, "r") as f:
            lines = f.readlines()
    except Exception as e:
        debug_log(f"Error reading transcript for last message: {e}")
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        # Skip subagent output — we only forward the orchestrator's own message.
        if entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _message_text_blocks(message.get("content"))
        if text:
            return text

    debug_log("No assistant text block found in transcript")
    return None


def _tail_escape(raw: str, max_escaped_len: int) -> tuple[str, bool]:
    """HTML-escape ``raw``, keeping only the tail that fits ``max_escaped_len``.

    Telegram needs valid HTML, so we escape *before* measuring — and trim from
    the *raw* side (then re-escape) so a cut never lands inside an entity like
    ``&amp;``. Returns ``(escaped_tail, truncated)``.
    """
    escaped = html.escape(raw, quote=False)
    if len(escaped) <= max_escaped_len:
        return escaped, False

    # Trim raw from the front in chunks until the escaped form fits. Escaping is
    # monotonic in length, so this converges quickly.
    trimmed = raw
    while trimmed and len(html.escape(trimmed, quote=False)) > max_escaped_len:
        # Drop a generous chunk; the body budget is large so few iterations run.
        trimmed = trimmed[256:]
    # Snap to the next line/word boundary so we don't start mid-token.
    newline = trimmed.find("\n")
    if 0 <= newline <= 120:
        trimmed = trimmed[newline + 1:]
    return html.escape(trimmed, quote=False), True


def build_notification_text(
    workspace_name: str,
    session_name: str | None,
    last_message: str | None,
    fallback: str,
) -> str:
    """Compose the HTML notification body forwarded to Telegram."""
    title = f"<b>{html.escape(workspace_name, quote=False)}</b>"
    if session_name:
        title += f" <i>{html.escape(session_name, quote=False)}</i>"

    lines = [title, "💤 <b>Idle</b> — waiting for input", ""]

    body = (last_message or "").strip()
    if body:
        escaped, truncated = _tail_escape(body, MAX_ESCAPED_BODY)
        if truncated:
            lines.append("<i>…(truncated)</i>")
        lines.append(f"<blockquote>{escaped}</blockquote>")
    else:
        # No agent text available — fall back to Claude Code's canned string.
        lines.append(html.escape(fallback or "Claude is waiting for your input.", quote=False))

    return "\n".join(lines)


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
        if has_active_background_agents(transcript_path):
            debug_log("Skipping idle notification: background agents are still running")
            sys.exit(0)

        # Bring up the relay transport. If it's not configured we silently no-op
        # (the terminal still shows the idle state); nothing to escalate to.
        load_telegram_config()
        if not telegram_router.TELEGRAM_ENABLED:
            debug_log("Relay not configured; skipping idle notification")
            sys.exit(0)

        workspace_name = get_workspace_name(cwd)
        session_name = get_session_name(session_id, cwd)
        last_message = extract_last_agent_message(transcript_path)

        text = build_notification_text(
            workspace_name=workspace_name,
            session_name=session_name,
            last_message=last_message,
            fallback=message,
        )

        # Derive the dedupe key from the composed message itself: an idle prompt
        # re-fired for the same state produces identical text, so the relay's
        # idempotency layer replays the original send instead of double-posting.
        # Any change to the message (new last message, slug appearing) yields a
        # new key and a fresh notification.
        body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        dedupe_key = f"idle:{session_id or 'unknown'}:{body_hash}"
        message_id = send_idle_notification(text, dedupe_key)
        if message_id is None:
            debug_log("Idle notification send failed (relay returned no id)")
        else:
            debug_log(f"Idle notification sent: relay message {message_id}")

        # Always exit 0 - notification failure shouldn't block Claude
        sys.exit(0)

    except Exception as e:
        debug_log(f"ERROR: {type(e).__name__}: {str(e)}")
        # Don't fail the hook - notification issues shouldn't block Claude
        sys.exit(0)


if __name__ == '__main__':
    main()
