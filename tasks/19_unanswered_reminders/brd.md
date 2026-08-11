# Epic 19 — Unanswered prompts: hashtag recall, availability hours, nudge-replies

**Status:** planning · **Owner:** Anton · **Created:** 2026-08-11 · **Rev:** 2

> Broken down into tasks — see [state.md](./state.md) for ordering and
> cross-task invariants.

## 1. Problem & thesis

Permission prompts and questions get missed in the Telegram chat, most often
when several sessions are live at once. Two failure modes hide behind the one
complaint, and they want different answers:

1. **The notification was missed.** The message is intact and correctly
   formatted, just scrolled away. Needs a *fresh notification*.
2. **It is buried under concurrent sessions.** Three sessions posted; the newest
   got answered; the older two are somewhere above. Needs a *way to enumerate
   what is outstanding* — another ping only adds to the pile.

**Thesis:** the relay already owns the exact state both answers need. The
`messages` table has `state='open'`, and `reaper_tick` sweeps that predicate
every 30 s. So:

- **(2)** is solved by making every open message carry an **`#unanswered`
  hashtag**, which Telegram's own hashtag search turns into a live list. No new
  message, no pin, no digest to keep in sync — the tag *is* the index, and it
  disappears when the message resolves.
- **(1)** is solved by an opt-in **nudge-reply**: a short message replying to the
  unanswered one, which notifies and whose reply-quote is a one-tap jump back to
  the live buttons.

And because a nudge is by definition an interruption, both are gated by
**availability hours** per person — the first time this system has any notion of
*when* a human is reachable, rather than only *whether*.

## 2. Constraints discovered

**2.1 — A "person" is a chat, not an installation.** `architecture.md` is
explicit that many installations bind to the same chat (that is how one operator
runs several machines). Timezone, hours and nudge-enable are therefore keyed on
`telegram_chat_id`, not `installations.id`. Epic 15's `roles.toml` is the wrong
home too: it is *client-side workspace vocabulary*, unreadable by the reaper,
which is the process that has to decide whether it is 03:00 for the recipient.

**2.2 — Terminal transitions are spread over four code paths, and only some of
them rewrite the message text.**

| Path | Where | Touches text today? |
|---|---|---|
| Client bakes its own answer | `telegram_permission_router.py:806` `finalize_message` — PATCH text, then cancel | yes, with text the **hook** composes |
| Relay finalizes a group | `app.py` `_finalize_group_if_complete` | yes |
| Cancel endpoint | `app.py:760` | no — `edit_reply_markup` only |
| Reaper expiry | `reaper.py` — deliberate "Option A" | no — keyboard strip only |

So "the edit we already do removes the tag" does **not** hold for two of the
four. The consequence is §4.2: the tag must be *relay-owned*, not hook-authored.

**2.3 — Hashtags must be plain text.** Telegram does not linkify a `#tag` inside
`<pre>`/`<code>`, and the relay sends HTML parse mode with code blocks in the
body (task 02-03). The tag therefore lives on its own trailing plain-text line.

**2.4 — Hashtag search scope varies by client, and that is accepted.** Tapping a
hashtag opens Telegram's hashtag search; whether it defaults to *this chat* or
global differs across iOS / Android / Desktop. **Decided (Anton, 2026-08-11):
either behaviour is fine.** If a global scope proves noisy in practice the
remedy is a more distinctive tag (`#unanswered_cc`), which is a one-line
constant change, not a design change. This is therefore not a gate on shipping.

**2.5 — A bot cannot deep-link to a message in its own private chat.** There is
no `t.me` form that jumps to a message in a bot DM, which is why a `/unanswered`
command that *lists* pending items is strictly weaker than the hashtag: it can
summarize but not navigate. Hashtag is the mechanism; a command is only a hedge.

**2.6 — 4096-character cap.** Bodies already approach it (long diffs, code
blocks). Appending the tag line must be truncation-aware, not blind.

**2.7 — Rate limits.** ~1 message/s per chat. Nudges are emitted from a single
reaper tick that may find many due at once; the pass must batch and pace rather
than fan out.

**2.8 — Every re-render reads `payload_json`, which PATCH never updates.** Four
call sites rebuild a message body from `_payload_for(row)["text"]`: group
finalization (`app.py:1289`), the multi-select toggle (`:1598`), the grouped
reply paths (`:1690`, `:1726`). The PATCH endpoint (`:695`) sends the client's
text straight to Telegram and **does not write it back to `payload_json`** —
so the stored payload and the visible message already diverge the moment a hook
calls `finalize_message`. Latent today; load-bearing here, because §4.3 gives
cancel and expiry a text edit and they have no other source for the body. This
is the finding that forces §4.2's invariant.

**2.9 — `finalize_message` PATCHes *then* cancels** (`router.py:806`, and the
order is deliberate — a cancelled message can still be edited). The PATCH lands
while the row is still `open`, so a naive implementation re-appends the tag and
then the cancel — keyboard-only today — leaves it there permanently. Cancel is
the path that must remove it, on the very message whose text it did not author.

**2.10 — The backend has no reply-to.** `send_message` takes no
`reply_to_message_id` and `send_text` is a bare send. A nudge needs a new
backend entry point, mirrored in `FakeBackend` so the reaper tests can assert on
what was sent and to which target.

## 3. Availability hours

The gate for everything that interrupts. New server-side state, per chat.

**3.1 — Model.** Per `telegram_chat_id`: an IANA timezone (`Europe/Berlin`) and
a set of weekly windows (`mon-fri 09:00-19:00`, `sat 11:00-15:00`). Stdlib
`zoneinfo` does DST; nothing is stored in UTC-offset form, because an offset
stored in March is wrong in November.

**3.2 — Defaults.** No configuration = **always available**. Availability only
subtracts; it never enables anything that was off. Combined with nudges being
off by default (§4.1), an unconfigured chat behaves exactly as it does today.

**3.3 — Configuration surface.** Bot commands, because a team member may have
no shell access to the relay host and no `roles.toml` of their own:

```
/tz Europe/Berlin
/hours mon-fri 09:00-19:00, sat 11:00-15:00
/hours off              → always available
/nudge on | off | 15m,45m,3h
/me                     → tz, hours, whether currently active, nudge schedule
```

Validated against `zoneinfo.available_timezones()`, echoed back with the next
active window resolved to a concrete local time so a typo is visible immediately.
`relay-admin` gets read/write equivalents for the operator; `claude-roles` grows
a column so the client-side diagnostic can show who is reachable when.

These are **relay-local** commands: they read and write server state and involve
no machine, so they are handled directly in the webhook beside `/bind` and carry
**no dependency on epic 16's command queue**. `app.py:1359` currently ignores
every slash-command except `/bind` specifically so they cannot be mistaken for
answers; this is an additive branch above that guard. `/me` rather than
`/status` deliberately — epic 16's listener has a status concept and the two
should not collide.

**3.4 — Active-time arithmetic is the core primitive.** The requirement "nudges
repeat during active hours and pause for the duration of inactive hours" is not
"skip a nudge that falls at night" — it is a clock that only runs while the
person is available. One function, heavily tested, used everywhere:

```
advance_active(now, delta, tz, windows) -> datetime
    the wall-clock instant at which `delta` seconds of *active* time
    will have elapsed, starting from `now`
```

A message raised at 18:50 with a 30 m nudge delay and a window ending at 19:00
nudges the next morning at 09:20, not at 19:20 and not at 09:00. A message
raised at 02:00 gets its first nudge at 09:00 + delay.

**3.5 — TTL stays wall-clock** (agreed, Anton 2026-08-11). `expires_at` is not
paused by inactive hours.
The TTL expresses how long the *agent* is willing to block (12 h today), which
is a property of the session, not of the human's calendar. Consequence, stated
plainly because it is a real one: an overnight prompt may expire having received
one nudge or none. If that proves wrong in practice, the fix is a longer TTL,
not an active-time TTL — see §8.

## 4. The `#unanswered` tag

**4.1 — Rule.** A message carries the trailing `#unanswered` line iff it is
`state='open'`, its `kind` is **not** `notification`, and it is actually
awaiting a human (`reply_required`, or a keyboard, or a `group_id`). Every
transition out of `open` removes it.

**Idle-session notifications are excluded** (decided, Anton 2026-08-11).
`notification_hook` sends them with `reply_required=True` whenever
reply-from-Telegram is on, so the naive predicate would have swept every idle
session into the tag *and*, once §5 lands, nudged about it. `#unanswered` means
**an agent is blocked on you** — not "a session finished and you may want to
look". An idle session left overnight must never produce a 09:00 ping. Finding
idle sessions stays a scrolling problem, as today.

This is why the rule keys on `kind` and not on `reply_required` alone; anyone
tempted to simplify it back should read this paragraph first.

**4.2 — The relay owns the tag, and one render function owns the append.** Two
rules, together closing §2.8 and §2.9:

- **`payload_json.text` is the canonical body, always untagged.** Every writer
  keeps it current — which means **PATCH must now persist the client's text into
  `payload_json`**, not just forward it to Telegram. Without this, the text edit
  §4.3 adds to cancel would re-render from a stale payload and silently wipe the
  `✍️`/`✅` answer the hook had just baked in.
- **The tag exists only in the render layer.** A single `render_body` used
  by *every* send and *every* edit appends the tag iff the row is `open` and
  awaiting a human. No call site decides for itself; the four re-render sites in
  §2.8 become callers of it. Idempotent by construction — a retried PATCH cannot
  double the tag, and a client that types `#unanswered` into its own text is
  stripped and re-rendered, not doubled.

The client keeps composing whatever text it likes and stays entirely unaware the
tag exists. That is what makes §2.2's four-path spread survivable.

**4.3 — Two of the four paths grow a text edit.** The cancel endpoint
(`app.py:760`) and the reaper's expiry both currently strip only the keyboard;
both must now rewrite the text to drop the tag, sourcing the body from the
payload per §4.2. This reverses task 05's "Option A" choice for expiry, and the
reversal should be recorded there: the extra `editMessageText` per expiry was
rejected as unjustified rate-limit surface, and this epic is the justification.

Both edits must tolerate Telegram's "message is not modified" 400 the way
`patch_message` and `cancel_message` already do (`_is_not_modified`) — an
untagged message being re-cancelled is the common, correct case, not a fault.

**4.4 — Variants, decided later.** A per-workspace companion tag
(`#unanswered #claudehooks`) is attractive when several projects are live in one
chat, and cheap — but it depends on §2.4's verification and on the relay not
currently knowing the workspace (the session name is composed client-side into
the body). Deferred, not rejected.

## 5. Nudge-replies

**5.1 — Opt-in, per person, off by default.** `/nudge on` with an optional
schedule. Absent configuration, nothing about today's behaviour changes.

**5.2 — Shape.** A short plain message sent with `reply_to_message_id` pointing
at the unanswered message. Not a repost: the original keeps its position, its
buttons and its id, so callbacks, threaded replies and the client's PATCH path
are all untouched. The reply-quote is the affordance — tapping it jumps to the
live message.

**5.3 — One nudge per series.** Two levels of coalescing, and the distinction
matters because they have different keys:

- **Within a group** (`group_id` — an AskUserQuestion that spans several
  messages): one nudge for the whole group, replying to the **first** member.
  Never one per question.
- **Within a chat**: at most one nudge per reaper tick, replying to the
  **oldest** open target, with the others carried as a count
  (`+2 more #unanswered`) rather than as their own messages.

> Assumption to confirm: "series of multiple questions" was read as the
> group case, with the chat-level burst rule added because the same tick can
> otherwise emit four notifications for four sessions — which is the original
> complaint in a new costume. If only the first was meant, the chat-level rule
> is separable and can be dropped without touching anything else.

**5.4 — Ladder, not a metronome.** Default `15m, 45m, 3h`, measured in *active*
time (§3.4), capped at 3 by default. Each new nudge deletes its predecessor:
one live nudge per target at any moment, so a long-pending prompt does not grow
a column of identical reminders.

**5.5 — A nudge is owned by exactly one row.** The chat-level nudge of §5.3
belongs to the message it replies to — the oldest open target — and only that
row carries `nudge_tg_message_id`; the rows folded into its `+N more` have their
`next_nudge_at` pushed but spawn nothing of their own. When the owner resolves,
its nudge is deleted along with it, and the next tick nudges whatever is now
oldest. Without this rule "delete the nudge on resolve" is undefined for a nudge
that spoke for several messages.

**5.6 — A reply aimed at the nudge must still land.** People reply to the
message that notified them. Today `_handle_update` looks the reply target up in
`messages` by `telegram_message_id`, finds nothing for a nudge, and **returns
silently** (`app.py:1345`) — the answer is swallowed with no feedback. So nudge
ids must resolve to their target row on the reply path. Related: the
loose-reply ambiguity counter (`_distinct_open_targets`) must not count nudges,
or a single pending prompt plus its nudge will start reporting itself as
ambiguous and refuse plain replies.

**5.7 — Cleanup is the sharp edge.** The nudge must be deleted when its target
leaves `open` — via any of §2.2's four paths, including the two that live on the
client. Deletion hangs off the same server-side state transition that strips the
tag, so the two features share one chokepoint. Anything that relies on the
*hook* to clean up will leak nudges when a machine sleeps mid-request. This is
the part most worth adversarial tests against the fake backend.

## 6. Data & placement

- **Schema v3.** New table keyed by `telegram_chat_id` (tz, windows, nudge
  enable, schedule). New columns on `messages`: `nudge_count`,
  `next_nudge_at`, `nudge_tg_message_id`. The `messages_state_expiry` index
  generalizes to cover `next_nudge_at`.
- **Reaper.** A second pass beside expiry: rows `state='open' AND
  next_nudge_at < now`, filtered by the recipient's availability, paced per
  §2.7. No new process, no new long-poll, no client-side timer. Availability is
  resolved once per chat per tick, not once per row.
- **Seeding `next_nudge_at`.** Written at create time from the recipient's
  schedule, `NULL` when that chat has nudges off — which is what keeps the
  default path free of any new work. `/nudge on` backfills the open rows in
  that chat, otherwise turning nudges on would do nothing until the next
  prompt. Group collapsing (§5.3) happens at tick time, not at seed time: all
  members get a due time, the tick emits one nudge for the group.
- **Config.** Server-side knobs for the defaults (`nudge_default_schedule`,
  `nudge_max`), per §3.3 overridable per chat.

## 7. Success criteria

- [ ] An open permission prompt shows `#unanswered`; tapping it lists every
      currently-open prompt and nothing that has been answered, denied,
      cancelled or expired.
- [ ] The tag disappears on all four transition paths, including terminal-side
      resolution and silent expiry.
- [ ] A hook that PATCHes its own finalized text cannot leave a stale tag behind
      or produce a doubled one — and the `✍️`/`✅` answer it baked in **survives**
      the cancel that follows (§2.8/§2.9: the regression this epic could most
      easily introduce).
- [ ] A multi-select toggle re-renders without losing or duplicating the tag.
- [ ] With nudges off (the default), the chat is byte-for-byte as it is today.
- [ ] With nudges on, an unanswered prompt raised at 18:50 nudges the next
      morning inside the window, not at 19:20 and not at 09:00 sharp.
- [ ] An AskUserQuestion spanning four messages produces exactly one nudge.
- [ ] Four sessions going idle in the same tick produce one nudge, not four.
- [ ] Answering, denying, resolving-in-terminal or expiring deletes the nudge.
- [ ] Replying to the *nudge* answers the prompt it points at (§5.6), and a
      prompt plus its own nudge is never reported as an ambiguous target.
- [ ] `/tz`, `/hours`, `/nudge`, `/status` round-trip and echo the next active
      window as a concrete local time.

## 8. Out of scope

- **Pinned pending-digest.** Superseded by the hashtag: the tag needs no
  reconciliation and cannot drift from the truth it describes.
- **Delete-and-repost.** Rejected in the design discussion — it churns
  `telegram_message_id`, loses reading position, and worsens failure mode (2).
- **Forum topics / one thread per session.** The strongest structural fix for
  cross-session confusion and worth its own epic; it touches bindings, send,
  reply-attribution and epic 16's listener, so it must not ride along here.
- **Quiet-hours applied to ordinary message delivery** (`disable_notification`
  outside active hours for *all* messages, not just nudges). Natural next use of
  §3, deliberately not in the first cut.
- **Active-time TTL** (§3.5).
- **Escalation to another person** when the first is unavailable — epic 15
  already owns `escalate_after` and should absorb availability as an input
  rather than this epic growing a second routing engine.
