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


def _write(f, data: Dict[str, Dict[str, str]]) -> None:
    """Overwrite the file with ``data`` and flush to disk.

    The flush+fsync MUST happen while the caller still holds the flock: Python
    otherwise defers the actual write to file close, which occurs *after* the
    lock is released, letting concurrent writers clobber each other.
    """
    f.seek(0)
    f.truncate()
    f.write(json.dumps(data))
    f.flush()
    os.fsync(f.fileno())


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
            _write(f, data)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def disable(session_id: str) -> None:
    """Clear the YOLO flag for ``session_id``. Best-effort; no-op if absent."""
    if not session_id:
        return
    try:
        with open(STORE_FILE, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                data = _read(f)
                if session_id in data:
                    del data[session_id]
                    _write(f, data)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (FileNotFoundError, OSError):
        return


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
                    _write(f, kept)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (FileNotFoundError, OSError):
        return


def _looks_like_session_id(value: str) -> bool:
    """Reject empty / unsubstituted-template values so the /yolo commands fail
    loudly instead of writing a garbage key. Claude Code substitutes
    ``${CLAUDE_SESSION_ID}`` before the command's shell runs; if substitution
    didn't happen (e.g. an older client), the literal ``${...}`` arrives here."""
    return bool(value) and not value.startswith("${") and "$" not in value


def _main(argv) -> int:
    """Tiny CLI so the /yolo and /yolo-off slash commands can toggle the flag
    for the current session: ``session_yolo_store.py {enable|disable|status} <id>``."""
    if len(argv) != 2 or argv[0] not in ("enable", "disable", "status"):
        print("usage: session_yolo_store.py {enable|disable|status} <session_id>")
        return 2
    action, session_id = argv
    if not _looks_like_session_id(session_id):
        print(f"refusing to {action}: invalid session id {session_id!r} "
              "(is ${CLAUDE_SESSION_ID} supported by this Claude Code version?)")
        return 1
    if action == "enable":
        enable(session_id)
        print(f"YOLO enabled for session {session_id}")
    elif action == "disable":
        disable(session_id)
        print(f"YOLO disabled for session {session_id}")
    else:  # status
        print("on" if is_enabled(session_id) else "off")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
