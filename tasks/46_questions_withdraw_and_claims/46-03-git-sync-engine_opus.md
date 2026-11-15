# 46-03 — Git engine: claim on the upstream, push only queue files

**Status:** done · **Depends on:** 46-02
**Read first:** [brd.md](./brd.md) §2.2–§2.6, D6–D9, D13 · [architecture.md](./architecture.md) §4 ·
`.claude/hooks/questions_store.py` (capture, `id_floor`)

## Goal

`.claude/hooks/questions_git.py` — the only module that starts `git`. One entry
point, `run(store, op, summary=…)`, wraps a single store mutation in the
transaction of architecture §4.2.

## Scope

- `sync = "off"` → `return op(store), None`; no process is started.
- `sync = "commit"` → write, `git commit --only -- <files>`.
- `sync = "push"` → fetch; set `id_floor` from the upstream; in a capture,
  integrate the upstream's queue-file changes (§4.3) and run `op`; build a
  commit on the upstream tip with a private index; push it by hash; on a
  non-fast-forward rejection discard the capture and loop (`retries`); on
  acceptance materialize and record locally — moving the branch onto the pushed
  commit when `HEAD` is the upstream tip, a twin commit otherwise.
- Every row of the §4.4 table: the entry is written, the report names the row,
  the repository is marked pending.
- Sync state and `pending` in `<git-common-dir>/claude-questions-sync.json`;
  `sync(store)` and `is_pending(store)` for the retry owners (§4.5).
- Subprocess discipline of §4.6: timeouts, `GIT_TERMINAL_PROMPT=0`, hooks off,
  inherited `GIT_DIR`/`GIT_INDEX_FILE` dropped, the private index removed.
- `run` never raises for a git reason; `op`'s own exceptions propagate.

## Tests

`tests/test_unit_questions_git.py`, against real repositories (a bare remote
and clones in a temp dir; the developer's global git config is masked):

- off: `subprocess.run` patched to fail the test if called;
- in sync: one upstream commit, one file, local `HEAD` == the pushed commit, no
  twin, the private index gone;
- ahead by unpushed commits: exactly one commit published, the unpushed work
  absent from the remote, a later `git pull --rebase` clean with the twins gone;
- dirty, staged and untracked files byte-identical in `git status` afterwards;
- two machines: a stale checkout allocates above the upstream; **a push that
  loses the race re-allocates** (`op` called twice, ids differ, no duplicate
  left behind); retries exhausted → local commit, pending;
- both sides appended → merged; the same id on both sides → refused, nothing
  pushed; **two answers to one entry on two machines → refused** (the line-merge
  defect review reproduced); the entry-level merge rules one by one, without a
  repository (`TestMergeFunctions`);
- what the engine refuses to publish: a symlinked queue file, an upstream
  symlink, a path outside the queue set, stray files under a wide glob, a
  git-ignored queue;
- after the remote accepted: a disk error keeps the result and the entry is not
  pushed away by the retry; a failed local commit stays on record when the
  repository already owed; a push that timed out after acceptance is detected;
- budgets: `offline`, `network_timeout`, `retries`, the total budget, the
  cool-down after a network failure;
- (round 2) **two checkouts asking from threads at once**: eight asks, eight
  ids, all pushed — fails three ways without the race fix; the classifier on
  the exact `failed to update ref` output; a refusal while the branch moved is
  retried, a refusal with the branch where it was is not;
- (round 2) **the operator's pull after two machines asked in turn**: `git pull`
  and `git pull --rebase` are clean through the merge driver; a real conflict
  is still a conflict; registration is local, idempotent, keeps other
  attributes, and `merge_driver = false` registers nothing;
- (round 2) a glob match that is not a queue is neither merged nor brought down
  nor allowed to block a push; an interrupted write is seen and healed by the
  next write, the next sync, and `is_pending`; the documented way out of an id
  collision works end to end;
- every degraded state of §4.4;
- pending paid by `sync()`, by the next ordinary write, and after a blocked
  checkout unblocks;
- workspace hooks not run (mutation-checked: removing the suppression fails it).

## Done when

- With the checkout N commits ahead, an `ask` publishes one commit touching one
  file.
- No test leaves a file in the clone that `git status` reports and the test did
  not put there.
