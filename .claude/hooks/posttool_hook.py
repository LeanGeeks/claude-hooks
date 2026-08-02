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


def resolve_role_token(request) -> "str | None":
    """Re-resolve the installation token for ``request.role``.

    The token is never persisted — only the role id is — so this hook, which
    runs in a separate process from the one that sent the message, has to look
    it up again from the row's own ``cwd``.

    Returns ``None`` (the default destination) when the request carries no role
    or when resolution fails for any reason. A failed revoke must never affect
    tool execution, so every failure is logged and swallowed.
    """
    role = getattr(request, 'role', None)
    if not role:
        return None
    try:
        import roles_config

        catalog = roles_config.load_catalog(request.cwd)
        if catalog is None:
            log_debug(f"No roles catalog for {request.cwd}; using default client")
            return None
        bindings = roles_config.load_bindings()
        destination = roles_config.resolve_destination(catalog, bindings, role)
        if destination.token is None:
            log_debug(f"Role {role!r} has no binding; using default client")
        return destination.token
    except Exception as e:
        log_debug(f"Role token resolution failed for {role!r}: {type(e).__name__}: {e}")
        return None


def revoke_telegram_message(request) -> bool:
    """
    Revoke a Telegram message by removing buttons and adding reaction.

    Args:
        request: The PermissionRequest whose ``telegram_message_id`` should be
            revoked. Taking the whole row (not a bare id) is what lets us reach
            the right destination: the relay scopes messages to the installation
            that created them, so cancelling a role message against the default
            token would 404.

    Returns:
        True if successful, False otherwise
    """
    message_id = request.telegram_message_id
    token = resolve_role_token(request)

    # Remove buttons
    telegram_permission_router.remove_inline_buttons(message_id, token=token)

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
            success = revoke_telegram_message(pending_request)
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
