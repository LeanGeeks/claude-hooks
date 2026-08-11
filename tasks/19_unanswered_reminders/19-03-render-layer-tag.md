# 19-03 — Render layer and the `#unanswered` tag

**Status:** done · **Depends on:** none · **Independently shippable**
**Read first:** [brd.md](./brd.md) §2.2, §2.3, §2.6, §2.8, §2.9, §4 ·
[state.md](./state.md) invariants 1, 2, 7, 8

## Goal

Every open message that awaits a human carries a trailing `#unanswered` line;
every transition out of `open` removes it. Telegram's hashtag search then *is*
the pending-work index, with nothing to reconcile and no way to drift.

The tag is the easy half. The real work is the invariant underneath it:
**`payload_json.text` is the canonical untagged body, and one render function
owns the append.** Establish that and the tag is four lines; skip it and this
task silently destroys answer text in production.

**This task ships alone.** No nudges, no availability, no new notification
behaviour — the chat gets quieter to read, not louder.

## The trap, stated plainly

`finalize_message` (`router.py:806`) PATCHes the baked answer text and *then*
cancels. The PATCH lands while the row is still `open`, so the tag is
re-appended; cancel must therefore remove it. But cancel today is
`edit_reply_markup` only (`app.py:760`) — it has never authored text, and the
only body it can reach is `payload_json`, **which PATCH does not update**
(`app.py:695`). So the naive implementation — "give cancel a text edit that
renders from the payload" — rewrites the message with the *original* body and
wipes the `✍️ <answer>` the hook just baked in.

That is the regression this task exists to not ship. It is invisible in unit
tests that only assert on the tag.

## Scope

### Canonical text — `app.py`

- **PATCH writes back.** `patch_message` persists `body.text` into
  `payload_json["text"]` in the same transaction that edits Telegram. After
  this task, payload and message never disagree.
- Everything that stores a body stores it **untagged**. The tag never enters
  `payload_json`, ever, from any direction — including a client that types
  `#unanswered` into its own text (strip on ingest, per below).

### `render_body(payload, state) -> str`

One function, in `app.py` beside `_payload_for`. **It takes a payload dict and a
state, not a row** — because the create path has no usable row yet: the insert
writes `telegram_message_id = 0` as a placeholder and the real send happens
*before* the id is known (`app.py:571-650`). At create time the caller has
`body.model_dump()` and the literal string `'open'`, and re-reading the row it
just inserted to render text would be a pointless round trip.

```
render_body(payload, state):
    body = strip_tag(payload.get("text") or "")
    return body + TAG_LINE if awaits_human(payload, state) else body

render_body_row(row) = render_body(_payload_for(row), row["state"])   # thin wrapper
```

- `awaits_human(payload, state)` =
  `state == 'open'` **and** `kind != 'notification'` **and** (`reply_required`
  or a keyboard or a `group_id`). All four fields live in `payload_json`
  already — `create_message` stores `body.model_dump_json()` wholesale
  (`app.py:573`) — so no new persisted field is needed anywhere.
- **The `kind` clause is load-bearing, not redundant.** `notification_hook`
  sends idle-session messages with `reply_required=True` whenever
  reply-from-Telegram is on, so dropping it would tag every idle session and
  (via 19-04) nudge about it at 09:00. Decided against — brd §4.1. Do not
  "simplify" the predicate.
- `strip_tag` removes a trailing tag line wherever it appears, so the function
  is idempotent under retry and under a client echoing its own text back.
- `TAG_LINE` is `"\n\n#unanswered"` — plain text, never inside `<pre>`/`<code>`
  (brd §2.3), and one constant so brd §2.4's fallback (a more distinctive tag)
  stays a one-line change.
- **Length guard** (brd §2.6): bodies already approach 4096. Append only if it
  fits; otherwise trim the body at a character boundary that cannot split an
  HTML entity or land inside a tag, and prefer dropping the tag to sending a
  message Telegram rejects.

### Call sites converted

Every send and every edit goes through it. The five that exist today:

| Site | File:line | Change |
|---|---|---|
| create | `app.py:602` | `render_body(body.model_dump(), 'open')` — no row needed |
| group finalize | `app.py:1289` | `render_body_row` + `_answer_line` |
| multi-select toggle | `app.py:1598` | `render_body_row` — row is still open, keeps the tag |
| grouped reply re-render | `app.py:1690` | `render_body_row` |
| grouped reply bake | `app.py:1726` | `render_body_row` + reply prefix |

### Two paths grow a text edit

- **`cancel_message`** (`app.py:760`) — `edit_message` with
  `render_body_row(row)` after the state flip, then the keyboard strip as today.
  Order matters: flip state first, and **re-read the row** — `_load_message` ran
  before the flip, so the cached row still says `open` and would keep the tag.
- **Reaper expiry** (`reaper.py`) — same, replacing the keyboard-only "Option A"
  strip. Record the reversal in `tasks/05_telegram_prompt_lifecycle_management.md`
  (19-06 owns the write-up; note it here so it is not forgotten).
  **`_fetch_expired` must grow its SELECT**: it returns only
  `id, telegram_chat_id, telegram_message_id` today and the text edit needs
  `payload_json` as well. Select it in the same query rather than re-reading per
  row — the reaper holds the shared connection lock and a per-row round trip in
  a sweep that can cover dozens of messages is exactly the wrong shape.

Both must tolerate Telegram's "message is not modified" 400 exactly as
`patch_message` and `cancel_message` already do via `_is_not_modified` — an
untagged message being re-cancelled is the common correct case, not a fault.
Both are **best-effort**: a failed edit must never block a state transition or
crash a reaper tick.

## Implementation notes

- The state flip and the render must not race: read the row *after* the flip, or
  pass the intended terminal state explicitly. Rendering from a stale row is how
  a tag survives a cancel.
- `delete_message` (the endpoint) needs no change — the message is gone.
- Do not add a `tagged` column. Derived state that can disagree with `state` is
  exactly the drift a pinned digest would have had, reintroduced.

## Testing

`relay-server/tests/test_messages.py` / `test_question_groups.py` /
`test_reaper.py`, against `FakeBackend`:

- Create a permission message → sent text ends with the tag.
- Informational notification (no keyboard, no `reply_required`) → **no** tag.
- **Idle notification *with* `reply_required=True` → no tag.** The regression
  guard on brd §4.1; without it the predicate quietly reverts to the naive form.
- Answer via callback → final text has no tag and *has* the `✅` line.
- **The regression test:** PATCH a finalized body (`"…\n\n✍️ answer"`), then
  cancel; assert the final Telegram text still contains `✍️ answer` and does not
  contain the tag. This is the one test that must exist before the code.
- PATCH twice with the same text → exactly one tag, no doubling.
- Client text that itself contains `#unanswered` → one tag in the output.
- Multi-select toggle while open → tag survives the re-render.
- Group of four finalizes → all four lose the tag.
- Reaper expiry → text edited, tag gone, keyboard stripped, waiters notified.
- Telegram returning "not modified" on either new edit → transition still
  completes, no 502, tick continues.
- A body at 4090 characters → no oversize send; assert on the guard's choice.

## Done criteria

- [ ] Tag present on open human-awaiting messages, absent everywhere else.
- [ ] All five render sites call `render_body`; no call site appends the tag.
- [ ] `payload_json["text"]` matches the visible body after a PATCH.
- [ ] The finalize-then-cancel regression test passes and would fail against a
      payload-rendering cancel.
- [ ] Expiry edits text; the task-05 reversal is noted for 19-06.
- [ ] Nothing under `.claude/hooks/` changed.
