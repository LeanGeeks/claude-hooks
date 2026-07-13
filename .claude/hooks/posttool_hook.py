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
)
import telegram_permission_router

# Debug logging
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = Path.home() / ".claude" / "posttool_debug.log"


def log_debug(message: str):
    """Log debug message if debug mode is enabled."""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now(timezone.utc).isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass


def revoke_telegram_message(message_id: int) -> bool:
    """
    Revoke a Telegram message by removing buttons and adding reaction.

    Args:
        message_id: Telegram message ID

    Returns:
        True if successful, False otherwise
    """
    # Remove buttons
    telegram_permission_router.remove_inline_buttons(message_id)

    # Add reaction
    return telegram_permission_router.set_message_reaction(message_id, '✅')


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
        agent_id = input_data.get('agent_id') or None

        log_debug(f"Session: {session_id}")
        log_debug(f"CWD: {cwd}")
        log_debug(f"Tool: {tool_name}")
        log_debug(f"Agent ID: {agent_id}")

        # Skip if Telegram is not enabled
        if not telegram_permission_router.TELEGRAM_ENABLED:
            log_debug("Telegram not enabled, skipping")
            sys.exit(0)

        # Find pending request for this tool/session/agent
        pending_request = find_pending_request_by_tool_session(
            session_id=session_id,
            tool_name=tool_name,
            cwd=cwd,
            agent_id=agent_id,
        )

        if not pending_request:
            log_debug("No pending Telegram request found for this tool")
            sys.exit(0)

        log_debug(f"Found pending request: {pending_request.request_id}")
        log_debug(f"  Message ID: {pending_request.telegram_message_id}")

        # Mark as resolved via terminal
        updated = resolve_via_terminal(pending_request.request_id)

        if not updated:
            log_debug("Failed to update request state (already resolved?)")
            sys.exit(0)

        # Revoke the Telegram message if we have a message ID
        if pending_request.telegram_message_id:
            success = revoke_telegram_message(pending_request.telegram_message_id)
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
