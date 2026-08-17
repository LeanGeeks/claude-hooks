"""`relay-admin` — local Click CLI that talks directly to the SQLite DB.

Subcommands: ``issue``, ``list``, ``revoke``, ``rotate``, ``recipients``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import click

from .availability import (
    describe_active_status,
    format_nudge_schedule,
    format_windows,
    parse_nudge_schedule,
    parse_tz,
    parse_windows,
)
from .config import load_config
from .db import connect, init_schema, load_recipient
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


@cli.group("recipients")
def recipients_group() -> None:
    """Inspect and manage per-chat availability / nudge configuration."""


@recipients_group.command("list")
@click.pass_context
def recipients_list(ctx: click.Context) -> None:
    """List all recipient rows (chats with configured availability or nudges)."""
    conn = _open_db(ctx.obj["db_path"])
    rows = conn.execute(
        "SELECT telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule,"
        " updated_at FROM recipients ORDER BY telegram_chat_id"
    ).fetchall()
    if not rows:
        click.echo("(no recipient rows)")
        return
    cfg = load_config()
    now = datetime.now(timezone.utc)
    for r in rows:
        windows = parse_windows(r["windows_json"]) if r["windows_json"] else None
        status = describe_active_status(now, r["tz"], windows)
        sched = r["nudge_schedule"] or f"{cfg.nudge_default_schedule} (default)"
        nudge = f"on ({sched})" if r["nudge_enabled"] else "off"
        click.echo(
            f"chat_id={r['telegram_chat_id']}"
            f"  tz={r['tz'] or '(none)'}"
            f"  hours={r['windows_json'] or 'always'}"
            f"  status={status}"
            f"  nudge={nudge}"
            f"  updated={r['updated_at']}"
        )


@recipients_group.command("set-tz")
@click.argument("chat_id", type=int)
@click.argument("tz_name")
@click.pass_context
def recipients_set_tz(ctx: click.Context, chat_id: int, tz_name: str) -> None:
    """Set the IANA timezone for CHAT_ID."""
    conn = _open_db(ctx.obj["db_path"])
    validated = parse_tz(tz_name)
    if validated is None:
        raise click.ClickException(
            f"Unknown timezone {tz_name!r}. Use an IANA name like Europe/Berlin."
        )
    now_iso = _utcnow_iso()
    with conn:
        conn.execute(
            "INSERT INTO recipients"
            " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, ?, NULL, 0, NULL, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "   tz = excluded.tz,"
            "   updated_at = excluded.updated_at",
            (chat_id, validated, now_iso),
        )
    click.echo(f"Timezone for chat {chat_id} set to {validated}.")


@recipients_group.command("clear-tz")
@click.argument("chat_id", type=int)
@click.pass_context
def recipients_clear_tz(ctx: click.Context, chat_id: int) -> None:
    """Clear the timezone for CHAT_ID (UTC assumed)."""
    conn = _open_db(ctx.obj["db_path"])
    now_iso = _utcnow_iso()
    with conn:
        conn.execute(
            "INSERT INTO recipients"
            " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, NULL, NULL, 0, NULL, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "   tz = NULL,"
            "   updated_at = excluded.updated_at",
            (chat_id, now_iso),
        )
    click.echo(f"Timezone for chat {chat_id} cleared.")


@recipients_group.command("set-hours")
@click.argument("chat_id", type=int)
@click.argument("spec")
@click.pass_context
def recipients_set_hours(ctx: click.Context, chat_id: int, spec: str) -> None:
    """Set availability hours for CHAT_ID (SPEC like 'mon-fri 09:00-19:00')."""
    conn = _open_db(ctx.obj["db_path"])
    try:
        windows = parse_windows(spec)
    except ValueError as exc:
        raise click.ClickException(f"Bad window spec: {exc}.") from exc
    if windows is None:
        raise click.ClickException(
            "Empty spec. Use clear-hours to clear, or provide a window spec."
        )
    canonical = format_windows(windows)
    now_iso = _utcnow_iso()
    with conn:
        conn.execute(
            "INSERT INTO recipients"
            " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, NULL, ?, 0, NULL, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "   windows_json = excluded.windows_json,"
            "   updated_at = excluded.updated_at",
            (chat_id, canonical, now_iso),
        )
    click.echo(f"Hours for chat {chat_id} set to: {canonical}.")


@recipients_group.command("clear-hours")
@click.argument("chat_id", type=int)
@click.pass_context
def recipients_clear_hours(ctx: click.Context, chat_id: int) -> None:
    """Clear availability hours for CHAT_ID (always available)."""
    conn = _open_db(ctx.obj["db_path"])
    now_iso = _utcnow_iso()
    with conn:
        conn.execute(
            "INSERT INTO recipients"
            " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, NULL, NULL, 0, NULL, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "   windows_json = NULL,"
            "   updated_at = excluded.updated_at",
            (chat_id, now_iso),
        )
    click.echo(f"Hours for chat {chat_id} cleared (always available).")


@recipients_group.command("set-nudge")
@click.argument("chat_id", type=int)
@click.argument("state", type=click.Choice(["on", "off"], case_sensitive=False))
@click.pass_context
def recipients_set_nudge(ctx: click.Context, chat_id: int, state: str) -> None:
    """Enable or disable nudges for CHAT_ID."""
    conn = _open_db(ctx.obj["db_path"])
    enabled = 1 if state.lower() == "on" else 0
    now_iso = _utcnow_iso()
    with conn:
        conn.execute(
            "INSERT INTO recipients"
            " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, NULL, NULL, ?, NULL, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "   nudge_enabled = excluded.nudge_enabled,"
            "   updated_at = excluded.updated_at",
            (chat_id, enabled, now_iso),
        )
    click.echo(f"Nudges for chat {chat_id} set to {state.lower()}.")


@recipients_group.command("set-nudge-schedule")
@click.argument("chat_id", type=int)
@click.argument("schedule")
@click.pass_context
def recipients_set_nudge_schedule(
    ctx: click.Context, chat_id: int, schedule: str
) -> None:
    """Set the nudge schedule for CHAT_ID (e.g. '15m,45m,3h')."""
    conn = _open_db(ctx.obj["db_path"])
    cfg = load_config()
    try:
        tds = parse_nudge_schedule(schedule, cfg.nudge_max)
    except ValueError as exc:
        raise click.ClickException(f"Bad nudge schedule: {exc}.") from exc
    canonical = format_nudge_schedule(tds)
    now_iso = _utcnow_iso()
    with conn:
        conn.execute(
            "INSERT INTO recipients"
            " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
            " VALUES (?, NULL, NULL, 0, ?, ?)"
            " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
            "   nudge_schedule = excluded.nudge_schedule,"
            "   updated_at = excluded.updated_at",
            (chat_id, canonical, now_iso),
        )
    click.echo(f"Nudge schedule for chat {chat_id} set to: {canonical}.")


if __name__ == "__main__":
    cli()
