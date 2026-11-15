# 46-01 — Relay: cancel reaches the escalated copy; per-message ladders get their own cap

**Status:** todo · **Depends on:** — (independent root; touches only `relay-server/`)
**Read first:** [brd.md](./brd.md) §2, D3, D10 · [architecture.md](./architecture.md) §2 ·
`tasks/23_async_questions/state.md` (log entry for 23-02 — how escalated children
work) · `relay-server/relay_server/app.py` (`cancel_message`, `create_message`),
`reaper.py` (`cancel_escalated_children`, `_row_ladder` / the per-message ladder
parse), `render.py`, `config.py`, `client.py`

## Goal

Two relay defects stand between the questions client and a clean withdrawal:

1. `POST /v1/messages/{id}/cancel` strips the keyboard and retires the ladder of
   the message it is given, and **leaves an already-sent escalated copy live** —
   buttons, `#unanswered` tag, nudges and all. Answer and expiry both call
   `cancel_escalated_children`; cancel does not.
2. A per-message `nudge_schedule` is validated against `nudge_max`, the cap on a
   *chat's* `/nudge` ladder (default 3, not settable from the environment, absent
   from the deploy example). The questions client's documented default ladder
   `4h,1d,3d,7d*` has four rungs, so a stock `ask` is rejected with HTTP 422.

## Scope

### 1. Cancel cascades to escalated children

In `cancel_message` (`app.py`), after the parent is handled, cancel every open
escalated child of the message with the existing `cancel_escalated_children`.

- **Hang it off the call, not off the flip.** A parent that was already
  `cancelled` (by a relay that predates this task) or already `answered` may
  still have an open child; a repeated cancel must clean it up. Cancelling
  children of an already-terminal parent is always correct — a child never
  outlives its parent.
- Best-effort, like every other Telegram edit on this path: a failed child edit
  is logged and never turns the cancel into an error.

### 2. Cancel may carry the text the chat should end on

`cancel` accepts an **optional** JSON body `{"final_text": "<html>"}`.

- Absent body, empty body, or no `final_text`: behaviour is byte-identical to
  today (every existing caller sends no body).
- With `final_text`, and only when this call flipped the row from `open`: the
  text becomes the message's canonical body (`payload_json["text"]`, stored
  through `strip_tag`, displayed through `render_body`) and is what the cancel's
  re-render shows. A row that was already terminal keeps whatever text it has —
  a late cancel must never overwrite an answer another path baked in (the
  existing comment in `cancel_message` explains why).
- The same `final_text` is passed to `cancel_escalated_children`, which uses it
  as the child's final body (and persists it to the child's payload) instead of
  re-rendering the child's own text. Without it, children render as today.
- Cap `final_text` at Telegram's 4096 characters; longer is a 422.

`cancel_escalated_children` grows a keyword-only `final_text: str | None = None`.
Its two existing callers (answer, expiry) pass nothing and behave as before.

### 3. `nudge_message_max`

Add `nudge_message_max: int = 8` to `RelayConfig`. It caps the rung count of a
**per-message** `nudge_schedule`; `nudge_max` keeps capping the per-chat ladder
(`/nudge`, `nudge_default_schedule`, the admin CLI).

- Use it at all three per-message parse sites: the send-time validation and the
  nudge seeding in `app.py`, and the override parse in `reaper.py`. They must
  agree — a ladder accepted at send time and rejected by the reaper silently
  falls back to the chat ladder and stops repeating.
- Make both caps settable from the environment: `RELAY_NUDGE_MAX`,
  `RELAY_NUDGE_MESSAGE_MAX` (integers; add them to `_ENV_MAP` / `_ENV_COERCE`).
- Document `nudge_default_schedule`, `nudge_max` and `nudge_message_max` in
  `deploy/config.toml.example`, `.env.example` and the README's config section.
- The 422 detail for an over-long per-message ladder must name the cap that was
  applied, so a client can report it.

### 4. Client

`RelayClient.cancel_message(message_id, final_text: str | None = None)` — sends
the JSON body only when `final_text` is given. An old relay ignores the body, so
the new client stays compatible with an undeployed server.

## Out of scope

- Any schema change. `SCHEMA_VERSION` stays where it is.
- The questions client (`questions-mcp/`, `.claude/hooks/questions_*`) — 46-02…46-05.
- Deploying. Do not run `docker`, and do not touch `relay-server/.env`.

## Tests

In `relay-server/tests/` (pytest, `FakeTelegramBackend`):

- cancel of an open parent with an open, delivered escalated child: both rows
  `cancelled`, both keyboards stripped, the child's ladder retired;
- cancel of an **already-cancelled** parent with an open child still cancels the
  child (the retry path);
- a child with `telegram_message_id = 0` is cancelled without a Telegram edit;
- `final_text` on an open row: stored untagged in `payload_json`, shown on the
  parent and on the child; on an already-answered row: text untouched;
- no body / `{}` body: the recorded Telegram calls are identical to today's
  (compare against the existing cancel tests rather than re-deriving them);
- a four-rung `4h,1d,3d,7d*` per-message ladder is accepted with the default
  config, is seeded, and is parsed by the reaper as repeating; a nine-rung one is
  a 422 naming `nudge_message_max`; the per-chat `/nudge` cap is still `nudge_max`;
- `RELAY_NUDGE_MAX` / `RELAY_NUDGE_MESSAGE_MAX` are read and coerced to int;
- `RelayClient.cancel_message` sends no body by default and `{"final_text": …}`
  when asked.

Run the whole relay suite and the top-level suite; neither may lose a test.

## Done when

- A cancelled question leaves no live keyboard anywhere, including on an
  escalated copy, and a repeated cancel is harmless.
- The questions client's four-rung default ladder is accepted by a relay running
  the default config.
- Every pre-existing relay call that sends no cancel body produces the same
  Telegram traffic as before.
