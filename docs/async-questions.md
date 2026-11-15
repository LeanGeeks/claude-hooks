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

The system has four tools:

- **`ask`** — write an entry to the queue file, send to Telegram, return the
  allocated id immediately.
- **`notify`** — send an async statement; with `ack: true` it carries an
  `[ Acknowledge ]` button so the nudge ladder has something to chase.
- **`withdraw`** — close a question that has become moot: the entry is marked
  withdrawn with a reason and the Telegram message loses its buttons, reminders
  and escalation.  See [Withdrawing a question](#withdrawing-a-question).
- **`claim`** — reserve the next id(s) without asking anything, for a question
  filed by hand.  See [Reliable ids](#reliable-ids-claiming-on-the-upstream).

`withdraw` and `claim` are also `claude-questions --withdraw` / `--claim`; both
front ends call the same function.

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

sync   = "push"             # "off" (default) | "commit" | "push" — see Reliable ids
claims = "claims.md"        # id ledger inside dir

[questions.git]             # optional tuning for sync
branch  = "main"            # the branch the queue lives on
remote  = "origin"
retries = 5                 # re-allocations after a lost push race
message = "chore(questions): {summary}"

[questions.format]
# Declare your real status vocabulary so no headings need editing.
status = ["open", "resolved", "answered"]
withdrawn = "withdrawn"     # the word `withdraw` writes; always recognised

[questions.queue]
# Map each role_id to its queue file.  Omit for a flat single-file workspace.
hpl = "for-product-lead.md"
htl = "for-tech-lead.md"
```

A full copy-paste template is in `docs/questions.example.toml`.

Every key from `sync` down is optional and off by default: a workspace that
does not set `sync` starts no git process and behaves as it did before epic 46.

Without a `[questions]` section the system is fully inert — no queue is
touched, no listener runs, every existing flow is byte-identical.

---

## Withdrawing a question

```
mcp__questions__withdraw(id="Q-042", reason="The change went ahead by itself.")
claude-questions --withdraw Q-042 --reason "The change went ahead by itself."
```

What happens, in this order:

1. **The file.**  The entry's status token is flipped from the open word to the
   withdrawn word (`[questions.format] withdrawn`, default `withdrawn`), and a
   block is inserted before the entry's boundary:

   ````markdown
   <!-- withdrawn -->
   **Withdrawn:** 2026-11-15T12:00:00Z · agent · session 1350e006

   ```text
       The change went ahead by itself.
   ```
   ````

   The reason is escaped exactly like an answer; it cannot forge a heading, a
   status token or a marker.  A withdrawn entry no longer counts toward
   `max_open`.  With git sync on, this is one commit, pushed.
2. **Telegram.**  The message is replaced by
   `🚫 Withdrawn — <title>` + the reason, and cancelled on the relay: the
   keyboard is stripped, the nudge ladder retired, and the escalation never
   fires.  If the question had **already been escalated**, the escalated copy
   gets the same text and loses its keyboard too.

**Status vocabulary.**  The withdrawn word is always part of the recognised
status set, whether or not it is listed in `status`.  A workspace that already
has a word for a question closed without an answer can reuse it
(`withdrawn = "closed"`).  Whether an entry *is* withdrawn is decided by the
`<!-- withdrawn -->` marker, never by the word — so a hand-written `[closed]`
entry is not mistaken for one.

**What can be withdrawn.**  Only an open question.  For anything else the call
is refused and changes nothing:

| Entry | Result |
|---|---|
| open | `withdrawn: true` |
| already withdrawn | `withdrawn: true`, the file untouched, **the relay cancel is sent again** |
| answered / resolved / any other status | `withdrawn: false`, `status` = what it found |
| no such id, or two entries with that id | `withdrawn: false`, `error` |

**A failed relay call** leaves a withdrawn entry whose buttons are still live:
`relay_cancelled: false` plus an `error`.  Run the withdrawal again — the second
row of the table is the retry.

**An answer that races the withdrawal.**  The file decides, and nothing is ever
discarded:

- *The answer is applied first.*  The withdrawal is refused
  (`status: "resolved"`); the answer stands.
- *The withdrawal is written first*, and an answer then arrives — a tap that
  beat the keyboard strip, or an answer the relay had recorded and the listener
  had not yet applied.  The answer is written under the entry as a **late
  answer**, with the line `**Late answer** — recorded after this entry was
  marked [withdrawn]; status unchanged.`, and **the status stays withdrawn**.
  The Telegram message is finalized `✅ <answer>` with a note that the question
  was no longer open.

This is one instance of a general rule: **an answer flips the status only from
the open word.**  An answer to an entry that a human has since marked
`[SUPERSEDED by Q-269]` is recorded the same way and no longer rewrites the
heading to `[resolved by Q-269]`.

**An acknowledge-notification** (`notify(ack=True)`) has no queue entry; cancel
it by its relay message id: `withdraw(message_id=7001, reason=…)` or
`claude-questions --withdraw relay:7001 --reason …`.

**Relay version.**  Stripping the keyboard of an *escalated copy* needs a relay
that includes epic 46's task 46-01.  Against an older relay the original
message is still withdrawn correctly, but an already-sent escalated copy keeps
its buttons — and **a tap on it is discarded by the relay** (the parent is
cancelled, so the relay cancels the copy and records nothing; it does not reach
the queue file even as a late answer).  Redeploy the relay before relying on
withdrawal for questions that may already have escalated.

**What Telegram shows after a race.**  The file is authoritative; the message is
whatever was written to it last.  A late answer finalizes the message as
`✅ <answer>` over the "Withdrawn" text; repeating the withdrawal afterwards puts
"Withdrawn" back.  Either way the entry reads withdrawn *and* carries the late
answer.  Once the relay has cancelled a message, a later tap on it is refused by
the relay with a toast and is not recorded at all — a late answer can only be
one the relay had already accepted before the cancel reached it.

---

## Reliable ids: claiming on the upstream

By default `ask` allocates `max + 1` over the queue files in the primary
checkout.  That cannot see an id used on a branch that has not landed, an id a
session reserved for a hand-filed question, or another machine's checkout.

```toml
[questions]
sync = "push"

[questions.git]
branch = "main"
```

With `sync = "push"`, **an id is taken when a commit containing it has been
accepted by the remote**.  One `ask`, under the repository's lock:

```
fetch origin main
compose the entry against the upstream's copy of the queue files   (nothing on disk yet)
build a commit on the upstream tip that changes only the queue file  (private index)
push <sha>:refs/heads/main
    rejected (someone else pushed first) → start again: the id is re-allocated
    accepted → write the file here and record it on the local branch
send the question to Telegram
```

Then the `**Dispatched:**` line is written and published the same way, so an
`ask` is two commits: `chore(questions): ask Q-042` and
`chore(questions): dispatched Q-042 (relay #4187)`.

### What is pushed, and what is not

- **One commit, touching only queue files, pushed by hash.**  The local branch
  itself is never pushed.  If your `main` is 26 commits ahead of `origin/main`,
  an `ask` publishes one commit touching one file, and your 26 stay where they
  are.
- **The file, not just the edit.**  The pushed copy of the queue file is the
  primary checkout's copy.  Earlier edits to *that same queue file* that you had
  not pushed — a hand-filed entry, a hand-written answer — therefore travel
  with it.  The result says so: `carried_local_changes: true`.  No other path is
  ever published.
- **Queue files only, and the engine checks.**  A path is committed or pushed
  only if it is a regular file in this workspace's queue set: a file the config
  names (`[questions.queue]` / `file`), the claims ledger, or a file matching
  `[questions.format] glob`.  When a sync publishes "everything that is owed", a
  file that merely matches the glob is included only if it actually parses as a
  queue (and is not a dotfile or a temp file) — under `glob = "*.md"`, a
  `README.md` in the queue directory stays yours.  A symbolic link is never
  synced, in either direction.
- **No hooks.**  The workspace's `pre-commit`, `commit-msg`, `post-commit` and
  `pre-push` hooks are not run for these commits: a hook that runs a test suite
  would run inside `ask` and inside the listener.

> **This bypasses the approval an agent's own `git push` needs.**  That is the
> point — a claim that waits for a human is not a claim — and it is why `sync`
> is off unless the workspace writes it down in `roles.toml`.

### What happens to the primary checkout

Only ever, through a pathspec: the queue file is written and
`git commit --only -- <file>` records it.  The tool never checks out, stashes,
resets, rebases, merges or pulls your branch.

- **In sync** (`HEAD` is the upstream tip): the local branch is moved onto the
  very commit that was pushed.  Nothing diverges.
- **Ahead, behind or diverged**: the local commit is a *twin* of the pushed one
  (`twin: true`): it records the queue file as it now stands upstream, on your
  branch, under a different hash.  `git status` then reports "ahead N, behind
  M".  Reconcile whenever you like, with `git pull` or `git pull --rebase` —
  see the next section for why that is clean.

### Your own `git pull`: the merge driver

A twin is not always "the same change" as the pushed commit.  One made while
this checkout was behind holds the other machine's entries *and* the new one;
once the upstream moves again, git's line merge sees two sides that both
appended to the end of one file and calls it a conflict.  Left alone, two
machines asking in turn would hit that on every pull.

So the first time a tool writes with `sync` on, it registers a **merge driver**
for the queue files in the repository's *local* git configuration — nothing
tracked, nothing pushed:

```
.git/config            [merge "claude-questions"]  driver = … claude-questions --merge-driver %O %A %B %P
.git/info/attributes   /docs/questions/for-*.md merge=claude-questions      (one line per queue pattern + the ledger)
```

What the driver does when git asks it to merge a queue file:

1. The entry-by-entry merge the tools themselves use (below).  If it can
   decide, that is the result: entries appended on both sides, an answer on one
   side and a new question on the other, a twin of something already upstream,
   a twin that still shows a question open which the upstream has since
   answered or withdrawn.
2. Otherwise git's own three-way merge — for what the entry merge will not
   decide (an entry removed on one side, the text before the first entry
   changed on both) and for a file that is not a queue.  Its result is taken
   only if no id ends up heading two entries: a line merge can come out "clean"
   and be exactly that.
3. Otherwise conflict markers, as always, and a human decides: one entry really
   was changed in two different ways.

If the driver itself cannot run — a CLI on this machine too old to know
`--merge-driver`, a broken install, an interpreter that dies — the command
falls back to git's ordinary merge.  A failed driver never leaves a file that
looks resolved and is not.

With it, `git pull` merges cleanly and `git pull --rebase` drops the twins as
already applied.  It applies to `merge`, `pull`, `rebase` and `cherry-pick`
alike.

- It is per clone: each machine's repository gets it the first time a tool
  writes there.  A fresh clone that has never run a tool merges by line.
- It needs the installed CLI (`~/.claude/shell/claude-questions`); without it
  the command falls back to git's ordinary merge.
- Turn it off with `[questions.git] merge_driver = false`.  Remove it with
  `git config --local --remove-section merge.claude-questions` and by deleting
  the `merge=claude-questions` lines from `.git/info/attributes`.

Without the driver (turned off, or a clone that has never run a tool), a
conflict in a queue file after cross-machine asks is resolved by taking the
upstream's copy — it already contains everything the tools pushed:

```
claude-questions --sync                       # FIRST: publish anything this machine still owes
git pull                                      # conflict in docs/questions/<file>
git checkout --theirs -- docs/questions/      # "theirs" is the upstream in a merge…
git add docs/questions && git commit --no-edit
# …and in a rebase it is "ours":
#   git checkout --ours -- docs/questions/ && git add docs/questions && git rebase --continue
```

Do not skip the first line: taking the upstream's copy discards whatever this
machine had not pushed.

### When git cannot do its part

The question is never lost and the caller is always told.  In every row the
entry is written to the queue file and the id is allocated above everything the
last known upstream contains; `sync.reason` names the row.

| State of the primary checkout | Local commit | Push | `sync.reason` |
|---|---|---|---|
| on another branch than the sync branch | no | no | names both branches |
| detached HEAD | no | no | `detached HEAD` |
| rebase / merge / cherry-pick / bisect in progress, or `index.lock` held | no | no | names the operation |
| no remote, or no such branch on the remote | yes | no | `no upstream: …` |
| network down | yes | no | `fetch failed: …` |
| the remote refuses the push (protected branch, server hook) | yes | no | `push refused: …` |
| upstream and local copies cannot be merged (see below) | yes | no | names the file |
| the push race was lost `retries + 1` times, or the time budget ran out | yes | no | `lost the push race …` |
| the remote failed less than a minute ago | yes | no | `push deferred: the remote was unreachable Ns ago` |
| a queue file is a symbolic link, or is ignored by git | no | no | names the file |
| the path written is not one of the workspace's queue files | no | no | `… is not one of this workspace's queue files` |
| the push was accepted but the local write failed (disk full) | no | **yes** | `pushed …, but the local write failed` |
| other files dirty or staged | yes | yes | — (they are not touched) |

**Merging the upstream's copy into yours.**  Before every write the upstream's
copy of each queue file is brought into the local one, **entry by entry**:
an entry only one side changed since the last sync takes that side's version;
new entries from the upstream are appended; whatever is yours is kept byte for
byte.  When both sides have a different version of one entry and one of them is
simply the other's **later state** — the same question, every line of the
earlier version still there in order, with a status flipped and an answer or a
withdrawal added — the later one is taken: an entry only ever grows, so that
loses nothing.  The merge is refused — and nothing is pushed — when one entry
was changed in two different ways (two machines answered one question), when
one id heads two entries or two different questions, or when an entry was
deleted on one side, or when it was closed in two different ways (resolved on
one side, withdrawn on the other).  Lines are never merged:
a line merge of two answer blocks produces a file that still parses and is
wrong.

### When the files cannot be merged

The reason names the entries, e.g. `Q-042 was given to two different questions
(one here, one upstream)`.  The repository stays *pending*: questions and
answers are still written and sent, nothing is pushed until a human has
reconciled the file, and every result says so.

- **One id, two questions.**  Both machines asked while one of them could not
  reach the remote (or during the minute after a network failure — see "How
  long a write can take"), or one of them lost the push race `retries + 1`
  times in a row and kept its question locally while others went on pushing.
  The upstream's question keeps the id.  For the local one:

  ```
  claude-questions --withdraw Q-042 --reason "id collision with another machine; asked again"
  ```

  That cancels its Telegram message, so no answer can arrive for it.  Then
  delete the withdrawn entry from the local queue file by hand, run
  `claude-questions --sync` (the files now merge), and ask the question again —
  it gets a fresh id.  Nothing renumbers an entry automatically: the id is
  already in a Telegram message and in whatever the agent wrote down (task 47).
- **One entry changed on both sides** (two machines answered it, or it was
  edited by hand in two places): decide which version stands, make the local
  entry identical to the upstream's (`git show origin/main:docs/questions/<file>`),
  then `claude-questions --sync`.
- **An entry removed on one side**: remove it on the other too, or restore it.

Ids are allocated above everything the *last fetched* upstream contains, so a
machine that cannot reach the remote still avoids every id it has seen.  It
cannot avoid one it has not seen: offline, allocation is best effort, and the
collision above is how that shows.

Whatever is owed is recorded in `<git-dir>/claude-questions-sync.json`
(`pending: true`) and paid by whichever comes first:

- the **next tool write** in that repository (it publishes every queue file that
  differs from the upstream, not only the one it touched);
- the **listener**, which retries each owing repository every five minutes;
- **`claude-questions --sync`**, by hand.

**How long a write can take.**  Git runs under the repository's lock, so the
budget is fixed and short.  Each fetch and each push gets 20 s; no new push
attempt starts after 60 s; after a network failure the remote is not tried
again for a minute (a burst of asks during an outage waits once, and the rest
are written at once).  So an `ask` normally costs one fetch and one push, and at
worst about 100 s against a remote that accepts the connection and then stalls
every time.  The listener gets less — 8 s per call, one re-try, at most 32 s for
one answer — and writes immediately, without touching the network, while a
repository's last attempt is known to have failed.  In every one of these cases
the question or the answer is written; only the push waits for later.

`claude-questions` (no flags) shows `sync:` and, when something is owed, since
when and why.  `questions-listen --status` lists owing repositories under
`git sync owed`.

### Who commits an answer when no session is running

The listener.  `apply_answer` goes through the same engine, so an answer that
arrives at 03:00 is written, committed (`chore(questions): answer Q-042 (relay
#4187)`) and pushed with nobody at the keyboard.

It cannot fight a session working in the same checkout: every tool write in a
repository takes one lock, touches the index for the queue file's path only,
and stands down — writing the file, recording the debt — whenever git itself is
mid-operation.

**Hand edits.**  The listener's retry pass only ever looks at a repository that
a tool has marked as owing, so an idle repository's queue files are left alone
however long a hand edit sits in them.  But be clear about what a sync
publishes when one does run: the *file*.  A write to a queue file carries that
file's earlier uncommitted edits with it, and paying a debt publishes every
queue file that differs from the upstream.  If you edit a queue file by hand,
finish the edit and run `claude-questions --sync`; do not leave it half done in
a workspace whose tools are syncing.

### Claiming an id without asking

```
mcp__questions__claim(count=1, note="hf-123 design halt")
claude-questions --claim --note "hf-123 design halt"      # prints Q-591
```

The reservation is one line in the ledger (`[questions] claims`, default
`docs/questions/claims.md`):

```markdown
- Q-591 · 2026-11-15T10:22:11Z · agent · session 1350e006 · hf-123 design halt
```

Allocation reads the ledger, so the id is never handed out again; with
`sync = "push"` the ledger line is committed and pushed like any other write,
and `claimed_on_remote` in the result says whether it got there.  Only a list
item that *starts* with an id reserves it.  Lines may be pruned once the entry
exists.  `ask(id="Q-591", …)` accepts an id only if the ledger holds it and no
entry uses it.

This is the answer to "ids on a branch that has not landed": the branch's
question is written on the branch, but its **id is claimed on the queue's own
branch first**.  The tool does not scan other refs for ids.

### Limits worth knowing

- **A queue file deleted upstream comes back** on this machine's next write to
  it: the tool publishes the file it has.  Delete it here too.
- **`dir` pointing into another repository** (an absolute path, or a symlinked
  directory) makes *that* repository's checkout the one that is committed to
  and pushed from.  It works; make sure it is what you meant.
- **Repositories that rewrite line endings on checkout** (`core.autocrlf`, an
  `eol=crlf` attribute) are untested; `carried_local_changes` is known to be
  reported when nothing was carried.
- **A crash between an accepted push and the local write** leaves the entry
  upstream and not here.  The interrupted write left a marker in the sync
  state; the next tool write, the listener's retry pass or `--sync` sees it and
  brings the entry down.  Nothing is lost — but if it was an `ask`, the
  question was never sent to Telegram (task 47).

### `sync = "commit"`

Every tool write is committed in the primary checkout; nothing is fetched or
pushed.  Useful for a repository with no remote, or one where pushing must stay
a human act.  It does not make ids reliable across machines.

### The lock file

The store's lock used to be `<dir>/.lock`, which showed up as an untracked file
in every adopting repository.  In a git workspace it is now
`<git-dir>/claude-questions.lock`.

If you have a `docs/questions/.lock` left over from before the upgrade: it is
still honoured — the store locks **both** while it exists, so a session started
before the upgrade (still running the old code) and one started after stay
mutually exclusive.  Delete it once every such session has ended; it is not
re-created.  Outside a git repository the lock stays in the queue directory;
add `.lock` to that directory's ignore file if it bothers you.

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

This enables the unit, **starts it**, and runs `loginctl enable-linger` so it
survives logout.  `./install.sh disable questions-listen` disables and stops it.
Every later `./install.sh --yes` restarts an enabled listener, because the unit
is a resident process and the run may have just replaced the library it loaded.

**That sub-toggle is the only switch.**  A listener that has been started runs;
there is no second "enabled" check inside it.  All it needs from
`~/.config/claude-tg-relay/config.toml` is a `server_url` and an installation
token.  The `[questions_listen]` section is optional tuning:

```toml
[questions_listen]
poll_seconds = 25    # long-poll budget per cycle
max_attempts = 10    # pending-apply retries before the sidecar fallback
```

`[questions_listen] enabled` was the opt-in before the installer had
sub-toggles.  It is inert: nothing reads it, and `--status` prints a note if it
is still present.  (Until task 45 the binary did still read it and exited
whenever the section was missing, while this page said otherwise — an enabled
unit on a machine with no such section never listened.)

The unit is `Restart=on-failure` with `RestartPreventExitStatus=78`.  The
listener exits `0` when another copy already holds its lock and `78` when
`config.toml` has no token or no `server_url`; neither is restarted.

`questions-listen --status` and `claude-questions --status` print `enabled:`
from the installer manifest (falling back to the systemd `wants` link), without
calling `systemctl` — they answer from a shell that has no user bus.

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

### `claude-questions --withdraw ID --reason TEXT`

Withdraw an open question, or `relay:<N>` for an acknowledge-notification.
Exit 0 only when the entry is withdrawn **and** nothing went wrong on the relay;
exit 2 on a refusal or when the relay could not be reached (run it again).

### `claude-questions --claim [N] [--note TEXT]`

Reserve the next N ids (default 1).  The ids are the only thing on stdout, one
per line — `ID=$(claude-questions --claim)` works; where it was reserved and
what git did go to stderr.  Exit 2 when the workspace has `sync = "push"` and
the claim was not pushed.

The three operations cannot be combined with each other or with a diagnostic
flag, and a workspace named with `--workspace-dir` wins over one inherited from
`$CLAUDE_PROJECT_DIR`.

### `claude-questions --sync`

Commit and push whatever the queue files owe.  Use it after editing a queue
file by hand, or when a result reported `pending`.  Exit 2 when something is
still owed afterwards; the reason is printed.

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
  `systemctl --user restart claude-questions-listen`
  (from a shell with no `XDG_RUNTIME_DIR`, prefix
  `XDG_RUNTIME_DIR=/run/user/$(id -u)`), then read
  `journalctl --user -u claude-questions-listen -n 20` if it does not stay up
- `running: no` + `enabled: no` → `./install.sh enable questions-listen`
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
