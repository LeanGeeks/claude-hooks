# Epic 16 — Architecture

**Rev 1 · 2026-08-02.** Design detail behind [brd.md](./brd.md). Written before
the task breakdown: shapes and invariants are settled here, field-by-field
specification lands with the task files.

Read [`../../architecture.md`](../../architecture.md) first — this document only
describes what changes.

---

## 1. Component map

```
Telegram ──/new──► relay webhook ──┐
                                   │ commands table (per installation)
                                   ▼
                   GET /v1/commands?wait=N  ◄── long-poll ── amux-spawn listen
                                   │                              │ (systemd --user)
                   POST /v1/commands/{id}/result ◄────────────────┤
                                                                  │
   wizard steps (workspace picker, prompt force-reply) ───────────┤
   = ordinary POST /v1/messages + GET .../answer, sent BY the listener
                                                                  ▼
                                                     amux-spawn spawn --plain
                                                                  ▼
                                          plain amux session, seeded, detached
                                                                  ▼
                                    Notification(idle_prompt) → task-09 loop
```

Two rules keep the split honest:

1. **The relay routes; the listener decides.** The relay knows installations,
   labels and chat membership. It never learns a directory, a profile or a
   prompt template.
2. **Wizard UI is client-generated.** Every keyboard except the machine picker is
   a normal relay message created by the listener, so threaded-reply routing,
   group finalization, TTLs and cancellation all work exactly as they do for
   permissions and questions. No new UI primitive.

---

## 2. Relay changes

### 2.1 Schema — one table

```sql
CREATE TABLE IF NOT EXISTS commands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id     INTEGER REFERENCES installations(id),  -- NULL until targeted
    telegram_chat_id    INTEGER NOT NULL,
    telegram_user_id    INTEGER NOT NULL,
    telegram_message_id INTEGER,          -- relay-owned picker message, if any
    kind                TEXT NOT NULL,    -- spawn | resolve | ls
    payload_json        TEXT NOT NULL,
    state               TEXT NOT NULL,    -- pending | targeting | claimed | done | failed | expired
    result_json         TEXT,
    created_at          TIMESTAMP NOT NULL,
    claimed_at          TIMESTAMP,
    expires_at          TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_delivery ON commands(installation_id, state, expires_at);
```

A schema-version migration in `db.py:MIGRATIONS`. The reaper (`reaper.py`) grows
one more sweep: `pending`/`targeting`/`claimed` past `expires_at` → `expired`,
with a chat notice when a spawn expires unclaimed.

`installation_id` is NULL only while a command is `targeting` (awaiting a machine
picker tap or a broadcast resolution).

### 2.2 Endpoints

| Method & path | Purpose |
|---|---|
| `GET /v1/commands?wait=N` | installation-authenticated long-poll; returns the oldest `pending` command for this installation and flips it to `claimed`, or `204` on timeout |
| `POST /v1/commands/{id}/result` | listener reports `{ok, summary, detail, data}`; the relay stores it, marks `done`/`failed` and wakes the fan-out. The chat message is written by the command surface (§2.4–2.6), not here |

`?wait=N` reuses the waiter pattern of `GET /v1/messages/{id}/answer`
(`app.py:760`) but keys the registry by `installation_id` rather than
`message_id`, since the whole point is that no message exists yet. A **second**
registry, keyed by command id, wakes the fan-out (§2.5) when a result arrives.

Claiming is a single-writer `UPDATE … WHERE state='pending'` so a duplicated
listener (restart overlap) cannot deliver the same command twice.

**Two-phase TTL.** `expires_at` before a claim is the delivery deadline (120 s
for `spawn`, 10 s for `resolve`/`ls`); the claim rewrites it to
`claimed_at + command_max_run_s`, so a listener that dies mid-wizard leaves a row
the reaper retires instead of one stuck in `claimed` forever.

The queue layer itself sends **nothing** to Telegram — it moves payloads and
wakes waiters. Every chat-visible string in this epic is written by the command
surface (§2.4–2.6) or by the listener's wizard (§3.3).

### 2.3 Command envelope

```json
{ "id": 41, "kind": "spawn",
  "payload": { "workspace": "claude-hooks", "prompt": "fix the flaky login test",
               "modifiers": ["glm5", "opus"],
               "chat_id": 12345, "origin_message_id": 907 } }
```

`workspace` and `modifiers` are passed through **verbatim and unparsed** — the
relay strips the `+` and nothing else, because it does not know which token is a
profile and which is a model tier (brd §5.3); the listener decides that against
its own `profiles.toml`. Any of `workspace`, `prompt` and `modifiers` may be
absent — that is precisely what puts the wizard into play. `resolve` carries only
`{workspace}`; `ls` carries nothing.

### 2.4 Webhook parsing

`_handle_update` gains a `/new` and `/ls` branch **before** the existing
`text.startswith("/")` early-return (`app.py:1316`), which today swallows every
non-`/bind` command. The bound-user check that guards loose replies
(`app.py:1329-1333`) is hoisted so it also guards commands.

Parsing is deliberately dumb and single-pass: `+`-prefixed tokens are modifiers,
the first other token is the target, and the token after that starts the prompt,
verbatim to the end. A target containing `.` is split on the **first** dot and the
prefix is matched against installation labels for this chat; if it matches
nothing, the whole token is treated as a workspace name.

### 2.5 Target resolution and broadcast

```
explicit machine.workspace ──────────────► route directly
bare workspace, 1 installation bound ────► route directly
bare workspace, N bound ─────────────────► fan out `resolve` to all live
                                            listeners, deadline ~5 s
        1 claimant ─► route
        >1 claimant ─► machine picker (relay-owned message)
        0 claimant  ─► "no machine has <ws>" + who was offline
```

`/ls` uses the same fan-out with no resolution step: collect every answer within
the deadline, render one grouped message, list non-answering machines as offline.

Listeners answer `resolve` with `{claim, ambiguous}` — a boolean and a
disambiguation flag, **never a path**. A machine that owns two workspaces with the
same basename claims with `ambiguous: true` and settles it with the user itself
(§3.3), so directories still never reach the server.

**Liveness** is `last_seen_at` within 90 s — a listener long-polling every 25 s
keeps it fresh through the existing `_touch` (`app.py:180`), so 90 s tolerates
three missed polls.

A `spawn` for a machine that is **not live is refused outright, not inserted**.
Inserting it would produce either two messages for one action ("offline" now,
"expired" two minutes later) or — far worse — a session that starts moments after
the user was told nothing would happen, which is the surprise §5.4 exists to
prevent. One command, one outcome, one message:

| Machine | Behaviour |
|---|---|
| not live | refuse immediately, insert nothing |
| live, claims it | normal path; the ack is the only message |
| live, never claims | expires at the delivery TTL → "did not pick this up" |

`resolve` and `ls` are broadcast to every bound installation regardless of
liveness — they are harmless reads whose non-answer *is* the signal.

### 2.6 Callback namespace

The machine picker is the one keyboard the relay owns, so its buttons carry
`c:{command_id}:o:{idx}`. `callback_data.decode` is strict on `m:…:o:…` and
returns `None` for anything else (`callback_data.py:41`), so `_handle_callback_query`
branches on the prefix before the existing path and the message flow is untouched.

---

## 3. The listener — `amux-spawn listen`

A subcommand rather than a new binary: `amux-spawn` is already installed and on
`PATH`, already imports `amux_spawn_lib`, and already owns spawning.

Layout mirrors epic 10's: the logic lives in `.claude/hooks/amux_listen_lib.py`
(installed through `REQUIRED_HOOKS`, importable, testable), and `cmd_listen` in
`.claude/bin/amux-spawn` is process lifecycle only. Three small files sit beside
the existing hook stores: `~/.claude/telegram_spawns.json` (the spawn ledger
behind the caps), `~/.claude/amux-spawn-listen.lock` (single instance), and
`~/.claude/amux-spawn-listen.status.json`, which the loop refreshes so
`listen --status` — a separate process that cannot hold the lock — has something
truthful to read.

### 3.1 Lifecycle

```ini
# ~/.config/systemd/user/amux-spawn-listen.service
[Unit]
Description=amux-spawn Telegram listener
After=network-online.target

[Service]
ExecStart=%h/.local/bin/amux-spawn listen
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Installed by `install-claude-config.sh`, enabled only when `[listen].enabled` is
true. `loginctl enable-linger` so it survives logout. Single-instance via an
`flock` on `~/.claude/amux-spawn-listen.lock` — a second copy exits rather than
double-claiming commands. Logs go to the journal; `amux-spawn listen --status`
prints connection state, last claim, live spawn count and the resolved config for
diagnosis.

### 3.2 Loop

Long-poll `GET /v1/commands?wait=25`, reconnect with jittered backoff on network
errors, treat `401` as fatal-with-retry (the token was revoked; keep retrying
slowly and say so in `--status`). Laptops sleep, so a dropped connection is the
normal case, not an error worth logging loudly.

Each claimed command runs in its own thread: a wizard blocks on human answers for
minutes, and a second `/new` (or an `/ls`) must not queue behind it.

### 3.3 Wizard state machine

State lives in the thread, not on disk. A listener restart loses in-flight
wizards and their messages expire — acceptable, and strictly safer than resuming
a half-specified spawn.

```
claimed(spawn)
   │ workspace known? ──no──► send question msg: workspace buttons (seen-store, recency)
   │                            └─ answer / cancel / expire
   │ bare /new? ──yes──► send question msg: model row [+ profile row if >1 profile]
   │                            └─ any explicit +token suppresses this step
   │ prompt given? ──no──► send question msg, reply_required=true: "Prompt for <ws>?"
   │                            └─ free text, multi-line, cancel button
   ▼
 preflight: dir exists · in seen-store · caps ok · profile resolves
   ▼
 amux-spawn spawn --plain --json --dir <abs> [--profile P] [--model T] -- "<prompt>"
   ▼
 POST /v1/commands/{id}/result  → bot posts the ack
```

Every step is `POST /v1/messages` + `GET .../answer`, i.e. the same transport the
permission and question flows use, with the same TTL and cancellation semantics.

### 3.4 Spawn invocation

`--plain` is new (brd §2.3): an explicit override of the TTY inference at
`.claude/bin/amux-spawn:189-191`. The listener always passes it, along with
`--json` so it reads the created name from a machine-readable object rather than
scraping a human sentence. The prompt is passed positionally after `--`, as
`cmd_spawn` already expects.

**Multi-line seeding is unverified.** `_amux_create_detached` passes the prompt as
a single argv element and claims multi-line survives
(`.claude/bin/amux-spawn:355-360`), but amux persists launch state to
`~/.amux/sessions/<name>.env`, and a newline in a shell-sourced env file is a
plausible break. It must be checked against a real amux before 16-06 is done —
multi-line is the *common* case here (brd §3.1), not an edge case. Task 09's
injector flattens newlines for an unrelated reason: `send-keys` would submit
early (`reply_injector.py:63-70`).

**Env: the spawn is a subprocess, not an import.** `cmd_spawn` does
`os.environ.update(profile_env)` (`.claude/bin/amux-spawn:173-180`) so amux's
`update-environment` allowlist can copy the profile's vars into the child pane
(epic 10 D-Env). That mutation is correct in a short-lived CLI and poison in a
process that lives for weeks — one spawn's auth would leak into the next. The
listener therefore passes `--profile` and lets a child process do the mutating;
its own environment is never touched.

### 3.5 Caps

`max_live` counts live tmux sessions this listener spawned — recorded in
`~/.claude/telegram_spawns.json`, since plain sessions have no handle to count —
and `min_interval_s` is a floor between spawns that survives a restart. The
ledger self-drains: an entry whose tmux session is gone is dropped on read. Both
caps refuse loudly rather than queueing.

---

## 4. The seen-store

`~/.claude/telegram_workspaces.json`, written with the same atomic
tmp+rename+flock discipline as `permission_state_store.py`:

```json
{ "/data/sync/work/leangeeks-ai/claude-hooks":
    { "name": "claude-hooks", "last_seen": "2026-08-02T15:28:11Z", "count": 37 } }
```

Three writers, all fail-open and all one call: the permission send and question
send in `telegram_permission_router.py`, and `notification_hook.py`'s idle path
(which already computes the workspace name at `notification_hook.py:640` and
currently discards it). The listener is the only reader.

Recorded only after a **successful** send: the store's claim is "this workspace
reached Telegram from this machine", and an unreachable relay must not populate
an allowlist. Paths are normalised up to the nearest `.git`/`.claude` ancestor so
a session running in a subdirectory does not register `hooks` as a workspace.

Pruning: entries older than 180 days, or beyond 200 entries, are dropped on
write. The file is a pick list, not an audit log — `permission_actions.jsonl` is
the audit log.

---

## 5. Sequences

### 5.1 One-message spawn, single machine bound

```
user ▸ /new claude-hooks fix the flaky login test
  relay: bound-user ok → 1 installation → insert command(spawn, pending)
  listener: long-poll returns it → claimed
  listener: preflight ok → amux-spawn spawn --plain --json --dir … -- "fix the flaky…"
  listener: POST result{ok, "claude-hooks-2"}
  relay → chat: ▶ claude-hooks-2 · workstation · claude
  … session runs, goes idle …
  notification_hook → idle notification + reply injector (task 09, unchanged)
```

### 5.2 Bare workspace, two machines bound

```
user ▸ /new claude-hooks fix …
  relay: insert command(targeting) → fan out resolve{claude-hooks} to both
  thinkpad:    claims? no  (not in its seen-store)
  workstation: claims? yes {claim:true, ambiguous:false}   ← no path crosses the wire
  relay: single claimant → set installation_id, state=pending → workstation claims
```

Two claimants would instead produce a picker on the `c:` namespace; zero
claimants a "no machine has it" notice naming which machines answered.

### 5.3 Wizard

```
user ▸ /new
  relay: >1 bound → machine picker (relay-owned, c: callbacks)
  user ▸ [workstation]
  relay: command(spawn, pending, installation=workstation)
  listener: no workspace → question msg with workspace buttons (seen-store)
  user ▸ [claude-hooks]
  listener: model row [default][fable][opus][sonnet][haiku] → user ▸ [opus]
  listener: force-reply "Prompt for claude-hooks?"
  user ▸ (multi-line reply)
  listener: spawn → result → ack
```

---

## 6. Timeouts

| Thing | Value | Why |
|---|---|---|
| `GET /v1/commands` long-poll | 25 s | matches the existing answer long-poll chunk |
| `resolve` / `ls` fan-out deadline | ~5 s | a live listener answers in ~100 ms; longer just delays the picker |
| `resolve` / `ls` command TTL | 10 s | an answer after the fan-out gave up is worthless |
| Spawn command TTL (delivery) | 120 s | live-only delivery (brd §5.4); no surprise sessions |
| Post-claim run TTL (`command_max_run_s`) | 3600 s | retires rows a dead listener left `claimed` |
| Wizard step TTL | ~30 min | long enough to walk away mid-wizard, short enough to not linger |
| Spawn subprocess timeout | ~60 s | amux create + tmux liveness check is seconds; a hang is a failure |

---

## 7. Testing

Follows the existing conventions — `tests/run_all_tests.py`, `FakeTelegramBackend`,
patched `RelayClient`, no network:

- **Relay:** command insert/claim/expire, single-claim under concurrent pollers,
  `/new` parsing table (every row of brd §3.1), target resolution incl. 0/1/N
  claimants, `c:` callback dispatch not disturbing `m:` callbacks, bound-user
  rejection.
- **Listener:** wizard state machine with a fake relay client (each step's answer,
  cancel, expiry), `+token` resolution incl. the profile/tier collision error,
  preflight rejections, caps, single-instance lock, reconnect backoff.
- **Launcher:** `--plain` / `--track` override the TTY inference in both
  directions, `--json` emits one parseable object, existing tracked-spawn tests
  stay green.
- **Seen-store:** concurrent writers, atomic replace, pruning, and that a missing
  or corrupt file degrades to an empty pick list rather than an exception.
- **Regression floor:** with the listener stopped, permission / question / idle /
  injection flows produce byte-identical relay calls.

Live gates (need a real relay, a real bot and two bound machines): the brd §7
criteria, plus one deliberate `kill -9` of the listener mid-wizard.
