# 19-07 — Live verification

**Status:** todo · **Depends on:** 19-06 · **Human task**
**Read first:** [brd.md](./brd.md) §7 · [state.md](./state.md)

## Why this is human

Three things cannot be established against `FakeBackend`:

1. **How Telegram clients actually render and search the tag** — the hashtag has
   to be tapped, on real phones, in a real bot DM.
2. **Whether a nudge notifies** the way a person needs it to (banner, sound,
   badge) as opposed to merely being delivered.
3. **That a window closing overnight behaves**, which requires a night.

## Setup

A real relay, a real bound chat, and at least two workspaces posting
concurrently — the multi-session case is the one the epic exists for, and it is
the one a single-session smoke test will not show.

## Checks

### Tag (19-03) — before nudges are enabled at all

- [ ] An open permission prompt renders `#unanswered` as a tappable link, not
      literal text, on **iOS, Android and Desktop**.
- [ ] Tapping it lists the currently-open prompts. Note per client whether the
      search scopes to this chat or globally — brd §2.4 says either is
      acceptable; if global is noisy, this is the moment to switch the constant
      to `#unanswered_cc` and re-check.
- [ ] Answer one prompt in Telegram and one **in the terminal**; both lose the
      tag, and the terminal-resolved one **still shows its `✍️`/`✅` answer
      line**. (The brd §2.9 regression, verified where it would actually bite.)
- [ ] Let one prompt expire untouched: tag gone, keyboard gone.
- [ ] An AskUserQuestion group: every member tagged while open, none after.

### Preferences (19-02)

- [ ] `/tz`, `/hours`, `/nudge`, `/me` from the phone; each echo names a
      concrete local time that matches reality.
- [ ] A deliberate typo in the timezone produces a useful error, not silence.

### Nudges (19-04, 19-05)

- [ ] Enable nudges; leave a prompt unanswered inside the active window. The
      nudge arrives on the ladder and **notifies** — check with the phone locked.
- [ ] Tapping the reply-quote jumps to the original with live buttons.
- [ ] **Reply to the nudge itself** — the session receives the answer.
- [ ] Two sessions idle at once → one nudge with `+1 more`, not two nudges.
- [ ] An AskUserQuestion group → one nudge, not one per question.
- [ ] Answer the prompt → the nudge disappears from the chat.
- [ ] Resolve a prompt **in the terminal** → the nudge disappears. (Client-side
      transition, the one most likely to leak.)

### Overnight (19-01, the point of the epic's arithmetic)

- [ ] Set a window that closes this evening. Leave a prompt open past it.
- [ ] No nudge arrives during the night. Confirm at least once by looking at a
      quiet phone, not only at the log.
- [ ] The next nudge arrives *after* the window opens, offset by the remaining
      ladder interval — not at the window's opening second, and not at the
      wall-clock time it would have fired overnight.

### Regression sweep

- [ ] A second chat with **no** configuration at all behaves exactly as before:
      no nudges, tag present, everything else unchanged.
- [ ] **Both** log surfaces are clean after a day of use: client-side
      `permission_telegram_errors.log`, and the relay's own container logs on
      the server (`docker compose logs` at the deploy path) — the two new edits
      in 19-03 fail *server*-side, so the client log would not show them.
      Specifically: no recurring "not modified" 400s, no reaper tracebacks.

## Done criteria

- [ ] Every box above ticked, on real devices, with the overnight case actually
      spanning a night.
- [ ] Any client-specific hashtag behaviour recorded in brd §2.4 as observed
      fact, replacing the "varies by client" hedge.
- [ ] Anything that did not behave as specified is filed as a follow-up task
      rather than fixed silently in this one.
