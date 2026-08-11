# 19-04 — Nudge engine

**Status:** todo · **Depends on:** 19-01, 19-03
**Read first:** [brd.md](./brd.md) §2.7, §2.10, §5, §6 ·
[state.md](./state.md) invariants 4, 5, 6, 7, 8

## Goal

An opt-in reply-message that re-notifies about a prompt still waiting, on a
ladder measured in active time, coalesced so a busy chat gets one nudge and not
four, and deleted the moment its target resolves.

**Off by default.** With no `/nudge on`, `next_nudge_at` is NULL everywhere and
the new reaper pass touches nothing — the chat is byte-for-byte as it is today.

## Scope

### Schema

**Already landed in 19-01** — `nudge_count`, `next_nudge_at`,
`nudge_tg_message_id` and the `messages_nudge_due` index. This task writes them;
it does not define them and must not add a migration.

### Config — `config.py`

`nudge_default_schedule` (default `"15m,45m,3h"`) and `nudge_max` (default 3),
both overridable per chat by `recipients.nudge_schedule`. Server-side only,
following the existing `reaper_interval` pattern of an optional attribute read
defensively.

### Backend — `telegram_backend.py`

`send_message` takes no `reply_to_message_id` and `send_text` is a bare send
(brd §2.10). Add one entry point — a reply-shaped send — on the real backend and
on `FakeBackend`, recording target and text so the reaper tests can assert on
both. Also needs `delete_message`, which already exists.

### Seeding `next_nudge_at`

- At create time, from the recipient's schedule via `advance_active`. NULL when
  that chat has nudges off, or when `advance_active` returns `None` (a
  never-active window — 19-01).
- **NULL for `kind='notification'`.** Idle sessions never nudge (brd §4.1).
  Reuse 19-03's `awaits_human` as the eligibility predicate rather than writing
  a second one — one definition of "waiting on a human", used by both the tag
  and the ladder, is what keeps them from drifting apart.
- Backfilled by `/nudge on` (19-02).
- Cleared on every terminal transition.

### The reaper pass — `reaper.py`

A second pass beside expiry, in the same tick:

1. Select `state='open' AND next_nudge_at IS NOT NULL AND next_nudge_at < now`.
2. Group by chat. **Resolve availability once per chat** (19-01), not per row.
   Inactive chat → push every due row to the next active start and emit nothing.
3. **Coalesce** (brd §5.3), two keys:
   - within a `group_id`: one nudge for the whole group, targeting the **first**
     member — never one per question;
   - within a chat: one nudge per tick, targeting the **oldest** open row, other
     due rows folded into a `+N more #unanswered` count.
4. Delete the target's previous nudge, send the new one, store its id on the
   target row, bump `nudge_count`, and set the next due time via
   `advance_active` from the ladder — stopping at the cap.
5. Pace sends per brd §2.7 (~1/s per chat); a slow chat must not stall the tick
   for other chats or delay the expiry pass.

### Ownership and cleanup

- **A nudge belongs to exactly one row** — the one it replies to (brd §5.5).
  Rows folded into a `+N more` own nothing and simply get their due time pushed.
- **Deletion hangs off the state transition**, in the same place 19-03 strips
  the tag: answer, group finalize, cancel, expiry. One chokepoint, four callers.
  Nothing may depend on a *hook* to clean up — a machine that sleeps mid-request
  would leak the nudge forever.
- When the owner resolves while others are still pending, the nudge is deleted
  with it; the next tick nudges whatever is now oldest. Do not attempt to
  re-target a live nudge.

### Text

Short, and deliberately not a copy of the prompt: the reply-quote already shows
it. Something on the order of "⏳ still waiting — <first line of the body>",
plus the `+N more` clause when it speaks for several. It carries **no
keyboard** — the buttons live on the original, which is one tap away through the
quote.

## Implementation notes

- **Nudges are not `messages` rows.** They have no state, no answer, no TTL and
  no waiter; they are a Telegram id hanging off the row they serve. Making them
  first-class rows would put them in every open-message query in the codebase,
  including the ambiguity counter and the expiry sweep.
- **`nudge_count` caps the ladder**, and the cap is checked before send, not
  after — an off-by-one here is a fourth 03:00 notification.
- **Best-effort throughout.** A failed send or delete logs and continues; the
  tick must never die and a nudge failure must never affect expiry.
- A nudge whose delete fails leaves a stale id — on the next transition, attempt
  the delete again and clear the column regardless.
- **`TelegramForbidden` is terminal, not transient.** The bot was blocked or
  removed. Every other path in `app.py` responds by calling
  `_unbind_installation`; the reaper has no request to fail, so it must instead
  stop: clear `next_nudge_at` for that chat's open rows rather than retrying
  every 30 s forever against a chat that will never accept another message.
  Whether the reaper should also unbind is an open call — prefer *not* to
  (unbinding from a background sweep would silently disconnect a machine whose
  user merely archived the chat) and log loudly instead.

## Testing

`relay-server/tests/test_reaper.py`, inline-tick pattern, `FakeBackend`:

- Nudges off → due rows are never selected; no sends.
- An idle notification with `reply_required=True`, nudges **on**, left open past
  every ladder interval → never nudged, `next_nudge_at` NULL throughout.
- On, active, due → exactly one reply-send at the right target.
- **Group of four, all due → exactly one nudge**, targeting the first member.
- **Four chats' worth of rows due in one tick → four nudges (one each); four
  rows in one chat → one nudge with `+3 more`.**
- Inactive hours → nothing sent; due times pushed to the next window start; the
  nudge then fires there (the brd §7 18:50 → 09:20 case, asserted end to end).
- Ladder: three nudges then silence at the cap; each send deletes the prior.
- Cleanup on all four transitions, including expiry in the same tick that would
  otherwise nudge.
- Send failure and delete failure → tick completes, expiry still runs.
- Never-active window → `next_nudge_at` NULL, no nudge, no crash.
- `TelegramForbidden` on a nudge send → that chat's open rows stop being due;
  the next tick makes no further attempt; expiry still runs.

## Done criteria

- [ ] With nudges off, no query, no send, no column write — verified, not assumed.
- [ ] One nudge per group and one per chat per tick, both tested.
- [ ] Ladder measured in active time, capped, each nudge replacing the last.
- [ ] Every terminal transition deletes the nudge, including the two that
      originate on the client.
- [ ] The reaper's expiry pass is unaffected by any nudge failure.
- [ ] Nothing under `.claude/hooks/` changed.
