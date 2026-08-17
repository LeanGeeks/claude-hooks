"""Tests for SQLite schema migrations and admin CLI."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from relay_server.admin_cli import cli
from relay_server.db import SCHEMA_VERSION, connect, get_schema_version, init_schema
from relay_server.tokens import hash_token


def test_init_schema_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = connect(db)
    init_schema(conn)
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "installations",
        "messages",
        "binding_codes",
        "idempotency_keys",
        "schema_version",
    }.issubset(tables)
    assert get_schema_version(conn) == SCHEMA_VERSION


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = connect(db)
    init_schema(conn)
    init_schema(conn)  # second call must not error or duplicate version row
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert len(rows) == 1


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = connect(db)
    init_schema(conn)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_messages_index_exists(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = connect(db)
    init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "messages_state_expiry" in indexes


def test_admin_cli_issue_list_revoke_rotate(tmp_path: Path) -> None:
    db = str(tmp_path / "admin.db")
    runner = CliRunner()

    r = runner.invoke(cli, ["--db", db, "issue", "--label", "laptop"])
    assert r.exit_code == 0, r.output
    assert "Token:" in r.output
    token = [
        line.split("Token:", 1)[1].strip()
        for line in r.output.splitlines()
        if line.strip().startswith("Token:")
    ][0]

    # Token is stored as hash, not plaintext.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM installations").fetchone()
    assert row["token_hash"] == hash_token(token)
    assert row["label"] == "laptop"
    inst_id = row["id"]

    r = runner.invoke(cli, ["--db", db, "list"])
    assert r.exit_code == 0
    assert "laptop" in r.output

    r = runner.invoke(cli, ["--db", db, "revoke", "--id", str(inst_id)])
    assert r.exit_code == 0
    row = conn.execute(
        "SELECT revoked_at FROM installations WHERE id = ?", (inst_id,)
    ).fetchone()
    assert row["revoked_at"] is not None

    r = runner.invoke(cli, ["--db", db, "rotate", "--id", str(inst_id)])
    assert r.exit_code == 0, r.output
    assert "New token:" in r.output
    new_token = [
        line.split("New token:", 1)[1].strip()
        for line in r.output.splitlines()
        if line.strip().startswith("New token:")
    ][0]
    row = conn.execute(
        "SELECT token_hash, revoked_at FROM installations WHERE id = ?",
        (inst_id,),
    ).fetchone()
    assert row["token_hash"] == hash_token(new_token)
    assert row["revoked_at"] is None  # rotate also un-revokes
    assert hash_token(new_token) != hash_token(token)


def test_admin_revoke_unknown_id_errors(tmp_path: Path) -> None:
    db = str(tmp_path / "admin.db")
    runner = CliRunner()
    r = runner.invoke(cli, ["--db", db, "revoke", "--id", "999"])
    assert r.exit_code != 0


def test_init_schema_rejects_unknown_future_version(tmp_path: Path) -> None:
    """A DB stamped with a version newer than the code knows about must
    refuse to initialize."""
    import pytest

    db = tmp_path / "future.db"
    conn = connect(db)
    init_schema(conn)
    with conn:
        conn.execute(
            "UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 99,)
        )
    conn.close()

    conn = connect(db)
    with pytest.raises(RuntimeError, match="Unsupported schema version"):
        init_schema(conn)
    conn.close()


def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for *table*."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _get_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def _get_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_v3_fresh_db_has_recipients_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.db")
    init_schema(conn)
    assert "recipients" in _get_tables(conn)


def test_v3_fresh_db_has_nudge_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.db")
    init_schema(conn)
    cols = _get_columns(conn, "messages")
    assert "nudge_count" in cols
    assert "next_nudge_at" in cols
    assert "nudge_tg_message_id" in cols
    assert "render_dirty" in cols


def test_v3_fresh_db_has_new_indexes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.db")
    init_schema(conn)
    indexes = _get_indexes(conn)
    assert "messages_nudge_due" in indexes
    assert "messages_render_dirty" in indexes


def test_v3_version_stamp(tmp_path: Path) -> None:
    from relay_server.db import SCHEMA_VERSION
    conn = connect(tmp_path / "fresh.db")
    init_schema(conn)
    assert get_schema_version(conn) == 3
    assert SCHEMA_VERSION == 3


def _build_v2_db(path: Path) -> sqlite3.Connection:
    """Create a v2 database (no nudge columns, no recipients) with a message row."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (2);

        CREATE TABLE installations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            telegram_chat_id INTEGER,
            bound_user_id INTEGER,
            created_at TIMESTAMP NOT NULL,
            last_seen_at TIMESTAMP,
            revoked_at TIMESTAMP
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id INTEGER NOT NULL REFERENCES installations(id),
            telegram_chat_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            answer_json TEXT,
            created_at TIMESTAMP NOT NULL,
            answered_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        );

        CREATE INDEX IF NOT EXISTS messages_state_expiry
            ON messages(state, expires_at);

        CREATE TABLE binding_codes (
            code TEXT PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installations(id),
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP,
            bound_chat_id INTEGER,
            bound_user_id INTEGER
        );

        CREATE TABLE idempotency_keys (
            key TEXT NOT NULL,
            installation_id INTEGER NOT NULL,
            request_hash TEXT,
            response_json TEXT,
            created_at TIMESTAMP NOT NULL,
            PRIMARY KEY (installation_id, key)
        );

        INSERT INTO installations(label, token_hash, telegram_chat_id, bound_user_id, created_at)
            VALUES ('test', 'hash1', 42, 7, datetime('now'));

        INSERT INTO messages(installation_id, telegram_chat_id, telegram_message_id,
            kind, payload_json, state, created_at, expires_at)
            VALUES (1, 42, 1000, 'question', '{}', 'open', datetime('now'), datetime('now', '+1 hour'));
    """)
    return conn


def test_v2_migrates_to_v3_schema(tmp_path: Path) -> None:
    """A v2 database migrates to v3 with all new columns and tables present."""
    db_path = tmp_path / "v2.db"
    conn = _build_v2_db(db_path)
    conn.close()

    conn = connect(db_path)
    init_schema(conn)

    assert get_schema_version(conn) == 3
    assert "recipients" in _get_tables(conn)
    cols = _get_columns(conn, "messages")
    assert "nudge_count" in cols
    assert "next_nudge_at" in cols
    assert "nudge_tg_message_id" in cols
    assert "render_dirty" in cols
    indexes = _get_indexes(conn)
    assert "messages_nudge_due" in indexes
    assert "messages_render_dirty" in indexes


def test_v2_migrates_message_rows_intact(tmp_path: Path) -> None:
    """Existing message rows survive the v2→v3 migration with sane defaults."""
    db_path = tmp_path / "v2_rows.db"
    conn = _build_v2_db(db_path)
    conn.close()

    conn = connect(db_path)
    init_schema(conn)

    row = conn.execute(
        "SELECT nudge_count, next_nudge_at, nudge_tg_message_id, render_dirty"
        " FROM messages WHERE id = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == 0, "nudge_count should default to 0"
    assert row[1] is None, "next_nudge_at should default to NULL"
    assert row[2] is None, "nudge_tg_message_id should default to NULL"
    assert row[3] == 0, "render_dirty should default to 0"


def test_fresh_and_migrated_schemas_identical(tmp_path: Path) -> None:
    """A fresh v3 DB and a migrated v2→v3 DB must have identical schemas."""
    fresh_path = tmp_path / "fresh.db"
    conn_fresh = connect(fresh_path)
    init_schema(conn_fresh)

    v2_path = tmp_path / "v2.db"
    conn_v2 = _build_v2_db(v2_path)
    conn_v2.close()
    conn_migrated = connect(v2_path)
    init_schema(conn_migrated)

    # Compare columns for each table.
    for table in ("messages", "recipients", "installations", "binding_codes", "idempotency_keys"):
        fresh_cols = _get_columns(conn_fresh, table)
        mig_cols = _get_columns(conn_migrated, table)
        assert fresh_cols == mig_cols, (
            f"Column mismatch for table '{table}': fresh={fresh_cols}, migrated={mig_cols}"
        )

    # Compare index names (excluding auto-generated sqlite_ indexes).
    fresh_idx = {i for i in _get_indexes(conn_fresh) if not i.startswith("sqlite_")}
    mig_idx = {i for i in _get_indexes(conn_migrated) if not i.startswith("sqlite_")}
    assert fresh_idx == mig_idx, f"Index mismatch: fresh={fresh_idx}, migrated={mig_idx}"


def test_admin_rotate_warns_when_un_revoking(tmp_path: Path) -> None:
    """Rotating a revoked installation should print a stderr warning."""
    db = str(tmp_path / "warn.db")
    # click < 8.2 mixes stderr into stdout unless asked not to; 8.2 removed the
    # parameter and captures stderr separately by default.
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:
        runner = CliRunner()

    r = runner.invoke(cli, ["--db", db, "issue", "--label", "x"])
    assert r.exit_code == 0
    inst_id = sqlite3.connect(db).execute(
        "SELECT id FROM installations"
    ).fetchone()[0]

    r = runner.invoke(cli, ["--db", db, "revoke", "--id", str(inst_id)])
    assert r.exit_code == 0

    r = runner.invoke(cli, ["--db", db, "rotate", "--id", str(inst_id)])
    assert r.exit_code == 0
    assert "was revoked" in r.stderr


def test_recipients_list_round_trips_written_data(tmp_path: Path) -> None:
    """relay-admin recipients set-* then list shows the written data (19-06)."""
    db = str(tmp_path / "recip.db")
    runner = CliRunner()

    # Set timezone
    r = runner.invoke(
        cli, ["--db", db, "recipients", "set-tz", "42", "Europe/Berlin"]
    )
    assert r.exit_code == 0, r.output

    # Set availability hours
    r = runner.invoke(
        cli, ["--db", db, "recipients", "set-hours", "42", "mon-fri 09:00-17:00"]
    )
    assert r.exit_code == 0, r.output

    # Enable nudges
    r = runner.invoke(
        cli, ["--db", db, "recipients", "set-nudge", "42", "on"]
    )
    assert r.exit_code == 0, r.output

    # List — the row must reflect everything written above.
    r = runner.invoke(cli, ["--db", db, "recipients", "list"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "chat_id=42" in out
    assert "Europe/Berlin" in out
    assert "mon-fri 09:00-17:00" in out
    assert "nudge=on" in out


def test_recipients_list_empty(tmp_path: Path) -> None:
    """relay-admin recipients list on an empty DB prints the no-data message."""
    db = str(tmp_path / "empty.db")
    runner = CliRunner()
    r = runner.invoke(cli, ["--db", db, "recipients", "list"])
    assert r.exit_code == 0, r.output
    assert "no recipient rows" in r.output
