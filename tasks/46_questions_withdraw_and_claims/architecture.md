# Epic 46 — Architecture

Read [brd.md](./brd.md) first. Section numbers are referenced from the task files.

## 1. Shape

```
MCP server (questions-mcp/)          CLI (shell/claude-questions)       listener
  ask ──────────────┐                  --withdraw / --claim / --sync      apply_answer
  withdraw ─┐       │                         │                              │
  claim ────┤       │                         │                              │
            ▼       ▼                         ▼                              ▼
      questions_ops.py  (withdraw / claim / sync orchestration — installed hook module)
            │                                                                │
            ▼                                                                ▼
      questions_git.run(store, op)  ── the one place git is ever invoked ────┘
            │                    │
            ▼                    ▼
      questions_store.py      git (subprocess, pathspec-limited, private index)
      (pure: files only)
```

- `questions_store.py` stays pure (brd §2.5). It gains withdrawal, the claims
  ledger, an explicit-id path, an id floor, a re-entrant lock and a *capture*
  overlay that lets a caller run a mutation without touching the disk.
- `questions_git.py` (new, `.claude/hooks/`) is the only module that starts
  `git`. It wraps one store mutation in a claim-on-the-upstream transaction.
- `questions_ops.py` (new, `.claude/hooks/`) holds the operations two front ends
  share (withdraw, claim, sync). `ask` stays in `questions_mcp_lib.py`, where it
  already lives, and calls `questions_git.run` for its two writes.

## 2. Relay (46-01)

- `POST /v1/messages/{id}/cancel` cancels open escalated children on every call
  (not only when it flipped the parent), and accepts an optional
  `{"final_text": …}` that becomes the canonical body of the parent (if this call
  flipped it) and of each cancelled child.
- `nudge_message_max` (default 8) caps per-message ladders at all three parse
  sites; `nudge_max` keeps capping the per-chat ladder. Both env-settable.
- `RelayClient.cancel_message(id, final_text=None)`.
- No schema change.

## 3. Store (46-02)

### 3.1 Status vocabulary

`QuestionsFormat.withdrawn` (default `"withdrawn"`). `status_set` =
`status ∪ {withdrawn}`. `open_status` and `resolved_status` are unchanged.

### 3.2 Withdraw

`withdraw(qid, reason, *, withdrawn_by, rel_path=None, role=None, withdrawn_at=None) → WithdrawResult`

| Entry state | Outcome | File |
|---|---|---|
| status is the open word, no marker | `withdrawn` | status → withdrawn word; block inserted |
| `<!-- withdrawn -->` marker present | `already_withdrawn` | untouched |
| any other status, or no status bracket | `not_open` (carries the status) | untouched |
| no entry / two entries | `not_found` / `conflict` | untouched |

The block, inserted before the boundary like an answer block:

```
<!-- withdrawn -->
**Withdrawn:** <when> · <who>

```text
    <reason, through escape_answer_text>
```
```

The marker is recognised only anchored at line start **inside the entry's own
line range**. `escape_answer_text` already guards a line starting with `<!--`,
so neither an answer nor a reason can forge it.

`WithdrawResult` also carries the entry's title, its file, and the relay message
id read from its `**Dispatched:** … relay #N` line — everything the caller needs
to finish the job on the relay, read under the same lock as the write.

### 3.3 `apply_answer` — flip only from open

- status word is the open word → flip to resolved, insert the block → `applied`
  (unchanged).
- no status bracket → insert the block → `applied` (unchanged, rule 6).
- anything else (withdrawn, resolved by hand, superseded, …) → the heading is
  **not touched**; the block is inserted with one extra line after the marker,
  `**Late answer** — recorded after this entry was marked [<status>]; status
  unchanged.` → `ApplyResult.LATE`.

Idempotency on the `<!-- answer:N -->` marker is unchanged and still checked
first.

### 3.4 Claims ledger

`<dir>/<claims>` (default `claims.md`). Created on first claim with a two-line
header. One line per claimed id:

```
- Q-591 · 2026-11-15T10:22:11Z · <who> · <note>
```

`claim(count, *, note, claimed_by) → list[str]` allocates `max + 1 …` under the
lock and appends. `_max_number` reads the ledger (ids at the start of a list
item only). `claimed_ids()` returns the set.

`create_entry(..., qid=None)`: an explicit id must parse, must be in the ledger
and must not head an entry anywhere in the queue set — otherwise
`QuestionsStoreError`. The ledger line stays; it is history.

`id_floor` (instance attribute, default 0) is a lower bound on `_max_number`,
set by the git engine from what the upstream contains.

### 3.5 Lock

`lock_path`: `<git-common-dir>/claude-questions.lock` when the queue directory is
inside a git work tree, else `<dir>/.lock` as before. If a legacy `<dir>/.lock`
exists the store flocks it **as well** (after the primary lock, never creating
it), so an old process and a new one still exclude each other (brd D14).

`lock()` is re-entrant per store instance and thread: the engine holds it across
a whole transaction and the mutators it calls take it again.

### 3.6 Capture

```python
with store.capture() as cap:      # nothing below reaches the disk
    cap.seed(path, text)          # pretend the file holds this
    result = store.create_entry(...)
cap.writes                        # {Path: new text} — seeds included
store.materialize(cap)            # atomic write of every captured file
```

Every read in the store goes through `_read_lines`, every write through
`_write`; inside a capture both consult the overlay first. A capture is only
meaningful under the store lock, and `capture()` takes it.

## 4. Git engine (46-03)

### 4.1 Entry point

```python
result, report = questions_git.run(store, op, summary=…, sync_only=False)
```

`op(store)` performs one store mutation and returns its result. `summary` is a
string or a function of that result (the id is not known until `op` has run).
`report` is a `SyncReport`; `report.to_dict()` is what a tool result carries
under `sync`.

With `sync = "off"` `run` is `return op(store), None` — no import of
`subprocess`-using code paths is exercised, no git process starts (brd D13).

`run` never raises for a git reason. `op`'s own exceptions propagate unchanged.

### 4.2 The transaction (`sync = "push"`)

```
store.lock()                                   # one lock per repository
  state ← inspect the checkout                 # §4.4; decides what is possible
  loop up to 1 + retries times:
    fetch <remote> <branch>                    # failure → local path, "network"
    U ← tip of the upstream branch
    store.id_floor ← highest id in U's queue files and ledger
    capture:
      integrate U's queue-file changes         # §4.3; failure → local path
      result ← op(store)                       # allocates against U's content
    nothing written → return                   # e.g. an answer already applied
    C ← commit(parent U, tree U + captured files)   # private index, hash-object
    record "in flight: C" in the sync state     # §4.5
    push C:refs/heads/<branch>
      accepted → break
      a lost race → discard the capture, loop   (re-allocation)
      anything else → fetch, then decide by what is true on the remote:
        C is on the branch            → accepted (a push that timed out after it landed)
        the branch moved off U        → a lost race, whatever the remote called it
        the branch is where it was    → local path, with the remote's reason
  materialize:
    HEAD == U and the captured paths are clean  → git merge --ff-only C
    otherwise  → write the files, git commit --only -- <files>     (the twin)
  record the pushed blobs as the new sync base  # §4.3
```

**What a lost race looks like.** Not only `non-fast-forward` / `fetch first` /
`stale info`. Two pushes updating the ref at the same instant give the loser
`! … [remote rejected] (failed to update ref)` with `cannot lock ref
'refs/heads/main': is at X but expected Y` on stderr — a *remote rejected* line
that a retry does fix. Round 1's classifier tested `[remote rejected]` first and
called it a refusal; the local path then allocated the id the winner had just
taken (review round 2 measured 8 duplicated ids in 12 concurrent asks). The
race markers are now tested first, and — because no list of wordings is ever
complete — any push that did not succeed is settled by fetching and looking.

**Local path** (any degraded state, and the whole of `sync = "commit"`): run
`op` in a capture with the best known `id_floor`, materialize, `git commit
--only` if the checkout allows a commit, mark the repository *pending* (§4.5)
when a push was wanted and did not happen.

### 4.3 Integration and the sync base

For each queue-set file (and the ledger) that exists upstream, with `ours` = the
primary checkout's working copy, `theirs` = the copy at `U`, and `base` = the
content this tool last pushed for that path (from the sync-state file; falling
back to the copy at `merge-base(HEAD, U)` when none is recorded):

| Condition | Action |
|---|---|
| `theirs == ours` | nothing |
| `theirs == base` | nothing — upstream has not moved; the local copy wins (snapshot) |
| `ours == base`, or the file is absent locally | seed the capture with `theirs` |
| both changed | an **entry-level** three-way merge (`merge_queue_text`; `merge_ledger_text` for the ledger); integration fails if it refuses |

The entry-level merge: the unit is the entry (heading to the next entry
heading), never the line. Per entry, and for the preamble — the sides agree →
take it; one side changed it since the base → take that side's; both changed it
differently → for an entry, take the one that is the other's **later state**
(below), else **refuse**. Upstream-only entries are appended; what is ours is
kept byte for byte. It also refuses when an id heads two entries on either side
and when an entry was deleted on one side. The ledger merges as the union of its
claim lines, refusing one id claimed two ways.

> The first implementation used `git merge-file`, falling back to `--union` with
> a check that every id survived exactly once. Review reproduced the hole: two
> machines answer one entry, `--union` keeps both hunks and merges their common
> tail, and the result is one unclosed fence with the other answer's marker
> inside it — which parses, passes the check, and was pushed. A line merge
> cannot be trusted with a block-structured file.

The base cannot be git's own merge base: after the first twin commit the merge
base stops advancing, so an entry this machine pushed once and the upstream then
changed again would look "changed on both sides" (brd §2.3).

**The later state** (`_later_state`, added after review round 3). An entry only
ever grows: asked → dispatched → answered, or → withdrawn (→ a late answer);
each step keeps every line of the one before, flips the status word and adds
lines. So when two versions of the same question (same heading apart from the
status word) differ and one contains the other's lines in order, the containing
one is later and taking it loses nothing. Never the reverse of a closure: if
the longer version is the *open* one and the other is closed, that is two
changes, and it is refused. This is what a three-way base cannot settle when
the base predates the entry — a twin carries another machine's question as it
was when the twin was made, and the upstream has since recorded the answer —
and it is the rule that makes the operator's pull clean (§4.7) and keeps a
clone with a stale base from refusing every write. Two versions closed with
*different* status words are never one history, whatever their lines. And when
the base does have the entry, the earlier version must contain every line of
the base: a version that *lost* a line is an edit, not an earlier state, and
taking the other one whole would quietly put the line back (round 5) — the
engine refuses it, and the driver hands it to git's line merge, which keeps
both the deletion and the answer. The base still decides
first whenever it can: a hand edit upstream that *shortens* an entry is taken,
not overruled by the longer local copy.

**What is recorded as the base**: for every integrated file, the upstream's
blob — the pushed blob for the files just published, the tip's blob for the
ones only brought down. Round 3 found the second half missing: a sync that only
brought an entry down recorded nothing, so the upstream's next change to that
entry was compared against a base that predated it.

**The base is recorded only when the local file was actually written.** "The
upstream equals the base" means "the upstream has not moved; ours wins". A base
the local file never reached (a full disk after an accepted push) would make the
next sync push the stale local file over the entry just published.

Sync state lives at `<git-common-dir>/claude-questions-sync.json`:

```json
{ "upstream": "origin/main",
  "base": { "docs/questions/for-tech-lead.md": "<blob sha>" },
  "pending": false, "pending_since": "", "pending_reason": "" }
```

### 4.4 Checkout states

| State | Entry written | Local commit | Push | `sync.reason` |
|---|---|---|---|---|
| normal, in sync | yes (via ff) | the pushed commit itself | yes | — |
| normal, ahead / behind / diverged | yes | twin | yes | — (`twin: true`) |
| queue file has earlier uncommitted edits | yes | includes them | yes | — (`carried_local_changes: true`) |
| other files dirty or staged | yes | unaffected (`--only`) | yes | — |
| primary checkout on another branch than the sync branch | yes | **no** | **no** | names both branches |
| detached HEAD | yes | no | no | `detached HEAD` |
| rebase / merge / cherry-pick / bisect in progress, or `index.lock` held | yes | no | no | names the operation |
| no upstream / no remote | yes | yes | no | `no upstream` |
| fetch fails | yes | yes | no | the fetch error |
| push refused by the remote (protected branch, hook) | yes | yes | no | the remote's message |
| upstream copy cannot be merged with the local one | yes | yes | no | names the file |
| retries exhausted | yes | yes | no | `lost the push race N times` |
| not a git work tree | yes | no | no | `not a git work tree` |
| a queue path is a symbolic link, or ignored by git | yes | no | no | names the path |
| the mutation wrote a path outside the queue set | yes | no | no | names the path |
| the remote failed < 60 s ago (cool-down) | yes | yes | no | `push deferred: …` |
| pushed, then the local write failed | **no** | no | yes | `pushed …, but the local write failed` |
| a push that timed out but was accepted | yes | as normal | yes | — (detected by re-fetching) |

In every row but the first three and the last the repository is marked pending.

### 4.5 Pending and retry

`pending` in the sync-state file means "this repository owes its upstream a
queue-file commit, or its own branch a commit". It is set by a degraded
transaction and cleared by a successful one.

`questions_git.run(store, op=noop, sync_only=True)` — `claude-questions --sync`,
and the listener's retry pass — commits whatever queue-set files differ from
`HEAD` and pushes a snapshot of every queue-set file that differs from the
upstream after integration.

The listener runs that pass for each repository its index knows about, at most
once every five minutes per repository, **only when the pending flag is set**:
an idle repository is never touched. What a sync publishes when it does run is
the *file* — every queue file that differs from the upstream, hand edits in it
included (review corrected an earlier claim here that hand edits are never swept
up; that is true of *when* the listener syncs, not of *what* a sync carries).

**Interrupted after the push.** The `in_flight` marker is written before the
push and removed once the local half is done. `run` holds the repository lock,
so a marker found at the start of a transaction was left by a process that
died in between: the entry is upstream, the checkout lacks it. The transaction
marks the repository pending, runs in "everything" mode (which brings the
upstream's copy down) and removes the marker. `is_pending` — read by the
listener without the lock — counts a marker only once it is older than any live
transaction can be (`IN_FLIGHT_STALE_S`).

### 4.6 Subprocess discipline

- Every call has a timeout (15 s local, 20 s for a fetch or a push) and
  `GIT_TERMINAL_PROMPT=0`; a prompt is a failure, never a hang.
- A retry after a lost race waits a short random time first (50–300 ms × the
  attempt number): checkouts that lost to the same winner would otherwise retry
  in step and lose to each other. `retries` defaults to 5.
- No new push attempt starts after `TOTAL_BUDGET_S` (60 s); after a network
  failure the remote is not tried for `NETWORK_COOLDOWN_S` (60 s), except by an
  explicit sync. Callers may pass a tighter `network_timeout` / `retries`, or
  `offline=True`.
- **Every** command runs with `-c core.hooksPath=/dev/null` (and commit/push
  with `--no-verify`): a workspace's pre-commit hook may run a test suite, a
  `reference-transaction` hook fires on `update-ref`, and `ask` holds the
  repository lock throughout.
- Only regular files (tree mode 100644/100755) are read from the upstream; a
  path is committed or pushed only if it is in the workspace's queue set.
- git's stderr is scrubbed of URL credentials and `hint:` lines before it
  becomes a `reason`.
- Blobs are written with `git hash-object -w --path=<rel>` so the repository's
  clean filters apply as they would for `git add`.
- The private index is a temp file inside the git directory, removed in a
  `finally`.
- The identity is the repository's configured `user.name`/`user.email`; none
  configured is a reported commit failure.

### 4.7 The merge driver (D15)

`_ensure_merge_driver`, called once the checkout has been inspected, whenever
`sync` is not off and `[questions.git] merge_driver` is not false:

- `git config --local merge.claude-questions.{name,driver}`; the driver is the
  fixed text `MERGE_DRIVER_COMMAND` — the installed CLI if it is executable,
  and `git merge-file %A %O %B` if it is not **or if it exits with anything but
  0 or 1**. Fixed, so the checkout's copy of the engine (the MCP server's) and
  the installed copy (the listener's) never rewrite each other's value.
- **Exit contract.** The CLI answers `MERGE_DRIVER_MERGED` (10) or
  `MERGE_DRIVER_CONFLICT` (11), and only after it has written the result; the
  command maps those to 0 and 1 and runs `git merge-file` for **every** other
  status. Round 3 showed why a failure must not look like a result (git treats
  any non-zero status as "conflict" and keeps `%A` as it was — an unmerged file
  with no markers and without the upstream's entries); round 4 showed why the
  two statuses cannot be 0 and 1 (1 is what an interpreter exits with when it
  dies — a syntax error in a half-updated install).
- `<git-common-dir>/info/attributes`: one `/<path> merge=claude-questions` line
  per named queue file, the ledger, and `/<dir>/<glob>`. Existing lines are
  kept; ours are replaced as a block. Patterns containing whitespace are
  skipped.
- The command and the lines are remembered in the sync state (`merge_driver`),
  so a repository already set up costs one dictionary comparison per write.
- Best-effort: a failure is logged and the write goes on.

`merge_driver(%O, %A, %B, %P)` (`claude-questions --merge-driver`, hidden):

1. `merge_queue_text` / `merge_ledger_text`, when the work tree has a
   `[questions]` config (read from `<toplevel>/.claude/roles.toml` — git runs
   drivers from the top of the work tree; `$CLAUDE_PROJECT_DIR` is deliberately
   not consulted, the merge may be in another repository than the session's)
   and the file holds an entry on either side or is the ledger. Decided →
   written, *merged*.
2. else `git merge-file -p`. Clean **and sound** (no id heads two entries) →
   written, *merged*. Clean and unsound → both whole files between conflict
   markers, *conflict*. Not clean → git's markers, *conflict*. A file with no
   entries (a README under a wide glob) therefore merges exactly as it always
   did.

Rounds 2–3 had these two the other way round ("never change a merge git could
already do"). Round 4 showed git's line merge returning a clean file with two
entries headed by one id — from an ordinary pull, with a hand-written entry in
the file shifting the alignment — which the engine then refused to touch. A
line merge is not to be trusted with a block-structured file even when it says
it succeeded; the entry merge goes first.

It never raises. The result is written atomically.

### 4.8 Which files are queue files

Three sets, narrowest last:

| Set | Is | Used for |
|-----|----|----------|
| queue set | named files + the ledger + every glob match | "may a mutation write this path?" |
| integrated | named files + the ledger + glob matches that hold an entry, here or upstream, and are not dotfiles / temp files | what is read from the upstream, merged and brought down |
| owed | the same test, on the local file | what an "everything" sync publishes beyond the paths just written |

Round 2 found the second set was the first: under `glob = "*.md"` a README
changed on both sides blocked every push, and one changed upstream was written
into the checkout and committed inside a question's commit.

## 5. Operations (46-04)

`questions_ops.py`:

- `withdraw(workspace_dir, qid=None, reason, message_id=None, *, by) → dict`
  1. `questions_git.run(store, lambda s: s.withdraw(…), summary="withdraw <id>")`
  2. `withdrawn` or `already_withdrawn` → resolve the token from the index entry
     (fallback: the entry's role through the role catalog) and call
     `cancel_message(final_text=text)`; `edit_message(text)` follows only for a
     relay that does not report how the text was shown (task 49 reversed the
     original order, in which a refused edit kept the cancel from being sent).
  3. Return the brd §4 shape. `relay_cancelled` is false with an `error` when the
     relay call failed; calling again retries it (brd D4).

  With `message_id` and no `qid` it withdraws an ack-notification: relay only,
  and only for a message the local index knows.
- `claim(workspace_dir, count=1, note="", *, by) → dict`
- `sync(workspace_dir) → dict`

`questions_mcp_lib.ask`:

- step 2 (`create_entry`) and step 7 (`mark_dispatched`) each go through
  `questions_git.run`. Two commits per ask is the price of keeping epic 23's
  invariant 1 (the entry is durable — and here, claimed — before the first relay
  call).
- a 422 `invalid nudge_schedule` from the relay is retried once with no
  per-message ladder; the result gains `warnings: […]`.
- `sync` is added to the result only when the engine returned a report.

## 6. Listener (46-05)

- `_apply` calls `apply_answer` through `questions_git.run`
  (`summary="answer <id>"`). `LATE` is success; the finalize text gains
  `(recorded as a late answer — the question was no longer open)`.
- The listener's git budget is its own: `LISTENER_GIT_TIMEOUT_S` (8 s) per
  network call, one re-try, and `offline=True` while a repository's last attempt
  failed within the retry interval — a batch of answers during an outage costs
  one timeout.
- A sync-pending retry pass (§4.5) after the pending-apply pass.
- `--status` lists repositories with a pending sync and the reason.
- No git failure can reach the loop: `run` reports, never raises.

## 7. Compatibility

- **Old server process, new files** (brd §2.7): dual locking (§3.5); the index
  shape does not change (pending sync lives in the repository, not the index);
  an old process that writes an entry simply does not sync it — the next synced
  write publishes it with the snapshot.
- **New client, old relay**: `cancel` ignores the body; escalated copies keep
  their keyboard until 46-01 is deployed. A four-rung ladder is retried without
  the ladder.
- **Workspaces with `[questions]` and no `sync`**: `status_set` gains the
  withdrawn word; an answer to a non-open entry no longer rewrites its status
  word; the lock file moves. Nothing else.
