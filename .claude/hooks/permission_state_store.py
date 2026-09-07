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
import socket
import uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum


# Configuration
#
# Paths honor env-var overrides so tests (and any sandboxed run) can point the
# state store at a temp dir instead of polluting the real ~/.claude store. The
# default remains ~/.claude/... for normal hook execution.
def _store_path(env_var: str, default_name: str) -> Path:
    override = os.environ.get(env_var)
    return Path(override) if override else (Path.home() / ".claude" / default_name)


STATE_FILE = _store_path("CLAUDE_PERMISSION_STATE_FILE", "permission_requests.jsonl")
AUDIT_LOG_FILE = _store_path("CLAUDE_PERMISSION_AUDIT_FILE", "permission_actions.jsonl")
# Cold storage for compaction (epic 22, task 22-05). Append-only; never read by
# the hot paths, only by a human or an agent doing archaeology.
ARCHIVE_FILE = _store_path(
    "CLAUDE_PERMISSION_ARCHIVE_FILE", "permission_requests.archive.jsonl"
)
# The "why did this prompt" log written by ``pretool_hook.log_manual_confirmation``.
# The store does not write it — it only rotates it during compaction, so the
# default must resolve to exactly the path the hook appends to. The override name
# is the one ``permissions_mcp_lib`` already reads, so all three agree.
MANUAL_CONFIRM_LOG_FILE = _store_path(
    "CLAUDE_MANUAL_CONFIRM_LOG", "bash_manual_confirm.log"
)
DEFAULT_TTL_SECONDS = 3600  # 1 hour default TTL for pending requests
# Retention for terminal rows in the hot state file (state.md default 2: a
# number, not a design — change it here and in the reviewer prompt together).
DEFAULT_RETENTION_DAYS = 30
# Rotate the manual-confirmation log once it passes this size. It is JSONL that
# the daily reviewer greps over a 1-day window, so the threshold only has to keep
# the file from growing without bound.
DEFAULT_CONFIRM_LOG_MAX_BYTES = 5 * 1024 * 1024
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = _store_path("CLAUDE_PERMISSION_DEBUG_LOG", "permission_state_debug.log")


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
# An AI agent decided the request (epic 22): the permissions MCP writes it, and
# the relay-path wait loop adopts a terminal state only when it carries this
# source. Callers pass it explicitly — see update_request_state's inference note.
RESOLUTION_SOURCE_AGENT = "agent"
# Epic 26 layer 1: the hook caught a signal and cleaned up its own rows.
RESOLUTION_SOURCE_INTERRUPTED = "interrupted"
# Epic 26 layer 2: the owning process was gone when a later hook swept the row.
RESOLUTION_SOURCE_ORPHANED = "orphaned"
# The session runs in bypassPermissions mode (`claude --dangerously-skip-permissions`,
# which is what `amux-spawn --yolo` expands to on the Claude path, or an interactive
# switch to bypass mode). The hook auto-allowed without ever asking anyone, so the row
# is neither a Telegram tap nor a terminal answer.
RESOLUTION_SOURCE_BYPASS = "bypass"


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
    # Human-readable actor string for an agent-written decision, e.g.
    # "abc123def456 @ claude-hooks" (session id prefix @ cwd basename). None for
    # every human/timeout resolution. Composed by the caller, never here.
    actor_agent: Optional[str] = None
    resolution_source: Optional[str] = None  # "telegram" | "terminal" | "timeout" | "agent"
    resolved_at: Optional[str] = None  # ISO timestamp when resolved
    expired_notified_at: Optional[str] = None  # ISO timestamp when Telegram was revoked on expiry
    agent_id: Optional[str] = None  # subagent id (None for parent session)
    role: Optional[str] = None  # resolved role id; None = default destination
    # What the terminal answered, recorded by the PostToolUse hook so the parked
    # PermissionRequest hook can patch it into the chat the question went to.
    # JSON: {"answers": {question: answer}, "notes": {question: notes}} — a
    # *reduced* view of ``tool_response``, never the raw payload (the store is
    # append-only JSONL, re-read in full on every hook invocation).
    terminal_answers: Optional[str] = None
    # Who created this row (epic 26 layer 2). A row whose owner is gone is
    # closed by ``sweep_orphaned_requests``; a row with no owner (written before
    # epic 26) is never swept and keeps its TTL behaviour.
    owner_pid: Optional[int] = None
    owner_start_ticks: Optional[int] = None   # /proc/<pid>/stat field 22
    owner_host: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PermissionRequest':
        """Create from dictionary"""
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


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
    actor_agent: Optional[str] = None  # set when an agent, not a human, decided

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)


def debug_log(message: str):
    """Log debug message if debug mode is enabled."""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now(timezone.utc).isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass


def _ensure_state_directory():
    """Ensure the state file directory exists"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _acquire_lock(file_obj):
    """Acquire exclusive lock on file"""
    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)


def _acquire_lock_bounded(file_obj, timeout: float) -> bool:
    """Non-blocking acquire with a deadline. True if the lock is held."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.025)


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


def append_agent_decision_reason(entry: AuditEntry) -> None:
    """Write an agent decision-reason audit entry through the store's lock/append protocol.

    This is the sanctioned public path for callers (e.g. the permissions MCP
    server) that need to record a free-text justification alongside a state
    transition.  ``update_request_state`` has no channel for the reason string,
    so the reason is written as its own ``agent_decision`` audit entry; this
    function ensures it flows through the same writer that all other audit
    entries use (invariant 6: no bespoke JSONL writers outside this module).

    The on-disk shape is identical to what ``_append_audit_log`` would produce —
    this function delegates to it and adds no transformation of its own.
    """
    _append_audit_log(entry)


def _proc_start_ticks(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat, or None if it cannot be read.

    ``comm`` (field 2) is parenthesised and may contain spaces *and* ')', so the
    only safe split is on the LAST ') ' — never ``split()`` on the whole line.
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            rest = f.read().rsplit(") ", 1)[1].split()
        return int(rest[19])          # field 22 = index 19 after state (field 3)
    except Exception:
        return None


def create_request(
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    permission_suggestions: List[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    agent_id: Optional[str] = None,
    role: Optional[str] = None,
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
        agent_id: Subagent id (None for parent session)
        role: Resolved role id this request was routed to (None = default
            destination). The role's *token* is deliberately not persisted —
            it is re-resolved from this id when needed.

    Returns:
        PermissionRequest object with request_id
    """
    _ensure_state_directory()

    now = _utc_now()
    # 12 hex chars (48 bits). Short enough to stay greppable/readable, wide
    # enough that collisions are negligible across the tens-of-thousands of
    # rows this append log accumulates (8 hex / 32 bits was collision-prone).
    request_id = uuid.uuid4().hex[:12]

    # Stamp owner identity (epic 26 layer 2). All three inside one try/except so
    # a row that cannot be stamped keeps None for all three and is never swept —
    # exactly today's behaviour for legacy rows.
    owner_pid: Optional[int] = None
    owner_start_ticks: Optional[int] = None
    owner_host: Optional[str] = None
    try:
        _pid = os.getpid()
        owner_pid = _pid
        owner_start_ticks = _proc_start_ticks(_pid)
        owner_host = socket.gethostname()
    except Exception:
        owner_pid = None
        owner_start_ticks = None
        owner_host = None

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
        agent_id=agent_id,
        role=role,
        owner_pid=owner_pid,
        owner_start_ticks=owner_start_ticks,
        owner_host=owner_host,
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
        expire_pending_requests(request_id=request_id)

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
    terminal_answers: Optional[str] = None,
    actor_agent: Optional[str] = None,
    lock_timeout: Optional[float] = None,
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
        resolution_source: Optional source of resolution
            ("telegram" | "terminal" | "timeout" | "agent" | "interrupted" | "orphaned")
        terminal_answers: Optional reduced JSON of the terminal's answers.
            Written only when a value is passed, so a later sweep over sibling
            rows cannot blank out what PostToolUse recorded a moment earlier.
        actor_agent: Optional human-readable actor string for an agent-written
            decision. Written only when a value is passed (same guarded shape as
            actor_user_id), so a later write cannot blank out the attribution.
        lock_timeout: When None (default), acquires the file lock with blocking
            LOCK_EX, exactly as before. When a float, uses a bounded non-blocking
            acquire (25 ms polling) that gives up after this many seconds and
            returns None — the same "did not update" contract the function already
            has for a terminal row. Used by the signal handler (epic 26 layer 1)
            to avoid deadlocking against a lock the main thread already holds.

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

    # Determine resolution source based on state if not provided.
    #
    # This inference must never claim an agent decision: an agent writer
    # (RESOLUTION_SOURCE_AGENT) always passes ``resolution_source`` explicitly,
    # because the relay-path wait loop gates on that exact value. A write that
    # let itself be inferred here would be labelled "telegram" and silently lose
    # its attribution (epic 22, invariant 5).
    if resolution_source is None and new_state in TERMINAL_STATES:
        if new_state == RequestState.RESOLVED_TERMINAL:
            resolution_source = RESOLUTION_SOURCE_TERMINAL
        elif new_state == RequestState.EXPIRED:
            resolution_source = RESOLUTION_SOURCE_TIMEOUT
        else:
            resolution_source = RESOLUTION_SOURCE_TELEGRAM

    # Hold lock for entire read-modify-write operation to prevent race conditions
    with open(STATE_FILE, 'r+') as f:
        if lock_timeout is not None:
            if not _acquire_lock_bounded(f, lock_timeout):
                debug_log(
                    f"update_request_state: could not acquire lock within {lock_timeout}s "
                    f"for {request_id}; giving up (layer 2 sweep will recover)"
                )
                return None
        else:
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
                        if actor_agent is not None:
                            data['actor_agent'] = actor_agent
                        if terminal_answers is not None:
                            data['terminal_answers'] = terminal_answers
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
            actor_agent=actor_agent,
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


def expire_pending_requests(request_id: Optional[str] = None) -> List[PermissionRequest]:
    """
    Mark expired pending requests as expired and keep them for audit/callback context.

    Returns:
        Requests newly transitioned to expired.
    """
    if not STATE_FILE.exists():
        return []

    # Hold lock for entire read-modify-write operation to prevent race conditions
    expired_requests: List[PermissionRequest] = []
    now = _utc_now()

    with open(STATE_FILE, 'r+') as f:
        _acquire_lock(f)
        try:
            # Ensure we read from beginning of file
            f.seek(0)
            # Read all requests
            lines = f.readlines()

            kept_lines = []

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                    expires_at = data.get('expires_at', '')

                    if (data.get('state') == RequestState.PENDING.value and
                        (request_id is None or data.get('request_id') == request_id) and
                        _is_expired(expires_at)):
                        data['state'] = RequestState.EXPIRED.value
                        data['updated_at'] = now
                        data['resolution_source'] = RESOLUTION_SOURCE_TIMEOUT
                        data['resolved_at'] = now
                        expired_requests.append(PermissionRequest.from_dict(data))

                    kept_lines.append(json.dumps(data) + '\n')

                except json.JSONDecodeError:
                    kept_lines.append(line + '\n')

            # Write back (truncate and rewrite)
            f.seek(0)
            f.truncate()
            f.writelines(kept_lines)
            f.flush()
            os.fsync(f.fileno())

        finally:
            _release_lock(f)

    for request in expired_requests:
        audit_entry = AuditEntry(
            timestamp=_utc_now(),
            request_id=request.request_id,
            action=RequestState.EXPIRED.value,
            actor_user_id=None,
            previous_state=RequestState.PENDING.value,
            new_state=RequestState.EXPIRED.value,
        )
        _append_audit_log(audit_entry)

    if expired_requests:
        debug_log(f"Expired {len(expired_requests)} requests")

    return expired_requests


def cleanup_expired_requests() -> int:
    """
    Mark expired pending requests as expired.

    Returns:
        Number of requests marked expired.
    """
    return len(expire_pending_requests())


def sweep_orphaned_requests() -> List[PermissionRequest]:
    """Close pending rows whose owning process no longer exists.

    Returns:
        List of rows that were swept (state set to RESOLVED_TERMINAL /
        orphaned). The caller is responsible for revoking their Telegram
        messages — the store does no network I/O.

    Decision table for each pending, non-expired row:

    | Condition                                  | Action          |
    |--------------------------------------------|-----------------|
    | owner_pid is None (pre-epic row)           | skip            |
    | owner_host != socket.gethostname()         | skip            |
    | owner_pid == os.getpid()                   | skip (us)       |
    | os.kill(pid, 0) raises ProcessLookupError  | sweep           |
    | os.kill(pid, 0) raises PermissionError     | skip (alive)    |
    | _proc_start_ticks(pid) is None             | skip (unknown)  |
    | stored owner_start_ticks is None           | skip (unknown)  |
    | ticks differ from stored value             | sweep (reused)  |
    | otherwise                                  | skip (alive)    |

    Unknown liveness always means LEAVE ALONE (brd §3 H4): failing to sweep
    is invisible and harmless; sweeping a live row destroys a prompt the
    operator is looking at.
    """
    if not STATE_FILE.exists():
        return []

    swept_requests: List[PermissionRequest] = []
    now = _utc_now()
    changed = False

    try:
        this_host = socket.gethostname()
    except Exception:
        # Cannot determine our hostname — skip the entire sweep.
        debug_log("sweep_orphaned_requests: gethostname() failed, skipping")
        return []

    this_pid = os.getpid()

    with open(STATE_FILE, 'r+') as f:
        _acquire_lock(f)
        try:
            f.seek(0)
            lines = f.readlines()
            kept_lines: List[str] = []

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    data = json.loads(stripped)

                    if data.get('state') != RequestState.PENDING.value:
                        kept_lines.append(json.dumps(data) + '\n')
                        continue

                    # Skip expired rows — leave them to expire_pending_requests.
                    if _is_expired(data.get('expires_at', '')):
                        kept_lines.append(json.dumps(data) + '\n')
                        continue

                    owner_pid = data.get('owner_pid')
                    if owner_pid is None:
                        # Legacy row (no owner stamp) — never swept.
                        kept_lines.append(json.dumps(data) + '\n')
                        continue

                    if data.get('owner_host') != this_host:
                        # Different host — we cannot see its /proc.
                        kept_lines.append(json.dumps(data) + '\n')
                        continue

                    if owner_pid == this_pid:
                        # This process created the row — cannot be orphaned yet.
                        kept_lines.append(json.dumps(data) + '\n')
                        continue

                    # --- Liveness check ---
                    do_sweep = False
                    try:
                        os.kill(owner_pid, 0)
                        # Process exists (or we lack permission). Check start time
                        # to guard against PID reuse.
                        current_ticks = _proc_start_ticks(owner_pid)
                        stored_ticks = data.get('owner_start_ticks')
                        if current_ticks is None or stored_ticks is None:
                            # Cannot confirm identity — leave alone.
                            do_sweep = False
                        elif current_ticks != stored_ticks:
                            # PID was reused by a different process — owner is gone.
                            do_sweep = True
                        else:
                            # Same PID, same start time — owner is alive.
                            do_sweep = False
                    except ProcessLookupError:
                        # PID is gone.
                        do_sweep = True
                    except PermissionError:
                        # Process exists but belongs to another user — alive.
                        do_sweep = False
                    except Exception:
                        # Unexpected error — fail open, leave alone.
                        do_sweep = False

                    if do_sweep:
                        data['state'] = RequestState.RESOLVED_TERMINAL.value
                        data['resolution_source'] = RESOLUTION_SOURCE_ORPHANED
                        data['resolved_at'] = now
                        data['updated_at'] = now
                        swept_requests.append(PermissionRequest.from_dict(data))
                        changed = True

                    kept_lines.append(json.dumps(data) + '\n')

                except json.JSONDecodeError:
                    kept_lines.append(stripped + '\n')

            # Only rewrite the file when something changed.
            if changed:
                f.seek(0)
                f.truncate()
                f.writelines(kept_lines)
                f.flush()
                os.fsync(f.fileno())

        finally:
            _release_lock(f)

    for request in swept_requests:
        audit_entry = AuditEntry(
            timestamp=_utc_now(),
            request_id=request.request_id,
            action=RESOLUTION_SOURCE_ORPHANED,
            actor_user_id=None,
            previous_state=RequestState.PENDING.value,
            new_state=RequestState.RESOLVED_TERMINAL.value,
        )
        _append_audit_log(audit_entry)

    if swept_requests:
        debug_log(f"sweep_orphaned_requests: swept {len(swept_requests)} rows")

    return swept_requests


def mark_expired_notified(request_id: str) -> bool:
    """Record that the Telegram message for an expired request was revoked."""
    if not STATE_FILE.exists():
        return False

    now = _utc_now()

    with open(STATE_FILE, 'r+') as f:
        _acquire_lock(f)
        try:
            f.seek(0)
            lines = f.readlines()
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
                        data['expired_notified_at'] = now
                        data['updated_at'] = now
                    updated_lines.append(json.dumps(data) + '\n')
                except json.JSONDecodeError:
                    updated_lines.append(line + '\n')

            if not found:
                return False

            f.seek(0)
            f.truncate()
            f.writelines(updated_lines)
            f.flush()
            os.fsync(f.fileno())
        finally:
            _release_lock(f)

    return True


def get_expired_unnotified_requests() -> List[PermissionRequest]:
    """Return expired requests whose Telegram messages still need revocation."""
    if not STATE_FILE.exists():
        return []

    requests: List[PermissionRequest] = []

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if (data.get('state') == RequestState.EXPIRED.value and
                        data.get('telegram_message_id') and
                        not data.get('expired_notified_at')):
                        requests.append(PermissionRequest.from_dict(data))
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    return requests


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


def _classify_candidate(
    row_tool_input: Dict[str, Any],
    posted_tool_input: Optional[Dict[str, Any]],
    tool_name: str,
) -> str:
    """Three-valued classifier for one candidate row (§4, task 27).

    Returns one of:
      "same"       — the row's stored input identifies this specific call
      "different"  — the row's stored input identifies a *different* call
      "cannot_tell" — no verified comparator for this shape, or insufficient
                     data to decide; treated as eligible for legacy fallback

    Rules:
    - If ``posted_tool_input`` is None, every row is "cannot_tell" so that
      callers that omit tool_input preserve existing behaviour exactly.
    - Only ``AskUserQuestion`` and ``Bash`` have verified comparators (§4.2).
      Everything else is "cannot_tell" — an unverified comparator that wrongly
      returns "different" is the H2 failure mode (orphaned cards).
    - H4: two byte-identical questions asked in parallel stay indistinguishable;
      "cannot_tell" is the correct verdict and ``created_at`` picks one.
      Both candidates carry the same answer, so the wrong pick is harmless.
    - H1 (fail open): any exception within this function is caught by the
      caller, which treats the row as "cannot_tell" and logs.
    """
    if posted_tool_input is None:
        return "cannot_tell"

    if tool_name == "AskUserQuestion":
        # Compare question TEXT only — never the whole dict.
        # Two live reasons: role-routed rows store the alias-stripped header
        # (the row holds 'Q-531' where the payload says '@htl Q-531'), and a
        # Telegram-answered call reaches PostToolUse with updatedInput applied
        # ({**tool_input, 'answers': {...}}). Question strings survive both;
        # the dict does not (§4.2).
        row_q = row_tool_input.get("question") if isinstance(row_tool_input, dict) else None
        # posted tool_input may be the original shape {questions: [...]} or the
        # alias-stripped single-question shape {question: "..."}. Accept both.
        posted_q: Optional[str] = None
        if isinstance(posted_tool_input, dict):
            if "question" in posted_tool_input:
                posted_q = posted_tool_input.get("question")
            elif "questions" in posted_tool_input:
                questions = posted_tool_input.get("questions")
                if isinstance(questions, list) and questions:
                    # Take first question's text as the representative string.
                    # For single-question calls this is exact. For multi-question
                    # calls each child row was created with its own single
                    # question, so the child's row_q matches the posted first
                    # question only if it is that child — others are "different".
                    first = questions[0]
                    if isinstance(first, dict):
                        posted_q = first.get("question")
        if row_q and posted_q:
            return "same" if row_q == posted_q else "different"
        return "cannot_tell"

    if tool_name == "Bash":
        # command is the identity; description is model prose (§4.2).
        row_cmd = (row_tool_input.get("command") if isinstance(row_tool_input, dict) else None) or ""
        posted_cmd = (posted_tool_input.get("command") if isinstance(posted_tool_input, dict) else None) or ""
        if row_cmd and posted_cmd:
            return "same" if row_cmd == posted_cmd else "different"
        return "cannot_tell"

    # All other tools: no verified comparator — cannot tell.
    return "cannot_tell"


def find_pending_request_by_tool_session(
    session_id: str,
    tool_name: str,
    cwd: Optional[str] = None,
    agent_id: Optional[str] = None,
    tool_input: Optional[Dict[str, Any]] = None,
) -> Optional[PermissionRequest]:
    """
    Find a pending request matching tool name and session.

    Used by PostToolUse hook to find and revoke Telegram messages when
    a tool is executed via terminal.

    Args:
        session_id: Claude session UUID
        tool_name: Name of the tool that was executed
        cwd: Optional working directory to match
        agent_id: Optional subagent id to match (prevents cross-agent cancellation)
        tool_input: The posted tool input for this specific call.  When supplied,
            uses a three-valued per-row classifier (§4, task 27) to exclude rows
            that definitively belong to a *different* concurrent call of the same
            tool in the same session.  When omitted (None) every row is treated as
            "cannot_tell" — preserving the pre-task-27 behaviour exactly.

    Returns:
        Most recent matching pending PermissionRequest if found, None otherwise.
        Returns None when every candidate is excluded as "different call" — the
        correct no-op for a call whose row was already resolved by its own hook.

    Three-valued classification per candidate row:
      "same"        → eligible, preferred
      "different"   → excluded outright — never reaches any fallback
      "cannot_tell" → eligible, used only when no "same" row exists
    """
    if not STATE_FILE.exists():
        return None

    same_call: List[PermissionRequest] = []
    cannot_tell: List[PermissionRequest] = []

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
                        # Filter by agent_id: a PostToolUse from agent X must
                        # only match requests created by agent X (or both None
                        # for the parent session).
                        if data.get('agent_id') != agent_id:
                            continue
                        request = PermissionRequest.from_dict(data)
                        # Skip if expired
                        if _is_expired(request.expires_at):
                            continue
                        # Three-valued classification (task 27 §4).
                        # H1: any exception from _classify_candidate is caught
                        # here so the row falls through to "cannot_tell".
                        try:
                            verdict = _classify_candidate(
                                request.tool_input,
                                tool_input,
                                tool_name,
                            )
                        except Exception as e:  # noqa: BLE001 — H1 fail open
                            # debug_log() itself is wrapped in try/except so
                            # it cannot re-raise and defeat the fail-open goal.
                            debug_log(
                                f"_classify_candidate error for "
                                f"{tool_name}/{request.request_id}: "
                                f"{type(e).__name__}: {e}"
                            )
                            verdict = "cannot_tell"
                        if verdict == "same":
                            same_call.append(request)
                        elif verdict == "cannot_tell":
                            cannot_tell.append(request)
                        # "different" → excluded outright, not appended anywhere.
                        # The fallback (cannot_tell) is therefore unreachable by
                        # a row that has been definitively excluded — this is the
                        # key invariant that prevents the §4.1 trap.
                except json.JSONDecodeError:
                    continue
        finally:
            _release_lock(f)

    # Prefer "same call" rows; fall back to "cannot_tell"; empty → None.
    eligible = same_call if same_call else cannot_tell
    if not eligible:
        return None

    # Return most recent by created_at (§4.3: multiple matches are legitimate).
    eligible.sort(key=lambda r: r.created_at, reverse=True)
    return eligible[0]


def resolve_via_terminal(
    request_id: str, terminal_answers: Optional[str] = None
) -> Optional[PermissionRequest]:
    """
    Mark a request as resolved via terminal prompt.

    This is called by the PostToolUse hook when a tool is executed
    after the user responded via terminal instead of Telegram.

    Args:
        request_id: The request ID to mark as terminal-resolved
        terminal_answers: Reduced JSON of what the terminal answered
            (AskUserQuestion only). Written **only when a value is passed** —
            the PermissionRequest hook's own sweep over still-pending sibling
            rows calls this with no answers and must not blank out what
            PostToolUse just recorded.

    Returns:
        Updated PermissionRequest if successful, None otherwise
    """
    return update_request_state(
        request_id,
        RequestState.RESOLVED_TERMINAL,
        resolution_source=RESOLUTION_SOURCE_TERMINAL,
        terminal_answers=terminal_answers,
    )


def get_requests(
    states: Optional[List[Any]] = None,
    since: Optional[str] = None,
) -> List[PermissionRequest]:
    """
    General reader over the state store, for callers that need rows in states
    other than ``pending`` (the permissions MCP's listing and history tools).

    Lives here rather than in the caller because readers follow the same
    discipline as writers: one module owns the file, its lock protocol and its
    JSONL format (epic 22 invariant 6). Nothing outside this module parses
    ``permission_requests.jsonl``.

    Args:
        states: Iterable of states to keep, as ``RequestState`` members or raw
            values (``"pending"``, ``"allow"``, ...). ``None`` keeps every state.
        since: Optional ISO-8601 timestamp. Keeps rows whose most recent
            activity is at or after it — ``resolved_at`` when the row is
            resolved, else ``updated_at``, else ``created_at``. A row with an
            unparseable timestamp is KEPT (dropping rows silently on a bad
            timestamp would hide exactly the anomalies a reviewer is looking
            for). A malformed ``since`` raises ``ValueError``.

    Returns:
        Matching requests in file order. This is a **pure** reader: unlike
        ``get_request``/``get_all_pending_requests`` it never marks lapsed
        pending rows expired as a side effect, so a caller asking for
        ``pending`` here may see rows whose TTL has passed.
    """
    if not STATE_FILE.exists():
        return []

    wanted: Optional[set] = None
    if states is not None:
        wanted = {
            state.value if isinstance(state, RequestState) else str(state)
            for state in states
        }

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

    def _activity_after_cutoff(data: Dict[str, Any]) -> bool:
        raw = (data.get('resolved_at') or data.get('updated_at')
               or data.get('created_at'))
        if not raw:
            return True
        try:
            stamp = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp >= since_dt

    matched: List[PermissionRequest] = []

    with open(STATE_FILE, 'r') as f:
        _acquire_lock(f)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if wanted is not None and data.get('state') not in wanted:
                    continue
                if since_dt is not None and not _activity_after_cutoff(data):
                    continue
                matched.append(PermissionRequest.from_dict(data))
        finally:
            _release_lock(f)

    return matched


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


def _row_activity(data: Dict[str, Any]) -> Optional[datetime]:
    """The row's most recent activity timestamp, or None if unusable.

    Same precedence as ``get_requests``' ``since`` filter — ``resolved_at``,
    else ``updated_at``, else ``created_at`` — so "old" means the same thing to
    the reader and to compaction.
    """
    raw = data.get('resolved_at') or data.get('updated_at') or data.get('created_at')
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def rotate_manual_confirm_log(
    max_bytes: int = DEFAULT_CONFIRM_LOG_MAX_BYTES,
) -> Optional[str]:
    """Rename ``bash_manual_confirm.log`` aside when it exceeds ``max_bytes``.

    The PreToolUse hook appends with ``open(path, 'a')``, which recreates a
    missing file on the next write, so a rename needs no coordination with a
    running hook: a writer holding the old descriptor finishes its line into the
    renamed file, and the next invocation starts a fresh one.

    Returns the path the log was rotated to, or None when no rotation was
    needed. Never raises: a failed rotation must not sink a compaction run.
    """
    path = MANUAL_CONFIRM_LOG_FILE
    try:
        if not path.exists():
            return None
        if path.stat().st_size <= max_bytes:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = path.with_name(f"{path.name}.{stamp}")
        # Never clobber an existing rotation from the same second.
        suffix = 1
        while destination.exists():
            destination = path.with_name(f"{path.name}.{stamp}.{suffix}")
            suffix += 1
        path.rename(destination)
        return str(destination)
    except Exception as e:
        debug_log(f"Failed to rotate manual-confirm log: {e}")
        return None


def compact(
    max_age_days: float = DEFAULT_RETENTION_DAYS,
    rotate_confirm_log: bool = True,
    confirm_log_max_bytes: int = DEFAULT_CONFIRM_LOG_MAX_BYTES,
) -> Dict[str, Any]:
    """Move terminal rows older than the cutoff out of the hot state file.

    Retention policy (epic 22, task 22-05 / brd D8): the hot file keeps every
    **pending** row regardless of age — expiry is what retires those, and a
    pending row moved to cold storage would strand a waiting hook — plus every
    terminal row whose most recent activity is at or after
    ``now - max_age_days``. Everything else is appended to
    ``permission_requests.archive.jsonl``.

    A row whose timestamps are missing or unparseable is **kept**. Dropping rows
    on a bad timestamp would quietly delete exactly the anomalies a reviewer is
    looking for (same posture as ``get_requests``).

    Runs entirely inside this module's flock protocol (brd H7 / invariant 6):
    the exclusive lock on the state file is taken before the read and released
    only after the rewrite is fsynced, so no concurrent writer can slip an
    update into the window between them. The archive is appended **before** the
    hot file is truncated, so a crash mid-run duplicates a row into cold storage
    rather than losing it.

    Args:
        max_age_days: Retention window for terminal rows, in days.
        rotate_confirm_log: Also rotate ``bash_manual_confirm.log`` when it is
            over ``confirm_log_max_bytes`` (the daily reviewer does both in one
            run). Pass False to compact the store only.
        confirm_log_max_bytes: Size threshold for that rotation.

    Returns:
        A counts dict: ``scanned``, ``archived``, ``kept``, ``kept_pending``,
        ``kept_unparseable``, plus ``cutoff``, ``state_file``, ``archive_file``
        and ``confirm_log_rotated_to`` (None when nothing was rotated).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=float(max_age_days))
    result: Dict[str, Any] = {
        "scanned": 0,
        "archived": 0,
        "kept": 0,
        "kept_pending": 0,
        "kept_unparseable": 0,
        "cutoff": cutoff.isoformat(),
        "max_age_days": float(max_age_days),
        "state_file": str(STATE_FILE),
        "archive_file": str(ARCHIVE_FILE),
        "confirm_log_rotated_to": None,
    }

    terminal_values = {state.value for state in TERMINAL_STATES}

    if STATE_FILE.exists():
        with open(STATE_FILE, 'r+') as f:
            _acquire_lock(f)
            try:
                f.seek(0)
                lines = f.readlines()

                kept_lines: List[str] = []
                archived_lines: List[str] = []

                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue

                    result["scanned"] += 1

                    try:
                        data = json.loads(stripped)
                    except json.JSONDecodeError:
                        # Unparseable line: keep it verbatim. It is evidence.
                        kept_lines.append(stripped + '\n')
                        result["kept"] += 1
                        result["kept_unparseable"] += 1
                        continue

                    if data.get('state') not in terminal_values:
                        kept_lines.append(stripped + '\n')
                        result["kept"] += 1
                        result["kept_pending"] += 1
                        continue

                    activity = _row_activity(data)
                    if activity is None:
                        kept_lines.append(stripped + '\n')
                        result["kept"] += 1
                        result["kept_unparseable"] += 1
                        continue

                    if activity >= cutoff:
                        kept_lines.append(stripped + '\n')
                        result["kept"] += 1
                        continue

                    archived_lines.append(stripped + '\n')
                    result["archived"] += 1

                if archived_lines:
                    # Append to cold storage first, then rewrite the hot file.
                    ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with open(ARCHIVE_FILE, 'a') as archive:
                        archive.writelines(archived_lines)
                        archive.flush()
                        os.fsync(archive.fileno())

                    f.seek(0)
                    f.truncate()
                    f.writelines(kept_lines)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                _release_lock(f)

    if rotate_confirm_log:
        result["confirm_log_rotated_to"] = rotate_manual_confirm_log(
            confirm_log_max_bytes
        )

    debug_log(
        f"Compacted store: archived {result['archived']}, kept {result['kept']}"
    )
    return result


def _cli(argv: List[str]) -> int:
    """Command-line entry: ``python3 permission_state_store.py compact``.

    Exists so the daily reviewer — and a human — can run retention without
    hand-editing JSONL, which is the one thing invariant 6 forbids.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="permission_state_store",
        description="Maintenance entry points for the permission state store.",
    )
    subparsers = parser.add_subparsers(dest="command")

    compact_parser = subparsers.add_parser(
        "compact",
        help="Archive terminal rows older than the retention window.",
    )
    compact_parser.add_argument(
        "--max-age-days",
        type=float,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Retention window for terminal rows (default: {DEFAULT_RETENTION_DAYS}).",
    )
    compact_parser.add_argument(
        "--no-rotate-log",
        action="store_true",
        help="Skip the bash_manual_confirm.log rotation.",
    )
    compact_parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=DEFAULT_CONFIRM_LOG_MAX_BYTES,
        help=f"Rotate the confirm log above this size (default: {DEFAULT_CONFIRM_LOG_MAX_BYTES}).",
    )
    compact_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the counts dict as JSON instead of a human summary.",
    )

    subparsers.add_parser(
        "selftest",
        help="Exercise create/update/cleanup against the configured state file.",
    )

    args = parser.parse_args(argv)

    if args.command == "compact":
        counts = compact(
            max_age_days=args.max_age_days,
            rotate_confirm_log=not args.no_rotate_log,
            confirm_log_max_bytes=args.log_max_bytes,
        )
        if args.json:
            print(json.dumps(counts, indent=2))
        else:
            print(f"state file:  {counts['state_file']}")
            print(f"archive:     {counts['archive_file']}")
            print(f"cutoff:      {counts['cutoff']} ({counts['max_age_days']} days)")
            print(f"scanned:     {counts['scanned']}")
            print(f"archived:    {counts['archived']}")
            print(
                f"kept:        {counts['kept']} "
                f"({counts['kept_pending']} pending, "
                f"{counts['kept_unparseable']} undatable)"
            )
            rotated = counts['confirm_log_rotated_to']
            print(f"confirm log: {rotated if rotated else 'not rotated'}")
        return 0

    if args.command == "selftest":
        return _selftest()

    parser.print_help()
    return 0


def _selftest() -> int:
    """The original ``__main__`` smoke script, now behind an explicit subcommand.

    It writes real rows, so it must never run by accident — a bare
    ``python3 permission_state_store.py`` prints help instead.
    """
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

    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_cli(sys.argv[1:]))
