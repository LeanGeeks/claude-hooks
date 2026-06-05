# Task 09: Reply from Telegram (close the idle loop)

## Goal

Let the user **reply from Telegram to an idle session and have that reply
injected as the next user turn** into the running Claude Code session — turning
the one-way idle notification (task built just before this) into a full
conversational loop, all from a phone.

```
[idle hook] forwards agent's last message ─────► Telegram (force-reply)
   ▲                                                  │ user replies (threaded)
   │                                          [relay records answer]
   │                                                  │
   └─ Claude works, goes idle again ◄─ [injector] amux send <name> "<reply>"
```

The reply lands as Claude's next turn → Claude works → goes idle → the idle hook
forwards the new last message → repeat.

---

## Background / why this shape

- **The only way to inject a user turn into a running interactive session** is
  through the terminal. Hooks cannot inject turns (they return decisions for the
  event they handle), and a plain MCP server is pull-only. Anthropic's
  **Channels** can push turns, but it is Anthropic-auth only (no third-party API
  keys), research-preview, and judged unreliable — **rejected**.
- Sessions run under **amux**, which wraps each session in tmux and exposes
  `amux send <name> <text>` (text + Enter via `send-keys`). That is our
  injection vector. Therefore **reply mode only works for sessions started via
  amux** (e.g. `amux start hyppie-flow`), not a bare `claude`. Bare sessions
  keep today's notify-only behavior. This limitation is accepted.
- The relay already routes a **threaded Telegram reply** to the exact
  originating message/installation (`app.py` `_handle_update`, via
  `reply_to_message_id` → `_load_message_by_tg_id`). So multi-machine routing
  with one shared chat is correct **as long as the user uses Telegram's Reply**,
  not a loose message. (Loose messages hit the ambiguous
  `_load_last_open_in_chat` fallback.)

See `architecture.md` for the full existing system.

---

## Business requirements

1. When an amux-hosted session goes idle and the idle notification is sent, the
   user can **reply in Telegram** and that text becomes the session's next
   prompt, with no terminal interaction.
2. Works across **multiple machines sharing one chat**: a reply is injected only
   on the machine that owns the replied-to message.
3. **Graceful degradation:** non-amux sessions, amux unavailable, or relay
   unreachable → fall back to today's one-way notification; never break the
   session.
4. The session must **remain usable locally** while a reply is pending — the
   hook must not block the session waiting for a Telegram answer.
5. Reply mode must be safe to leave on by default; a stray/late reply must not
   corrupt a half-typed local prompt (see decision D4).

---

## Decisions on behavior

### D1 — Matching amux session ⇄ Claude session
Resolve the amux name **from inside the session** via the tmux session name:

```
tmux display-message -p -t "$TMUX_PANE" '#{session_name}'   →  amux-<name>
strip leading "amux-"                                       →  <name>
```

If `$TMUX_PANE` is unset or the session name does not start with `amux-`, the
session is **not** amux-hosted → notify-only, no injector. This is robust where
cwd-matching is **not**: a session's cwd may differ from its registered
`CC_DIR` (observed: `amux-hyppie-flow` running in `/home/anton/.bin/claude-hooks`).

### D2 — Idle "ready to receive" is the hook, not a heuristic
We do **not** try to detect "prompt is empty / Claude is idle" by parsing pane
output. The `Notification(idle_prompt)` hook firing **is** the idle signal — we
only arm injection because the session just told us it is idle.

### D3 — Clear any stray prompt text before injecting
Rather than detect a non-empty prompt, before sending the reply the injector
sends **two Escape keypresses** (`amux send <name> --keys 'Escape' 'Escape'` or
equivalent) to clear whatever might be in the input box, then sends the reply
text. Two escapes is cheap and idempotent; on an already-empty prompt it is a
no-op.

### D4 — Threaded replies only (multi-machine correctness)
The idle notification is sent with **force-reply** so the natural action in
Telegram is a threaded reply. Document that loose (non-threaded) messages are
ambiguous across machines and may be mis-routed by the relay fallback. No code
change to the relay's attribution is required.

### D5 — Non-blocking, detached injector (no daemon)
There is no client daemon (the old one was deleted; hooks long-poll directly).
The idle hook **spawns a detached injector process** and exits immediately, so
the session stays usable. The injector long-polls the relay for *its* message
only, then injects. (A reintroduced daemon would need a hook→daemon handoff
spool **and** a new relay "answered feed" endpoint — strictly more moving parts
for no benefit, since reply attribution is already per-message.)

### D6 — Injector lifetime = message TTL
The injector waits up to the relay message TTL (currently 43200s / 12h), the
same budget as a permission request. On answer it injects once and exits; on
expiry/cancel/timeout it exits without injecting.

### D7 — One injection per message
Exactly one `amux send` per answered message. After injecting, the injector
exits; it does not loop. (The *next* idle is a new notification with a new
injector.)

---

## Architecture (new pieces)

Everything reuses the existing relay client and message lifecycle.

1. **`notification_hook.py` (modify)**
   - Resolve amux name (D1).
   - Send the idle notification with `reply_required=True` (force-reply) so the
     relay captures a threaded reply. (Today it is `reply_required=False`.)
   - If amux-hosted, `Popen` a **detached** `reply_injector.py` with the relay
     `message_id` and the amux `<name>`, then exit. If not amux-hosted, behave
     exactly as today (notify-only).

2. **`telegram_permission_router.py` (modify)**
   - `send_idle_notification(...)` gains the ability to request force-reply
     (`reply_required=True`) and must **return the `message_id`** so the hook can
     hand it to the injector. (It already returns the id.)

3. **`reply_injector.py` (new hook-adjacent script)**
   - Args: `--message-id <int> --amux <name>` (+ optional config path).
   - Load `RelayClient.from_config()`, `wait_for_answer(message_id, ttl, chunk)`.
   - On a **text** answer: `amux send <name> --keys Escape Escape` (clear), then
     `amux send <name> "<reply text>"` (inject + Enter). Sanitize the text
     (collapse/strip newlines so `send-keys` doesn't submit early; decide on
     multiline handling).
   - On `expired` / `cancelled` / timeout / non-text: exit quietly.
   - Fail open: any error → log + exit 0; never wedge.

No relay-server changes required.

---

## Important facts (verified)

- amux tmux session name = `amux-<CC_NAME>` (e.g. `amux-hyppie-flow`,
  `amux-safe-choice-crm`); window name = `<CC_NAME>`.
- `amux send <name> <text>` = text + Enter; `amux send <name> --keys 'C-c'` =
  raw keys. `amux peek <name> [lines]` reads pane output (not needed given D2/D3).
- `CC_NAME` / `CC_DIR` from `~/.amux/sessions/<name>.env` are **not** exported
  into the session env; cwd may differ from `CC_DIR`. Hence D1 uses tmux, not env
  or cwd.
- Relay free-text reply attribution: `reply_to_message_id` →
  `_load_message_by_tg_id(chat_id, tg_id)` → exact message → installation.
  Fallback otherwise is `_load_last_open_in_chat` (ambiguous across machines).
- Answers are only acted upon while a process long-polls
  `GET /v1/messages/{id}/answer`; there is no background drain (hence D5's
  detached injector must be the long-poller).
- `kind="notification"` messages are answerable; answerability is per-message,
  not gated by kind. `reply_required=True` yields a force-reply prompt.

---

## Out of scope

- Re-answering / editing an injected reply.
- Buttons on the idle notification (quick "continue"/"stop"); free-text only for
  v1.
- Injecting into non-amux sessions (tmux-direct, SSH, etc.).
- Any change to the relay's reply-attribution logic.

---

## Definition of done

- [ ] Idle notification from an amux session is sent as a force-reply.
- [ ] A threaded Telegram reply is injected as the next user turn in the correct
      session via `amux send`, preceded by two Escapes to clear the prompt.
- [ ] The session remains usable locally while a reply is pending (hook does not
      block).
- [ ] Multi-machine: a reply is injected only on the owning machine (threaded
      reply routing).
- [ ] Non-amux sessions and amux/relay-unavailable cases degrade to notify-only,
      with no errors that disrupt the session.
- [ ] Injector injects exactly once per answered message and exits on
      expiry/timeout.
- [ ] Tests cover: amux-name resolution (incl. non-amux → None), the
      force-reply send, and the injector's answer→`amux send` path (amux + relay
      mocked).
