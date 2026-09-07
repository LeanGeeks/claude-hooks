#!/usr/bin/env python3
"""Format-owning module for Claude lifecycle event logs (epic 37, task 37-01).

This is the single module that reads and writes the per-session
``<name>.lifecycle.jsonl`` artifact.  The producer
(``spawn_producer_hook.py``) imports it to append events; future readers
(37-02 reducer, 37-03 watch) import it to parse them.  Neither
reimplements serialization or sizing.

The artifact is append-only, one JSON line per lifecycle transition.  Each
record is bounded to fit inside a single atomic ``O_APPEND`` write
(``< PIPE_BUF`` = 4096 bytes on Linux), so no lock is needed and
interleaving of concurrent writes is impossible for well-formed records.

Naming follows the Codex convention (``amux_spawn_lib.py:690``) — a
non-``.json`` extension that cannot collide with the handle glob, keyed by
the amux session name:

    ``~/.amux/spawn/<name>.lifecycle.jsonl``

**Retention (BRD s6):** the log is NOT deleted by ``cmd_rm``.  ``cmd_rm``
currently removes Codex artifacts through ``remove_codex_artifacts``, but
knows nothing about ``.lifecycle.jsonl`` files, so they survive reap by
default.  This is intentional: BRD s6 requires a run to stay
reconstructable from the streams after its workers are reaped.

**Reused handle names:** events from successive sessions under the same
name accumulate in one file.  Each record carries ``session_id`` so a
reader can partition by session.

Pure library: no relay, no Telegram, no subprocess, no network.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

#: Suffix for Claude lifecycle event logs.  Distinct from the Codex
#: ``CODEX_EVENT_SUFFIX = ".events.jsonl"`` (architecture.md s3, state.md
#: decision 10).  The ``.jsonl`` extension cannot collide with the
#: ``*.json`` handle glob used by ``list_handles`` / ``live_tracked_count``.
CLAUDE_EVENT_SUFFIX = ".lifecycle.jsonl"

#: Maximum serialized record size in bytes.  Must be < ``PIPE_BUF`` (4096
#: on Linux) so a single ``O_APPEND`` write is atomic.  We leave headroom
#: for the trailing newline and minor platform variance.
MAX_RECORD_BYTES = 4000

#: Truncation marker inserted when ``last_message`` is shortened to fit.
TRUNCATION_MARKER = "[…truncated]"

# ── Event types ──────────────────────────────────────────────────────────────

EVENT_TURN_START = "turn_start"
EVENT_STOP = "stop"
EVENT_SUBAGENT_STOP = "subagent_stop"
EVENT_PERMISSION_PROMPT = "permission_prompt"
EVENT_SESSION_END = "session_end"

EVENT_TYPES = frozenset({
    EVENT_TURN_START,
    EVENT_STOP,
    EVENT_SUBAGENT_STOP,
    EVENT_PERMISSION_PROMPT,
    EVENT_SESSION_END,
})


# ── Record construction ─────────────────────────────────────────────────────

def make_event(
    *,
    event: str,
    state: str,
    session_id: str | None,
    seq: int,
    last_message: str | None = None,
    background_tasks_count: int = 0,
    permission_pending: bool = False,
    last_state: str | None = None,
) -> dict[str, Any]:
    """Build a lifecycle event record.

    The record carries everything a consumer needs to act without opening
    the worker's transcript or its pane:

    - ``event`` -- which lifecycle hook fired
    - ``state`` -- the handle's state AFTER this event
    - ``ts`` -- ISO-8601 timestamp (producer-owned, not a file stat)
    - ``seq`` -- monotonic within the log for ordering
    - ``session_id`` -- identifies the session (for reused handle names)
    - ``last_message`` -- the agent's last response (Stop only; bounded)
    - ``background_tasks_count`` -- number of outstanding background tasks
    - ``permission_pending`` -- whether a permission prompt is outstanding
    - ``last_state`` -- the state before termination (session_end only)
    """
    record: dict[str, Any] = {
        "seq": seq,
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "state": state,
        "session_id": session_id,
    }
    if last_message is not None:
        record["last_message"] = last_message
    record["background_tasks_count"] = background_tasks_count
    record["permission_pending"] = permission_pending
    if last_state is not None:
        record["last_state"] = last_state
    return record


# ── Serialization ────────────────────────────────────────────────────────────

def _encode(record: dict[str, Any]) -> bytes:
    """Serialize a record dict to a newline-terminated UTF-8 JSON line."""
    return (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def serialize_bounded(record: dict[str, Any]) -> bytes:
    """Serialize a record to a bounded newline-terminated JSON line.

    If the serialized form exceeds :data:`MAX_RECORD_BYTES`, truncates
    ``last_message`` (keeping the **tail**, where the conclusion lives)
    with a marker until it fits.  If the record is still too large after
    removing ``last_message`` entirely (should not happen with normal
    payloads), a hard truncation is applied as an ultimate fallback.

    Returns bytes ready for a single ``O_APPEND`` write.
    """
    encoded = _encode(record)
    if len(encoded) <= MAX_RECORD_BYTES:
        return encoded

    # The only field that can plausibly cause oversizing is last_message.
    if "last_message" not in record or not record["last_message"]:
        return encoded[:MAX_RECORD_BYTES]

    record = dict(record)
    msg = record["last_message"]
    excess = len(encoded) - MAX_RECORD_BYTES
    marker_len = len(TRUNCATION_MARKER)

    # First attempt: cut (excess + marker_len + 100) chars from the front
    # of the message.  The generous extra handles JSON-escaping expansion.
    cut = excess + marker_len + 100
    if cut < len(msg):
        record["last_message"] = TRUNCATION_MARKER + msg[cut:]
    else:
        record["last_message"] = TRUNCATION_MARKER

    encoded = _encode(record)

    # If still too large (heavy JSON escaping edge case), trim iteratively.
    while len(encoded) > MAX_RECORD_BYTES:
        body = record["last_message"]
        if len(body) <= marker_len + 1:
            # Cannot shrink further -- drop the message entirely.
            record.pop("last_message")
            encoded = _encode(record)
            break
        # Drop another chunk from the front of the body (after the marker).
        drop = max(len(encoded) - MAX_RECORD_BYTES, 50)
        remaining = body[marker_len + drop:]
        record["last_message"] = TRUNCATION_MARKER + remaining
        encoded = _encode(record)

    # Truncation succeeded in the normal case.
    if len(encoded) <= MAX_RECORD_BYTES:
        return encoded
    # Hard fallback: emit a minimal valid record rather than a truncated byte slice.
    minimal = {
        "seq": record.get("seq", 0),
        "ts": record.get("ts", ""),
        "event": record.get("event", "unknown"),
        "state": record.get("state", "unknown"),
        "session_id": record.get("session_id"),
        "truncated": True,
    }
    return _encode(minimal)


# ── Path helpers ─────────────────────────────────────────────────────────────

def lifecycle_log_path(spawn_dir: Path, name: str) -> Path:
    """The lifecycle log path for a given handle name.

    Deterministic: ``<spawn_dir>/<name>.lifecycle.jsonl``.
    """
    return spawn_dir / f"{name}{CLAUDE_EVENT_SUFFIX}"


# ── Append ───────────────────────────────────────────────────────────────────

def _count_lines(path: Path) -> int:
    """Count existing event lines in the log (0 if absent/unreadable)."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def append_event(
    spawn_dir: Path,
    name: str,
    *,
    event: str,
    state: str,
    session_id: str | None,
    last_message: str | None = None,
    background_tasks_count: int = 0,
    permission_pending: bool = False,
    last_state: str | None = None,
) -> None:
    """Append one lifecycle event to the log.  **Fail-soft: never raises.**

    The record is bounded and the write is ``O_APPEND``, so no lock is
    needed and the write cannot block or hang.  Called after the handle
    write (ordering invariant: state on disk before the event announcing
    it).
    """
    try:
        log_path = lifecycle_log_path(spawn_dir, name)
        seq = _count_lines(log_path) + 1
        record = make_event(
            event=event,
            state=state,
            session_id=session_id,
            seq=seq,
            last_message=last_message,
            background_tasks_count=background_tasks_count,
            permission_pending=permission_pending,
            last_state=last_state,
        )
        data = serialize_bounded(record)
        # O_APPEND + O_CREAT: atomic append, auto-create, mode 0600.
        fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 — fail-open: never disrupt the session
        pass


# ── Reading (for tests and future consumers) ─────────────────────────────────

def read_events(spawn_dir: Path, name: str) -> list[dict[str, Any]]:
    """Read all events from the lifecycle log.  Returns ``[]`` on any error."""
    log_path = lifecycle_log_path(spawn_dir, name)
    events: list[dict[str, Any]] = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return events
