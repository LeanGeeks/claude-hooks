# 19-06 — Diagnostics, docs, installer

**Status:** todo · **Depends on:** 19-02, 19-05
**Read first:** [brd.md](./brd.md) §3.3, §4.3 · [state.md](./state.md)

## Goal

Make the epic legible from outside: a diagnostic that shows who is reachable
when, documentation for the commands, and the two records that keep earlier
design decisions honest.

## Scope

### `shell/claude-roles`

Grows an availability column: per destination, the recipient's timezone,
whether they are active right now, and whether nudges are on. This is the one
sanctioned client-side read in the epic (state.md invariant 8) — a diagnostic,
not a behaviour.

The data arrives through the fields 19-02 added to `GET /v1/installations/me`,
which `--check` already probes (`shell/claude-roles:122`); there is **no new
client-side computation** — `active_now` is resolved server-side in the
recipient's timezone, because the querying machine's clock and zone are
irrelevant and would be wrong. A relay predating 19-02 omits the fields, so the
column must render blank on a missing key rather than raising.

### `relay-admin`

Whatever 19-02 did not already cover: a listing that shows, for each chat,
tz / windows / active-now / nudge state — the operator's view of the same table.

### `docs/availability.md`

New, user-facing, written for a team member with no shell access to the relay:
the four commands, the window grammar with worked examples, what "active time"
means for the nudge ladder, and the one thing that surprises people — TTL is
wall-clock, so an overnight prompt can expire having been nudged once or not at
all (brd §3.5).

### Top-level `architecture.md`

- `recipients` in the schema section; the three new `messages` columns.
- The reaper's second pass, beside the expiry pass already described.
- The relay-local command surface, distinguished from epic 16's queue-backed
  commands so the two are not confused later.
- `#unanswered` as a documented, relay-owned property of message text, with the
  render invariant (brd §4.2) stated where someone editing `app.py` will see it.

### Record the reversal — `tasks/05_telegram_prompt_lifecycle_management.md`

Task 05 chose "Option A" for expiry: strip the keyboard, never edit the text,
explicitly to avoid the extra `editMessageText`. 19-03 reverses it. Add a dated
note there pointing at brd §4.3 — a future reader finding an `editMessageText`
in the expiry path deserves to find out why rather than "fixing" it back.

### Installer

Confirm `install-claude-config.sh` needs **no** change (nothing client-side was
added) and say so in the task's completion note. If that turns out false, the
epic has broken invariant 8 and that is the finding, not a quiet fix.

## Testing

Documentation is verified by 19-07, not by unit tests. What is testable here:

- `claude-roles` against a relay without the new fields → blank column, exit 0.
- `claude-roles` against a configured chat → shows the server's `active_now`
  verbatim; assert it does **not** recompute locally (a machine in another zone
  must print the same answer).
- `relay-admin` listing round-trips what 19-02 wrote.

## Done criteria

- [ ] `claude-roles` shows availability and degrades cleanly against an old relay.
- [ ] `docs/availability.md` exists and covers all four commands plus the
      wall-clock TTL caveat.
- [ ] `architecture.md` documents the table, the columns, the second reaper pass,
      the command surface and the render invariant.
- [ ] Task 05 carries a dated note recording the Option-A reversal.
- [ ] The installer is confirmed unchanged, in writing.
