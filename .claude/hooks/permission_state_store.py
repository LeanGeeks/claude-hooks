#!/usr/bin/env python3
"""
Permission State Store - Manages pending permission request state

Provides atomic, file-locked operations for storing and updating permission
request state, including:
- Creating new pending requests
- Updating request state (allow/deny/stop/whitelist/reply/expired)
- Querying request state
- Cleaning up expired requests

State file location: ~/.claude/permission_requests.jsonl
Each line is a JSON object representing a single request.

State machine: pending -> allow|deny|stop|whitelist|reply|expired
"""

import json
import os
import fcntl
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum


# Configuration
STATE_FILE = Path.home() / ".claude" / "permission_requests.jsonl"
AUDIT_LOG_FILE = Path.home() / ".claude" / "permission_actions.jsonl"
DEFAULT_TTL_SECONDS = 300  # 5 minutes default TTL for pending requests
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = Path.home() / ".claude" / "permission_state_debug.log"


class RequestState(Enum):
    """Valid states for a permission request"""
    PENDING = "pending"
    ALLOW = "allow"
    DENY = "deny"
    STOP = "stop"
    WHITELIST = "whitelist"
    REPLY = "reply"
    EXPIRED = "expired"
    RESOLVED_TERMINAL = "resolved_terminal"  # Resolved via terminal prompt


# Terminal states (no further transitions allowed)
TERMINAL_STATES = {
    RequestState.ALLOW,
    RequestState.DENY,
    RequestState.STOP,
    RequestState.WHITELIST,
    RequestState.REPLY,
    RequestState.EXPIRED,
    RequestState.RESOLVED_TERMINAL,
}

# Resolution sources
RESOLUTION_SOURCE_TELEGRAM = "telegram"
RESOLUTION_SOURCE_TERMINAL = "terminal"
RESOLUTION_SOURCE_TIMEOUT = "timeout"


@dataclass
class PermissionRequest:
    """Represents a permission request with full context"""
    request_id: str
    session_id: str
    cwd: str
    tool_name: str
    tool_input: Dict[str, Any]
    permission_suggestions: List[str]
    state: str
    created_at: str
    updated_at: str
    expires_at: str
    telegram_message_id: Optional[int] = None
    decision: Optional[Dict[str, Any]] = None
    reply_text: Optional[str] = None
    actor_user_id: Optional[int] = None
    resolution_source: Optional[str] = None  # "telegram" | "terminal" | "timeout"
    resolved_at: Optional[str] = None  # ISO timestamp when resolved

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PermissionRequest':
        """Create from dictionary"""
        return cls(**data)


@dataclass
class AuditEntry:
    """Represents an audit log entry"""
    timestamp: str
    request_id: str
    action: str
    actor_user_id: Optional[int]
    previous_state: str
    new_state: str
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)


def debug_log(message: str):
    """Log debug message if debug mode is enabled."""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now().isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass


def _ensure_state_directory():
    """Ensure the state file directory exists"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _acquire_lock(file_obj):
    """Acquire exclusive lock on file"""
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)


def _release_lock(file_obj):
    """Release file lock"""
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.now(timezone.utc).isoformat()


def _expires_at(ttl_seconds: int) -> str:
    """Calculate expiration timestamp"""
    expires = datetime.now(timezone.utc).timestamp() + ttl_seconds
    return datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()


def _is_expired(expires_at: str) -> bool:
    """Check if a timestamp is in the past"""
    try:
        expires_dt = datetime.fromisoformat(expires_at)
        return datetime.now(timezone.utc) > expires_dt
    except Exception:
        return True


def _append_audit_log(entry: AuditEntry):
    """Append an entry to the audit log"""
    _ensure_state_directory()
    try:
        with open(AUDIT_LOG_FILE, 'a') as f:
            f.write(json.dumps(entry.to_dict()) + '\n')
    except Exception as e:
        debug_log(f"Failed to append audit log: {e}")


def create_request(
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    permission_suggestions: List[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> PermissionRequest:
    """
    Create a new pending permission request.

    Args:
        session_id: Claude session UUID
        cwd: Current working directory
        tool_name: Name of the tool being called
        tool_input: Input parameters for the tool
        permission_suggestions: Suggested permission patterns from hook input
        ttl_seconds: Time-to-live in seconds

    Returns:
        PermissionRequest object with request_id
    """
    _ensure_state_directory()

    now = _utc_now()
    request_id = str(uuid.uuid4())[:8]  # Short UUID for readability

    request = PermissionRequest(
        request_id=request_id,
        session_id=session_id,
        cwd=cwd,
        tool_name=tool_name,
        tool_input=tool_input,
        permission_suggestions=permission_suggestions,
        state=RequestState.PENDING.value,
        created_at=now,
        updated_at=now,
        expires_at=_expires_at(ttl_seconds),
    )

    # Append to state file with lock
    with open(STATE_FILE, 'a') as f:
        _acquire_lock(f)
        try:
            f.write(json.dumps(request.to_dict()) + '\n')
        finally:
            _release_lock(f)

    debug_log(f"Created request {request_id} for {tool_name}")
    return request


def get_request(request_id: str) -> Optional[PermissionRequest]:
    """
    Get a request by ID.

    Args:
        request_id: The request ID to look up

    Returns:
        PermissionRequest if found and not expired, None otherwise
    """
    if not STATE_FILE.exists():
        return None

    request = None
    needs_expiration_mark = False

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get('request_id') == request_id:
                        request = PermissionRequest.from_dict(data)
                        # Check if expired
                        if _is_expired(request.expires_at) and request.state == RequestState.PENDING.value:
                            needs_expiration_mark = True
                            request = None  # Don't return expired pending requests
                        break
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    # Mark as expired OUTSIDE the lock to avoid deadlock
    if needs_expiration_mark:
        update_request_state(request_id, RequestState.EXPIRED)

    return request


def get_pending_request_for_session(session_id: str) -> Optional[PermissionRequest]:
    """
    Get the most recent pending request for a session.

    Args:
        session_id: Claude session UUID

    Returns:
        Most recent pending PermissionRequest if found, None otherwise
    """
    if not STATE_FILE.exists():
        return None

    pending_requests = []

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if (data.get('session_id') == session_id and
                        data.get('state') == RequestState.PENDING.value):
                        request = PermissionRequest.from_dict(data)
                        # Skip if expired
                        if not _is_expired(request.expires_at):
                            pending_requests.append(request)
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    if not pending_requests:
        return None

    # Return most recent by created_at
    pending_requests.sort(key=lambda r: r.created_at, reverse=True)
    return pending_requests[0]


def update_request_state(
    request_id: str,
    new_state: RequestState,
    decision: Optional[Dict[str, Any]] = None,
    reply_text: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    resolution_source: Optional[str] = None,
) -> Optional[PermissionRequest]:
    """
    Update the state of a request.

    Idempotent: If request is already in a terminal state, returns None.
    Duplicate callbacks are safely ignored.

    Args:
        request_id: The request ID to update
        new_state: The new state to set
        decision: Optional decision data (for whitelist actions)
        reply_text: Optional reply text (for reply actions)
        actor_user_id: Optional Telegram user ID who performed the action
        resolution_source: Optional source of resolution ("telegram" | "terminal" | "timeout")

    Returns:
        Updated PermissionRequest if successful, None if request not found
        or already in terminal state
    """
    if not STATE_FILE.exists():
        debug_log(f"State file not found for request {request_id}")
        return None

    updated_request = None
    previous_state = None
    now = _utc_now()

    # Determine resolution source based on state if not provided
    if resolution_source is None and new_state in TERMINAL_STATES:
        if new_state == RequestState.RESOLVED_TERMINAL:
            resolution_source = RESOLUTION_SOURCE_TERMINAL
        elif new_state == RequestState.EXPIRED:
            resolution_source = RESOLUTION_SOURCE_TIMEOUT
        else:
            resolution_source = RESOLUTION_SOURCE_TELEGRAM

    # Hold lock for entire read-modify-write operation to prevent race conditions
    with open(STATE_FILE, 'r+') as f:
        _acquire_lock(f)
        try:
            # Ensure we read from beginning of file
            f.seek(0)
            # Read all requests
            lines = f.readlines()

            # Find and update the request
            updated_lines = []
            found = False

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)

                    if data.get('request_id') == request_id:
                        found = True
                        current_state = RequestState(data.get('state', 'pending'))
                        previous_state = current_state.value

                        # Check if already in terminal state
                        if current_state in TERMINAL_STATES:
                            debug_log(f"Request {request_id} already in terminal state: {current_state.value}")
                            return None

                        # Check if expired
                        if _is_expired(data.get('expires_at', '')):
                            debug_log(f"Request {request_id} has expired")
                            return None

                        # Update the request
                        data['state'] = new_state.value
                        data['updated_at'] = now

                        if decision is not None:
                            data['decision'] = decision
                        if reply_text is not None:
                            data['reply_text'] = reply_text
                        if actor_user_id is not None:
                            data['actor_user_id'] = actor_user_id
                        if resolution_source is not None:
                            data['resolution_source'] = resolution_source
                            data['resolved_at'] = now

                        updated_request = PermissionRequest.from_dict(data)
                        updated_lines.append(json.dumps(data) + '\n')
                    else:
                        updated_lines.append(line + '\n')

                except json.JSONDecodeError:
                    updated_lines.append(line + '\n')

            if not found:
                debug_log(f"Request {request_id} not found")
                return None

            # Write back all requests (truncate and rewrite)
            f.seek(0)
            f.truncate()
            f.writelines(updated_lines)
            f.flush()
            os.fsync(f.fileno())

        finally:
            _release_lock(f)

    # Append audit log
    if updated_request:
        audit_entry = AuditEntry(
            timestamp=_utc_now(),
            request_id=request_id,
            action=new_state.value,
            actor_user_id=actor_user_id,
            previous_state=previous_state or 'unknown',
            new_state=new_state.value,
            details={'decision': decision} if decision else None,
        )
        _append_audit_log(audit_entry)

    debug_log(f"Updated request {request_id}: {previous_state} -> {new_state.value}")
    return updated_request


def set_telegram_message_id(request_id: str, message_id: int) -> bool:
    """
    Set the Telegram message ID for a request.

    Args:
        request_id: The request ID
        message_id: Telegram message ID

    Returns:
        True if successful, False otherwise
    """
    if not STATE_FILE.exists():
        return False

    # Hold lock for entire read-modify-write operation to prevent race conditions
    with open(STATE_FILE, 'r+') as f:
        _acquire_lock(f)
        try:
            # Ensure we read from beginning of file
            f.seek(0)
            # Read all requests
            lines = f.readlines()

            # Find and update the request
            updated_lines = []
            found = False

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)

                    if data.get('request_id') == request_id:
                        found = True
                        data['telegram_message_id'] = message_id
                        data['updated_at'] = _utc_now()

                    updated_lines.append(json.dumps(data) + '\n')

                except json.JSONDecodeError:
                    updated_lines.append(line + '\n')

            if not found:
                return False

            # Write back all requests (truncate and rewrite)
            f.seek(0)
            f.truncate()
            f.writelines(updated_lines)
            f.flush()
            os.fsync(f.fileno())

        finally:
            _release_lock(f)

    return True


def cleanup_expired_requests() -> int:
    """
    Remove expired requests from the state file.

    This also marks any pending expired requests as expired.

    Returns:
        Number of requests cleaned up
    """
    if not STATE_FILE.exists():
        return 0

    # Hold lock for entire read-modify-write operation to prevent race conditions
    with open(STATE_FILE, 'r+') as f:
        _acquire_lock(f)
        try:
            # Ensure we read from beginning of file
            f.seek(0)
            # Read all requests
            lines = f.readlines()

            # Filter out expired requests
            kept_lines = []
            cleaned = 0

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                    expires_at = data.get('expires_at', '')

                    # Keep if not expired, or if in a non-pending state (for audit)
                    if not _is_expired(expires_at) or data.get('state') != RequestState.PENDING.value:
                        # Mark pending expired as expired
                        if (_is_expired(expires_at) and
                            data.get('state') == RequestState.PENDING.value):
                            data['state'] = RequestState.EXPIRED.value
                            data['updated_at'] = _utc_now()
                            cleaned += 1

                        kept_lines.append(json.dumps(data) + '\n')
                    else:
                        cleaned += 1

                except json.JSONDecodeError:
                    # Keep malformed lines (don't delete data we can't parse)
                    kept_lines.append(line + '\n')

            # Write back (truncate and rewrite)
            f.seek(0)
            f.truncate()
            f.writelines(kept_lines)
            f.flush()
            os.fsync(f.fileno())

        finally:
            _release_lock(f)

    if cleaned > 0:
        debug_log(f"Cleaned up {cleaned} expired requests")

    return cleaned


def find_request_by_message_id(message_id: int) -> Optional[PermissionRequest]:
    """
    Find a request by Telegram message ID.

    Args:
        message_id: Telegram message ID

    Returns:
        PermissionRequest if found, None otherwise
    """
    if not STATE_FILE.exists():
        return None

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get('telegram_message_id') == message_id:
                        return PermissionRequest.from_dict(data)
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    return None


def find_pending_request_by_session(session_id: str) -> Optional[PermissionRequest]:
    """
    Find a pending request for a session (alias for get_pending_request_for_session).

    Args:
        session_id: Claude session UUID

    Returns:
        Most recent pending PermissionRequest if found, None otherwise
    """
    return get_pending_request_for_session(session_id)


def find_pending_request_by_tool_session(
    session_id: str,
    tool_name: str,
    cwd: Optional[str] = None,
) -> Optional[PermissionRequest]:
    """
    Find a pending request matching tool name and session.

    Used by PostToolUse hook to find and revoke Telegram messages when
    a tool is executed via terminal.

    Args:
        session_id: Claude session UUID
        tool_name: Name of the tool that was executed
        cwd: Optional working directory to match

    Returns:
        Most recent matching pending PermissionRequest if found, None otherwise
    """
    if not STATE_FILE.exists():
        return None

    matching_requests = []

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if (data.get('session_id') == session_id and
                        data.get('tool_name') == tool_name and
                        data.get('state') == RequestState.PENDING.value):
                        # Optionally filter by cwd
                        if cwd and data.get('cwd') != cwd:
                            continue
                        request = PermissionRequest.from_dict(data)
                        # Skip if expired
                        if not _is_expired(request.expires_at):
                            matching_requests.append(request)
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    if not matching_requests:
        return None

    # Return most recent by created_at
    matching_requests.sort(key=lambda r: r.created_at, reverse=True)
    return matching_requests[0]


def resolve_via_terminal(request_id: str) -> Optional[PermissionRequest]:
    """
    Mark a request as resolved via terminal prompt.

    This is called by the PostToolUse hook when a tool is executed
    after the user responded via terminal instead of Telegram.

    Args:
        request_id: The request ID to mark as terminal-resolved

    Returns:
        Updated PermissionRequest if successful, None otherwise
    """
    return update_request_state(
        request_id,
        RequestState.RESOLVED_TERMINAL,
        resolution_source=RESOLUTION_SOURCE_TERMINAL,
    )


def get_all_pending_requests() -> List[PermissionRequest]:
    """
    Get all pending (non-expired) requests.

    Used by the daemon to know which messages to monitor.

    Returns:
        List of all pending PermissionRequest objects
    """
    if not STATE_FILE.exists():
        return []

    pending = []

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get('state') == RequestState.PENDING.value:
                        request = PermissionRequest.from_dict(data)
                        if not _is_expired(request.expires_at):
                            pending.append(request)
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    return pending


# For testing
if __name__ == '__main__':
    import sys

    print("=== Permission State Store Tests ===\n")

    # Test create request
    request = create_request(
        session_id="test-session-123",
        cwd="/test/path",
        tool_name="Bash",
        tool_input={"command": "ls -la"},
        permission_suggestions=["Bash(ls:*)"],
        ttl_seconds=60,
    )
    print(f"Created request: {request.request_id}")
    print(f"  State: {request.state}")
    print(f"  Expires: {request.expires_at}")

    # Test get request
    retrieved = get_request(request.request_id)
    print(f"\nRetrieved request: {retrieved.request_id if retrieved else 'None'}")

    # Test update state
    updated = update_request_state(
        request.request_id,
        RequestState.ALLOW,
        actor_user_id=12345,
    )
    print(f"\nUpdated state: {updated.state if updated else 'None (already terminal)'}")

    # Test idempotency - second update should return None
    second_update = update_request_state(
        request.request_id,
        RequestState.DENY,
        actor_user_id=12345,
    )
    print(f"Second update result: {second_update.state if second_update else 'None (idempotent)'}")

    # Cleanup
    cleaned = cleanup_expired_requests()
    print(f"\nCleaned up {cleaned} expired requests")

    print("\n=== Tests Complete ===")
