# Async questions — prompt guidance (adapt into CLAUDE.md)

This file is a template to copy into your workspace `CLAUDE.md` or any prompt
file and edit to match your project.  Nothing generates or validates it; keeping
it in sync with `.claude/roles.toml` and `[questions]` is your responsibility.

---

## When to use `ask` instead of `AskUserQuestion`

Use `mcp__questions__ask` when:

- The question is non-blocking: you can continue other work while waiting for
  the human's answer, and you intend to exit or move on immediately.
- The answer may arrive hours or days later (the human is asleep, in a meeting,
  or in a different timezone).
- You need a permanent, searchable record of the question and its answer in the
  repository.

Use `AskUserQuestion` (the terminal tool) when:

- You are blocked and cannot proceed at all without the answer.
- The question needs an answer in the next few minutes.
- You are inside an interactive session and the user is at the keyboard.

Do **not** use `ask` to defer a question you should answer yourself.  Async
questions are for genuine human decisions — product scope, design choices,
policy calls — not for confirming what the spec already says.

---

## What to put in `body` vs `options`

**`body`** — the full context the human needs to make an informed decision.
Write it as you would a Slack message to a busy colleague who has not read the
thread: include the problem, the alternatives you considered and why you
rejected them, and the impact of the decision.  The body is stored verbatim in
the queue file and displayed in Telegram.

**`options`** — a short list of discrete choices (2–5 items).  Each option
becomes a Telegram button.  The human can tap a button or reply with free text;
both reach the queue file.  Omit `options` when the question is truly open-ended.

**Worked example:**

```python
result = mcp__questions__ask(
    title="Database for user sessions",
    body=(
        "The session store needs to handle ~10k concurrent users.  "
        "Redis is already in our stack but adds an external dependency for the "
        "auth service.  In-process SQLite keeps things simple but caps us at "
        "one replica.  Postgres is already required for the main DB.  "
        "What should we use?"
    ),
    options=["Redis", "SQLite (single replica)", "Postgres sessions table"],
    role="arch",        # routes to the tech-lead role
)
# result.id is e.g. "Q-042"
```

---

## Citing the returned id when halting

When you stop work to wait for an answer, record the `id` in your final message
so the human can trace which question caused the halt:

```
HALT: waiting for database decision (Q-042).
Resume when Q-042 is resolved.  Run `claude-questions --status` to see pending.
```

The id is allocated by the store and guaranteed unique across the queue set and
`answered/`.  Reference it in commit messages, PR descriptions, or notes.

---

## Withdrawing a question that has become moot

If a question you asked no longer needs an answer — the change went ahead, the
blocker disappeared, you found the answer in the spec — **withdraw it**:

```python
mcp__questions__withdraw(
    id="Q-042",
    reason="The migration ran clean on staging, so no decision is needed.",
)
# → { id, withdrawn: true, status: "withdrawn", file, message_id, relay_cancelled: true }
```

That marks the entry `[withdrawn]` with your reason, replaces the Telegram
message with "Withdrawn — <reason>", and removes its buttons, its reminders and
its escalation.  The human reads the reason, so write it for them.

Do **not** edit the heading by hand to close a question.  A hand-edited
`[resolved]` leaves the Telegram buttons live, the reminders running and the
escalation armed, and a late tap is then written into the entry.

Rules worth knowing:

- Only an **open** question can be withdrawn.  If the human already answered,
  the call is refused (`withdrawn: false`, `status: "resolved"`) and the answer
  stands — read it instead.
- If an answer arrives *after* the withdrawal, it is kept: recorded under the
  entry as a **late answer**, status unchanged.  Look for it before assuming a
  withdrawn question has nothing to say.
- `relay_cancelled: false` with an `error` means the file is withdrawn but
  Telegram could not be reached.  Call `withdraw` again later with the same id;
  it changes nothing in the file and retries the Telegram side.
- For a `notify(ack=True)` that has gone stale, pass its `message_id` instead of
  an `id`.

A pattern this enables: ask "this change is waiting on you" with buttons, carry
on, and withdraw the question if the change goes ahead by itself.

---

## Never pick a question id yourself

`ask` allocates the id.  If you are writing an entry into a queue file **by
hand** — or need to cite an id before the entry exists — reserve it first:

```python
mcp__questions__claim(count=1, note="hf-123 design halt")
# → { ids: ["Q-591"], file: "docs/questions/claims.md", claimed_on_remote: true }
```

Reading the queue files and taking "highest + 1" is how two questions end up
with the same id: an id used on a branch that has not landed, or on another
machine, is not in the files you can see.  A claim is a line in the ledger on
the queue's own branch; with `[questions] sync = "push"` it is committed and
pushed, which is what makes it binding everywhere.

- `claimed_on_remote: false` means the reservation exists only in this
  checkout (sync is off, or the push did not happen — the `note` says why).
  Treat the id as provisional and say so.
- To *ask* with an id you claimed earlier, pass it: `ask(id="Q-591", …)`.  An
  id that was never claimed, or is already used, is refused.
- From a shell: `claude-questions --claim` prints the id.

When the workspace has sync on, do not `git commit` or `git push` queue files
yourself after a tool call — the tool already did.  After editing a queue file
by hand, run `claude-questions --sync` rather than committing it.

---

## Routing to a role

`role` is the role_id from `.claude/roles.toml` (e.g. `hpl`, `htl`, `ux`).
Omit it to use the workspace default role.  An unresolvable role is refused
before anything is written — the store never creates a dangling entry.

One `ask` call addresses exactly one role.  If two roles need to decide
independently, make two separate calls.

---

## What happens after you call `ask`

1. The entry is written to the queue file immediately and fsynced.
2. The Telegram message is sent (best-effort; if the relay is unreachable the
   entry still exists and the human can find it by reading the queue).
3. `ask` returns `{ id, file, dispatched, message_id, pending_answers }`.
4. `pending_answers > 0` means previous answers have not yet been applied;
   this is visible on every call as an early warning of a backlog.
5. In a workspace with git sync on, the result also carries `sync`:
   `{ mode, committed, pushed, commit, upstream, … }`.  `pushed: true` means the
   id is claimed for every checkout.  `pushed: false` comes with a `reason`
   (no network, the checkout is mid-rebase, the primary checkout is on another
   branch, …): the question **was still written and sent**, the id is
   provisional, and the push is retried automatically — report the reason in
   your halt message rather than trying to fix git yourself.
6. `warnings`, when present, means the question was sent in a degraded form
   (today: the relay refused the reminder ladder).  It is not an error.

The answer arrives later, applied by the listener `questions-listen` with no
session running and no AI in the loop.  It is written back to the same queue
entry by flipping the status token and inserting an answer block — and, with
git sync on, committed and pushed by the listener.

---

## Recovery: "a question was answered and nothing happened"

Run:

```
claude-questions --status          # is the listener running? are there pending applies?
claude-questions --reindex         # rebuild any missing index entries (rare edge case)
questions-listen --once            # force one poll cycle
```

See `docs/async-questions.md` for the full walkthrough.
