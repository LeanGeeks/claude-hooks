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

### Running the tests

The server's own suite needs `fastapi`, `pydantic>=2.5`, `uvicorn` and
`pytest-asyncio`. Debian/Ubuntu ship versions too old to satisfy
`requirements.txt` (pydantic 1.10, fastapi 0.101), and system Python is
PEP 668 "externally-managed", so use a local venv:

```bash
cd relay-server
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -e .
./.venv/bin/python -m pytest tests -q
```

`.venv/` is gitignored. This is only for the relay server — the hooks suite at
the repository root runs on system Python with `python3 tests/run_all_tests.py`
and deliberately has no third-party dependencies beyond `httpx`.

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

## Deployment with Docker Compose + caddy-docker-proxy

This is an alternative to the bare-metal systemd + Caddy setup above. It is
the recommended path when you already run
[caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy) on
the host (it auto-generates Caddy configuration from Docker labels, handling
TLS automatically via Let's Encrypt).

### One-time host setup

```bash
# Create the shared Docker network that caddy-docker-proxy listens on.
# The relay defaults to `front-proxy`; override via CADDY_NETWORK in .env.
# Skip this if the network already exists.
docker network create front-proxy
```

Ensure caddy-docker-proxy is running in the same network. A minimal
compose snippet for that service:

```yaml
services:
  caddy:
    image: lucaslorentz/caddy-docker-proxy:ci-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - caddy-data:/data
    networks:
      - front-proxy
volumes:
  caddy-data:
networks:
  front-proxy:
    external: true
```

### Deploy the relay

```bash
cd relay-server/

# 1. Create your .env from the template.
cp .env.example .env
$EDITOR .env   # fill in RELAY_BOT_TOKEN, RELAY_WEBHOOK_SECRET,
               # RELAY_PUBLIC_URL, RELAY_PUBLIC_HOST

# 2. Start (builds the image if needed).
docker compose up -d --build
```

`RELAY_PUBLIC_HOST` (e.g. `relay.example.com`) must resolve to the Docker
host's public IP. caddy-docker-proxy reads the `caddy=` label on the relay
container and automatically provisions a TLS certificate via ACME / Let's
Encrypt.

There is **no `ports:` mapping** in the compose file — Caddy reaches the
container on port 8080 through the shared external Docker network
(default name `front-proxy`, override with `CADDY_NETWORK` in `.env`).

### Logs and status

```bash
docker compose logs -f relay
docker compose ps
```

### Upgrades

```bash
git pull
docker compose up -d --build
```

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

### Admin CLI inside a Docker Compose container

When running via Docker Compose, exec into the container and point the CLI at
the volume mount:

```bash
# Issue a token for a new device.
docker compose exec relay python -m relay_server.admin_cli \
    --db /var/lib/relay/relay.db issue --label anton-laptop
# Prints: Installation id: 1   Token: rly_8f3a2b...  (store safely)

# List all installations.
docker compose exec relay python -m relay_server.admin_cli \
    --db /var/lib/relay/relay.db list

# Revoke a token.
docker compose exec relay python -m relay_server.admin_cli \
    --db /var/lib/relay/relay.db revoke --id 2

# Rotate a token.
docker compose exec relay python -m relay_server.admin_cli \
    --db /var/lib/relay/relay.db rotate --id 1
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

Device setup has two independent halves that use **different** Python
environments — get this right or notifications silently stay disabled:

1. **The client CLI** (`relay-client` — `config init` / `bind` / `whoami`),
   which you run by hand. Install it in isolation with **pipx** (recommended).
2. **The Claude Code hook** (`telegram_permission_router.py`, imported by the
   permission/notification hooks), which Claude Code runs under **system
   `python3`**. It needs `relay_server` + `httpx` importable *there* — pipx's
   isolated venv is invisible to it. See "Hook runtime dependencies" below.

### Install the CLI with pipx (recommended)

```bash
# Isolated install; exposes the `relay-client` (and `relay-admin`) entry points.
pipx install /path/to/relay-server/

# `config init` writes TOML, which needs tomli_w — not a declared dependency,
# so inject it into the same pipx venv.
pipx inject relay-server tomli_w
```

Why pipx over `pip install`: the package pulls server deps
(`fastapi`, `uvicorn`, …) even for client-only use, and modern distros are
PEP 668 "externally-managed" — a bare `pip install` into system Python is
blocked and, if forced, pollutes it. pipx keeps the CLI in its own venv.

> Prefer a plain venv? `python3 -m venv ~/.venvs/relay-client &&
> ~/.venvs/relay-client/bin/pip install /path/to/relay-server/ tomli_w`, then
> call `~/.venvs/relay-client/bin/relay-client …`. Either way, the hook still
> needs the system-Python deps below.

### Hook runtime dependencies (system `python3`)

The Claude Code hooks are invoked as `python3 …/hook.py` (see the `command`
entries in `settings.json`), so the hook's `import relay_server.client` runs in
system Python, **not** the pipx/venv environment. `install-claude-config.sh`
already drops a user-site `.pth` so `import relay_server` resolves to this
repo, but the client module also imports `httpx`. Install it (plus `tomli_w`
if you'll also run the CLI under system Python) into the same user site:

```bash
# --user keeps it in ~/.local; --break-system-packages satisfies PEP 668.
python3 -m pip install --user --break-system-packages httpx tomli_w
```

Skip this and the hook's import fails silently — the relay disables itself and
you get no Telegram prompts even though the CLI's `whoami` works fine.

### 1. Write the config

The admin sends the installation token out-of-band (Signal, password manager,
etc.). On the device:

```bash
relay-client config init \
    --server-url https://relay.example.com \
    --token rly_8f3a2b...
```

This writes `~/.config/claude-tg-relay/config.toml`. (Equivalent, if you
installed under system Python instead of pipx:
`python3 -m relay_server.client_cli config init …`.)

### 2. Bind a Telegram chat

```bash
relay-client bind
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
relay-client whoami
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
