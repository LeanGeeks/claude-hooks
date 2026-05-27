"""relay-client — CLI for interacting with the relay server.

Phase 3 subcommands:
- ``relay-client config init`` — write ~/.config/claude-tg-relay/config.toml
- ``relay-client whoami``       — print installation info
- ``relay-client bind``         — request a binding code and poll until bound

Config file: ``~/.config/claude-tg-relay/config.toml``
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

try:
    import tomli_w  # optional; only needed for config init
except ImportError:  # pragma: no cover
    tomli_w = None  # type: ignore[assignment]

_CONFIG_PATH = Path.home() / ".config" / "claude-tg-relay" / "config.toml"

# Polling backoff for ``bind``: seconds per attempt, capped at 5 s.
_POLL_INITIAL = 1
_POLL_CAP = 5


def _load_config(config_path: Path | None = None) -> dict[str, str]:
    path = config_path or _CONFIG_PATH
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        click.echo(
            f"Config file not found: {path}\n"
            "Run `relay-client config init --server-url URL --token TOKEN` first.",
            err=True,
        )
        sys.exit(1)


def _make_client(cfg: dict[str, str]) -> httpx.Client:
    server_url = cfg.get("server_url", "").rstrip("/")
    token = cfg.get("installation_token", "")
    if not server_url or not token:
        click.echo(
            "Config missing server_url or installation_token.", err=True
        )
        sys.exit(1)
    return httpx.Client(
        base_url=server_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


@click.group()
def cli() -> None:
    """relay-client — bind and manage Telegram relay installations."""


# ---- config sub-group -------------------------------------------------------


@cli.group()
def config() -> None:
    """Manage local client configuration."""


@config.command("init")
@click.option("--server-url", required=True, help="Relay server base URL.")
@click.option("--token", required=True, help="Installation token (rly_...).")
@click.option(
    "--config-path",
    default=None,
    type=click.Path(),
    help=f"Config file path (default: {_CONFIG_PATH}).",
)
def config_init(server_url: str, token: str, config_path: str | None) -> None:
    """Write the config file with the given credentials."""
    if tomli_w is None:
        # Fallback: write manually in valid TOML.
        content = (
            f'server_url = "{server_url}"\n'
            f'installation_token = "{token}"\n'
        )
        path = Path(config_path) if config_path else _CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    else:
        data = {"server_url": server_url, "installation_token": token}
        path = Path(config_path) if config_path else _CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump(data, f)
    click.echo(f"Config written to {path}.")


# ---- whoami -----------------------------------------------------------------


@cli.command()
@click.option(
    "--config-path",
    default=None,
    type=click.Path(),
    help="Config file path.",
)
def whoami(config_path: str | None) -> None:
    """Print information about the current installation."""
    cfg = _load_config(Path(config_path) if config_path else None)
    with _make_client(cfg) as client:
        r = client.get("/v1/installations/me")
        if r.status_code != 200:
            click.echo(f"Error: {r.status_code} {r.text}", err=True)
            sys.exit(1)
        data: dict[str, Any] = r.json()
    click.echo(f"id            : {data.get('id')}")
    click.echo(f"label         : {data.get('label')}")
    click.echo(f"chat_bound    : {data.get('chat_bound')}")
    click.echo(f"last_seen_at  : {data.get('last_seen_at')}")


# ---- bind -------------------------------------------------------------------


@cli.command()
@click.option(
    "--config-path",
    default=None,
    type=click.Path(),
    help="Config file path.",
)
def bind(config_path: str | None) -> None:
    """Request a binding code and wait for the user to /bind in Telegram."""
    cfg = _load_config(Path(config_path) if config_path else None)

    import uuid

    idem_key = str(uuid.uuid4())

    with _make_client(cfg) as client:
        # Request a fresh binding code.
        r = client.post(
            "/v1/bindings/request",
            headers={"Idempotency-Key": idem_key},
        )
        if r.status_code != 200:
            click.echo(f"Error requesting code: {r.status_code} {r.text}", err=True)
            sys.exit(1)
        data = r.json()
        code: str = data["code"]
        expires_at: str = data["expires_at"]

        click.echo(
            "Send this message to the bot in the chat you want notifications in:\n"
            f"  /bind {code}\n"
            f"(waiting up to 10 min, code expires {expires_at}...)"
        )

        # Poll until consumed or expired.
        # 5xx / connection errors are transient — retry up to 3 times with
        # exponential backoff before giving up.  4xx are terminal.
        _MAX_TRANSIENT_RETRIES = 3
        delay = _POLL_INITIAL
        while True:
            time.sleep(delay)
            delay = min(delay + 1, _POLL_CAP)

            transient_attempts = 0
            transient_delay = 2
            while True:
                try:
                    poll = client.get(f"/v1/bindings/{code}")
                    status = poll.status_code
                except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
                    # Treat connection-level errors as transient.
                    transient_attempts += 1
                    if transient_attempts > _MAX_TRANSIENT_RETRIES:
                        click.echo(
                            f"Connection error after {_MAX_TRANSIENT_RETRIES} retries:"
                            f" {exc}",
                            err=True,
                        )
                        sys.exit(1)
                    time.sleep(transient_delay)
                    transient_delay = min(transient_delay * 2, 16)
                    continue

                if 500 <= status < 600:
                    transient_attempts += 1
                    if transient_attempts > _MAX_TRANSIENT_RETRIES:
                        click.echo(
                            f"Server error {status} after"
                            f" {_MAX_TRANSIENT_RETRIES} retries: {poll.text}",
                            err=True,
                        )
                        sys.exit(1)
                    time.sleep(transient_delay)
                    transient_delay = min(transient_delay * 2, 16)
                    continue

                # Non-5xx response — handle below.
                break

            if status == 410:
                click.echo(
                    "✗ Code expired before /bind was received.", err=True
                )
                sys.exit(1)
            if status == 404:
                click.echo(
                    "✗ Binding code not found (expired or invalid).", err=True
                )
                sys.exit(1)
            if status != 200:
                click.echo(
                    f"Unexpected response: {status} {poll.text}", err=True
                )
                sys.exit(1)

            state_data = poll.json()
            if state_data.get("state") == "bound":
                chat_id = state_data.get("chat_id")
                user_id = state_data.get("telegram_user_id")
                click.echo(
                    f"✓ Bound to chat_id={chat_id} (user telegram_id={user_id})"
                )
                return


if __name__ == "__main__":  # pragma: no cover
    cli()
