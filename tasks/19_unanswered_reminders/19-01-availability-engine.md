# 19-01 — Schema + availability model + active-time engine

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §2.1, §3 · [state.md](./state.md) invariants 3–5

## Goal

The storage and the arithmetic for "is this person reachable right now, and when
next?". A table keyed by chat, a parser for timezones and weekly windows, and
one function — `advance_active` — that every scheduling decision in the epic
calls.

**This task owns the epic's entire schema change**, including the three
`messages` columns 19-04 will use. One migration, one version bump, one place to
reconcile against epic 16. Splitting them across two tasks was the earlier plan
and it forced 19-02 to write a column that might not exist yet.

**This task touches no Telegram code and sends nothing.** No commands (19-02),
no nudges (19-04). It ends with a pure module and a migration that together can
answer the two questions the rest of the epic asks.

## Scope

### Schema — `db.py`

`SCHEMA_VERSION` 2 → 3 **or** 3 → 4 — see [state.md](./state.md) "Cross-epic
conflict". Pick the number at implementation time against what is on `main`,
and put it in exactly one place.

```sql
CREATE TABLE IF NOT EXISTS recipients (
    telegram_chat_id  INTEGER PRIMARY KEY,
    tz                TEXT,             -- IANA name; NULL = unset
    windows_json      TEXT,             -- canonical windows; NULL = always available
    nudge_enabled     INTEGER NOT NULL DEFAULT 0,
    nudge_schedule    TEXT,             -- "15m,45m,3h"; NULL = server default
    updated_at        TIMESTAMP NOT NULL
);
```

Keyed on the chat, **not** `installations.id` — brd §2.1. A row is created
lazily on first preference write; its absence is a valid, meaningful state
("unconfigured", i.e. always available and nudges off).

Plus the nudge columns on `messages` (used by 19-04, defined here):

```sql
ALTER TABLE messages ADD COLUMN nudge_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN next_nudge_at TIMESTAMP;          -- NULL = never nudge
ALTER TABLE messages ADD COLUMN nudge_tg_message_id INTEGER;
CREATE INDEX IF NOT EXISTS messages_nudge_due ON messages(state, next_nudge_at);
```

All three are nullable or defaulted, so the migration is `ALTER TABLE` with no
table rebuild. Add them to `SCHEMA_SQL`'s `messages` definition too, so a fresh
database and a migrated one converge — `test_schema.py` should assert exactly
that. Leave `messages_state_expiry` alone; expiry still uses it.

`MIGRATIONS[n]` is the `CREATE` plus the three `ALTER`s — there is no data to
move.

### New module — `relay_server/availability.py`

Pure: no DB, no network, no clock reads except the `now` handed in. Importable
by anything (state.md, "Relationship to epic 15").

```python
parse_tz(name)            -> str | None          # validated via zoneinfo
parse_windows(spec)       -> list[Window] | None # "mon-fri 09:00-19:00, sat 11:00-15:00"
format_windows(windows)   -> str                 # canonical round-trip form
is_active(now, tz, windows)            -> bool
next_active_start(now, tz, windows)    -> datetime | None
advance_active(now, delta, tz, windows) -> datetime
```

`Window` is `(weekday, start_minute, end_minute)` in **local** time. Nothing is
stored as a UTC offset: an offset captured in March is wrong in November.

### `advance_active` — the core primitive

Returns the wall-clock instant at which `delta` seconds of *active* time will
have elapsed, starting from `now`. Not "skip a nudge that lands at night" — a
clock that only runs while the person is available (brd §3.4).

Required behaviours, each a test:

| `now` | window | `delta` | result |
|---|---|---|---|
| 09:00 Mon | 09:00–19:00 daily | 30 m | 09:30 Mon |
| 18:50 Mon | 09:00–19:00 daily | 30 m | 09:20 Tue |
| 02:00 Tue | 09:00–19:00 daily | 30 m | 09:30 Tue |
| 18:00 Fri | mon–fri only | 4 h | 12:00 Mon |
| any | `windows is None` | `delta` | `now + delta` (always available) |
| any | empty window list | any | never active — see below |

**Never-active is a real input.** `/hours` can be given a spec that parses but
never opens (`sat 11:00-11:00`). `advance_active` must not loop: bound the
search at 14 days and return `None`, and let 19-04 treat `None` as "do not
schedule" rather than "schedule at the epoch".

Also required: **DST transitions** — a window spanning a spring-forward gap
loses an hour of active time and must not double-count or skip a day; a
fall-back repeat must not credit the hour twice. Use `zoneinfo` and normalize
via UTC arithmetic between local-time boundary computations rather than adding
naive `timedelta`s to local wall times.

### Accessor — `relay_server/db.py` or a small helper in `app.py`

`load_recipient(conn, chat_id)` returning a parsed record with defaults applied
(tz `None`, windows `None`, nudges off). One lookup per chat per reaper tick is
the access pattern 19-04 will use, so it must be cheap and side-effect free.

## Implementation notes

- **Parse once, at write time.** `windows_json` holds the canonical, already
  validated form. The reaper must never parse user text.
- **Window spec grammar**, deliberately small: comma-separated clauses, each
  `<day-or-range> <HH:MM>-<HH:MM>`, days `mon…sun`, ranges inclusive and
  wrapping (`fri-mon` is legal). Reject rather than guess; the error string is
  19-02's problem, so return a structured reason, not a sentence.
- **Windows crossing midnight** (`22:00-02:00`) split into two stored windows at
  parse time. Every consumer then sees non-wrapping windows only, which is what
  keeps `advance_active` tractable.
- **`now` is always passed in.** No `datetime.now()` inside this module — that
  is what makes the table above testable without freezing the clock.

## Testing

New `relay-server/tests/test_availability.py`:

- The table above, case by case.
- Round-trip: `format_windows(parse_windows(s)) == canonical(s)` for a dozen
  specs including midnight-crossing and wrapping day ranges.
- Rejection cases: bad tz, `25:00`, inverted times, unknown day, empty string.
- DST: spring-forward and fall-back in `Europe/Berlin`, asserted on
  `advance_active` output, not on internals.
- Never-active spec terminates and returns `None`.
- Property-ish: for random `now`/`delta` with an always-available config,
  `advance_active(now, d) == now + d`.

Extend `test_schema.py`: fresh DB gets `recipients` and the three `messages`
columns; a v2 DB migrates to the same shape with existing message rows intact
(`nudge_count` 0, the other two NULL); fresh and migrated schemas are asserted
**identical**; version stamp is correct.

## Done criteria

- [ ] `recipients` and the three `messages` columns exist on fresh and migrated
      databases, and the two schemas are provably identical.
- [ ] Existing message rows survive the migration with sane defaults.
- [ ] `availability.py` is importable with no relay state and holds no clock.
- [ ] Every row of the behaviour table is a passing test, DST included.
- [ ] A never-active window returns `None` in bounded time.
- [ ] Nothing outside `db.py`, `availability.py` and the tests changed — in
      particular no endpoint, no reaper pass and no send path yet.
