# Architecture

This repository wires **Claude Code** to **Telegram** so that permission
prompts, questions, and idle notifications can be handled from a phone. It has
two cooperating halves:

1. **Client-side hooks** (`.claude/hooks/`) — per-invocation Python scripts that
   Claude Code runs on hook events. They talk HTTP to the relay.
2. **The relay server** (`relay-server/`) — a central HTTPS service that owns the
   Telegram bot token, receives Telegram callbacks via webhook, and routes
   answers back to whichever installation is waiting.

A separate, unrelated subsystem (`ai-notification-extension/`) is a legacy
GNOME notification experiment and is **not** part of the Telegram flow.

---

## Why a central relay (not a per-device bot poller)

Telegram allows only one consumer of `getUpdates` per bot token, so multiple
machines polling the same bot raced — a button tap could land on the wrong
device. The relay fixes this: the **server** owns the token and uses a
**webhook**, and each device authenticates with its own **installation token**.
Answers are routed back to the exact installation that created the message.
The old per-device `telegram_daemon.py` poller has been **deleted**; there is no
long-running client-side daemon anymore. Each hook invocation long-polls the
relay HTTP API itself.

---

## Repository layout

```
.claude/
  hooks/
    pretool_hook.py              PreToolUse(Bash): allow/ask/deny against allowlist
    bash_command_parser.py       compound-command splitter (|, &&, ||, ;, …)
    settings_loader.py           reads permissions.allow/deny from settings.json
    permission_request_hook.py   PermissionRequest: send to relay, long-poll, decide
    telegram_permission_router.py relay transport + message formatting + answer→decision
    permission_state_store.py    local JSON state store; cross-hook race coordination
    posttool_hook.py             PostToolUse: cancel relay msg when terminal resolved
    notification_hook.py         Notification(idle_prompt): forward last msg to Telegram
  settings.json                  permissions allow/deny (+ statusline); hooks merged in by installer
install-claude-config.sh         merges permissions + wires hooks into global settings.json
shell/
  profiles.example.toml          shipped template for ~/.claude/profiles.toml
  amux-spawn.bash                shell integration: auto-generates aliases from profiles
  amux-spawn-completion.bash     bash completion for amux-spawn + profile names
relay-server/
  relay_server/
    app.py                       FastAPI app: HTTP API + Telegram webhook + update dispatch
    client.py                    synchronous RelayClient used by the hooks
    telegram_backend.py          Bot API calls (HTML parse mode); Fake backend for tests
    models.py                    Pydantic request/response models; MessageKind/State literals
    db.py                        SQLite (WAL) schema + connection helpers
    waiters.py                   in-process long-poll waiter registry, keyed by message_id
    reaper.py                    background task: expire stale messages, purge idem keys
    binding_codes.py             BIND-XXXX-XXXX code generation/validation
    callback_data.py             pack (message_id, option_idx) into Telegram's 64-byte cap
    tokens.py                    installation token generation + hashing
    config.py                    server config (TOML + env precedence)
    client_cli.py / admin_cli.py relay-client / relay-admin CLIs
tasks/                           design/spec docs, one per feature increment
tests/                           unit + integration tests (run_all_tests.py)
```

---

## Configuration & identity

- **Client config:** `~/.config/claude-tg-relay/config.toml` with `server_url`
  and `installation_token`. Loaded by `RelayClient.from_config()`.
- **Server config:** `/etc/relay/config.toml` (or `RELAY_CONFIG`), keys
  `bot_token`, `webhook_secret`, `public_url`, `db_path`, … (env vars win).
- **Installation = device.** Each machine has its own installation token. Many
  installations can bind to the **same Telegram chat** (the user does this to run
  several machines from one chat).
- **Binding/pairing:** `relay-client bind` calls `POST /v1/bindings/request`,
  which returns a `BIND-XXXX-XXXX` code. The user sends that code to the bot; the
  webhook handler attaches the chat to the installation. Thereafter the relay
  knows `installation ↔ telegram_chat_id ↔ bound_user_id`.

---

## Client-side hook events

Hooks are wired into the **global** `~/.claude/settings.json` by
`install-claude-config.sh` (never in project settings, to avoid double-firing):

| Event | Matcher | Script | Role |
|-------|---------|--------|------|
| `PreToolUse` | `Bash` | `pretool_hook.py` | Split compound command, check each sub-command against `permissions.allow/deny`. All allowed → allow; otherwise let Claude Code surface a `PermissionRequest`. |
| `PermissionRequest` | `*` | `permission_request_hook.py` | Send the request to Telegram via the relay, long-poll for the answer, map it to an allow/deny/stop/whitelist/reply decision. Also handles `AskUserQuestion`. timeout 43200s. |
| `PostToolUse` | `*` | `posttool_hook.py` | If the request was resolved in the terminal instead, cancel the relay message (strip buttons) so the Telegram prompt goes dead. |
| `Notification` | `idle_prompt` | `notification_hook.py` | When the session goes idle, forward the agent's **last message** to Telegram as a notification (see below). |

### permission_state_store.py — cross-hook coordination

Hook invocations are separate processes, so they coordinate through a local
JSON state store keyed by `request_id`. It records state
(`pending`/`allow`/`deny`/`stop`/`whitelist`/`reply`/`resolved_terminal`), the
chosen decision, and the relay `telegram_message_id`. This is how the
`PermissionRequest` long-poll and the `PostToolUse` terminal-resolution signal
race each other: whichever resolves first wins, and the loser is cancelled.

---

## The relay HTTP API

`RelayClient` (sync, `httpx`) is the only thing the hooks use. Endpoints:

| Method & path | Purpose |
|---------------|---------|
| `GET /health` | liveness |
| `GET /v1/installations/me` | who am I / is the chat bound |
| `POST /v1/bindings/request` | start pairing → returns `BIND-XXXX-XXXX` |
| `GET /v1/bindings/{code}` | poll binding status |
| `POST /v1/messages` | send a message (`kind`, `text`, `keyboard`, `reply_required`, `ttl_sec`, `group_id`/`group_total`); idempotent via `Idempotency-Key` |
| `PATCH /v1/messages/{id}` | edit text |
| `DELETE /v1/messages/{id}` | delete |
| `POST /v1/messages/{id}/cancel` | strip keyboard + mark cancelled |
| `GET /v1/messages/{id}/answer?wait=N` | **long-poll** for the answer (parks on a waiter up to `N`s; 204 = keep polling) |
| `POST /telegram/webhook/{secret}` | Telegram → relay update delivery |

`MessageKind` is `question | permission | notification`. `reply_required`
controls whether Telegram shows a force-reply prompt. Answerability is per
`message_id`, **not** gated by kind — a `notification` with `reply_required` is
answerable like any other message.

### Idempotency

`POST /v1/messages` hashes the **actual body** under the `Idempotency-Key`.
Replaying the same key with an identical body **replays** the stored response
(no double-send); a different body returns **422**. (The idle-notification hook
exploits this: it derives its key from the composed message text so a re-fired
idle prompt for the same state can't double-post.)

### Waiters & long-poll

`waiters.py` is an in-process registry keyed by `message_id`. When a webhook
records an answer it calls `waiters.notify(message_id)`, waking any in-flight
`GET …/answer` long-poll. **Crucial consequence:** an answer only gets acted on
if *someone on the originating machine is long-polling that `message_id`*. There
is no background drain — the waiting process is the actor.

### reaper.py

A server background task expires stale messages (TTL) and purges old idempotency
keys on an interval.

---

## Inbound Telegram dispatch (`_handle_update`)

Two answer paths (`app.py`):

- **Button tap** → `callback_query`; `callback_data` decodes to
  `(message_id, option_idx)` (packed to fit Telegram's 64-byte limit), recorded
  as the answer.
- **Free-text reply** → if the Telegram message has `reply_to_message_id`, the
  relay looks up the **exact** message by `(chat_id, telegram_message_id)` and
  attributes the answer to it (and thus to its installation). This is what makes
  **threaded replies route to the correct machine** even when several
  installations share one chat. If the `reply_to` matches no *open* message the
  update is dropped — it is **not** downgraded to the fallback (doing so
  mis-threaded replies aimed at since-resolved messages).
- **Fallback** (no reply-to, bound user only): attribute to the single open
  message in the chat — but **only when exactly one target is open** (a question
  group counts as one target). With multiple open messages (e.g. several idle
  sessions) the reply is ambiguous, so the relay ignores it and nudges the user
  to use Telegram's Reply. Idle notifications are therefore sent **without**
  force-reply: a chat-wide force-reply auto-targets the newest notification and
  would mis-thread a reply onto the wrong session.

---

## End-to-end flows

### Permission request

```
Claude wants to run a tool
        │  PreToolUse(Bash): pretool_hook → allow? ──► yes ──► runs, done
        │                                   └─ no/other tool
        ▼
PermissionRequest: permission_request_hook
   create state-store request ──► telegram_permission_router.send_permission_message
        │                                   │ POST /v1/messages (buttons: Allow/Deny/Stop/Whitelist)
        │                                   ▼
        │                            relay → Telegram
        │  long-poll GET /answer  ◄──────────────────── user taps button (webhook → waiter)
        │  (races PostToolUse "resolved_terminal" in the state store)
        ▼
   map answer → decision (allow / deny / stop / whitelist / reply) ──► hook output
```

### AskUserQuestion

One child request per question, tied by a shared `group_id` so the relay keeps
all sibling keyboards live until every question is answered. Buttons carry
`qa<N>` values; free-text replies are accepted too. Answers are assembled into
`updatedInput.answers`. Falls back to the native terminal UI if Telegram is
unreachable or the user answers in the terminal.

### Idle notification (current)

```
Session goes idle ──► Notification(idle_prompt): notification_hook
   suppressed while async background agents still run
   extract last MAIN-agent text from the transcript (skip sidechain/tool-only/thinking)
   HTML-escape + tail-truncate to ~3800 chars
   send_idle_notification → POST /v1/messages (kind=notification, no buttons)
   → relay → Telegram   (fire-and-forget; no reply consumed today)
```

---

## Host environment: amux

Sessions are commonly launched under **amux** (`amux.io`,
github.com/mixpeek/amux), a phone-friendly Claude Code session manager that wraps
each session in **tmux**. Relevant facts:

- amux starts each session as a tmux session named **`amux-<CC_NAME>`** with the
  window named `<CC_NAME>`, e.g. `tmux new-session -d -s amux-hyppie-flow …
  claude …`. From inside a session, `tmux display-message -p '#{session_name}'`
  yields `amux-<name>`; stripping `amux-` gives the amux session name.
- Per-session config lives in `~/.amux/sessions/<name>.env` (`CC_NAME`,
  `CC_DIR`, `CC_FLAGS`). **These are not exported into the session environment**,
  and a session's working directory need not equal its registered `CC_DIR`.
- `CLAUDE_CODE_SESSION_ID` *is* present in the session env (set by Claude Code).
- amux exposes a CLI (`amux send <name> <text>` injects text+Enter via
  `send-keys`; `amux peek <name> [lines]` reads pane output) and a REST API at
  `$AMUX_URL` (default `:8822`) backed by `amux-server.py`, auth token at
  `~/.amux/auth_token`.

This is the substrate the **reply-from-Telegram** feature (task 09) builds on:
amux's `send` is the only available way to inject a remote reply as a new user
turn into a running interactive session.

---

## Model profiles (epic 13)

Model/provider configurations are stored as structured TOML at
`~/.claude/profiles.toml`. Each profile defines env vars for a specific
model backend (Anthropic native, GLM, DeepSeek, Kimi, local, etc.).

**File structure — three sections:**

```toml
[vars]              # interpolation-only; ${name} refs, never exported
[all-profiles]      # env vars applied to EVERY profile (overridden per-profile)
[profile.<name>]    # per-profile env vars; <name> becomes the shell alias
```

Merge order: `[all-profiles]` → `[profile.X]` (profile wins on collision).
Both layers support `${var}` interpolation from `[vars]`.

**Shell aliases** are auto-generated at source-time by `shell/amux-spawn.bash`
(calls `emit_shell_functions()` in `amux_spawn_lib.py`). Each alias checks for
`amux-spawn` on PATH: if present, routes through `amux-spawn spawn --profile
<name>` (tracked session, Telegram); if absent, exports env in a subshell and
exec's `claude` directly (same model, no tracking).

**Profile loader** lives in `amux_spawn_lib.py` — pure functions, no side
effects. `resolve_profile(name)` returns the merged env dict; `amux-spawn`
exports those vars into its own process before the create-detached-under-lock
flow (env reaches child via tmux `update-environment`, no `ps` leak).

**Install:** `install-claude-config.sh` copies `shell/profiles.example.toml`
to `~/.claude/profiles.toml` if the file does not exist (never overwrites).

---

## Testing

`tests/run_all_tests.py` aggregates unit + integration suites (decision mapper,
state store, whitelist, notification hook, pretool, permission-request
integration). The relay server has its own `relay-server/tests/`. Tests use
`FakeTelegramBackend` and patch `RelayClient`, so no network or real bot is
touched.
