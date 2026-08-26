# 23-01 — Relay: answer feed + non-expiring messages

**Status:** todo · **Depends on:** — (independent root)
**Read first:** [brd.md](./brd.md) §2.2, D5 · [architecture.md](./architecture.md)
§2.1, §2.4 · [state.md](./state.md) invariants 2, 4, 9 ·
`relay-server/relay_server/app.py` (`get_message_answer` — the waiter pattern to
copy), `waiters.py`, `reaper.py` (expiry pass), `db.py` (`MIGRATIONS`)

## Goal

Give a resident client a way to learn about answers to messages nobody is
waiting on, and let a message legitimately never expire. This is the transport
half of the epic and touches **no Telegram rendering** — nothing this task adds
sends, edits, or formats a chat message.

## Scope

### 1. Non-expiring messages via a sentinel (**not** NULL)

Read brd D11 first. `ttl_sec` today is **required** and capped at 24 h
(`models.py:25`: `ttl_sec: int = Field(gt=0, le=24*3600)`) — there is no default
to preserve — and `messages.expires_at` is **`NOT NULL`** (`db.py:47`). Dropping
that constraint means a full SQLite table rebuild plus None-guarding four readers
(`app.py:514-522`, `app.py:2159-2161`, `reaper.py:316`, `client_cli.py:163`).
Don't.

Instead: `CreateMessageRequest` gains `never_expires: bool = False`. When true the
server stores `expires_at = NEVER` — a module-level constant
`9999-12-31T00:00:00Z` — and ignores `ttl_sec`. Every existing sender is
untouched: same required field, same cap, same behaviour.

Then **assert the consequence rather than assuming it**: the expiry pass selects
`state='open' AND expires_at < now`, which the sentinel never satisfies. Add the
test, and confirm `messages_state_expiry (state, expires_at)` still serves
ordinary rows. Keeping the sentinel in one constant makes a later move to real
NULL a single-site change.

### 2. `GET /v1/answers?after=<id>&wait=N`

Installation-authenticated, like every other client endpoint.

- Returns answered rows for this installation with `id > after`, **ordered by
  `id` ascending**, capped at 500 (architecture §7). Each row:
  `{id, kind, answer_text, via, option_idx, answered_at}`.
- `via` and `option_idx` come from wherever `_record_answer` already stores the
  button-vs-free-text distinction; reuse it rather than inventing a second
  encoding — the client maps an option index back to a label from its own copy.
- Parks on a waiter registry **keyed by `installation_id`** when there is nothing
  newer, returning `204` on timeout. `waiters.py` is keyed by `message_id` today;
  add a second registry rather than overloading the first, and wake it from the
  same place `_record_answer` already wakes the message waiter.
- `after=0` is a legitimate full replay (index recovery) — the page cap is what
  bounds it; the client pages by advancing `after`.

Add index `messages_answer_feed` covering the scan; confirm with `EXPLAIN QUERY
PLAN` in a test that the feed does not table-scan.

**Scoping is by installation, not chat.** Several installations bind to one chat;
an answer belongs to the machine that sent the message. Test this explicitly with
two installations on one chat.

### 3. Client method

`RelayClient.get_answers(after, wait, token=None)` in `client.py`, following the
existing long-poll method's shape (timeouts, retry, `204` handling).

## Done when

- `never_expires: true` round-trips and the row survives repeated reaper ticks;
  every existing sender behaves byte-identically.
- Two installations on one chat receive only their own answers.
- A long-poll parked on the feed wakes within milliseconds of an answer being
  recorded.
- `after=0` replays everything, paged, in id order.
- Migration (indexes only — no table rebuild) applies cleanly forward on a copy
  of a real relay DB.

## Tests

Relay-side, `FakeTelegramBackend`, no network. A `never_expires` row vs repeated
full reaper ticks; `ttl_sec` still required and still capped; feed ordering, paging and the cap; installation scoping with a shared chat;
waiter wake on `_record_answer`; `EXPLAIN QUERY PLAN` on the feed query; every
`expires_at` reader against a NULL row. Regression floor: existing message,
permission and question flows byte-identical.
