# 19-02 — Preference commands

**Status:** todo · **Depends on:** 19-01
**Read first:** [brd.md](./brd.md) §3.2, §3.3 · [state.md](./state.md) invariants 3, 4, 9

## Goal

The only way a team member configures themselves: four bot commands, handled in
the webhook beside `/bind`, writing the `recipients` row 19-01 created.

**Relay-local.** No machine is involved, so there is **no dependency on epic
16's command queue** (state.md invariant 9). `app.py:1359` already ignores every
slash-command except `/bind` specifically so they cannot be mistaken for
answers; this is an additive branch above that guard.

## Scope

### Commands — `app.py` `_handle_update`

```
/tz Europe/Berlin
/hours mon-fri 09:00-19:00, sat 11:00-15:00
/hours off                  → always available
/nudge on | off | 15m,45m,3h
/me                         → current settings, resolved
```

Dispatch beside `_handle_bind_command`, before the free-text answer paths, and
**return** — a preference command must never be attributed as an answer to an
open message.

### Authorization

Same trust boundary as everywhere else: only the chat's `bound_user_id` may
write. An unbound chat gets "send me a `/bind` code first" and no row is
created. Anyone else in the chat is ignored silently (matching how the loose
reply path treats non-bound senders).

### Echo — the whole point of the feature

Every write echoes the **resolved** result, not a confirmation. `/hours` replies
with the canonical spec *and* the next active window as a concrete local time
("active now, until 19:00" / "inactive — next window Mon 09:00"). A typo in a
timezone or an inverted range is then visible in one line instead of surfacing
three days later as a nudge that never fires.

`/me` renders tz, canonical windows, active-now, nudge on/off and the schedule,
including which values are defaults rather than set.

### Errors

19-01 returns structured reasons; this task turns them into sentences. Unknown
timezone lists three near-matches from `zoneinfo.available_timezones()` rather
than dumping 600. An unparseable window spec shows the accepted grammar with one
example. Never partially apply a multi-clause `/hours`.

### `relay-admin`

Read/write equivalents so the operator can inspect and fix a chat without
Telegram: list recipients with their settings, set/clear tz, windows, nudge
enable and schedule for a chat id.

### `GET /v1/installations/me` — new fields

19-06's `claude-roles` column has no other way to see this. The endpoint
currently returns four fields (`app.py:298`) and `claude-roles --check` already
probes it (`shell/claude-roles:122`), so extending it is the whole API surface
needed: add the calling installation's recipient state — tz, canonical windows,
`active_now`, `nudge_enabled` — to `InstallationMeResponse`, all nullable so an
unconfigured chat and an old client both degrade to blanks.

## Implementation notes

- **Backfill on enable.** `/nudge on` seeds `next_nudge_at` on the chat's
  already-open rows (brd §6) — otherwise turning nudges on does nothing until
  the next prompt, which reads as broken. The columns exist as of 19-01, so this
  is an unconditional write with no version guard. The scheduling call is
  `advance_active` from 19-01; 19-04's ladder constants are not needed here
  (the *first* interval is all a backfill sets).
- **Nudge schedule parsing** reuses 19-01's duration handling. Reject a schedule
  with more entries than the server cap rather than truncating silently.
- **Idempotent writes.** Sending the same `/hours` twice is a no-op plus an echo.

## Testing

`relay-server/tests/test_webhook.py`:

- Each command from the bound user writes the expected row and echoes.
- Each command from a non-bound sender writes nothing.
- A preference command while a message is open is **not** recorded as its
  answer — assert the message stays `open`. (The regression this guard exists
  for.)
- Bad tz / bad window spec / inverted range → error text, no row written, no
  partial application.
- `/hours off` clears windows; `/me` reflects it.
- `/nudge on` backfills open rows in that chat and only that chat.
- `relay-admin` round-trips the same state.

## Done criteria

- [ ] Four commands work end to end against `FakeBackend`.
- [ ] Every echo names a concrete local time, not just a confirmation.
- [ ] No command can be swallowed as an answer, and no answer path regressed.
- [ ] Unbound and non-bound-user cases write nothing.
- [ ] `relay-admin` can read and write every field.
- [ ] `/v1/installations/me` exposes availability; an unconfigured chat returns
      nulls rather than erroring.
- [ ] No dependency on epic 16 code was introduced.
