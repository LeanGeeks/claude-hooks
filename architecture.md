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
    roles_config.py              role catalog + binding loader; roles_report, format_roles_table
  settings.json                  permissions allow/deny (+ statusline); hooks merged in by installer
install-claude-config.sh         merges permissions + wires hooks into global settings.json
shell/
  profiles.example.toml          shipped template for ~/.claude/profiles.toml
  amux-spawn.bash                shell integration: auto-generates aliases from profiles
  amux-spawn-completion.bash     bash completion for amux-spawn + profile names
  claude-roles                   diagnostic: per-role destination + errors (claude-roles --help)
docs/
  roles.example.toml             copy-paste template for .claude/roles.toml
  roles-prompt-example.md        agent-facing prose to adapt into CLAUDE.md
relay-server/
  relay_server/
    app.py                       FastAPI app: HTTP API + Telegram webhook + update dispatch
    client.py                    synchronous RelayClient used by the hooks
    telegram_backend.py          Bot API calls (HTML parse mode); Fake backend for tests
    models.py                    Pydantic request/response models; MessageKind/State literals
    db.py                        SQLite (WAL) schema + connection helpers
    waiters.py                   in-process long-poll waiter registry, keyed by message_id
    reaper.py                    background task: expire stale messages, cleanup sweep, nudge pass, purge idem keys
    render.py                    render_body + awaits_human: the single render function every send and edit uses
    availability.py              pure active-time arithmetic: parse_tz, parse_windows, is_active, advance_active
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

A server background task that runs every 30 s. Each tick has five passes:

1. **Expiry pass** — rows `state='open' AND expires_at < now`: transition to
   `'expired'`, best-effort text re-render (to strip `#unanswered` — see below),
   keyboard strip, delete any live nudge, wake waiters, evict waiter registry.
2. **Cleanup sweep** (epic 19-04) — rows that have **left** `open` still carrying
   a `render_dirty = 1` flag or a `nudge_tg_message_id IS NOT NULL`. This is the
   backstop for `_record_answer` — the fifth terminal path (brd §2.2) — which
   flips state in SQLite and performs **no Telegram call at all**, leaving the tag
   and the nudge to the hook's subsequent PATCH. When the machine sleeps or the
   hook dies between the flip and the PATCH, the cleanup sweep removes both within
   one tick. **Why a sweep and not an eager edit at flip time:** adding
   `editMessageText` to the hottest path in the system (every ungrouped button tap
   and plain-text reply) costs two edits on a path that already has one (the
   hook's PATCH lands milliseconds later with the baked `✅` text), only gains
   anything in the failure case, and drags nudge deletion onto that hot path
   unnecessarily. The reaper's answer — at most one tick of delay in the failure
   case — is strictly better (state.md 2026-08-16, invariant 10).
3. **Nudge pass** (epic 19-04) — rows `state='open' AND next_nudge_at IS NOT NULL
   AND next_nudge_at < now`, coalesced to one nudge per group and one nudge per
   chat per tick. `next_nudge_at NULL` (the default for unconfigured chats) is the
   gate that keeps the pass a no-op for uninterested chats — no SELECT hits the
   table unless at least one chat ran `/nudge on`.
4. **Binding codes** — no action; the `GET /v1/bindings/{code}` endpoint already
   returns HTTP 410 for expired codes.
5. **Idempotency key purge** — rows older than 24 h are deleted.

---

## SQLite schema (v3)

Three tables carry the relay's durable state. All queries use WAL mode.

### `installations`

Maps an installation token to a Telegram chat. One installation per machine;
many installations can bind to the same `telegram_chat_id`.

### `messages`

One row per relayed message. Key columns:

| Column | Purpose |
|--------|---------|
| `id` | Relay message id (not the Telegram message id) |
| `telegram_chat_id` | Destination chat |
| `telegram_message_id` | The message's id in Telegram (from the send response) |
| `kind` | `question \| permission \| notification` |
| `payload_json` | **Canonical, always-untagged body** — the source of truth for all renders. Every writer keeps it current; the PATCH endpoint writes the client's text back here so that the cancel and expiry renders never re-render from a stale payload (invariant 1 of epic 19). |
| `state` | `open \| answered \| denied \| cancelled \| expired` |
| `expires_at` | Wall-clock TTL deadline (not active-time) |
| `nudge_count` | How many nudge-replies have been sent for this row |
| `next_nudge_at` | When the next nudge is due (active-time arithmetic); `NULL` when nudges are off or the ladder is spent |
| `nudge_tg_message_id` | The Telegram message id of the row's current live nudge; `NULL` when none |
| `render_dirty` | `1` when `_record_answer` has flipped the state but the hook's PATCH has not yet re-rendered the text; the cleanup sweep uses this to remove the tag and the nudge asynchronously |

Indexes: `messages_state_expiry (state, expires_at)` covers the expiry pass;
`messages_nudge_due (state, next_nudge_at)` covers the nudge pass;
`messages_render_dirty (render_dirty)` covers the cleanup sweep.

### `recipients` (epic 19)

Per-chat availability and nudge configuration. Keyed on `telegram_chat_id`
(not `installations.id`) because many installations bind to the same chat —
availability is a property of the human, not the machine (brd §2.1).

| Column | Purpose |
|--------|---------|
| `telegram_chat_id` | Primary key |
| `tz` | IANA timezone string (`Europe/Berlin`); `NULL` means UTC assumed |
| `windows_json` | Canonical availability spec string (`mon-fri 09:00-19:00`); **not JSON** despite the column name — read with `availability.parse_windows`. `NULL` means always available |
| `nudge_enabled` | `1`/`0`; `0` by default so `next_nudge_at` is never seeded on an unconfigured chat |
| `nudge_schedule` | Comma-separated active-time intervals (`15m,45m,3h`); `NULL` means use the server default |

An absent `recipients` row is valid and means: always available, nudges off —
identical behaviour to before epic 19.

---

## `#unanswered` — relay-owned tag (epic 19)

**`payload_json.text` is the canonical body and is always untagged.** A single
`render_body` function (in `render.py`) is called by *every send and every edit*
and appends a trailing `#unanswered` line iff the row is `state='open'` and
`awaits_human` is true. No call site decides tagging for itself; the tag exists
only in the render layer (brd §4.2, epic 19 invariant 2).

**`awaits_human`** is true for a row whose `kind` is not `notification` and that
has `reply_required`, a keyboard, or a `group_id`. Idle-session notifications are
explicitly excluded — `#unanswered` means *an agent is blocked on you*, not "a
session finished" (brd §4.1, invariant 7).

**Why one function and not per-call-site decisions:** five terminal paths exist
(brd §2.2), three of which do not currently edit the message text. Keeping the
tag correct across all five without a single render chokepoint requires every path
to know about and correctly handle the tag — a fragile invariant. One `render_body`
called by every send and edit makes the invariant hold by construction and makes a
retried PATCH idempotent (a client that appends `#unanswered` to its own text is
stripped and re-rendered, not doubled).

**Where to look in `app.py`:** the PATCH endpoint (`patch_message`, `app.py:735`) is the most
important caller because it (a) writes the client's text back into `payload_json`
and (b) calls `render_body` before the Telegram edit, so the tag round-trips
correctly on every hook-side finalization.

---

## Relay-local commands (epic 19)

`/tz`, `/hours`, `/nudge`, `/me` are handled directly in the webhook handler
beside `/bind` (`app.py`). They read and write the `recipients` table and involve
no queuing and no machine.

**These are relay-local, not queue-backed.** Epic 16 plans a command queue (the
`commands` table) for operations that must reach a specific machine — starting a
session, injecting a reply. The availability commands are in a different
category: they affect server-side state only and do not need to reach any
machine. Confusing the two leads to over-engineering (the availability commands
would need a listener on every machine) or under-engineering (queue-backed
commands would not work when no machine is connected). The guard in `app.py` that
ignores slash commands except `/bind` was extended additively to handle these
four new ones above the existing guard, so they cannot be recorded as message
answers.

The operator equivalents are `relay-admin recipients` subcommands (`list`,
`set-tz`, `clear-tz`, `set-hours`, `clear-hours`, `set-nudge`,
`set-nudge-schedule`) which talk directly to the SQLite database.

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

**Multi-destination routing (epic 15, opt-in per workspace):** when
`.claude/roles.toml` is present, an agent can prefix the `header` field with
`@alias` (e.g. `@ux Layout`) to route the question to a specific human role.
The router parses the alias, resolves it through the binding chain in
`~/.config/claude-tg-relay/config.toml`, and sends the group to the role's
Telegram chat instead of the default one.  If the role is unreachable the
question falls back to the default role with an explanatory note in the message.
One call always maps to one role — mixed aliases are rejected with a deny that
names both and instructs the agent to split into separate calls.  A workspace
with no `roles.toml` behaves exactly as before: single default destination,
no new fields.  `claude-roles` (the `shell/claude-roles` diagnostic) shows the
resolved destination and escalation for each role in the current workspace.

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
- **This repo requires a *forked* amux, not upstream 0.3.0.** Epic 10's spawn
  chain needs launch behaviours the stock CLI does not expose — `--no-attach`,
  `--no-default-model`, env propagation via `update-environment`, and
  `--session-id` kept out of `CC_FLAGS`. Install it with **`./install-amux.sh`**
  (clone → branch → verify → `/usr/local/bin/amux`, CLI only) **before**
  `install-claude-config.sh`; the two are coupled only at runtime, since
  `amux-spawn` resolves `amux` from `PATH`. Details and the pinned commit:
  [tasks/12_amux_extensions.md](./tasks/12_amux_extensions.md). Note the fork
  does **not** bump `CC_VERSION` — `amux --version` still prints `0.3.0`, so the
  installer's feature probe, not the version string, is what distinguishes fork
  from upstream.

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

## Human roles for AskUserQuestion (epic 15)

Role routing is opt-in and workspace-scoped: a workspace with no
`.claude/roles.toml` behaves exactly as before — same destination, same
rendering, no new fields.

### Configuration — two files, deliberate split

**`.claude/roles.toml`** (committed, workspace vocabulary) defines aliases,
titles, the default role, and per-role escalation policy.  It carries **no
role descriptions** and no tokens: descriptions are free-form prose for an agent
to read in `CLAUDE.md`; tokens are per-machine secrets.  See
`docs/roles.example.toml` for a copy-paste template.

**`~/.config/claude-tg-relay/config.toml`** (per-machine, never committed)
binds each role alias to an installation token or a reference to another role.
A value starting with `rly_` is a real token; anything else is a role reference
resolved transitively.  Per-workspace overrides go in
`[workspace.<id>.roles]` and `[workspace.<id>.escalate_after]`.

### Resolution precedence

For role `R` in workspace `W` the binding lookup is:

1. `[workspace.W.roles].R`
2. `[roles].R`
3. Top-level `installation_token`, **only** if `R` is the default role.
4. Unresolved → fall back to the default role with an explanatory note in the
   Telegram message body.

Escalation follows the same four-level shape
(`[workspace.W.escalate_after].R` → `[escalate_after].R` → per-role →
top-level).

### @alias → role → token routing chain

An agent addresses a role by prefixing `header` with `@alias` (e.g.
`@ux Layout`).  The router:

1. Strips the prefix and identifies the role from `catalog.alias_index`.
2. Resolves the binding chain to a token (or falls back to the default role).
3. Sends the message group to that token's installation.
4. If `escalate_after` fires, a duplicate group goes to the default
   destination; the first group to finalise wins and the other is patched +
   cancelled.

### Constraints

Two facts about the relay shaped every decision:

* **`AskUserQuestion`'s input schema is closed** — `additionalProperties: false`
  at both levels.  The role tag rides in the `header` string because no new
  field can be added.
* **Question groups are scoped to a single chat** — `_load_group_members`
  filters by `telegram_chat_id`.  A group split across two chats can never
  finalise, which is why **one call addresses exactly one role** and mixing is
  rejected with a deny.

### Diagnostic

`claude-roles` (installed to `~/.claude/shell/` and symlinked into
`~/.local/bin/`) shows each role's resolved destination, escalation setting, and
any config errors without ever printing token material.  `claude-roles --check`
probes the relay for each distinct token and reports `bound` / `not bound` /
`invalid token`.

---

## Testing

`tests/run_all_tests.py` aggregates unit + integration suites (decision mapper,
state store, whitelist, notification hook, pretool, permission-request
integration). The relay server has its own `relay-server/tests/`. Tests use
`FakeTelegramBackend` and patch `RelayClient`, so no network or real bot is
touched.
