#!/usr/bin/env python3
"""Per-session "YOLO" (allow-all) preference store.

When the operator taps the YOLO button on a Telegram permission request, every
*subsequent* permission request from the same Claude session should auto-allow
without prompting. Session identity (``session_id``) is available only inside the
hook process — the relay knows nothing about Claude sessions — so this flag lives
here, next to the permission state store, rather than on the relay server.

The flag is keyed strictly by ``session_id`` and persists for the whole session:
there is no TTL. Stale entries are pruned opportunistically by ``prune`` (called
from the hook's periodic cleanup) once they age past ``MAX_AGE_SECONDS``, which
only bounds file growth — it is far longer than any real session.

The store is a single JSON object ``{session_id: {"enabled_at": iso8601}}`` guarded
by an exclusive file lock, mirroring ``permission_state_store``'s conventions
(env-var path override for tests, best-effort error handling).
"""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict


def _store_path() -> Path:
    override = os.environ.get("CLAUDE_SESSION_YOLO_FILE")
    return Path(override) if override else (Path.home() / ".claude" / "session_yolo.json")


STORE_FILE = _store_path()

# Prune entries older than this. Generous — only a backstop against unbounded
# growth across many sessions, not a per-session expiry (sessions auto-allow for
# their whole life regardless of age).
MAX_AGE_SECONDS = 7 * 24 * 3600


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(f) -> Dict[str, Dict[str, str]]:
    """Read the JSON object from an open file handle, tolerating empty/corrupt."""
    f.seek(0)
    raw = f.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def enable(session_id: str) -> None:
    """Mark ``session_id`` as YOLO/allow-all. Best-effort; no-op on empty id."""
    if not session_id:
        return
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Open for read+write (create if absent) and hold the lock across the
    # read-modify-write so concurrent hooks can't clobber each other.
    with open(STORE_FILE, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = _read(f)
            data[session_id] = {"enabled_at": _utc_now()}
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def is_enabled(session_id: str) -> bool:
    """Return True if ``session_id`` has been put into YOLO/allow-all mode."""
    if not session_id:
        return False
    try:
        with open(STORE_FILE, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                data = _read(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return session_id in data


def prune(max_age_seconds: int = MAX_AGE_SECONDS) -> None:
    """Drop entries older than ``max_age_seconds``. Best-effort backstop."""
    try:
        with open(STORE_FILE, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                data = _read(f)
                if not data:
                    return
                now = datetime.now(timezone.utc)
                kept = {}
                for sid, rec in data.items():
                    try:
                        enabled_at = datetime.fromisoformat(rec.get("enabled_at", ""))
                        if (now - enabled_at).total_seconds() <= max_age_seconds:
                            kept[sid] = rec
                    except (ValueError, TypeError):
                        # Unparseable timestamp — drop it.
                        continue
                if len(kept) != len(data):
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps(kept))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (FileNotFoundError, OSError):
        return
