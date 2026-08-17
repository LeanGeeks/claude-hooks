# Availability hours and nudge-replies

*Epic 19 — Unanswered reminders*

This document is written for a team member who has no shell access to the relay
host. Everything here is configured through bot commands sent to the Telegram
chat you already use for permission prompts.

---

## Overview

By default, the relay is always available and nudges are off — an unconfigured
chat behaves exactly as it did before epic 19. Availability only subtracts; it
never enables anything that was off.

Two features build on this:

- **`#unanswered` hashtag** — every open permission prompt or question carries a
  trailing `#unanswered` tag. Tapping it opens Telegram's hashtag search and
  lists everything that is still waiting on you. The tag disappears when you
  answer, deny, or the prompt expires — the hashtag *is* the index, and it
  cannot drift from the truth it describes.
- **Nudge-replies** — opt-in, per person, off by default. A short reply-message
  pointing back at the unanswered prompt so you receive a fresh notification.
  Nudges are gated by your availability hours: they only fire while you are
  supposed to be reachable.

---

## Commands

Send these to the Telegram bot in your chat. Each command echoes back what was
saved, including the next active window resolved to a concrete local time, so a
typo is visible immediately.

### `/tz <IANA timezone>`

Sets your timezone. Must be a valid IANA name.

```
/tz Europe/Berlin
/tz America/New_York
/tz Asia/Tokyo
```

The timezone is used for all active-time arithmetic — availability windows and
nudge scheduling. If you have not set a timezone, UTC is assumed.

### `/hours <spec>`

Sets your availability windows. The spec is a comma-separated list of
day-range/time-range clauses:

```
/hours mon-fri 09:00-19:00
/hours mon-fri 09:00-19:00, sat 11:00-15:00
/hours off
```

`/hours off` clears the windows and makes you always available (the default).

**Window grammar:**

```
<spec>     ::= <clause> ("," <clause>)*
<clause>   ::= <days> " " <time-range>
<days>     ::= <day> | <day> "-" <day>          (e.g.  mon  or  mon-fri)
<day>      ::= mon | tue | wed | thu | fri | sat | sun
<time-range> ::= HH:MM "-" HH:MM
```

Day ranges are inclusive on both ends. `fri-mon` is a wrapping range (Fri, Sat,
Sun, Mon). Times are in local time in your configured timezone. A window cannot
start and end at the same minute.

**Worked examples:**

| Spec | Meaning |
|------|---------|
| `mon-fri 09:00-19:00` | Weekdays, 9 am to 7 pm |
| `mon-fri 09:00-19:00, sat 11:00-15:00` | Weekdays plus Saturday morning |
| `mon-fri 09:00-19:00, sat 11:00-15:00, sun 12:00-14:00` | All three |
| `wed 00:00-08:00` | Wednesday midnight to 8 am |

After setting hours, the bot echoes back the next active window as a concrete
local time. Use this to catch typos before they matter.

### `/nudge on | off | <schedule>`

Enables, disables, or sets the nudge schedule.

```
/nudge on              → use the server default schedule (15m, 45m, 3h)
/nudge off             → no nudges
/nudge 30m,2h,6h       → custom: first after 30 active minutes, then 2h, then 6h
```

The schedule is a comma-separated list of durations (`15m`, `3h`, `2h30m`).
Each interval is measured in **active time** — the clock only runs while you are
inside an availability window (see "Active-time arithmetic" below). Up to 3
rungs by default; the server operator can raise or lower the cap.

Turning nudges on backfills open prompts that are already waiting: they will
receive nudges on the new schedule rather than waiting for the next prompt.

### `/me`

Shows your current configuration: timezone, windows, whether you are active
right now, and the nudge schedule.

```
/me
```

---

## Active-time arithmetic

The core concept: **a clock that only runs while you are inside an availability
window**.

The nudge ladder is measured in active time, not wall-clock time. This is the
behaviour that surprises people until they see the example:

> A prompt arrives at **18:50**. Your window is `mon-fri 09:00-19:00`. Your
> first nudge interval is **30 minutes**.
>
> 10 active minutes of your window remain — from 18:50 to 19:00. The 30-minute
> budget is not exhausted in that window: 10 minutes run, then the window
> closes. The remaining 20 minutes carry over to the next window, which opens
> at **09:00 the following morning**. The nudge fires at **09:20** — not at
> 19:20 (which is outside the window) and not at 09:00 sharp (which would skip
> the remaining 20 minutes of debt).

A prompt raised at **02:00** (outside the window) has its first nudge at
`09:00 + first interval` because no active time runs before the window opens.

---

## Wall-clock TTL — the important caveat

**TTL (the time before a prompt expires) is wall-clock, not active-time.**

The default TTL is 12 hours. This is the time the *agent* is willing to wait
for a human response, and it is a property of the session — not of your
calendar.

The practical consequence: **an overnight prompt may expire having received one
nudge or none**, even with nudges on. If a prompt arrives at 22:00 and your
window opens at 09:00, the 12-hour TTL expires at 10:00 — only one active hour
is available before expiry. The nudge fires at 09:15 (the full 15-minute interval runs from the window
opening), but if the next interval would fall after expiry it
never fires.

If this proves too short in practice, the fix is a longer TTL on the agent side
— not an active-time TTL. Active-time TTL is explicitly out of scope for this
epic.

---

## Operator commands (`relay-admin`)

Shell access is required for these. They are the operator's equivalent of the
bot commands and read/write the same per-chat state.

```
relay-admin recipients list                         # show all configured chats
relay-admin recipients set-tz <chat_id> <tz>
relay-admin recipients clear-tz <chat_id>
relay-admin recipients set-hours <chat_id> "<spec>"
relay-admin recipients clear-hours <chat_id>
relay-admin recipients set-nudge <chat_id> on|off
relay-admin recipients set-nudge-schedule <chat_id> "<schedule>"
```

The `list` subcommand shows, for each configured chat: timezone, availability
windows, current active/inactive status (server-side, in the recipient's
timezone), nudge on/off, and last updated time.

---

## Diagnostic (`claude-roles`)

`claude-roles --check` probes each role token against the relay and, on a relay
running epic 19, shows an availability column: `tz=`, `active=yes/no`,
`nudge=on/off`. The `active=` value comes from the server verbatim — it is
resolved in the recipient's timezone on the relay side and is correct regardless
of which machine runs the diagnostic.

On an older relay that does not have the availability fields, the column is
blank and the exit code is unchanged.

---

## Defaults summary

| Setting | Default |
|---------|---------|
| Timezone | UTC assumed |
| Hours | Always available |
| Nudges | Off |
| Nudge schedule (when on) | 15m, 45m, 3h (server default) |
| TTL | 12 h (wall-clock) |

An unconfigured chat behaves exactly as it did before epic 19 — no new messages,
no new behaviour, byte-for-byte identical.
