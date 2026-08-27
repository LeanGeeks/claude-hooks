#!/usr/bin/env python3
"""
PostToolUse Hook - Revoke Telegram messages when terminal is used

This hook is triggered after a tool is executed. It checks if there's
a pending Telegram permission request for that tool/session and revokes
the message (removes buttons, adds reaction).

This provides coordination between Telegram and terminal prompts:
- If user responds via Telegram first, buttons are disabled
- If user responds via terminal first, Telegram message is revoked
"""

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

# Add hooks directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from permission_state_store import (
    find_pending_request_by_tool_session,
    resolve_via_terminal,
    sweep_orphaned_requests,
)
import telegram_permission_router

# Debug logging
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = Path.home() / ".claude" / "posttool_debug.log"

# Backstop on the *reduced* terminal-answers value written onto the state-store
# row. Nothing realistic approaches it after the reduction below; capping the
# raw ``tool_response`` instead would truncate real captures into invalid JSON
# and silently disable the structured path for exactly the rich calls it serves.
MAX_TERMINAL_ANSWERS_BYTES = 8192


def log_debug(message: str):
    """Log debug message if debug mode is enabled."""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now(timezone.utc).isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass


def reduce_tool_response(tool_response) -> str:
    """Reduce an ``AskUserQuestion`` ``tool_response`` to what the wait phase needs.

    ``tool_response`` is a dict ``{questions, answers, annotations}``. Almost all
    of its bulk is ``questions`` (option descriptions) and
    ``annotations[*].preview``, and none of that is needed to render an answer in
    Telegram. We keep exactly two maps, both keyed by the verbatim question text::

        {"answers": {...}, "notes": {...}}

    Anything that is not a dict (older Claude Code, or a shape change) is stored
    as ``str(...)`` unchanged and left to the parser's prose tier.

    The result is capped at ``MAX_TERMINAL_ANSWERS_BYTES`` as a backstop only,
    shedding notes first and then the longest answers, so what survives is
    always still valid JSON.
    """
    if not isinstance(tool_response, dict):
        return str(tool_response)

    answers = dict(tool_response.get("answers") or {})
    annotations = tool_response.get("annotations") or {}
    notes = {}
    if isinstance(annotations, dict):
        notes = {
            q: a["notes"]
            for q, a in annotations.items()
            if isinstance(a, dict) and "notes" in a
        }

    reduced = {"answers": answers, "notes": notes}

    def _encode() -> str:
        return json.dumps(reduced, ensure_ascii=False)

    encoded = _encode()
    if len(encoded.encode("utf-8")) <= MAX_TERMINAL_ANSWERS_BYTES:
        return encoded

    # Backstop path: shed the least useful content first so the value stays
    # parseable rather than being truncated into invalid JSON.
    reduced["notes"] = {}
    encoded = _encode()
    while (
        len(encoded.encode("utf-8")) > MAX_TERMINAL_ANSWERS_BYTES
        and reduced["answers"]
    ):
        longest = max(
            reduced["answers"],
            key=lambda k: len(k) + len(str(reduced["answers"][k])),
        )
        del reduced["answers"][longest]
        encoded = _encode()
    return encoded


def main():
    """Main hook entry point."""
    try:
        # Load Telegram configuration
        telegram_permission_router.load_telegram_config()

        # Read hook input from stdin
        raw_input = sys.stdin.read()
        log_debug(f"=== PostToolUse Hook called ===")
        log_debug(f"Raw input: {raw_input[:500]}...")

        input_data = json.loads(raw_input)

        # Extract key fields
        session_id = input_data.get('session_id', '')
        cwd = input_data.get('cwd', os.getcwd())
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})
        tool_response = input_data.get('tool_response')
        agent_id = input_data.get('agent_id') or None

        log_debug(f"Session: {session_id}")
        log_debug(f"CWD: {cwd}")
        log_debug(f"Tool: {tool_name}")
        log_debug(f"Agent ID: {agent_id}")

        # Skip if Telegram is not enabled
        if not telegram_permission_router.TELEGRAM_ENABLED:
            log_debug("Telegram not enabled, skipping")
            sys.exit(0)

        # Sweep orphaned rows (epic 26 layer 2). Runs before find_pending so the
        # orphan check runs on every tool call — the highest-frequency hook event.
        # State first, buttons second (invariant 5); fail open (invariant 1).
        try:
            for _row in sweep_orphaned_requests():
                try:
                    telegram_permission_router.revoke_telegram_message(_row)
                except Exception as e:      # noqa: BLE001 — H1
                    log_debug(f"Sweep: revoke of {_row.telegram_message_id} failed: {e}")
        except Exception as e:          # noqa: BLE001 — H1 defence in depth
            log_debug(f"Sweep: unexpected error in sweep_orphaned_requests: {type(e).__name__}: {e}")

        # Find pending request for this tool/session/agent.
        # Pass tool_input so the three-valued classifier (task 27 §4) can
        # exclude rows that belong to a concurrent call of the same tool in
        # the same session, preventing cross-call crosstalk.
        pending_request = find_pending_request_by_tool_session(
            session_id=session_id,
            tool_name=tool_name,
            cwd=cwd,
            agent_id=agent_id,
            tool_input=tool_input,
        )

        if not pending_request:
            log_debug("No pending Telegram request found for this tool")
            sys.exit(0)

        log_debug(f"Found pending request: {pending_request.request_id}")
        log_debug(f"  Message ID: {pending_request.telegram_message_id}")

        # Carry the terminal's answers onto the row so the parked
        # PermissionRequest hook can patch them into whichever chat the question
        # went to (brd §5.5). Only AskUserQuestion has an ``answers`` map; every
        # other tool's response would just bloat the append-only JSONL.
        terminal_answers = None
        if tool_name == 'AskUserQuestion' and tool_response is not None:
            try:
                terminal_answers = reduce_tool_response(tool_response)
            except Exception as e:  # noqa: BLE001 — never affect tool execution
                log_debug(f"Failed to reduce tool_response: {type(e).__name__}: {e}")

        # Mark as resolved via terminal
        updated = resolve_via_terminal(pending_request.request_id, terminal_answers)

        if not updated:
            log_debug("Failed to update request state (already resolved?)")
            sys.exit(0)

        # Revoke the Telegram message if we have a message ID
        if pending_request.telegram_message_id:
            success = telegram_permission_router.revoke_telegram_message(pending_request)
            if success:
                log_debug(f"Revoked Telegram message {pending_request.telegram_message_id}")
            else:
                log_debug(f"Failed to revoke Telegram message")
        else:
            log_debug("No Telegram message ID to revoke")

        sys.exit(0)

    except Exception as e:
        # On error, just exit cleanly (don't affect tool execution)
        log_debug(f"ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        log_debug(traceback.format_exc())
        sys.exit(0)


if __name__ == '__main__':
    main()
