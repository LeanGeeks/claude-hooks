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
import re
import shutil
import subprocess
import sys
from pathlib import Path

import telegram_permission_router as telegram_router
from telegram_permission_router import (
    load_telegram_config,
    send_idle_notification,
)

# Configuration
CLAUDE_DIR = Path.home() / ".claude"

# Telegram caps a message at 4096 chars. We forward the *tail* of the agent's
# last message (the end is where the question / conclusion lives), HTML-escaped,
# leaving headroom for the title and markers. Budget is measured on the escaped
# string so we never overflow even when the text is escape-heavy.
MAX_ESCAPED_BODY = 3800

# amux launches each session in a tmux session named ``amux-<CC_NAME>``. We use
# this to detect amux-hosted sessions and recover the amux session name so a
# Telegram reply can be injected back via ``amux send`` (task 09).
AMUX_TMUX_PREFIX = "amux-"


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



def _is_background_tool_use(block: dict) -> bool:
    """Return True if a tool_use block represents a background task.

    Background tasks are:
    - Agent tool (background by default unless run_in_background is explicitly false)
    - Bash tool with run_in_background=true
    - Monitor tool (always background)
    - Workflow tool (always background)
    """
    name = block.get("name", "")
    inp = block.get("input") or {}
    if name == "Agent":
        return inp.get("run_in_background") is not False
    if name == "Bash":
        return inp.get("run_in_background") is True
    if name in ("Monitor", "Workflow"):
        return True
    return False


# task-notification payloads are XML-ish text inside a plain-string user (or
# queue-operation) entry, e.g.:
#   <task-notification><task-id>a1b2</task-id><tool-use-id>toolu_x</tool-use-id>
#   <status>completed</status>...</task-notification>
_NOTIF_TASK_ID_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_NOTIF_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")

# Fallbacks for pulling the task id out of a launch ack's *text* when the
# entry-level toolUseResult metadata is missing.
_ACK_TASK_ID_RES = (
    re.compile(r"agentId:\s*([A-Za-z0-9_-]+)"),
    re.compile(r"running in background with ID:\s*([A-Za-z0-9_-]+)"),
    re.compile(r"Monitor started \(task ([A-Za-z0-9_-]+)"),
)

# SendMessage resume ack: 'Agent "a47c..." was stopped (completed); resumed it
# in the background with your message.'
_RESUMED_AGENT_RE = re.compile(r'Agent "([^"]+)"')


def _tool_result_text(block: dict) -> str:
    """Flatten a tool_result block's content to plain text."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def has_active_background_agents(transcript_path: str) -> bool:
    """
    Detect if the current session has background work still running: async
    subagents (Agent tool), background shell commands (Bash with
    run_in_background), Monitor watchers, or Workflow runs.

    Background launches resolve their tool_result *immediately* with a launch
    ack ("Async agent launched successfully… agentId: X" / "Command running in
    background with ID: X"), so a matched tool_result does NOT mean the work
    finished. Actual completion is a later plain-string user entry carrying a
    ``<task-notification>`` whose ``<task-id>``/``<tool-use-id>`` point back at
    the launch. We replay the transcript in order:

    - background tool_use            → running (keyed by tool_use_id)
    - launch ack                     → extract task id (toolUseResult.agentId /
                                       backgroundTaskId / taskId / runId) so
                                       notifications can be matched; an ack with
                                       no async marker means the tool actually
                                       ran synchronously → not running
    - <task-notification>            → task stopped → not running; except a
                                       persistent Monitor's <event> (no
                                       <status>), which fires per event while
                                       the monitor keeps watching
    - TaskStop tool_use              → task killed → not running
    - SendMessage ack "resumed it in the background" → agent running again
    """
    if not transcript_path:
        debug_log("No transcript_path provided; cannot detect background agents")
        return False

    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        debug_log(f"Transcript file not found: {transcript_file}")
        return False

    # running: launch tool_use_id → kind ("launching:<Tool>", "agent", "bash",
    # "monitor", "monitor-persistent", "workflow").
    running: dict[str, str] = {}
    task_to_key: dict[str, str] = {}
    pending_sends: dict[str, str | None] = {}  # SendMessage tool_use_id → input "to"

    def resolve_launch_ack(tool_use_id: str, block: dict, tool_use_result) -> None:
        launch_kind = running[tool_use_id]  # "launching:<ToolName>"
        tool_name = launch_kind.split(":", 1)[1]
        if block.get("is_error"):
            running.pop(tool_use_id, None)
            return
        task_id = None
        persistent = False
        if isinstance(tool_use_result, dict):
            task_id = (
                tool_use_result.get("agentId")
                or tool_use_result.get("backgroundTaskId")
                or tool_use_result.get("taskId")
                or tool_use_result.get("runId")
            )
            persistent = bool(tool_use_result.get("persistent"))
        text = _tool_result_text(block)
        if not task_id:
            for pattern in _ACK_TASK_ID_RES:
                m = pattern.search(text)
                if m:
                    task_id = m.group(1)
                    break
            if tool_name == "Monitor" and "persistent" in text:
                persistent = True
        if task_id:
            if tool_name == "Monitor" and persistent:
                kind = "monitor-persistent"
            else:
                kind = tool_name.lower()
            running[tool_use_id] = kind
            task_to_key[task_id] = tool_use_id
        else:
            # No async-launch marker: the tool ran synchronously (its result IS
            # the completion) or the ack format changed. Fail open — a spurious
            # idle ping beats silently suppressing them for the whole session.
            debug_log(
                f"Launch ack for {tool_name} ({tool_use_id}) has no task id; "
                "treating as completed"
            )
            running.pop(tool_use_id, None)

    def resolve_send_ack(tool_use_id: str, tool_use_result) -> None:
        to = pending_sends.pop(tool_use_id, None)
        if not isinstance(tool_use_result, dict) or tool_use_result.get("success") is not True:
            return
        message = str(tool_use_result.get("message", ""))
        if "resumed" not in message:
            return
        m = _RESUMED_AGENT_RE.search(message)
        agent_id = m.group(1) if m else None
        # Only re-arm agents we can tie back to a launch in this transcript —
        # an unmatchable key (e.g. a name where notifications carry the id)
        # could never be cleared and would suppress idle pings forever.
        key = task_to_key.get(agent_id) or (task_to_key.get(to) if to else None)
        if not key:
            debug_log(
                f"SendMessage resumed unknown agent (to={to!r}); not tracking"
            )
            return
        running[key] = "agent"

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

                # Subagent sidechain entries track their own tools, not ours.
                if entry.get("isSidechain"):
                    continue

                msg = entry.get("message")

                # --- task-notification: a background task stopped ---
                # Delivered as a plain-string user message or a queued_command
                # attachment; the enqueue is also journaled as a queue-operation
                # entry (any one of them counts). Only these entry types count:
                # an assistant message *quoting* a notification must not.
                content_str = None
                if entry.get("type") == "queue-operation":
                    content_str = entry.get("content")
                elif entry.get("type") == "attachment":
                    prompt = (entry.get("attachment") or {}).get("prompt")
                    if isinstance(prompt, str):
                        content_str = prompt
                elif (
                    entry.get("type") == "user"
                    and isinstance(msg, dict)
                    and isinstance(msg.get("content"), str)
                ):
                    content_str = msg["content"]
                if isinstance(content_str, str) and "<task-notification>" in content_str:
                    has_status = "<status>" in content_str
                    is_event = "<event>" in content_str
                    keys = set()
                    for task_id in _NOTIF_TASK_ID_RE.findall(content_str):
                        key = task_to_key.get(task_id)
                        if key:
                            keys.add(key)
                    for tool_use_id in _NOTIF_TOOL_USE_ID_RE.findall(content_str):
                        if tool_use_id in running:
                            keys.add(tool_use_id)
                    for key in keys:
                        if (
                            running.get(key) == "monitor-persistent"
                            and is_event
                            and not has_status
                        ):
                            continue  # persistent monitor: fired, still watching
                        running.pop(key, None)
                    continue

                # --- assistant tool_use blocks: launches / stops / resumes ---
                if entry.get("type") == "assistant" and isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not (
                                isinstance(block, dict)
                                and block.get("type") == "tool_use"
                            ):
                                continue
                            name = block.get("name", "")
                            inp = block.get("input") or {}
                            tool_id = block.get("id")
                            if _is_background_tool_use(block) and tool_id:
                                running[tool_id] = f"launching:{name}"
                            elif name == "TaskStop":
                                task_id = inp.get("task_id") or inp.get("shell_id")
                                key = task_to_key.pop(task_id, None) if task_id else None
                                if key:
                                    running.pop(key, None)
                            elif name == "SendMessage" and tool_id:
                                pending_sends[tool_id] = inp.get("to")
                    continue

                # --- tool_result blocks: launch / SendMessage acks ---
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not (
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                            ):
                                continue
                            tool_use_id = block.get("tool_use_id")
                            if not tool_use_id:
                                continue
                            if running.get(tool_use_id, "").startswith("launching:"):
                                resolve_launch_ack(
                                    tool_use_id, block, entry.get("toolUseResult")
                                )
                            elif tool_use_id in pending_sends:
                                resolve_send_ack(tool_use_id, entry.get("toolUseResult"))

    except Exception as e:
        debug_log(f"Error scanning transcript for background agents: {e}")
        return False

    if running:
        debug_log(
            "Background tasks still active: "
            + ", ".join(f"{k}={v}" for k, v in sorted(running.items()))
        )
        return True

    debug_log("No active background tasks detected")
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


def resolve_amux_session() -> str | None:
    """Return this session's amux name, or None if it is not amux-hosted.

    amux starts each session in a tmux session named ``amux-<CC_NAME>``, so the
    *current pane's* tmux session name is the source of truth. We deliberately do
    not use cwd or env: ``CC_DIR``/``CC_NAME`` are not exported into the session,
    and a session's cwd can differ from its registered ``CC_DIR``.

    Returns the bare amux name (``hyppie-flow``), or None when there's no tmux,
    no ``$TMUX_PANE``, or the session isn't an ``amux-`` one (e.g. a bare
    ``claude`` in tmux) — in which case replies can't be injected and we stay
    notify-only.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane or not shutil.which("tmux"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        debug_log(f"tmux display-message failed: {e}")
        return None
    if result.returncode != 0:
        debug_log(f"tmux display-message rc={result.returncode}: {result.stderr.strip()}")
        return None
    session_name = result.stdout.strip()
    if not session_name.startswith(AMUX_TMUX_PREFIX):
        debug_log(f"Not an amux session (tmux session={session_name!r})")
        return None
    amux_name = session_name[len(AMUX_TMUX_PREFIX):]
    return amux_name or None


def spawn_reply_injector(message_id: int, amux_name: str) -> None:
    """Detach a ``reply_injector.py`` process for this idle notification.

    The injector long-polls the relay for this one message and, on a text reply,
    injects it into the amux session. We spawn it detached (``start_new_session``)
    and return immediately so the idle hook never blocks the session.
    """
    script = Path(__file__).with_name("reply_injector.py")
    if not script.exists():
        debug_log(f"reply_injector.py not found at {script}; skipping injection")
        return
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--message-id",
                str(message_id),
                "--amux",
                amux_name,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        debug_log(f"Spawned reply injector for message {message_id} → amux:{amux_name}")
    except Exception as e:  # noqa: BLE001
        debug_log(f"Failed to spawn reply injector: {e}")


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

        # An amux-hosted session can receive a reply back (task 09): we send the
        # notification as a force-reply so the user can answer in-thread, then
        # arm a detached injector. A non-amux session stays notify-only.
        amux_name = resolve_amux_session()
        reply_enabled = amux_name is not None

        # Derive the dedupe key from the composed message itself: an idle prompt
        # re-fired for the same state produces identical text, so the relay's
        # idempotency layer replays the original send instead of double-posting.
        # Any change to the message (new last message, slug appearing) yields a
        # new key and a fresh notification.
        body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        dedupe_key = f"idle:{session_id or 'unknown'}:{body_hash}"
        message_id = send_idle_notification(
            text, dedupe_key, reply_required=reply_enabled
        )
        if message_id is None:
            debug_log("Idle notification send failed (relay returned no id)")
        elif reply_enabled:
            spawn_reply_injector(message_id, amux_name)
            debug_log(
                f"Idle notification {message_id} sent; reply injector armed "
                f"for amux:{amux_name}"
            )
        else:
            debug_log(f"Idle notification sent: relay message {message_id} (notify-only)")

        # Always exit 0 - notification failure shouldn't block Claude
        sys.exit(0)

    except Exception as e:
        debug_log(f"ERROR: {type(e).__name__}: {str(e)}")
        # Don't fail the hook - notification issues shouldn't block Claude
        sys.exit(0)


if __name__ == '__main__':
    main()
