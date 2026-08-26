# Epic 23 — Asynchronous agent↔human questions

**Status:** todo · **Owner:** Anton · **Created:** 2026-08-26 · **Rev:** 1

> Broken down into tasks — see [state.md](./state.md) for ordering and
> cross-task invariants.

## 1. Problem & thesis

`AskUserQuestion` over Telegram (epics 07, 15) gave agents a way to reach named
human roles — product lead, tech lead, designer — from any session. It is the
most valuable human-in-the-loop surface the system has, and it is **blocking**.
When the human is asleep the session is parked for hours doing nothing. For a
planning session that is correct: the answer *is* the next step. For an
autonomous loop it is pure waste — the agent had other work it could have done,
and instead burned a session slot waiting.

The missing half is a **fire-and-forget** channel: an agent states its question,
tells a human, and exits. The answer arrives on a completely different timeline —
hours or days later, into a machine where the asking session is long dead, its
worktree possibly deleted, its branch possibly merged or abandoned.

**Thesis: the question is a file, not a conversation.** Workspaces that run
AI-managed projects already keep durable question queues in the repo
(the reference case: `hyppie-flow/docs/questions/for-{role}.md`, 279 entries,
a schema pinned in its `workflow.md` §5.2, an agent contract that says *halt,
write the question, exit*). Those files — not a session, not a relay row — are
the real medium. So this epic:

1. writes the question into the workspace's own queue file **and** pushes it to
   Telegram, in one MCP call, and returns immediately;
2. writes the human's answer back into that same file, **deterministically, with
   no AI in the loop**, whenever the machine is next awake.

Everything downstream — noticing the answer, resuming work — belongs to the
workspace's own workflow. We ship no tooling to spawn in-workspace agents.

## 2. Constraints discovered

Recorded because each one closed off an option.

**2.1 — `AskUserQuestion` cannot carry this, and should not be made to.** Its
input schema is closed (`additionalProperties: false` at both levels), which is
why epic 15's role tag had to ride inside `header`. Worse, the hook's only
possible outputs are allow-with-`updatedInput`, deny-with-message, or silence
(`permission_request_hook.py:212-324`) — so an "async" variant would have to
either fabricate an answer the human never gave, surface as a *failed* tool
call, or drop to the native terminal UI. No path fabricates an answer today and
that invariant is worth keeping. Independently, Claude Code caps the native tool
at a hard-coded, non-configurable **60 s** ("continued without an answer";
upstream request closed as not-planned), so the substrate is sinking regardless.
A new MCP tool has an open schema, a truthful return value, and no 60 s cap.

**2.2 — There is no background drain.** `waiters.py` wakes only an in-flight
`GET /v1/messages/{id}/answer`; an answer is acted upon exactly when a process
on the originating machine is parked on that message
(`../../architecture.md:144`). For an async question **nobody is parked** — the
asking session exited minutes after sending. Something resident is unavoidable.

**2.3 — Escalation lives inside the blocking wait loop.** `escalate_after` fires
from `_wait_for_group_answers` (`permission_request_hook.py:897-921`): the
*waiting process* is what notices the deadline and duplicates the group to the
default role. With nobody waiting, that mechanism has no host. Nudges are
already relay-side (epic 19's reaper pass) and therefore carry over free;
escalation does not.

**2.4 — Nudge ladders are finite, and async questions never expire.** The ladder
(`15m,45m,3h`) is spent after its last rung, which is correct for a message with
a 12 h TTL and wrong for one that may legitimately sit open for a week. An async
question that stops nudging is the exact failure this epic exists to prevent.

**2.5 — `awaits_human` excludes every `notification`.** `render.py` tags
`#unanswered` only for non-`notification` rows (epic 19 invariant 7 — the tag
means *an agent is blocked on you*, not *a session finished*). An
acknowledgeable notification therefore cannot be sent as `kind=notification`
without either losing its nudges or breaking that invariant.

**2.6 — Question queues are branch-scoped state in worktree projects.** The
reference workspace tracks its queue files in git (`git ls-files docs/questions/`)
and runs parallel tracks in sibling worktrees (`../<repo>--hf-NNN-<slug>/`). By
the time an answer lands the worktree may be deleted, switched to another
branch, or its branch merged or abandoned — so a path captured at ask time is
not a reliable address. Meanwhile `Q-NNN` ids are already *globally* unique
across every queue and `answered/`, so the convention already treats the queue as
shared state while the file layout treats it as branch state.

**2.7 — Sequential id allocation is admitted-unsafe under parallelism.** The
reference workflow states plainly that `Q-NNN` allocation "is safe at v0.4
max-parallel=1". Any tool that authors entries must allocate under a lock, or it
inherits a known race.

## 3. Decisions

**D1 — A new MCP server, not an `AskUserQuestion` extension** (§2.1). Two tools:
`ask` (async question) and `notify` (async statement). The existing blocking
path is left untouched; this epic adds a surface and changes no working feature.

**D2 — One call writes the file *and* sends the message.** The alternative —
agent writes the entry, then calls a dispatch tool — leaves a window where the
question exists and no human was told, in a session that is about to exit. It
also leaves id allocation to the agent (§2.7) and forces the tool to *parse*
options back out of prose to build the keyboard. Merging removes all three.

**D3 — The tool owns the frame; the agent owns the body.** The tool writes the
heading, the id, the status token, the routing line, the rendered options and the
dispatch marker. Everything else is an opaque `body` passthrough. Test of the
seam: when the reference workspace added a machine-readable `**Class:**` /
`**Proof:**` block for design-fixture halts, this design needs no release.

**D4 — Write first, send second.** The file write is the durable act; Telegram is
best-effort. A send failure returns `dispatched: false` with the id — the entry
still exists and a human still finds it by reading the queue. Worst case degrades
exactly to the pre-Telegram baseline.

**D5 — A watermark answer feed, not a command queue.** Epic 16 designs a generic
`commands` table for `/new` (targeting, broadcast, claim-once, two-phase TTL).
Answer delivery needs none of it: the answers already live in `messages`. A
replayable, installation-scoped `GET /v1/answers?after=&wait=N` is smaller, and
crash-safe by construction — a claim-once queue that dies between claim and file
write loses a human decision, while a watermark advanced only after a successful
write cannot. This epic therefore has **no dependency on epic 16**.

**D6 — Reminder policy travels with the message** (§2.3, §2.4). For an async
message nobody is waiting to enforce policy, so the message carries its own:
nudge ladder and `escalate_after` + escalation target are per-message properties,
defaulting to the chat's `recipients` config when absent. Role bindings stay
machine-local — the relay is told *where* to escalate, never *what a role is*.

**D7 — Ack-notifications ride `kind=question`** (§2.5). A `notify` with an
`[ Acknowledge ]` button is a message awaiting a human tap, so it is sent as a
one-option question and gets `#unanswered` and nudges with no relay change.
Idle-session notifications keep no keyboard and stay excluded. What separates a
question from a notice is our own routing index (an entry with a `qid` writes to
a file; one without only finalizes), not the relay's `kind`.

**D8 — The queue anchors to the repo's primary worktree by default** (§2.6).
`anchor = "repo"` resolves through `git rev-parse --git-common-dir`, so questions
raised in any worktree of a repo land in that repo's main checkout. For a
single-checkout project this is a no-op; for a worktree project it is what makes
deterministic write-back possible at all, and it independently stops append-only
queue files from conflicting on every branch landing. `anchor = "worktree"` opts
back into branch-scoped queues; `anchor = "path"` puts the queue outside the repo.

**D9 — No wake tooling.** We ship nothing that spawns in-workspace agents and
hold no knowledge of any project's loop. The answer landing in a file is the
signal; picking it up is the workspace's business. (For the reference workspace
that is three lines in a cron predicate it already owns.)

**D10 — Answers are applied idempotently, keyed on relay message id, and retried.**
Patching can still fail — mid-rebase, detached HEAD, an entry that only ever
existed on a dead branch. A failed apply is kept, retried, and surfaced; it is
never dropped, because a dropped apply is a lost human decision.

**D11 — "Never expires" is a far-future sentinel, not `NULL`.** `messages.expires_at`
is `NOT NULL` (`db.py:47`) and SQLite cannot drop a NOT NULL constraint without a
full table rebuild; four call sites also parse it unconditionally. A sentinel
(`9999-12-31T00:00:00Z`) makes the expiry pass a natural no-op, needs no schema
change, no reader audit and no index change. `ttl_sec` stays required and capped
as it is today (`models.py:25`, `le=24*3600`); a new optional `never_expires` flag
selects the sentinel instead.

**D12 — Asking is capped per workspace.** `ask` is agent-callable in a loop, and
both a queue file and a human's phone are on the other end. A per-workspace
`max_open` (default 20 unanswered entries) and `min_interval_s` (default 30)
refuse loudly rather than queueing — the shape epic 16 §4.1 uses for spawns.

## 4. Surface

### 4.1 `ask`

```
ask(title, body, options?, role?, tags?, anchor_hint?)
  → { id, file, dispatched, message_id, pending_answers }
```

Writes a new entry to the role's queue file and sends it to that role's Telegram
chat with one button per option (free-text replies accepted as always). Returns
the allocated id so the caller can reference it — for the reference workspace,
in its `HALT: <reason> (Q-NNN)` verdict line.

`role` is an override; the default comes from the entry's routing line, then the
workspace default role. An unresolvable role is **refused before anything is
written** — the reference queues already contain two `**Routed to:** @unknown`
entries, and this is the class of dangling record not to create.

`pending_answers` is a count of answers this machine has failed to apply so far
(D10) — a backlog that is visible on every ask rather than only in a log.

### 4.2 `notify`

```
notify(text, role?, ack?) → { dispatched, message_id }
```

An async statement. With `ack: true` (the default) it carries an
`[ Acknowledge ]` button so the nudge ladder has something to chase; with
`ack: false` it is fire-and-forget and silent forever. Writes nothing to any
file. Same role routing as `ask`.

### 4.3 What the human sees

The same Telegram surface as a blocking question: the body, option buttons,
threaded free-text replies, `@role` routing, the `#unanswered` tag, availability
windows and the nudge ladder. Two differences, both deliberate: **it never
expires**, and **there is no terminal race** — the keyboard-side equivalent of
answering is editing the queue file directly, which the workspace's own wake
predicate already notices.

## 5. Configuration

One optional section in `.claude/roles.toml` — already described by its own
header as "committed workspace vocabulary", and already the file that names the
roles a queue is keyed by. A second config file for one section would be worse.

```toml
[questions]
dir            = "docs/questions"   # workspace-relative; the only required key
anchor         = "repo"             # repo (default) | worktree | path
path           = ""                 # required iff anchor = "path"; absolute, ~ ok
nudge          = "4h,1d,3d,7d*"     # optional; trailing * repeats the last rung
escalate_after = "24h"              # optional; default = roles.toml top level

[questions.queue]                   # role id → queue file
hpl = "for-product-lead.md"
htl = "for-tech-lead.md"
ux  = "for-designer.md"
```

A role absent from `[questions.queue]` has **no queue** and is chat-only — which
is already true of `operator` in the reference workspace, and is now declared
rather than implied. A workspace with no roles at all gets one destination and
one queue file; the map degenerates rather than becoming mandatory.

Everything about the entry format is a default with an override beside it
(`[questions.format]`: `id`, `status`, `heading`, `answer_template`, `glob`).
The defaults are the reference workspace's §5.2 shapes because they are a good
schema and having *a* default beats requiring every project to specify one.

**The adoption test for reusability:** a greenfield project with a flat
`questions.md`, no roles, no worktrees and no PM loop must be able to adopt this
with `dir = "docs/questions"` and nothing else.

## 6. Behaviour

### 6.1 Asking

```
ask()  ─► resolve anchor + role + queue file
       ─► flock(dir/.lock): allocate id, compose entry, append, fsync, release
       ─► POST /v1/messages (kind=question, never_expires, buttons,
                             nudge ladder + escalation as message properties)
       ─► record message_id → {workspace_id, anchor, rel_path, qid} in the index
       ─► patch **Dispatched:** into the entry
       ─► return
```

### 6.2 Answering

```
human answers in Telegram (button or threaded reply)
       ─► relay records the answer  (existing path, unchanged)
questions-listen: GET /v1/answers?after=<watermark>&wait=25
       ─► join on the local index → workspace_id → resolve path NOW
       ─► flock: flip [open] → [resolved], insert the answer block
       ─► PATCH the Telegram message to ✅ <answer>
       ─► advance the watermark
```

The watermark advances **only** after a successful apply, so a crash replays
rather than loses. Applies are idempotent on message id: an answer block already
carrying that id is skipped, not doubled.

### 6.3 Failure

| Situation | Behaviour |
|---|---|
| Relay unreachable at ask time | Entry written; `dispatched: false` returned with the id; a human still finds it by reading the queue |
| Role unresolvable | Refused before any write — no dangling entry |
| Listener down / machine asleep | Nothing lost; the watermark replays every unapplied answer on the next connect |
| Queue file gone / id not found | Answer retained as pending, retried, counted in `pending_answers` |
| Entry existed only on a dead unmerged branch | After N retries, written to `<dir>/answered/<id>.md` with a note that the original entry could not be located — visible, never dropped |
| Two agents ask at once | Serialized by the directory lock; ids are unique by construction (§2.7) |
| Answer arrives while origin session still alive | Same path — it goes to the file. There is no injection special case |

## 7. Success criteria

- [ ] An agent calls `ask`, exits immediately, and the entry is in the workspace
      queue file in the project's own schema, with a `**Dispatched:**` marker.
- [ ] The human sees a normal Telegram question, answers it **at least 24 hours
      later**,
      and the answer lands in the queue file with `[open]` → `[resolved]` and a
      well-formed answer block — with no session running and no AI involved.
- [ ] The same works when the machine was **asleep** when the answer was given.
- [ ] An answer given for a question asked from a **since-deleted worktree**
      lands in the primary checkout's queue file.
- [ ] A never-answered question is still nudging after its third day, on the
      recipient's availability windows, and escalates once at `escalate_after`.
- [ ] `notify` with an ack button nudges; `notify` without one never does, and
      idle-session notifications are byte-identical to today.
- [ ] Killing the listener mid-apply loses nothing: on restart the answer is
      applied exactly once.
- [ ] A greenfield workspace adopts with `dir = …` alone (§5 adoption test).
- [ ] No regression: permission, blocking-question, idle and injection flows
      produce byte-identical relay calls with the listener stopped.

## 8. Security

Two surfaces, both bounded on purpose.

**Answer text becomes repo content.** A human's free-text reply is written into a
file that agents subsequently read *as instructions*. A compromised Telegram
account can therefore inject text into an AI-managed project's decision record —
a strictly larger capability than the one the product already accepts (that such
an account can approve permissions, epic 16 §5.6). Bounds: answers are inserted
**escaped and fenced** so they cannot forge a heading, a status token or an answer
block (invariant 11); they are attributed with `**Answered by:**` and the relay
message id, so every injected line is traceable; and they land in a git-tracked
file, so the change is reviewable and revertible like any other.

**Answer text can corrupt the queue.** Independent of malice: an answer that
happens to contain `## Q-999 … [open]` would break the parse anchors for every
later operation on that file. The same escaping is the fix, and it is the reason
escaping is an invariant rather than a nicety.

**Not in the threat model:** the relay never learns a path, a role or a workspace
(invariant 4), so a compromised *relay* cannot direct writes anywhere; and nothing
in this epic executes anything — no spawning, no shell, no git refs (D9).

## 9. Out of scope

- **Replacing blocking `AskUserQuestion`** with an MCP `ask` (the 60 s cap
  mitigation). It is the other half of the argument in §2.1, but the async path
  is self-contained and shippable; that migration is its own epic, informed by
  what this one learns. Working features are left alone without a reason.
- **Retiring the wait loop's own escalation** (§2.3). This epic adds relay-side
  escalation for messages that declare it; unifying the blocking path onto it is
  a later refactor of working code.
- **Spawning anything** — no handler sessions, no PM knowledge, no wake tooling
  (D9).
- **Authoring entries by any means other than `ask`**, and dispatching a
  hand-written entry (`dispatch(id)`), which would re-introduce option parsing.
- **Moving resolved entries to `answered/`** — archival stays a workspace action.
- **Agents answering async questions** (epic 22 §3.2 draws the same line from the
  other side).
- **Cross-machine answers.** The index is machine-local: an answer applies on the
  machine that asked.
