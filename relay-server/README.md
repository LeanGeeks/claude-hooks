# Claude Code Telegram Relay Server

A central HTTPS relay that sits between N Claude Code installations and the
Telegram Bot API. Each device authenticates with an installation token; the
server owns the bot token, receives callbacks via webhook, and routes answers
back to whichever device is waiting. This eliminates the multi-device
`getUpdates` race that caused button taps to land on the wrong machine.

---

## Server Deployment

### Prerequisites

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A public HTTPS domain (Caddy example below handles TLS automatically)

### Install

```bash
# From the repository root — installs relay_server package + entry points.
pip install -e relay-server/

# Or inside a virtualenv on the server box:
python3 -m venv /opt/relay-server/venv
/opt/relay-server/venv/bin/pip install -e /path/to/relay-server/
```

### Configuration

Settings are read from `/etc/relay/config.toml` (override path with
`RELAY_CONFIG` env var). Environment variables take precedence over the file.

| TOML key            | Env var                 | Required | Default               | Description                                         |
|---------------------|-------------------------|----------|-----------------------|-----------------------------------------------------|
| `bot_token`         | `RELAY_BOT_TOKEN`       | yes      | —                     | Telegram bot token from @BotFather                  |
| `webhook_secret`    | `RELAY_WEBHOOK_SECRET`  | yes      | —                     | Random string embedded in the webhook URL path      |
| `public_url`        | `RELAY_PUBLIC_URL`      | yes      | —                     | Public HTTPS base URL (no trailing slash)           |
| `db_path`           | `RELAY_DB_PATH`         | no       | `relay.db`            | Path to the SQLite database file                    |
| `listen`            | `RELAY_LISTEN`          | no       | `127.0.0.1:8080`      | Address:port for uvicorn                            |
| `reaper_interval`   | `RELAY_REAPER_INTERVAL` | no       | `30`                  | Seconds between expired-message cleanup ticks       |

Annotated config template: `deploy/config.toml.example`.

Generate the webhook secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Caddy (TLS termination + reverse proxy)

```caddy
relay.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Full snippet with logging: `deploy/Caddyfile.example`.

### systemd unit

```bash
cp deploy/relay.service.example /etc/systemd/system/relay.service
# Edit User=, WorkingDirectory=, and virtualenv path, then:
systemctl daemon-reload
systemctl enable --now relay
```

On first start the server calls `setWebhook` automatically. Check the logs to
confirm:

```bash
journalctl -u relay -f
```

Full unit template: `deploy/relay.service.example`.

---

## Admin Workflow

The `relay-admin` CLI runs on the server box and talks to SQLite directly
(no HTTP needed, SSH access is the auth layer).

```bash
# Issue a token for a new device.
python -m relay_server.admin_cli --db /var/lib/relay/relay.db issue --label anton-laptop
# Prints: Installation id: 1   Token: rly_8f3a2b...  (store safely — not recoverable)

# List all installations with binding status.
python -m relay_server.admin_cli --db /var/lib/relay/relay.db list

# Revoke a token (device immediately loses access).
python -m relay_server.admin_cli --db /var/lib/relay/relay.db revoke --id 2

# Rotate a token (generates a new one; old one is immediately invalid).
python -m relay_server.admin_cli --db /var/lib/relay/relay.db rotate --id 1
```

If the `relay-admin` entry point is on `PATH`:

```bash
relay-admin --db /var/lib/relay/relay.db issue --label workstation
```

### Database backups

The DB is a single SQLite file (WAL mode). Back it up with the server briefly
stopped or with the live-backup command:

```bash
# Zero-downtime backup via SQLite's online backup API:
sqlite3 /var/lib/relay/relay.db ".backup /var/lib/relay/relay.db.bak"

# Or stop the service first for a plain copy:
systemctl stop relay
cp /var/lib/relay/relay.db /var/backups/relay-$(date +%Y%m%d).db
systemctl start relay
```

---

## Device Setup

Each device needs the `relay-server` package installed (client-only use; no
server dependencies are pulled unless you run the server on the same machine):

```bash
pip install -e /path/to/relay-server/
```

### 1. Write the config

The admin sends the installation token out-of-band (Signal, password manager,
etc.). On the device:

```bash
python -m relay_server.client_cli config init \
    --server-url https://relay.example.com \
    --token rly_8f3a2b...
```

This writes `~/.config/claude-tg-relay/config.toml`.

### 2. Bind a Telegram chat

```bash
python -m relay_server.client_cli bind
# Prints: Send "/bind BIND-7H2K-9XQ4" to the bot in the chat you want notifications in.
# (waiting up to 10 min...)
```

Open Telegram, go to the desired chat (private with the bot, or a group that
includes it), and send exactly the `/bind BIND-...` message shown. The CLI
confirms once the server sees it:

```
Bound to chat "Anton (private)" (user @anton).
```

### 3. Verify

```bash
python -m relay_server.client_cli whoami
```

The Claude Code hooks (`telegram_permission_router.py`) read the config on
every invocation. No restart of Claude Code is needed after binding.

---

## Troubleshooting

**Hook output says "Telegram disabled"** — The config file is missing or
unreadable. Check:

```bash
cat ~/.config/claude-tg-relay/config.toml
curl https://relay.example.com/v1/installations/me \
    -H "Authorization: Bearer $(grep installation_token ~/.config/claude-tg-relay/config.toml | cut -d'"' -f2)"
```

Error details are written to `~/.claude/permission_telegram_errors.log`.

**`bind` polling times out** — The bot did not receive the `/bind` message.
Confirm the bot is in the chat and the message was sent verbatim. Re-run
`relay-client bind` to get a fresh code (old codes expire after 10 minutes).

**Bot stops sending messages; `403 Forbidden` in server logs** — The bot was
blocked or kicked from the bound chat. The server clears the binding
automatically. Re-run `bind` from the device:

```bash
python -m relay_server.client_cli bind
journalctl -u relay --since "1 hour ago"
```

**Server won't start / `setWebhook` fails** — Check `RELAY_PUBLIC_URL` is
reachable from the internet, TLS is valid, and the bot token is correct:

```bash
curl https://api.telegram.org/bot<token>/getMe
```
