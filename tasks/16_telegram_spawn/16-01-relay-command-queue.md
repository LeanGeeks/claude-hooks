# 16-01 — Relay command queue

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §2.1, §5.4 · [architecture.md](./architecture.md) §2.1–2.3

## Goal

The inbound half of the relay: a `commands` table, a long-poll endpoint a
listener parks on, a result endpoint it reports back through, and the two
`RelayClient` methods that call them.

**This task touches no Telegram code.** It does not parse `/new`, does not send a
message, does not render an ack. It moves a JSON payload from "something inserted
a row" to "the right machine claimed it and reported an outcome". 16-02 supplies
both ends. Keeping the split hard is what lets this task be tested with no bot
and no chat.

## Scope

### Schema — `db.py`

`SCHEMA_VERSION` 2 → 3, with the new table in `SCHEMA` and a `MIGRATIONS[3]`
entry that creates it (the migration for an existing DB is just the `CREATE`s —
there is no data to move).

```sql
CREATE TABLE IF NOT EXISTS commands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id     INTEGER REFERENCES installations(id),  -- NULL while targeting
    telegram_chat_id    INTEGER NOT NULL,
    telegram_user_id    INTEGER NOT NULL,
    telegram_message_id INTEGER,          -- relay-owned picker message (16-02)
    kind                TEXT NOT NULL,    -- spawn | resolve | ls
    payload_json        TEXT NOT NULL,
    state               TEXT NOT NULL,    -- pending|targeting|claimed|done|failed|expired
    result_json         TEXT,
    created_at          TIMESTAMP NOT NULL,
    claimed_at          TIMESTAMP,
    expires_at          TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_delivery ON commands(installation_id, state, expires_at);
```

### Models — `models.py`

```python
CommandKind  = Literal["spawn", "resolve", "ls"]
CommandState = Literal["pending", "targeting", "claimed", "done", "failed", "expired"]

class CommandResponse(BaseModel):        # GET /v1/commands
    id: int
    kind: CommandKind
    payload: dict[str, Any]
    expires_at: str

class CommandResultRequest(BaseModel):   # POST /v1/commands/{id}/result
    ok: bool
    summary: str = ""                    # one line, chat-ready (16-02 renders)
    detail: str | None = None            # multi-line, optional
    data: dict[str, Any] | None = None   # machine-readable; resolve/ls answers
```

### Endpoints — `app.py`

**`GET /v1/commands?wait=N`** (`require_installation`, `N` clamped to ≤30):

1. Claim atomically:
   `UPDATE commands SET state='claimed', claimed_at=?, expires_at=? WHERE id = (SELECT id FROM commands WHERE installation_id=? AND state='pending' AND expires_at>? ORDER BY id LIMIT 1) AND state='pending'`
   — a claim is accepted only when `rowcount == 1`, so two listeners racing (a
   restart overlap) cannot both take one command.
2. Hit → return `CommandResponse`.
3. Miss → park on the command waiter for this installation for up to `N`s, then
   retry the claim once and return `204` if still empty.

**Two-phase TTL.** `expires_at` before a claim is the *delivery* deadline
(brd §5.4 — 120 s for `spawn`, 10 s for `resolve`/`ls`). The claim rewrites it to
`claimed_at + command_max_run_s` (3600 default, server config) so a listener that
dies mid-wizard leaves a row the reaper can retire instead of one that is
`claimed` forever.

**`POST /v1/commands/{id}/result`** (`require_installation`): 404 unless the row
exists, is `claimed`, and `installation_id` matches the caller. Store
`result_json`, set `done`/`failed` from `ok`, notify the **result waiter** keyed
by command id. Returns 204.

### Waiters — `app.py` wiring only

Two more `WaiterRegistry` instances on `app.state`; `waiters.py` itself does not
change (it is already an int-keyed registry):

- `command_waiters` — keyed by `installation_id`, notified on insert/target so a
  parked `GET /v1/commands` wakes immediately.
- `command_result_waiters` — keyed by `command_id`, notified on result so 16-02's
  fan-out can await answers instead of polling.

Both need a `clear()` on terminal state so the dicts do not grow without bound.

### Reaper — `reaper.py`

One more sweep in `reaper_tick`: rows in `pending`/`targeting`/`claimed` past
`expires_at` → `expired`, and notify their result waiter so a fan-out awaiting an
answer from a dead listener is released at the deadline rather than at its own
timeout. Log counts the way the message sweep does.

### Client — `client.py`

```python
def poll_command(self, wait: int = 25) -> Command | None
def report_command_result(self, command_id: int, *, ok: bool,
                          summary: str = "", detail: str | None = None,
                          data: dict | None = None) -> None
def me(self) -> Installation          # GET /v1/installations/me
```

`Command` is a small frozen dataclass mirroring `CommandResponse`, alongside the
existing `MessageHandle` / `Answer`. `poll_command` returns `None` on 204 and
raises the existing `RelayError` family on transport failures — the listener
(16-05) owns backoff, not this method. Read timeout must exceed `wait` by the
same headroom `wait_for_answer` uses (`client.py:315`).

`me()` wraps the existing `GET /v1/installations/me` (`app.py:297`), which the
client cannot currently call at all. 16-05's `--status` needs the label and
`chat_bound` to answer "is this machine set up correctly", and it is three lines
here versus a raw request there.

### Insert helper

`create_command(conn, *, chat_id, user_id, kind, payload, installation_id=None,
ttl_s) -> int` lives next to the endpoints and is what 16-02 calls. Inserting
with an `installation_id` writes `pending` and notifies that installation's
waiter; inserting without one writes `targeting` and notifies nobody.

## Implementation notes

- Follow the existing `asyncio.to_thread`-wrapped `def _q()` / `def _w()` pattern
  used throughout `app.py`; do not introduce a second DB access style.
- `payload_json` / `result_json` are opaque JSON blobs to this layer. Do not
  validate their inner shape here — the envelope is agreed between 16-02 and
  16-05, and a schema check in the middle would just be a third place to update.
- Do not add a `commands` route to the OpenAPI examples or the admin CLI in this
  task.
- Server config gains `command_max_run_s` (default 3600) in `config.py`, read the
  same way as existing keys.

## Testing

Extend `relay-server/tests/` following its existing fixtures (fake backend, temp
DB):

- Claim is exclusive: two concurrent `GET /v1/commands` for one installation with
  one pending row → exactly one gets it, the other 204s.
- A command for installation A is never returned to installation B.
- Long-poll wakes on insert (park, insert, assert it returns well before `wait`).
- 204 on timeout with no rows.
- Result: happy path stores and flips state; wrong installation → 404; unclaimed
  row → 404; double report → 404 on the second.
- Two-phase TTL: unclaimed row past delivery deadline is reaped to `expired`;
  claimed row is reaped only after `max_run_s`.
- Reaper releases a result waiter when it expires a claimed row.
- Migration: a v2 database opens, migrates to v3, and keeps its existing
  messages/installations rows.

## Done criteria

- [ ] Schema v3 with the `commands` table, forward migration from v2 tested.
- [ ] `GET /v1/commands?wait=N` claims exclusively, long-polls, 204s on timeout.
- [ ] `POST /v1/commands/{id}/result` is restricted to the claiming installation
      and is idempotent-by-rejection (second report 404s).
- [ ] Delivery TTL and post-claim run TTL behave independently.
- [ ] Both waiter registries wake and are cleared on terminal states.
- [ ] Reaper expires stale commands and releases result waiters.
- [ ] `RelayClient.poll_command` / `report_command_result` / `me` land with tests.
- [ ] No Telegram-facing code changed in this task.
