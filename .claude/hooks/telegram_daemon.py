#!/usr/bin/env python3
"""
Telegram Daemon - Background process for handling Telegram callbacks

This daemon runs continuously to:
- Poll Telegram Bot API for callback queries (button presses)
- Update permission request state when buttons are pressed
- Update Telegram messages to reflect actions taken
- Handle graceful shutdown

The daemon is started automatically by permission_request_hook.py if not running.

PID file: ~/.claude/telegram_daemon.pid
Log file: ~/.claude/telegram_daemon.log
"""

import json
import os
import sys
import signal
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

# Add hooks directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from permission_state_store import (
    PermissionRequest,
    RequestState,
    get_request,
    update_request_state,
    find_request_by_message_id,
    get_all_pending_requests,
    cleanup_expired_requests,
    debug_log,
    RESOLUTION_SOURCE_TELEGRAM,
)
import telegram_permission_router

# Daemon configuration
PID_FILE = Path.home() / ".claude" / "telegram_daemon.pid"
DAEMON_LOG = Path.home() / ".claude" / "telegram_daemon.log"
HEALTH_CHECK_INTERVAL = 30  # seconds between health check logs
CLEANUP_INTERVAL = 60  # seconds between expired request cleanup

# Global shutdown flag
_shutdown = False


def daemon_log(message: str):
    """Log to daemon log file."""
    try:
        DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DAEMON_LOG, 'a') as f:
            timestamp = datetime.now().isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def write_pid():
    """Write PID file."""
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        daemon_log(f"Failed to write PID file: {e}")


def remove_pid():
    """Remove PID file."""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def is_already_running() -> bool:
    """Check if another daemon instance is running."""
    if not PID_FILE.exists():
        return False

    try:
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())

        # Check if process is running
        os.kill(pid, 0)  # Raises OSError if process doesn't exist
        return True
    except (ValueError, OSError):
        # PID file exists but process is dead
        remove_pid()
        return False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    daemon_log(f"Received signal {signum}, shutting down...")
    _shutdown = True


def _telegram_api_request(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Make a Telegram Bot API request.

    Args:
        method: API method name
        payload: Request payload

    Returns:
        Response result dict if successful, None otherwise
    """
    if not telegram_permission_router.TELEGRAM_ENABLED:
        return None

    url = f"https://api.telegram.org/bot{telegram_permission_router.TELEGRAM_BOT_TOKEN}/{method}"

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
                daemon_log(f"Telegram API error in {method}: {result.get('description')}")
                return None

    except urllib.error.URLError as e:
        daemon_log(f"Telegram network error in {method}: {e}")
        return None
    except Exception as e:
        daemon_log(f"Telegram request error in {method}: {e}")
        return None


def get_updates(offset: Optional[int] = None, timeout: int = telegram_permission_router.POLL_TIMEOUT) -> List[Dict[str, Any]]:
    """
    Get updates from Telegram Bot API using long polling.

    Args:
        offset: Update ID offset for acknowledgment
        timeout: Long polling timeout in seconds

    Returns:
        List of update objects
    """
    if not telegram_permission_router.TELEGRAM_ENABLED:
        return []

    payload = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
    if offset:
        payload["offset"] = offset

    result = _telegram_api_request("getUpdates", payload)
    return result if result else []


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
    if str(user_id) != str(telegram_permission_router.TELEGRAM_CHAT_ID):
        daemon_log(f"Unauthorized callback from user_id={user_id}")
        telegram_permission_router.answer_callback_query(callback['id'], "Unauthorized")
        return None

    # Parse callback data
    callback_data = callback.get('data', '')
    action, request_id = telegram_permission_router.parse_callback_data(callback_data)
    daemon_log(f"Received callback: action={action}, request_id={request_id}")

    # Find the request
    message = callback.get('message', {})
    message_id = message.get('message_id')

    request = None
    if request_id:
        request = get_request(request_id)
    elif message_id:
        request = find_request_by_message_id(message_id)

    if not request:
        daemon_log(f"Request not found for callback")
        telegram_permission_router.answer_callback_query(callback['id'], "Request not found or expired")
        return None

    # Validate action
    valid_actions = ['allow', 'deny', 'stop', 'whitelist']
    if action not in valid_actions:
        daemon_log(f"Invalid action: {action}")
        telegram_permission_router.answer_callback_query(callback['id'], f"Invalid action: {action}")
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

    # Map action string to RequestState enum
    state_map = {
        'allow': RequestState.ALLOW,
        'deny': RequestState.DENY,
        'stop': RequestState.STOP,
        'whitelist': RequestState.WHITELIST,
    }
    new_state = state_map.get(action)
    if new_state is None:
        daemon_log(f"Unknown action for state transition: {action}")
        return None

    updated = update_request_state(
        request.request_id,
        new_state,
        decision=decision,
        actor_user_id=user_id,
        resolution_source=RESOLUTION_SOURCE_TELEGRAM,
    )

    if updated:
        # Answer callback
        telegram_permission_router.answer_callback_query(callback['id'], f"Action: {action}")

        # Update message to show action taken
        _update_message_after_action(message_id, request.request_id, action)

        return decision
    else:
        # Already in terminal state
        telegram_permission_router.answer_callback_query(callback['id'], "Request already resolved")
        return None


def handle_text_reply(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Handle a text reply message.

    Args:
        message: Message object from Telegram

    Returns:
        Decision dict if action was taken, None otherwise
    """
    # Verify user is authorized
    from_user = message.get('from', {})
    user_id = from_user.get('id')
    if str(user_id) != str(telegram_permission_router.TELEGRAM_CHAT_ID):
        daemon_log(f"Unauthorized reply from user_id={user_id}")
        return None

    # Get reply text
    reply_to = message.get('reply_to_message', {})
    reply_to_message_id = reply_to.get('message_id')
    text = message.get('text', '')

    if not reply_to_message_id:
        return None

    daemon_log(f"Received reply to message {reply_to_message_id}: {text[:50]}...")

    # Find request by message ID
    request = find_request_by_message_id(reply_to_message_id)

    if not request:
        daemon_log(f"Request not found for reply")
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
        resolution_source=RESOLUTION_SOURCE_TELEGRAM,
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
    """Update the Telegram message to show action was taken.

    Removes buttons and adds an emoji reaction instead of replacing the message text.
    """
    # Map actions to emoji reactions
    action_emoji = {
        'allow': '👍',      # thumbs up - approval
        'deny': '👎',       # thumbs down - rejection
        'stop': '👎',       # thumbs down - rejection with interrupt
        'whitelist': '👌',  # OK hand - whitelist (adding to allowed list)
        'reply': '✍️',      # writing hand - text reply
    }

    emoji = action_emoji.get(action, '👍')
    daemon_log(f"Updating message {message_id}: action={action}, emoji={emoji}")

    # Remove buttons
    buttons_result = telegram_permission_router.remove_inline_buttons(message_id)
    daemon_log(f"Remove buttons result: {buttons_result}")

    # Add reaction
    reaction_result = telegram_permission_router.set_message_reaction(message_id, emoji)
    daemon_log(f"Set reaction result: {reaction_result}")


def revoke_message(message_id: int, request_id: str, reason: str = "terminal"):
    """
    Revoke a Telegram message (remove buttons, add reaction).

    Args:
        message_id: Telegram message ID
        request_id: Permission request ID
        reason: "terminal" or "expired"
    """
    # Map reasons to emoji reactions
    reason_emoji = {
        'terminal': '👍',  # thumbs up - approved via terminal
        'expired': '😢',   # crying - request expired
    }

    emoji = reason_emoji.get(reason, '👍')

    # Remove buttons
    telegram_permission_router.remove_inline_buttons(message_id)

    # Add reaction
    telegram_permission_router.set_message_reaction(message_id, emoji)


def run_daemon():
    """Main daemon loop."""
    global _shutdown

    # Check if already running
    if is_already_running():
        print("Daemon already running", file=sys.stderr)
        sys.exit(1)

    # Load Telegram config
    telegram_permission_router.load_telegram_config()

    if not telegram_permission_router.TELEGRAM_ENABLED:
        daemon_log("Telegram not enabled, daemon exiting")
        print("Telegram not configured", file=sys.stderr)
        sys.exit(1)

    # Write PID file
    write_pid()
    daemon_log("Daemon started")

    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    last_update_id = 0
    last_health_check = time.time()
    last_cleanup = time.time()

    try:
        while not _shutdown:
            # Get updates with long polling
            updates = get_updates(offset=last_update_id + 1, timeout=telegram_permission_router.POLL_TIMEOUT)

            for update in updates:
                last_update_id = update.get('update_id', 0)

                # Check for callback query (button press)
                callback = update.get('callback_query')
                if callback:
                    try:
                        handle_callback_query(callback)
                    except Exception as e:
                        daemon_log(f"Error handling callback: {e}")

                # Check for text reply
                message = update.get('message')
                if message and message.get('reply_to_message'):
                    try:
                        handle_text_reply(message)
                    except Exception as e:
                        daemon_log(f"Error handling reply: {e}")

            # Periodic health check log
            now = time.time()
            if now - last_health_check > HEALTH_CHECK_INTERVAL:
                pending = get_all_pending_requests()
                daemon_log(f"Health check: {len(pending)} pending requests")
                last_health_check = now

            # Periodic cleanup of expired requests
            if now - last_cleanup > CLEANUP_INTERVAL:
                cleaned = cleanup_expired_requests()
                if cleaned > 0:
                    daemon_log(f"Cleaned up {cleaned} expired requests")
                last_cleanup = now

    except Exception as e:
        daemon_log(f"Daemon error: {e}")
        import traceback
        daemon_log(traceback.format_exc())
    finally:
        remove_pid()
        daemon_log("Daemon stopped")


def start_daemon_if_needed():
    """
    Start the daemon if it's not already running.

    This is called by permission_request_hook.py to ensure
    the daemon is running before sending a message.

    Returns:
        True if daemon is running (started or already running), False on error
    """
    if is_already_running():
        return True

    # Fork to background
    try:
        pid = os.fork()
        if pid > 0:
            # Parent process - wait briefly for daemon to start
            time.sleep(0.5)
            return is_already_running()

        # Child process - become daemon
        os.setsid()

        # Redirect standard file descriptors
        sys.stdin.close()
        sys.stdout.close()
        sys.stderr.close()

        # Run daemon
        run_daemon()

    except OSError as e:
        print(f"Failed to start daemon: {e}", file=sys.stderr)
        return False

    return True


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Telegram Permission Daemon')
    parser.add_argument('--start', action='store_true', help='Start daemon if not running')
    parser.add_argument('--stop', action='store_true', help='Stop running daemon')
    parser.add_argument('--status', action='store_true', help='Check daemon status')
    parser.add_argument('--foreground', action='store_true', help='Run in foreground (for debugging)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args()

    if args.debug:
        os.environ['CLAUDE_HOOK_DEBUG'] = '1'

    if args.start:
        if start_daemon_if_needed():
            print("Daemon started")
        else:
            print("Failed to start daemon", file=sys.stderr)
            sys.exit(1)

    elif args.stop:
        if not PID_FILE.exists():
            print("Daemon not running")
            return

        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (PID {pid})")
        except (ValueError, OSError) as e:
            print(f"Failed to stop daemon: {e}", file=sys.stderr)
            remove_pid()

    elif args.status:
        if is_already_running():
            with open(PID_FILE, 'r') as f:
                pid = f.read().strip()
            print(f"Daemon running (PID {pid})")
        else:
            print("Daemon not running")

    elif args.foreground:
        # Run in foreground for debugging
        telegram_permission_router.load_telegram_config()
        if not telegram_permission_router.TELEGRAM_ENABLED:
            print("Telegram not configured", file=sys.stderr)
            sys.exit(1)
        run_daemon()

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
