# Task 08: Central Telegram Relay Server

## Goal

Replace the current per-device direct-to-Bot-API integration with a central relay server that owns the bot token and routes messages between N client installations and one Telegram bot. This eliminates the multi-device `getUpdates` race that causes button clicks and replies to be delivered to the wrong device.

---

## Background

Today every device runs its own `getUpdates` long-poll loop against the same bot token (`telegram_daemon.py:494`, `telegram_permission_router.py:835`). Telegram delivers each update to exactly one caller and the offset ack removes it from the queue. When device A sends a question and device B's poller grabs the callback first, B has no matching local state and A keeps waiting forever. The buttons "don't work sometimes."

Filesystem-based coordination was considered and rejected: not all devices share a sync layer, and Syncthing latency is too high for interactive UI.

---

## Architecture

```
device A ──┐                                            ┌── Telegram user
device B ──┼── HTTPS ──▶ relay server ──▶ Bot API ──▶ │
device C ──┘            (owns bot token)   ◀──webhook── └── (buttons / replies)
```

Three components:

1. **Server**: Python + FastAPI + SQLite (WAL), single process behind Caddy (TLS terminated upstream — out of scope here). Public VPS. Owns the bot token.
2. **Client library**: drop-in replacement for the send/edit/delete/await helpers in `telegram_permission_router.py`. No bot token on disk; only a server URL and an installation token.
3. **Bot**: same chat UX as today (inline keyboards, force-reply for free text). User shouldn't notice the swap.

Telegram delivers updates to the server via **webhook** (no polling — eliminates the original race inside the server too).

---

## Identity & authorization model

Two separate concerns, deliberately decoupled:

### "Is this device allowed to talk to the server?"
Answered by an admin-issued **installation token**.

- Server has a CLI (`relay-admin`) the human admin runs locally on the server box.
- `relay-admin issue --label "anton-laptop"` → prints a fresh installation token (opaque random string, stored hashed in the DB).
- Admin sends the token to the device owner out-of-band (Signal, password manager, etc.).
- Device owner pastes it into their local config.

No `/pair`-from-bot flow needed for token issuance. No public registration endpoint. The allowlist *is* the set of tokens the admin has issued.

### "Which Telegram chat do this device's notifications go to?"
Answered by a user-driven **chat binding** flow.

- Device user runs `relay-client bind` (CLI on the device, ships with the client lib).
- Client calls `POST /v1/bindings/request` (auth'd with installation token), receives a short code (e.g. `BIND-7H2K-9XQ4`, ~10 min TTL).
- CLI prints: `Send "/bind BIND-7H2K-9XQ4" to the bot in the chat you want notifications in.`
- User opens Telegram, opens the bot (or a group that includes it), sends the message.
- Server receives the `/bind` update via webhook, looks up the code, records the chat_id + the sending telegram_user_id, marks code consumed.
- CLI polls `GET /v1/bindings/{code}` until it returns 200 with the bound chat info, then exits.

Re-binding is allowed (overwrites the previous chat for that installation). Each installation is bound to exactly one chat.

---

## Data model (SQLite, WAL)

```sql
installations(
  id                INTEGER PRIMARY KEY,
  label             TEXT NOT NULL,
  token_hash        TEXT NOT NULL UNIQUE,    -- SHA-256 of installation token
  telegram_chat_id  INTEGER,                 -- null until bound
  bound_user_id    INTEGER,                  -- telegram_user_id that ran /bind
  created_at        TIMESTAMP NOT NULL,
  last_seen_at      TIMESTAMP,
  revoked_at        TIMESTAMP
)

messages(
  id                INTEGER PRIMARY KEY,     -- server-side id, used in callback_data
  installation_id   INTEGER NOT NULL REFERENCES installations(id),
  telegram_chat_id  INTEGER NOT NULL,
  telegram_message_id INTEGER NOT NULL,
  kind              TEXT NOT NULL,           -- 'question' | 'permission' | 'notification'
  payload_json      TEXT NOT NULL,           -- original request, for debugging
  state             TEXT NOT NULL,           -- 'open' | 'answered' | 'expired' | 'cancelled'
  answer_json       TEXT,                    -- null until answered
  created_at        TIMESTAMP NOT NULL,
  answered_at       TIMESTAMP,
  expires_at        TIMESTAMP NOT NULL
)
CREATE INDEX messages_state_expiry ON messages(state, expires_at);

binding_codes(
  code              TEXT PRIMARY KEY,
  installation_id   INTEGER NOT NULL REFERENCES installations(id),
  created_at        TIMESTAMP NOT NULL,
  expires_at        TIMESTAMP NOT NULL,
  consumed_at       TIMESTAMP,
  bound_chat_id     INTEGER,
  bound_user_id     INTEGER
)

idempotency_keys(
  key               TEXT NOT NULL,
  installation_id   INTEGER NOT NULL,
  response_json     TEXT NOT NULL,
  created_at        TIMESTAMP NOT NULL,
  PRIMARY KEY (installation_id, key)
)
```

No `users` table: the server doesn't track Telegram users as first-class entities, just records which telegram_user_id did each bind for audit purposes.

---

## HTTP API

All client requests carry `Authorization: Bearer <installation_token>` and `Idempotency-Key: <uuid>` (the latter only on POSTs that create resources).

### Messages

```
POST   /v1/messages
Body: { kind, text, keyboard?: [[{label, value}, ...]], reply_required?: bool, ttl_sec }
→ 200 { message_id, telegram_message_id }
→ 409 { error: "not_bound" }  if installation has no chat_id yet

PATCH  /v1/messages/{id}
Body: { text?, keyboard? }
→ 200 { }

DELETE /v1/messages/{id}
→ 204

GET    /v1/messages/{id}/answer?wait=30
→ 200 { state: "answered", answer: {...} }   immediately or on event
→ 200 { state: "expired" | "cancelled" }
→ 204                                         after wait timeout, client retries

POST   /v1/messages/{id}/cancel
→ 200 { }    sets state=cancelled, removes keyboard
```

### Chat binding

```
POST   /v1/bindings/request
→ 200 { code, expires_at }

GET    /v1/bindings/{code}
→ 200 { state: "pending" }
→ 200 { state: "bound", chat_id, telegram_user_id }
→ 410 { state: "expired" }
```

### Health / debug

```
GET    /v1/installations/me
→ 200 { id, label, chat_bound: bool, last_seen_at }
```

### Telegram webhook (server-internal)

```
POST   /telegram/webhook/{secret}
```

Path includes a long random secret known only to Telegram and the server (Telegram supports this natively via `setWebhook`). No other auth needed.

---

## Key flows

### Sending a question (replaces `_telegram_api_request("sendMessage", ...)`)

1. Hook builds keyboard from `AskUserQuestion` options.
2. Client POSTs `/v1/messages` with kind=`question`, keyboard, ttl_sec=300.
3. Server calls Telegram `sendMessage` (with `callback_data` = serialized `{message_id, option_idx}`), stores row state=`open`, returns `message_id`.
4. Hook loops on `GET /v1/messages/{id}/answer?wait=30`.
5. User taps a button on whichever device.
6. Telegram → server webhook. Server:
   - parses `callback_data`, loads message row,
   - writes `answer_json`, sets state=`answered`,
   - calls Telegram `answerCallbackQuery` ("Answered: X"),
   - notifies any in-process long-poll waiters via an `asyncio.Event` keyed by message_id.
7. Hook's parked long-poll wakes up, returns answer, hook unblocks.

### Free-text reply

Server sends with `force_reply=true`. When a Telegram message arrives via webhook with `reply_to_message_id` matching a known open `messages.telegram_message_id`, server attributes it to that question and follows the same answer-recording path as step 6 above.

Fallback (clients that strip force_reply): track per-chat "last awaiting" message; the next non-command message in that chat from the bound user is treated as the answer. Same heuristic the current daemon uses.

### Issuing a token (admin)

```
$ relay-admin issue --label anton-laptop
Token: rly_8f3a2b...  (store this safely; not recoverable)
$ relay-admin list
  id   label             bound_chat   last_seen
  1    anton-laptop      yes          2m ago
  2    anton-workstation no           never
$ relay-admin revoke --id 2
```

`relay-admin` is a small Click app that talks to SQLite directly (lives on the server box, not exposed over HTTP). Tokens are generated with `secrets.token_urlsafe(32)`, hashed with SHA-256 before storage.

### Binding a chat (device user)

```
$ relay-client bind
Send this message to the bot in the chat you want notifications in:
  /bind BIND-7H2K-9XQ4
(waiting up to 10 min...)
✓ Bound to chat "Anton (private)" (user @anton).
```

`relay-client` is a Click app shipping with the client library. Reads server URL + token from `~/.config/claude-tg-relay/config.toml`. The binding wait is a loop over `GET /v1/bindings/{code}` with backoff.

---

## TTLs and cleanup

A background `asyncio` task on the server runs every 30s:

- `messages` with state=`open` and `expires_at < now` → state=`expired`, strip keyboard via `editMessageReplyMarkup`, optionally append "(expired)" to text.
- `binding_codes` past `expires_at` and not consumed → leave for audit; just won't match.
- `idempotency_keys` older than 24h → delete.

---

## Client library shape

The current hooks call helpers in `telegram_permission_router.py`. The migration replaces those internals with thin wrappers over the relay HTTP API:

```python
# Before:
send_telegram_message(chat_id, text, keyboard)
edit_telegram_message(chat_id, msg_id, text)
delete_telegram_message(chat_id, msg_id)
# (custom getUpdates loop in telegram_daemon.py)

# After:
relay = RelayClient.from_config()
msg = relay.send_message(text=..., keyboard=..., kind="question", ttl_sec=300)
relay.edit_message(msg.id, text=...)
relay.delete_message(msg.id)
answer = relay.wait_for_answer(msg.id, timeout=300)   # internally long-polls
```

The daemon (`telegram_daemon.py`) and its `getUpdates` loop are **deleted entirely** — there is nothing left to poll. Permission state stored locally (`telegram_permission_router.py` ~lines 100-400 of state management) stays as-is; only the transport changes.

---

## Configuration

### Server (env vars or `/etc/relay/config.toml`)

```toml
bot_token          = "..."        # required
webhook_secret     = "..."        # random, used in webhook URL path
public_url         = "https://relay.example.com"  # for setWebhook
db_path            = "/var/lib/relay/relay.db"
listen             = "127.0.0.1:8080"  # Caddy fronts this
```

On startup the server calls `setWebhook` with `{public_url}/telegram/webhook/{webhook_secret}`.

### Client (`~/.config/claude-tg-relay/config.toml`)

```toml
server_url        = "https://relay.example.com"
installation_token = "rly_..."
```

`relay-client` writes this; hooks read it.

---

## Phased implementation plan

### Phase 1 — server skeleton
- FastAPI app, SQLite schema, `relay-admin` CLI for `issue`/`list`/`revoke`.
- `POST /v1/messages` + `GET /v1/messages/{id}/answer` working end-to-end with a *fake* Telegram backend (stub that records calls).
- Idempotency middleware.
- Tests: token auth, idempotency replay, long-poll wakeup.

### Phase 2 — Telegram integration
- Real Bot API calls (sendMessage, editMessageText, editMessageReplyMarkup, deleteMessage, answerCallbackQuery).
- Webhook endpoint, `setWebhook` on startup.
- Callback routing → message resolution → answer recording → long-poll wakeup.
- Free-text reply handling (force_reply + reply_to_message_id).

### Phase 3 — binding flow
- `POST /v1/bindings/request`, `GET /v1/bindings/{code}`.
- `/bind <code>` command handler in the webhook.
- `relay-client bind` CLI.

### Phase 4 — client library
- `RelayClient` class, config loading.
- Drop-in replacements for `send_telegram_message` etc. in `telegram_permission_router.py`.
- Delete `telegram_daemon.py` and its hook registration.

### Phase 5 — TTL/cleanup
- Background reaper for expired messages.
- Idempotency key GC.

### Phase 6 — migration
- Deploy server, issue tokens, bind chats on each device.
- Run install script to push the new client config + updated hooks.
- Delete the old direct-bot-API path.

---

## Resolved decisions

- **Admin CLI**: local-only Click app on the server box, talks to SQLite directly. No HTTP admin surface. SSH is the access control.
- **Group chats**: `/bind` accepted in any chat. **Any member of the bound chat can click buttons and reply with answers.** Trust boundary is "anyone in the chat" — group membership must be managed carefully (don't add casual collaborators to a chat bound to a dev box).
- **Multi-chat per installation**: deferred. 1:1 mapping. Extension path is an additive `chat_route` field on `POST /v1/messages`.
- **Auto-unbind on Telegram errors**: when `sendMessage`/`editMessage` returns Telegram `Forbidden` (bot blocked, kicked from group, etc.), clear `telegram_chat_id` on the installation row and return `409 not_bound` to the client. User must re-run `relay-client bind`.
- **Token rotation**: `relay-admin rotate --id N` from day one. Generates new token, hashes it in place, prints once. Old token immediately invalid.
- **Rate limiting**: not in phase 1. Telegram's global 30 msg/s cap is the only safeguard initially. Revisit if a runaway hook causes problems.

## Still open (low-priority, decide during implementation)

- Observability surface: structured stderr logs are the floor. A `/v1/admin/stats` endpoint or a `relay-admin stats` command can come later.
- Multi-bot support on one server: deferred. Single bot token per server.
