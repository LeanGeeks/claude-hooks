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
