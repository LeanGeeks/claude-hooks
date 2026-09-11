# Async questions — operator guide

This document is for the person who installs and maintains the async question
system.  For the adopter contract (how to onboard an existing workspace), see
`docs/questions-contract.md`.  For the agent-facing interface, see
`docs/questions-prompt-example.md`.

---

## What the system does

`mcp__questions__ask` lets a Claude Code agent write a question into a workspace
queue file and send it to Telegram, then exit.  The human answers hours or days
later from their phone; the answer is written back into the same file
automatically, with no session running and no AI in the loop.

The system has two tools:

- **`ask`** — write an entry to the queue file, send to Telegram, return the
  allocated id immediately.
- **`notify`** — send an async statement; with `ack: true` it carries an
  `[ Acknowledge ]` button so the nudge ladder has something to chase.

---

## Configuration

All configuration lives in `.claude/roles.toml` in the workspace.  Add a
`[questions]` section:

```toml
[questions]
dir    = "docs/questions"   # directory relative to the anchor root
anchor = "repo"             # "repo" | "worktree" | "path"
nudge  = "4h,1d,3d,7d*"    # nudge ladder (* = repeat last rung forever)
escalate_after = "24h"      # duplicate to default role after this window

[questions.format]
# Declare your real status vocabulary so no headings need editing.
status = ["open", "resolved", "answered"]

[questions.queue]
# Map each role_id to its queue file.  Omit for a flat single-file workspace.
hpl = "for-product-lead.md"
htl = "for-tech-lead.md"
```

A full copy-paste template is in `docs/questions.example.toml`.

Without a `[questions]` section the system is fully inert — no queue is
touched, no listener runs, every existing flow is byte-identical.

---

## Anchor modes

| Mode | What it resolves to | When to use |
|---|---|---|
| `"repo"` | Primary worktree root via `git rev-parse --git-common-dir` | Default; questions raised in any worktree land in the primary checkout |
| `"worktree"` | The cwd's own worktree root | Branch-scoped queues; the listener uses the captured absolute path, which may be gone by answer time |
| `"path"` | An explicit absolute path (expand `~` yourself) | Queue outside a git repository; requires `path = "…"` |

The default `"repo"` means questions from a worktree branch land in the main
checkout and survive branch deletion — the right behaviour for almost every
project.

---

## How answers travel

```
agent  ──mcp__questions__ask──►  queue file + Telegram message
                                           │
                                     (hours or days later)
                                           │
                                    human answers on phone
                                           │
                                 questions-listen (systemd user unit)
                                    ├─ index lookup: message_id → qid
                                    ├─ questions_store.apply_answer() ──► queue file
                                    └─ PATCH /v1/messages/{id} ──► ✅ answer
```

The listener polls `GET /v1/answers?after=<watermark>&wait=25` in a long-poll
loop.  It resolves the workspace from the index entry's anchor mode — not from
the path captured at ask time, which may be gone.  Applies are idempotent: a
crash and replay never produces a double answer.

---

## The `questions-listen` listener

### Running

```
questions-listen              # run the loop (start by systemd --user)
questions-listen --once       # one poll cycle, then exit (diagnostics)
questions-listen --status     # print state without holding the lock
```

### systemd unit

The installer creates `~/.config/systemd/user/claude-questions-listen.service`.
It is **not** enabled automatically.  To opt in:

```bash
./install.sh enable questions-listen
```

This enables the unit and runs `loginctl enable-linger` so it survives logout.
Then start it:

```
systemctl --user start claude-questions-listen
```

The `[questions_listen] enabled` key in `~/.config/claude-tg-relay/config.toml`
was the pre-epic opt-in mechanism. It is now inert — the sub-toggle in the
installer manifest is the setting. Machines upgraded from the old installer will
have the key migrated automatically on the next `./install.sh` run.

---

## Adoption walkthrough — greenfield workspace

A brand-new workspace with no roles and no queue:

### 1. Create the queue directory and file

```
mkdir -p docs/questions
touch docs/questions/questions.md
git add docs/questions/
```

### 2. Add four lines to `.claude/roles.toml`

```toml
[questions]
dir    = "docs/questions"
anchor = "repo"
file   = "questions.md"
```

That is it.  Four lines and a working `ask` call.

### 3. Install

```
./install.sh --yes
```

The installer registers the `questions` MCP server and installs the listener
binary in the same pass — hook library and MCP server are always installed
together to prevent a version window.

### 4. Verify

```
claude-questions               # overview: anchor, queue, listener state
claude-questions --check-contract   # 0 edits needed for a fresh file
```

---

## Diagnostic commands

### `claude-questions` (no flags)

Shows the resolved anchor and root, the role→queue map with each file's
existence and open-entry count, the id high-water mark, and the listener state.

Run this when you want a quick health check.

### `claude-questions --check`

Probes each distinct installation token against the relay
(`GET /v1/installations/me`).  Network required.  Exit 2 on any problem.
Mirrors `claude-roles --check`.  Use this when questions are not arriving in
Telegram.

### `claude-questions --check-contract`

Scans the workspace's queue files for contract violations: malformed ids,
missing status tokens, and duplicate ids within a file.  Never modifies any
file.  Exit 2 on findings.  See `docs/questions-contract.md` for the full
rule set and fix guidance.

Use this when adopting an existing workspace.

### `claude-questions --status`

Prints detailed listener state: running/not, enabled/not, per-feed watermarks,
last applied answer, and the full pending list with last errors.

**This is the first thing to read when "a question was answered and nothing
happened."**

### `claude-questions --reindex`

Scans queue files for `**Dispatched:** … relay #N` markers and rebuilds any
missing index entries.  Never overwrites an existing record.  Never touches a
queue file.

**Use this after `ask` returns `index_routing_failed: True`.** The entry and
the Telegram message both exist; only the routing record is missing.  After
`--reindex`, the listener picks up the answer on its next poll (or run
`questions-listen --once` to force one cycle immediately).

---

## "A question was answered and nothing happened"

This is the most common support scenario.  Work through it in order:

### Step 1 — Check the listener

```
claude-questions --status
```

Look at:
- `running: no` + `enabled: yes` → start the unit:
  `systemctl --user start claude-questions-listen`
- `running: no` + `enabled: no` → opt in via config.toml and re-run the installer
- Listener is running → go to step 2

### Step 2 — Check for pending applies

`claude-questions --status` shows the pending list.  Each item has:
- `relay #N` — the relay message id
- `attempts` — how many retries have fired
- `last_error` — the specific failure reason

Common causes and fixes:

| Error | Cause | Fix |
|---|---|---|
| `id not found` or `conflict` | The entry was edited or a second heading appeared | Edit the queue file to resolve the conflict, then wait for the next retry |
| `not a git repo` or `OSError` | The worktree was deleted or moved | Check out the branch or create a worktree at the original path; or move the queue file and --reindex |
| `detached HEAD` | Mid-rebase or mid-checkout | Resolve the git operation, the next retry will succeed |

After the underlying cause is fixed, force a retry:

```
questions-listen --once
```

### Step 3 — Rebuild a missing index entry

If `ask` logged `index_routing_failed: True` or the message_id is simply
missing from `~/.claude/async_questions.json`:

```
claude-questions --reindex
questions-listen --once
```

`--reindex` reads the `**Dispatched:** … relay #N` marker that `ask` always
writes to the queue entry — even when the index write fails — and reconstructs
the routing record from it.  Applies are idempotent, so re-running is safe.

---

## Pending applies and the backlog

Each answer that cannot be applied immediately moves to `pending` in the index.
The listener retries on each poll cycle with exponential backoff (60 s doubling
to hourly).  After `~10` failed attempts the answer is written to
`<dir>/answered/<qid>.md` as a sidecar — never silently lost (brd D10).

The sidecar file contains the full answer text and the relay message id.  A
human can apply it by hand or wait for the underlying issue to resolve.

`ask`'s return value includes `pending_answers: N` — the count of pending
applies at the moment of the call.  A rising count is an early warning that
something needs attention.

---

## Security notes

- No token material ever appears in any diagnostic output, including `--status`,
  `--check`, `--check-contract`, and the status file.  Fingerprints (16 hex
  characters) are used instead.
- Answer text from Telegram is escaped before insertion: no possible answer
  can forge a heading, a status token, or an answer-block field (invariant 11).
- The relay never learns a workspace path, a role name, or a queue file.
  Escalation is passed as a token hash; location exists only on the machine.
