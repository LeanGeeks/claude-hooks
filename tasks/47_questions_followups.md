# Task 47 — Async questions: follow-ups filed from the first adoption

**Status:** draft · **Type:** follow-up · **Created:** 2026-11-15 · **Rev:** 2

Found while `hyppie-flow` adopted epic 23 (2026-11-15). Tasks 45 and epic 46
took what was cheap or asked for; these are left, with what is known.

## 1. An undispatched entry is never re-sent

`ask` writes the entry before it sends (epic 23 invariant 1). When the send
fails the entry stays `[open]` with `dispatched: false`, counts toward
`max_open`, and nothing ever sends it. The human finds it only by reading the
file.

- Epic 46 removed the two *systematic* causes: the relay client that could not
  be imported (task 45) and the ladder the relay rejected (46-01, plus the
  client's retry without a ladder). What remains is the genuine case — the relay
  was unreachable when the question was asked.
- **Sketch.** `claude-questions --resend ID` and a listener pass that retries
  entries with no `**Dispatched:**` line. The entry holds everything needed
  except the options as a list: they are in the frame the store itself wrote
  (`**Options:**` + a numbered list), so reading them back is the store parsing
  its own output, not the "option parsing" epic 23's brd §9 ruled out for
  hand-written entries. The idempotency key (`ask:<ws>:<id>:send`) already makes
  a duplicate send impossible.
- **Open question.** Whether the listener should do this unasked. It would be
  the first time the listener *sends* anything.

## 2. The message frame is English-only

`_render_ask_body` / `_render_notify_body` hard-code "Question — <workspace>",
"for: <role>", "Tap a number below, or reply with text:", "Reply to this message
with your answer.", "Tap Acknowledge to confirm receipt." and the button label
"Acknowledge". Epic 46 added "Withdrawn" and the late-answer note. The relay's
nudge text is English too. Some roles read another language (the reference
workspace's `ux` and `hpl` work in Russian; agents already write the *body* in
Russian for them).

- **Sketch.** `[questions.text]` in `roles.toml`, optionally per role
  (`[role.ux] lang = "ru"` + `[questions.text.ru]`), with the current strings as
  defaults. The nudge text is the relay's and per chat, so it belongs with the
  recipient's settings there (`/lang`), not in a workspace file.
- Not cheap because it has two owners (client frame, relay nudge) and wants a
  decision on where a human's language lives.

## 3. `ask(role=…)` for a role that is the operator duplicates on escalation

A role that resolves to the operator's own chat gets a second copy in the same
chat after `escalate_after`. **The operator says that is fine — left as is.**
Recorded so nobody "fixes" it.

## 4. The store parses a well-formed id heading inside a fenced block as an entry

Noticed while fixing task 45 §3. `parse_entries` does not track fences, so
`## Q-042 — example  [open]` inside a ``` block *is* an entry (a `Q-NNN`
placeholder is not, because it is not a well-formed id). The contract doc tells
adopters to fence documentation; that is only safe for placeholders. No
workspace is known to be affected. Fixing it changes the parse contract (a
seventh rule) and needs the same care rule 1 got.

## 5. Git sync: what review left as documented limits (epic 46)

Each is stated in `docs/async-questions.md` ("Limits worth knowing"); none
loses a question or an answer.

- **A crash between an accepted push and the local write** leaves the entry on
  the upstream and not in the checkout; the next write or `--sync` brings it
  down. If the write was an `ask`, the question was never sent to Telegram —
  the same gap as §1, reached another way, and §1's re-send would close it.
- **A queue file deleted upstream is resurrected** by this machine's next write
  to it (the engine publishes the file it has). Detecting "deleted upstream
  since the base" and refusing is a few lines; it was left out because no
  workspace deletes queue files.
- **Line-ending conversion** (`core.autocrlf`, `eol=crlf`) is untested;
  `carried_local_changes` is known to read true when nothing was carried.
- **`dir` pointing into another repository** makes that repository the one
  committed to and pushed from. Works; possibly surprising.
- **An id that collided while a machine was offline is not renumbered.** The
  engine refuses to publish, names the id, and the way out is by hand: withdraw
  the local question, delete its entry, `--sync`, ask again (tested). Automatic
  renumbering of a local-only entry is possible — rewrite the heading, update
  the listener's index record, note the old id in the entry — but the id is
  already in a Telegram message and in whatever the asking agent wrote down, so
  it wants a decision, not a patch.
- **Silent fallback to the default role's token** when a role's binding was
  removed after the question was sent (epic 23 behaviour): the cancel then
  fails with a 404 that is explained, but the fallback itself is not reported
  at resolve time.
