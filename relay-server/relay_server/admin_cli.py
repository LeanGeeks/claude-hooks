"""`relay-admin` — local Click CLI that talks directly to the SQLite DB.

Subcommands: ``issue``, ``list``, ``revoke``, ``rotate``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import click

from .db import connect, init_schema
from .tokens import generate_token, hash_token

DEFAULT_DB = os.environ.get("RELAY_DB_PATH", "relay.db")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


@click.group()
@click.option(
    "--db",
    "db_path",
    default=DEFAULT_DB,
    show_default=True,
    help="Path to the relay SQLite database.",
)
@click.pass_context
def cli(ctx: click.Context, db_path: str) -> None:
    """Administer installation tokens for the relay server."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@cli.command()
@click.option("--label", required=True, help="Human label for the installation.")
@click.pass_context
def issue(ctx: click.Context, label: str) -> None:
    """Issue a new installation token. Prints the plaintext once."""
    conn = _open_db(ctx.obj["db_path"])
    token = generate_token()
    th = hash_token(token)
    with conn:
        cur = conn.execute(
            "INSERT INTO installations(label, token_hash, created_at)"
            " VALUES (?, ?, ?)",
            (label, th, _utcnow_iso()),
        )
        new_id = cur.lastrowid
    click.echo(f"Installation id: {new_id}")
    click.echo(f"Label:           {label}")
    click.echo(f"Token:           {token}")
    click.echo("(Store this safely; it is not recoverable.)")


@cli.command("list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """List all installations."""
    conn = _open_db(ctx.obj["db_path"])
    rows = conn.execute(
        "SELECT id, label, telegram_chat_id, last_seen_at, revoked_at"
        " FROM installations ORDER BY id"
    ).fetchall()
    if not rows:
        click.echo("(no installations)")
        return
    click.echo(f"{'id':>4}  {'label':<24}  {'bound':<5}  {'revoked':<7}  last_seen")
    for r in rows:
        bound = "yes" if r["telegram_chat_id"] is not None else "no"
        revoked = "yes" if r["revoked_at"] is not None else "no"
        last_seen = r["last_seen_at"] or "never"
        click.echo(
            f"{r['id']:>4}  {r['label']:<24}  {bound:<5}  {revoked:<7}  {last_seen}"
        )


@cli.command()
@click.option("--id", "installation_id", required=True, type=int)
@click.pass_context
def revoke(ctx: click.Context, installation_id: int) -> None:
    """Revoke an installation token (immediate)."""
    conn = _open_db(ctx.obj["db_path"])
    with conn:
        cur = conn.execute(
            "UPDATE installations SET revoked_at = ?"
            " WHERE id = ? AND revoked_at IS NULL",
            (_utcnow_iso(), installation_id),
        )
    if cur.rowcount == 0:
        raise click.ClickException(
            f"No active installation with id={installation_id}."
        )
    click.echo(f"Revoked installation id={installation_id}.")


@cli.command()
@click.option("--id", "installation_id", required=True, type=int)
@click.pass_context
def rotate(ctx: click.Context, installation_id: int) -> None:
    """Issue a fresh token for an existing installation. Old token dies."""
    conn = _open_db(ctx.obj["db_path"])
    row = conn.execute(
        "SELECT id, label, revoked_at FROM installations WHERE id = ?",
        (installation_id,),
    ).fetchone()
    if row is None:
        raise click.ClickException(f"No installation with id={installation_id}.")
    was_revoked = row["revoked_at"] is not None
    new_token = generate_token()
    with conn:
        conn.execute(
            "UPDATE installations SET token_hash = ?, revoked_at = NULL WHERE id = ?",
            (hash_token(new_token), installation_id),
        )
    if was_revoked:
        click.echo(
            f"warning: installation id={installation_id} was revoked; "
            "rotate has cleared the revocation.",
            err=True,
        )
    click.echo(f"Rotated installation id={installation_id} ({row['label']}).")
    click.echo(f"New token: {new_token}")


if __name__ == "__main__":
    cli()
