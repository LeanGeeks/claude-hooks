#!/usr/bin/env python3
"""
Telegram Permission Router - Handles Telegram-side control plane for permissions

This module provides the Telegram integration for the PermissionRequest hook:
- Sends messages with inline action buttons
- Consumes button callbacks and text replies
- Routes actions to state updates
- Handles whitelist updates safely

Transport: Long polling (chosen for simplicity - no webhook server needed)

Integration:
- Called by permission_request_hook.py to send messages and wait for responses
- Can run standalone for testing callback handling
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable

from permission_state_store import (
    PermissionRequest,
    RequestState,
    create_request,
    get_request,
    update_request_state,
    set_telegram_message_id,
    find_request_by_message_id,
    find_pending_request_by_session,
    cleanup_expired_requests,
    debug_log,
)

# Configuration
TELEGRAM_CONFIG_FILE = Path.home() / ".config/claude/telegram.conf"
TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None
TELEGRAM_ENABLED = False
TELEGRAM_CONFIG_SOURCE = "unknown"
ERROR_LOG_FILE = Path.home() / ".claude" / "permission_telegram_errors.log"

# Long polling configuration
POLL_TIMEOUT = 5  # seconds per poll
POLL_RETRY_DELAY = 1  # seconds between failed polls

# Callback data format: "action:request_id"
CALLBACK_DATA_SEPARATOR = ":"


def error_log(message: str):
    """Always write Telegram permission integration errors."""
    try:
        ERROR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG_FILE, 'a') as f:
            timestamp = datetime.now().isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def load_telegram_config():
    """Load Telegram configuration from file or environment."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED, TELEGRAM_CONFIG_SOURCE

    # Try environment variables first
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
    TELEGRAM_CONFIG_SOURCE = "env" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "unknown"

    # Try config file if env vars not set
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        if TELEGRAM_CONFIG_FILE.exists():
            try:
                with open(TELEGRAM_CONFIG_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('BOT_TOKEN='):
                            TELEGRAM_BOT_TOKEN = line.split('=', 1)[1].strip().strip('"').strip("'")
                        elif line.startswith('CHAT_ID='):
                            TELEGRAM_CHAT_ID = line.split('=', 1)[1].strip().strip('"').strip("'")
                if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and TELEGRAM_CONFIG_SOURCE != "env":
                    TELEGRAM_CONFIG_SOURCE = str(TELEGRAM_CONFIG_FILE)
            except Exception as e:
                debug_log(f"Error loading Telegram config: {e}")
                error_log(f"Failed loading Telegram config from {TELEGRAM_CONFIG_FILE}: {e}")
        else:
            error_log(
                f"Telegram config missing: {TELEGRAM_CONFIG_FILE} "
                "(and TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID env vars not set)"
            )

    # Minimal validation to make configuration issues visible.
    if TELEGRAM_BOT_TOKEN and ":" not in TELEGRAM_BOT_TOKEN:
        error_log("Invalid BOT_TOKEN format (expected '<id>:<token>')")
        TELEGRAM_BOT_TOKEN = None

    if TELEGRAM_CHAT_ID:
        try:
            int(str(TELEGRAM_CHAT_ID))
        except ValueError:
            error_log(f"Invalid CHAT_ID (must be integer): {TELEGRAM_CHAT_ID!r}")
            TELEGRAM_CHAT_ID = None

    TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    debug_log(f"Telegram enabled: {TELEGRAM_ENABLED}")
    if not TELEGRAM_ENABLED:
        error_log("Telegram integration disabled after config load")


def _telegram_api_request(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Make a Telegram Bot API request.

    Args:
        method: API method name
        payload: Request payload

    Returns:
        Response result dict if successful, None otherwise
    """
    if not TELEGRAM_ENABLED:
        error_log(f"Telegram API call skipped ({method}): integration disabled")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )

        timeout = payload.get('timeout', 10) + 5 if 'timeout' in payload else 15
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))

            if result.get('ok'):
                return result.get('result')
            else:
                debug_log(f"Telegram API error: {result.get('description')}")
                error_log(
                    f"Telegram API error in {method}: "
                    f"{result.get('description', 'unknown error')}"
                )
                return None

    except urllib.error.URLError as e:
        debug_log(f"Telegram URL error: {e}")
        error_log(f"Telegram network error in {method}: {e}")
        return None
    except Exception as e:
        debug_log(f"Telegram API request error: {e}")
        error_log(f"Telegram request exception in {method}: {e}")
        return None


def send_permission_message(
    request: PermissionRequest,
    workspace_name: str,
    session_name: Optional[str] = None,
) -> Optional[int]:
    """
    Send a Telegram message with action buttons for a permission request.

    Args:
        request: The permission request
        workspace_name: Name of the workspace
        session_name: Optional session name/slug

    Returns:
        Telegram message ID if successful, None otherwise
    """
    if not TELEGRAM_ENABLED:
        debug_log("Telegram not enabled, skipping message send")
        return None

    # Build message text
    title = f"*{workspace_name}*"
    if session_name:
        title += f" _{session_name}_"

    # Format command summary
    command_summary = _format_command_summary(
        request.tool_name,
        request.tool_input
    )

    message_lines = [
        title,
        "",
        f"*Permission Request* `{request.request_id}`",
        "",
        "```",
        command_summary,
        "```",
        "",
        "Approve this command?"
    ]

    message_text = "\n".join(message_lines)

    # Build inline buttons
    inline_buttons = [
        [
            {"text": "Allow", "callback_data": f"allow{CALLBACK_DATA_SEPARATOR}{request.request_id}"},
            {"text": "Deny", "callback_data": f"deny{CALLBACK_DATA_SEPARATOR}{request.request_id}"}
        ],
        [
            {"text": "Stop", "callback_data": f"stop{CALLBACK_DATA_SEPARATOR}{request.request_id}"},
            {"text": "Whitelist", "callback_data": f"whitelist{CALLBACK_DATA_SEPARATOR}{request.request_id}"}
        ]
    ]

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "Markdown",
        "disable_notification": False,
        "reply_markup": {
            "inline_keyboard": inline_buttons
        }
    }

    result = _telegram_api_request("sendMessage", payload)

    if result:
        message_id = result.get('message_id')
        debug_log(
            f"Telegram message sent: message_id={message_id}, "
            f"chat_id={TELEGRAM_CHAT_ID}, source={TELEGRAM_CONFIG_SOURCE}"
        )

        # Update request with message ID
        set_telegram_message_id(request.request_id, message_id)

        return message_id

    return None


def _format_command_summary(tool_name: str, tool_input: Dict[str, Any], max_length: int = 100) -> str:
    """
    Format a concise summary of the tool/command.

    Args:
        tool_name: Name of the tool being called
        tool_input: Input parameters for the tool
        max_length: Maximum length of the summary

    Returns:
        Formatted summary string
    """
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        if len(command) > max_length:
            return command[:max_length-3] + '...'
        return command
    elif tool_name in ('Read', 'Write', 'Edit'):
        file_path = tool_input.get('file_path', tool_input.get('path', ''))
        return f"{tool_name}({file_path})"
    elif tool_name == 'WebFetch':
        url = tool_input.get('url', '')
        return f"WebFetch({url[:50]}...)" if len(url) > 50 else f"WebFetch({url})"
    else:
        # Generic format
        input_str = json.dumps(tool_input)
        if len(input_str) > max_length:
            input_str = input_str[:max_length-3] + '...'
        return f"{tool_name}: {input_str}"


def answer_callback_query(callback_query_id: str, text: str = "") -> bool:
    """
    Answer a callback query to remove the loading state.

    Args:
        callback_query_id: ID of the callback query
        text: Optional text to show as notification

    Returns:
        True if successful, False otherwise
    """
    payload = {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": False
    }

    result = _telegram_api_request("answerCallbackQuery", payload)
    return result is not None


def edit_message_text(message_id: int, text: str, inline_buttons: Optional[List] = None) -> bool:
    """
    Edit a Telegram message's text.

    Args:
        message_id: ID of the message to edit
        text: New message text
        inline_buttons: Optional new inline keyboard

    Returns:
        True if successful, False otherwise
    """
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    if inline_buttons:
        payload["reply_markup"] = {
            "inline_keyboard": inline_buttons
        }

    result = _telegram_api_request("editMessageText", payload)
    return result is not None


def delete_message(message_id: int) -> bool:
    """
    Delete a Telegram message.

    Args:
        message_id: ID of the message to delete

    Returns:
        True if successful, False otherwise
    """
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id
    }

    result = _telegram_api_request("deleteMessage", payload)
    return result is not None


def set_message_reaction(message_id: int, emoji: str) -> bool:
    """
    Set a reaction on a Telegram message.

    Args:
        message_id: ID of the message to react to
        emoji: Emoji reaction (e.g., "✅", "❌", "👍")

    Returns:
        True if successful, False otherwise
    """
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "reaction": [
            {"type": "emoji", "emoji": emoji}
        ]
    }

    result = _telegram_api_request("setMessageReaction", payload)
    return result is not None


def remove_inline_buttons(message_id: int) -> bool:
    """
    Remove inline buttons from a message without changing its text.

    Args:
        message_id: ID of the message to update

    Returns:
        True if successful, False otherwise
    """
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "reply_markup": {
            "inline_keyboard": []
        }
    }

    result = _telegram_api_request("editMessageReplyMarkup", payload)
    return result is not None


def get_updates(offset: Optional[int] = None, timeout: int = POLL_TIMEOUT) -> List[Dict[str, Any]]:
    """
    Get updates from Telegram Bot API using long polling.

    Args:
        offset: Update ID offset for acknowledgment
        timeout: Long polling timeout in seconds

    Returns:
        List of update objects
    """
    if not TELEGRAM_ENABLED:
        return []

    payload = {"timeout": timeout}
    if offset:
        payload["offset"] = offset

    result = _telegram_api_request("getUpdates", payload)
    return result if result else []


def parse_callback_data(callback_data: str) -> tuple[str, Optional[str]]:
    """
    Parse callback data into action and request_id.

    Args:
        callback_data: Callback data string (format: "action:request_id" or "action")

    Returns:
        Tuple of (action, request_id or None)
    """
    parts = callback_data.split(CALLBACK_DATA_SEPARATOR)
    action = parts[0]
    request_id = parts[1] if len(parts) > 1 else None
    return action, request_id


def handle_callback_query(callback: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Handle a callback query from inline button press.

    Args:
        callback: Callback query object from Telegram

    Returns:
        Decision dict if action was taken, None otherwise
    """
    # Verify user is authorized
    from_user = callback.get('from', {})
    user_id = from_user.get('id')
    if str(user_id) != str(TELEGRAM_CHAT_ID):
        debug_log(f"Unauthorized callback from user_id={user_id}")
        answer_callback_query(callback['id'], "Unauthorized")
        return None

    # Parse callback data
    callback_data = callback.get('data', '')
    action, request_id = parse_callback_data(callback_data)
    debug_log(f"Received callback: action={action}, request_id={request_id}")

    # Find the request
    message = callback.get('message', {})
    message_id = message.get('message_id')

    request = None
    if request_id:
        request = get_request(request_id)
    elif message_id:
        request = find_request_by_message_id(message_id)

    if not request:
        debug_log(f"Request not found for callback")
        answer_callback_query(callback['id'], "Request not found or expired")
        return None

    # Validate action
    valid_actions = ['allow', 'deny', 'stop', 'whitelist']
    if action not in valid_actions:
        debug_log(f"Invalid action: {action}")
        answer_callback_query(callback['id'], f"Invalid action: {action}")
        return None

    # Build decision
    decision = {'action': action}

    # Handle whitelist action - echo permission_suggestions back as updatedPermissions.
    # Also add session-scoped copies so the permission takes effect immediately in the
    # current session (localSettings writes persist but may not be hot-reloaded).
    if action == 'whitelist':
        perms = list(request.permission_suggestions)
        session_perms = [dict(p, destination='session') for p in perms
                         if isinstance(p, dict) and p.get('destination') != 'session']
        decision['updatedPermissions'] = perms + session_perms

    # Update request state
    # Map action string to RequestState enum (values are lowercase)
    state_map = {
        'allow': RequestState.ALLOW,
        'deny': RequestState.DENY,
        'stop': RequestState.STOP,
        'whitelist': RequestState.WHITELIST,
    }
    new_state = state_map.get(action)
    if new_state is None:
        debug_log(f"Unknown action for state transition: {action}")
        return None
    updated = update_request_state(
        request.request_id,
        new_state,
        decision=decision,
        actor_user_id=user_id,
    )

    if updated:
        # Answer callback
        answer_callback_query(callback['id'], f"Action: {action}")

        # Update message to show action taken
        _update_message_after_action(message_id, request.request_id, action)

        return decision
    else:
        # Already in terminal state
        answer_callback_query(callback['id'], "Request already resolved")
        return None


def handle_text_reply(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Handle a text reply message.

    Text replies are classified as 'reply' action and the text is preserved.
    The upstream hook maps this to deny-with-message or deny+interrupt.

    Args:
        message: Message object from Telegram

    Returns:
        Decision dict if action was taken, None otherwise
    """
    # Verify user is authorized
    from_user = message.get('from', {})
    user_id = from_user.get('id')
    if str(user_id) != str(TELEGRAM_CHAT_ID):
        debug_log(f"Unauthorized reply from user_id={user_id}")
        return None

    # Get reply text
    reply_to = message.get('reply_to_message', {})
    reply_to_message_id = reply_to.get('message_id')
    text = message.get('text', '')

    if not reply_to_message_id:
        return None

    debug_log(f"Received reply to message {reply_to_message_id}: {text[:50]}...")

    # Find request by message ID
    request = find_request_by_message_id(reply_to_message_id)

    if not request:
        debug_log(f"Request not found for reply")
        return None

    # Update request state with reply
    decision = {
        'action': 'reply',
        'reply_text': text,
    }

    updated = update_request_state(
        request.request_id,
        RequestState.REPLY,
        decision=decision,
        reply_text=text,
        actor_user_id=user_id,
    )

    if updated:
        # Update message to show reply received
        _update_message_after_action(
            reply_to_message_id,
            request.request_id,
            'reply',
            extra_text=f"\n\n_Reply: {text[:100]}..._" if len(text) > 100 else f"\n\n_Reply: {text}_"
        )
        return decision

    return None


def _update_message_after_action(
    message_id: int,
    request_id: str,
    action: str,
    extra_text: str = ""
):
    """Update the Telegram message to show action was taken."""
    status_emoji = {
        'allow': 'allowed',
        'deny': 'denied',
        'stop': 'stopped',
        'whitelist': 'whitelisted',
        'reply': 'replied',
    }

    status = status_emoji.get(action, action)

    new_text = f"*Permission Request* `{request_id}`\n\n_Action: {status}_{extra_text}"

    # Remove buttons after action
    edit_message_text(message_id, new_text, inline_buttons=[])


def generate_whitelist_pattern(request: PermissionRequest) -> str:
    """
    Generate a permission pattern for whitelisting.

    Prefers permission_suggestions from hook input, falls back to
    conservative generated pattern.

    Args:
        request: The permission request

    Returns:
        Permission pattern string
    """
    # Prefer suggestions from hook input
    if request.permission_suggestions:
        # Use the first suggestion (most specific)
        return request.permission_suggestions[0]

    # Fallback: Generate conservative pattern based on tool and command
    if request.tool_name == 'Bash':
        command = request.tool_input.get('command', '')

        # Extract the base command (first word)
        # Handle sudo, env vars, etc.
        parts = command.split()
        base_cmd = None

        for part in parts:
            # Skip environment variable assignments
            if '=' in part and not part.startswith('-'):
                continue
            # Skip sudo
            if part == 'sudo':
                continue
            base_cmd = part
            break

        if base_cmd:
            # Generate pattern: Bash(command:*)
            return f"Bash({base_cmd}:*)"

    # Generic fallback - allow the specific tool
    return f"{request.tool_name}"


def wait_for_response(
    request_id: str,
    message_id: int,
    timeout_seconds: int = 60,
    on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Wait for a response to the permission request.

    Uses long polling to receive updates. Handles both callback queries
    (button presses) and text replies.

    Args:
        request_id: Unique identifier for this request
        message_id: Telegram message ID to monitor
        timeout_seconds: Maximum time to wait

    Returns:
        Decision dict with 'action' and optionally 'updatedPermissions', or None
    """
    if not TELEGRAM_ENABLED:
        return None

    start_time = time.time()
    last_update_id = 0

    debug_log(f"Waiting for Telegram response (request_id={request_id}, timeout={timeout_seconds}s)")

    # Cleanup expired requests periodically
    cleanup_expired_requests()

    while time.time() - start_time < timeout_seconds:
        # Get updates with long polling
        updates = get_updates(offset=last_update_id + 1, timeout=POLL_TIMEOUT)

        for update in updates:
            last_update_id = update.get('update_id', 0)

            # Check for callback query (button press)
            callback = update.get('callback_query')
            if callback:
                # Verify it's for our message or request
                message = callback.get('message', {})
                cb_message_id = message.get('message_id')

                # Parse callback data to get request_id
                callback_data = callback.get('data', '')
                action, cb_request_id = parse_callback_data(callback_data)

                # Check if this callback is for our request
                if cb_request_id == request_id or cb_message_id == message_id:
                    decision = handle_callback_query(callback)
                    if decision:
                        debug_log(f"Got decision: {decision}")
                        return decision

            # Check for text reply
            message = update.get('message')
            if message:
                reply_to = message.get('reply_to_message', {})
                reply_to_message_id = reply_to.get('message_id')

                # Check if this is a reply to our message
                if reply_to_message_id == message_id:
                    decision = handle_text_reply(message)
                    if decision:
                        debug_log(f"Got reply decision: {decision}")
                        return decision

            # Call on_update callback if provided
            if on_update:
                on_update(update)

        # Small sleep to avoid tight loop (getUpdates already has long polling)
        time.sleep(0.1)

    debug_log("Telegram response timeout")
    return None


def update_settings_local_json(
    workspace_dir: str,
    permission_pattern: str,
) -> bool:
    """
    Update .claude/settings.local.json with a new permission rule.

    Safely writes the update while:
    - Preserving existing JSON structure
    - Deduplicating entries
    - Using atomic write (write to temp, then rename)

    Args:
        workspace_dir: Path to workspace directory
        permission_pattern: Permission pattern to add (e.g., "Bash(ls:*)")

    Returns:
        True if successful, False otherwise
    """
    settings_path = Path(workspace_dir) / ".claude" / "settings.local.json"

    # Load existing settings or create new
    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path, 'r') as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            debug_log(f"Error parsing settings.local.json: {e}")
            # Create backup of corrupted file
            backup_path = settings_path.with_suffix('.json.backup')
            settings_path.rename(backup_path)
            settings = {}
        except Exception as e:
            debug_log(f"Error reading settings.local.json: {e}")
            settings = {}

    # Ensure permissions structure exists
    if 'permissions' not in settings:
        settings['permissions'] = {}
    if 'allow' not in settings['permissions']:
        settings['permissions']['allow'] = []

    allow_list = settings['permissions']['allow']

    # Check if pattern already exists (deduplication)
    if permission_pattern in allow_list:
        debug_log(f"Permission pattern already exists: {permission_pattern}")
        return True

    # Add new pattern
    allow_list.append(permission_pattern)
    debug_log(f"Added permission pattern: {permission_pattern}")

    # Atomic write: write to temp file, then rename
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_suffix('.json.tmp')

    try:
        with open(temp_path, 'w') as f:
            json.dump(settings, f, indent=2)

        # Atomic rename
        temp_path.rename(settings_path)
        debug_log(f"Updated {settings_path}")
        return True

    except Exception as e:
        debug_log(f"Error writing settings.local.json: {e}")
        # Cleanup temp file
        if temp_path.exists():
            temp_path.unlink()
        return False


def process_whitelist_update(request: PermissionRequest, decision: Dict[str, Any]) -> bool:
    """
    Process a whitelist decision by updating settings.local.json.

    Args:
        request: The permission request
        decision: The decision dict with updatedPermissions

    Returns:
        True if successful, False otherwise
    """
    # updatedPermissions is now the raw permission_suggestions array from Claude Code.
    # Claude Code processes rule objects itself; we only write string patterns to
    # settings.local.json as a local backup for Bash-style patterns.
    updated_perms = decision.get('updatedPermissions', [])

    # Handle legacy {"add": [...]} shape just in case
    if isinstance(updated_perms, dict):
        updated_perms = updated_perms.get('add', [])

    if not updated_perms:
        debug_log("No permission patterns to add")
        return True  # Not an error — Claude Code handles rule objects itself

    success = True
    for pattern in updated_perms:
        if isinstance(pattern, str):
            # Legacy string pattern (e.g. "Bash(ls:*)") — write to settings.local.json
            if not update_settings_local_json(request.cwd, pattern):
                success = False
        else:
            # Rule object (e.g. MCP addRules) — Claude Code saves this via updatedPermissions
            debug_log(f"Skipping rule object (handled by Claude Code): {pattern}")

    return success


# CLI interface for testing
def main():
    """Main entry point for CLI testing."""
    import argparse

    parser = argparse.ArgumentParser(description='Telegram Permission Router')
    parser.add_argument('--test-send', action='store_true', help='Test sending a message')
    parser.add_argument('--test-poll', action='store_true', help='Test polling for updates')
    parser.add_argument('--test-whitelist', type=str, help='Test whitelist update with pattern')
    parser.add_argument('--cwd', type=str, default=os.getcwd(), help='Workspace directory')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args()

    # Enable debug if requested
    if args.debug:
        os.environ['CLAUDE_HOOK_DEBUG'] = '1'

    load_telegram_config()

    if args.test_send:
        print("Testing message send...")
        request = create_request(
            session_id="test-session",
            cwd=args.cwd,
            tool_name="Bash",
            tool_input={"command": "ls -la /tmp"},
            permission_suggestions=["Bash(ls:*)"],
        )
        message_id = send_permission_message(request, "test-workspace", "test-session")
        print(f"Message ID: {message_id}")

    elif args.test_poll:
        print("Testing poll (press Ctrl+C to stop)...")
        last_update_id = 0
        while True:
            updates = get_updates(offset=last_update_id + 1)
            for update in updates:
                last_update_id = update.get('update_id', 0)
                print(f"Update: {json.dumps(update, indent=2)[:500]}")

    elif args.test_whitelist:
        print(f"Testing whitelist update with pattern: {args.test_whitelist}")
        success = update_settings_local_json(args.cwd, args.test_whitelist)
        print(f"Success: {success}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
