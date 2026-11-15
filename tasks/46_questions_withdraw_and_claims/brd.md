# Epic 46 — Async questions: withdraw a question, claim an id reliably

**Status:** in progress · **Owner:** Anton · **Created:** 2026-11-15 · **Rev:** 1

> Broken down into tasks — see [state.md](./state.md). Builds on epic 23
> ([brd](../23_async_questions/brd.md), [architecture](../23_async_questions/architecture.md),
> [state](../23_async_questions/state.md)); its invariants bind here unless a
> decision below says otherwise.

## 1. Problem

Epic 23 shipped a one-way door. A question, once asked, can only be answered.
The first workspace to adopt the system (`hyppie-flow`, 2026-11-15) hit two gaps
on its first day:

**A question cannot be withdrawn.** The workspace wants to send "this change is
waiting on you" with buttons, and close it when the change goes ahead by itself.
Today the only move is to edit the heading to `[resolved]` by hand, which frees a
`max_open` slot and nothing else: the Telegram buttons stay live, the ladder keeps
nudging a never-expiring message, the 24 h escalation still fires, and a late tap
is written into the entry and overwrites the status word.

**Question ids collide.** `ask` allocates `max + 1` over the queue files in the
primary checkout. It cannot see an id used on a branch that has not landed, an id
a session reserved for a question it is filing by hand, or another machine's
checkout. The reference workspace has already had two ids allocated twice
(Q-586/587). The operator's requirement, verbatim:

> always claim IDs / post questions in the main worktree, preferrably with commit
> & push, to make the claim reliable even when multiple machines are involved.

## 2. Constraints discovered

**2.1 — The relay can already cancel, and nothing calls it.**
`POST /v1/messages/{id}/cancel` strips the keyboard and retires the ladder, and
the escalation pass only fires for `state='open'`, so a cancelled message is
never escalated. But cancel does not reach an escalated copy that was *already*
sent — answer and expiry both call `cancel_escalated_children`, cancel does not.

**2.2 — The primary checkout is routinely ahead of its upstream.** The reference
workspace's `main` was 26 commits ahead of `origin/main` on the day this was
written: an agent's `git push` there asks for approval, so unpushed work piles
up. Pushing the branch to publish a question would publish all of it, unapproved.
A push made by the tool's own process does not pass through that approval — so
the tool must be incapable of pushing anything but queue files.

**2.3 — Two sides appending to one file always conflict in git.** Measured in a
scratch repository: if the upstream copy of a queue file and the local one each
gain a *different* tail, both `rebase` and `merge` conflict. If the upstream copy
is made **identical to the local file** (a snapshot), `merge` is clean and
`rebase` drops the local twin commit — conflicts appear only for a local commit
that edited the queue file and was never pushed. So the tool must publish the
file, not a replay of its own edit.

> **Corrected by review round 2.** That measurement was of one machine. With
> two, a twin made while *behind* holds the other machine's entries as well as
> the new one; when the upstream then gains another entry, the two sides have
> again "each gained a different tail", and `merge` and `rebase` both conflict.
> The snapshot is still right — it is what makes the *tool's* merge trivial —
> but it does not make the *operator's* pull clean. D15 does.

**2.4 — Re-allocating an id is only free before anything is written.** If the
entry is written locally first and the push then loses a race, the local entry
and the winner's entry share an id and a tail position: undoing that means
rewriting a file and a commit. If nothing local is written until the remote has
accepted the push, a lost race costs nothing.

**2.5 — `questions_store.py` is pure by contract** (no subprocess, no network —
epic 23 state.md, 23-03 log). Git cannot go in it.

**2.6 — Epic 23 ruled git out** (`brd.md` §8: "nothing in this epic executes
anything — no spawning, no shell, no git refs (D9)"). This epic reverses that for
workspaces that ask for it, and only for them.

**2.7 — Old MCP server processes outlive an install.** A session started before
an upgrade keeps its `questions` server process — old code, old lock path — until
the session ends. Anything that changes the lock or the index shape must be safe
next to a running copy of the previous version.

## 3. Decisions

**D1 — Withdrawal is a first-class operation**, exposed as an MCP tool
(`withdraw`) and a CLI flag (`claude-questions --withdraw`). Both call one
function in an installed hook module; neither reimplements it.

**D2 — A withdrawn entry says so in the file.** The status token is flipped to
the *withdrawn word* and a `<!-- withdrawn -->` block (who, when, the reason,
escaped like an answer) is inserted before the entry's boundary. The word is
`[questions.format] withdrawn`, default `"withdrawn"`, and is always part of the
recognised status set. A workspace whose vocabulary already has a word for this
(`closed`) names it there; "is this entry withdrawn?" is decided by the marker,
never by the word.

**D3 — Withdrawal reaches Telegram, including the escalated copy.** The relay
message is patched to `🚫 Withdrawn — <title> / <reason>`, cancelled (keyboard
stripped, ladder retired, never escalated), and any escalated copy gets the same
text and loses its keyboard. The last part needs a relay change (46-01); against
a relay that predates it the copy keeps its buttons, and the client says so.

**D4 — Only an `open` entry can be withdrawn.** Withdrawing an answered question
is refused and reports the status it found. Withdrawing twice is a no-op success
that re-sends the relay cancel — which is how a withdrawal whose relay call
failed is retried.

**D5 — The file decides a race; nothing is ever discarded.**
- The answer is applied first → the withdrawal is refused (`status: resolved`);
  the answer stands.
- The withdrawal is written first → a later answer is **recorded under the entry
  as a late answer and the status is not changed**. The Telegram message is
  finalized `✅ <answer>` with a note that the question had been withdrawn.

  This is one instance of a general rule this epic introduces: **an answer flips
  the status only from `open`.** Before, `apply_answer` replaced the status word
  whatever it was, turning `[SUPERSEDED by Q-269]` into `[resolved by Q-269]`.

**D6 — Ids are claimed by a commit on the upstream branch.** With
`[questions] sync = "push"`, an id is taken when a commit containing it has been
accepted by the remote. The entry is composed against the upstream's content,
pushed, and only then written to the primary checkout. A rejected push re-reads
the upstream and allocates again; nothing local needs undoing (§2.4).

**D7 — The tool pushes one commit that touches only queue files.** The commit is
built on the upstream tip with git plumbing — a private index, never the
checkout's — and pushed by hash (`<sha>:refs/heads/<branch>`). The local branch
is never pushed, so unpushed local commits stay unpublished (§2.2). The pushed
file is a snapshot of the primary checkout's copy (§2.3); earlier unpushed edits
to *that same queue file* therefore travel with it, and the result says so
(`carried_local_changes`).

**D8 — The primary checkout is only ever touched through a pathspec.** The tool
writes the queue file and runs `git commit --only -- <file>`. It never checks
out, stashes, resets, rebases, merges or pulls the branch. One exception, and it
is a no-op for every other path: when `HEAD` *is* the upstream tip, the local
branch is fast-forwarded onto the pushed commit, so an in-sync checkout stays
in sync instead of growing a twin.

**D9 — Every failure degrades to "written locally, and said so".** Detached
HEAD, a rebase or merge in progress, the primary checkout on another branch, no
upstream, no network, a push the remote refuses, an upstream copy that cannot be
merged with the local one: in each the entry is still written to the queue file
(the question is never lost), the id is still allocated above everything the
last known upstream contains, and the result carries a `sync` object stating
what was committed, what was pushed, and why not. What is owed is recorded and
retried by the listener and by `claude-questions --sync`.

**D10 — Per-message ladders get their own cap.** The relay validated a
per-message `nudge_schedule` against `nudge_max`, the cap for a chat's `/nudge`
ladder (3), so the documented default ladder (4 rungs) was rejected with 422 and
`ask` left an entry with no message. The relay gains `nudge_message_max`
(default 8); the client, against a relay that still rejects the ladder, re-sends
without it and reports the downgrade instead of leaving the entry orphaned.

**D11 — Claiming without asking.** `claim` (MCP) / `claude-questions --claim`
reserves the next id(s) in a ledger file (`[questions] claims`, default
`claims.md` in the queue directory) and syncs it like any other write. Allocation
reads the ledger, so a claimed id is never handed out again. `ask(id=…)` accepts
an id only if the ledger holds it and no entry uses it.

**D12 — The listener commits what it writes, through the same engine.** An
answer that arrives with no session running is applied, committed and pushed by
the listener. It cannot fight a session in the same checkout because every tool
write in a repository serialises on one lock, touches the index only for the
queue file's path, and stands down — writing the file, recording the debt —
whenever git itself is mid-operation.

**D13 — Opt-in, and inert without it.** `sync` defaults to `"off"`: no git
subprocess is ever started, and a workspace with `[questions]` but no `sync` key
behaves as it did before this epic, except for D5's rule and D14. A workspace
with no `[questions]` section is byte-identical to today (epic 23 invariant 9).
The default is off, not on, because turning it on lets a tool push to the
workspace's shared branch without the approval an agent's own `git push` would
need — that must be a decision somebody wrote down in `roles.toml`.

**D14 — The store's lock moves out of the working tree.** `<dir>/.lock` showed
up as an untracked file in every adopting repository. In a git workspace the
lock is now `<git-common-dir>/claude-questions.lock`. While a legacy
`<dir>/.lock` still exists the store takes **both**, so a pre-upgrade server
process (§2.7) and a post-upgrade one still exclude each other; deleting the
legacy file ends the dual locking.

**D15 — The operator's own pull is made clean by a merge driver, registered
locally.** (Added after review round 2.) The first tool write with `sync` on
registers `merge.claude-questions.driver` in the repository's `.git/config` and
the queue patterns in `.git/info/attributes` — nothing tracked, nothing pushed,
per clone. The driver runs the entry-level merge the engine itself uses, and
git's own three-way merge only for what that will not decide — and then only if
the result has no id heading two entries (rev. after review round 4: "git's
merge first" let a textually clean merge through that duplicated an entry). A
real conflict (one entry changed in two ways) is still a conflict. Rejected:
`merge=union` (built in, no executable — and it is exactly the line merge that
garbled two answers in round 1); leaving the queue file uncommitted so a pull
fast-forwards over it (a dirty or staged file blocks `pull --rebase` outright,
and is what D8 exists to avoid); a real merge commit with the pushed commit as
a parent (it would claim the upstream's other files were merged while keeping
ours — a silent revert on the next real merge). Off switch:
`[questions.git] merge_driver = false`.

## 4. Surface

```
ask(title, body?, options?, role?, tags?, id?)
  → { id, file, dispatched, message_id, pending_answers, sync?, warnings?, error? }

withdraw(id?, reason, message_id?)
  → { id, withdrawn, status, file, message_id, relay_cancelled, sync?, error? }

claim(count?, note?)
  → { ids, file, claimed_on_remote, sync?, error? }

claude-questions --withdraw Q-042 --reason "…"
claude-questions --claim [N] [--note "…"]
claude-questions --sync
```

`sync` appears in a result only when the workspace's `sync` is not `"off"`.

## 5. Configuration

```toml
[questions]
sync   = "push"        # "off" (default) | "commit" | "push"
claims = "claims.md"   # id ledger, relative to dir

[questions.git]        # all optional
remote  = "origin"     # default: the sync branch's configured remote, else "origin"
branch  = "main"       # default: the remote's default branch, else the checkout's branch
retries = 5            # re-allocations after a lost push race
message = "chore(questions): {summary}"
merge_driver = true    # D15: register the queue merge driver in this clone

[questions.format]
withdrawn = "withdrawn"   # the status word a withdrawal writes
```

## 6. Success criteria

- [ ] `withdraw` on an open, dispatched question: the entry reads `[withdrawn]`
      with the reason, the Telegram message shows it and has no keyboard, no
      nudge and no escalation follow, and `max_open` has a free slot.
- [ ] An escalated copy loses its buttons too (after 46-01 is deployed).
- [ ] An answer given to a withdrawn question is recorded as a late answer and
      does not change the status; a withdrawal of an answered question is refused.
- [ ] With `sync = "push"`, two machines asking at once get different ids, and
      neither pushes anything but a queue file.
- [ ] With the primary checkout 26 commits ahead of its upstream, an `ask`
      publishes exactly one commit touching exactly one file.
- [ ] Every degraded state in D9 leaves the entry in the file and names itself in
      the result.
- [ ] `claim` reserves an id that `ask` never reuses, with nothing sent.
- [ ] A workspace with no `sync` key starts no git process; a workspace with no
      `[questions]` section is byte-identical to before.

## 7. Out of scope

- **Scanning other branches for ids.** A question filed on a branch is protected
  by claiming its id on the sync branch first (D11), not by reading every ref.
- **Re-sending an undispatched entry**, and **localising the message frame** —
  filed as task 47.
- **Reconciling the operator's branch.** After the tool has pushed, a checkout
  that was ahead needs `git pull` or `git pull --rebase` before its own push.
  The tool reports it; it does not do it (D8). It does make it clean (D15).
- **Renumbering an entry whose id collided** while a machine was offline. The
  engine refuses to publish, names the id, and the documented way out is
  withdraw → delete → sync → ask again. Filed as task 47.
- **Line-ending-rewriting repositories** (`core.autocrlf`): untested.
