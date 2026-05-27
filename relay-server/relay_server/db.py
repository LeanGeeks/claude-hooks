"""SQLite (WAL) schema management and connection helpers.

Phase 1 uses synchronous ``sqlite3`` calls wrapped in ``asyncio.to_thread``.
SQLite I/O is fast and the operations performed per request are tiny; the
threadpool keeps the code simple. If a fully-async driver is ever required,
the swap is mechanical.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS installations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    label               TEXT NOT NULL,
    token_hash          TEXT NOT NULL UNIQUE,
    telegram_chat_id    INTEGER,
    bound_user_id       INTEGER,
    created_at          TIMESTAMP NOT NULL,
    last_seen_at        TIMESTAMP,
    revoked_at          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id      INTEGER NOT NULL REFERENCES installations(id),
    telegram_chat_id     INTEGER NOT NULL,
    telegram_message_id  INTEGER NOT NULL,
    kind                 TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    state                TEXT NOT NULL,
    answer_json          TEXT,
    created_at           TIMESTAMP NOT NULL,
    answered_at          TIMESTAMP,
    expires_at           TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_state_expiry
    ON messages(state, expires_at);

CREATE TABLE IF NOT EXISTS binding_codes (
    code             TEXT PRIMARY KEY,
    installation_id  INTEGER NOT NULL REFERENCES installations(id),
    created_at       TIMESTAMP NOT NULL,
    expires_at       TIMESTAMP NOT NULL,
    consumed_at      TIMESTAMP,
    bound_chat_id    INTEGER,
    bound_user_id    INTEGER
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key              TEXT NOT NULL,
    installation_id  INTEGER NOT NULL,
    request_hash     TEXT,                -- SHA-256 of canonical request body
    response_json    TEXT,                -- NULL while request is in flight
    created_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (installation_id, key)
);
"""

# Per-version migration SQL applied in ascending order when bringing an
# older database forward. Each entry is a list of statements that bring the
# schema from version (key-1) to version (key).
MIGRATIONS: dict[int, list[str]] = {
    2: [
        # v1 had no request_hash; v1 response_json was NOT NULL. Rebuild the
        # table to add the column and relax response_json nullability.
        "ALTER TABLE idempotency_keys RENAME TO idempotency_keys_v1;",
        """
        CREATE TABLE idempotency_keys (
            key              TEXT NOT NULL,
            installation_id  INTEGER NOT NULL,
            request_hash     TEXT,
            response_json    TEXT,
            created_at       TIMESTAMP NOT NULL,
            PRIMARY KEY (installation_id, key)
        );
        """,
        "INSERT INTO idempotency_keys(key, installation_id, response_json, created_at)"
        " SELECT key, installation_id, response_json, created_at FROM idempotency_keys_v1;",
        "DROP TABLE idempotency_keys_v1;",
    ],
}

T = TypeVar("T")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a sqlite connection with WAL + sensible pragmas."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    _configure(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if absent, run any pending migrations, and stamp version."""
    # Look up the version before applying CREATE-IF-NOT-EXISTS so a v1 database
    # (which already has idempotency_keys without request_hash) is correctly
    # detected and routed through the migration path.
    pre_existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    current = 0
    if pre_existing is not None:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is not None:
            current = int(row["version"])

    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported schema version: {current} (expected <= {SCHEMA_VERSION})"
        )

    if current == 0:
        # Fresh database — install latest schema directly.
        with conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        return

    if current == SCHEMA_VERSION:
        # Already at latest; re-apply CREATE-IF-NOT-EXISTS to catch any tables
        # that were dropped manually (defensive no-op in normal operation).
        with conn:
            conn.executescript(SCHEMA_SQL)
        return

    # Apply migrations sequentially.
    for target in range(current + 1, SCHEMA_VERSION + 1):
        stmts = MIGRATIONS.get(target)
        if stmts is None:
            raise RuntimeError(
                f"Missing migration for schema version {target}; "
                f"cannot upgrade from {current} to {SCHEMA_VERSION}"
            )
        with conn:
            for stmt in stmts:
                conn.execute(stmt)
            conn.execute("UPDATE schema_version SET version = ?", (target,))


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return int(row["version"]) if row else 0


# Serialises access to the shared sqlite connection. ``sqlite3.Connection``
# objects opened with ``check_same_thread=False`` can be used from multiple
# threads, but only one operation may be in flight on the connection at a time
# — concurrent calls can otherwise raise ``sqlite3.InterfaceError: bad
# parameter or other API misuse`` or, worse, interleave a multi-statement
# transaction with reads/writes from another thread. Because every DB call in
# this module funnels through ``run_in_thread``, taking this lock here gives
# us a single chokepoint where the connection is guaranteed to be used by at
# most one thread, and lets handlers compose ``BEGIN IMMEDIATE`` / SELECT /
# UPDATE / COMMIT sequences without worrying about cross-thread interference.
_conn_lock = threading.Lock()


def _run_locked(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    with _conn_lock:
        return fn(*args, **kwargs)


async def run_in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking sqlite callable in the default threadpool.

    Serialises callers via ``_conn_lock`` so that the shared ``sqlite3``
    connection is only ever touched by one worker thread at a time.
    """
    return await asyncio.to_thread(_run_locked, fn, *args, **kwargs)
