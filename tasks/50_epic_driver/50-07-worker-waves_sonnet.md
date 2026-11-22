# 50-07 — Worker waves (tick side)

**Status:** todo · **Depends on:** 50-04
**Read first:** [brd.md](./brd.md) §4 (OD-9) · [architecture.md](./architecture.md) §3 (worker names), §5.4
(`role.worker`, `repos`), §14 (all), §16.2 (`worktree_refused`) · [state.md](./state.md) P0-1, P0-7, OQ-1,
invariants 3, 4, 6 · review M4

## Goal

Give worker steps somewhere safe to work: driver-owned worktrees from a verified base, an integration branch the
driver owns, and a check that proves the closed work is on it — without ever touching the operator's checkouts or
discarding a change.

## Scope

In `epic_driver_lib.py`, all git through `World.git` (a thin `git -C <path> …` runner with a timeout,
`GIT_TERMINAL_PROMPT=0` and `-c core.hooksPath=/dev/null`, like `questions_git`'s discipline):

1. **Repo validation** (§5.4, §14.1): absolute, unique by `realpath`, a git work tree, unique `name`;
   `base_ref` → sha via `rev-parse --verify <ref>^{commit}`. Never reads the primary checkout's branch or status.
2. **Integration worktree** per repo at `<worktree_root>/integration/<name>` on `epic/<epic>`: create from `base_ref`
   when the branch is new, attach to the existing branch otherwise; refuse when `epic/<epic>` is checked out in any
   other worktree (`git worktree list --porcelain`), when the integration worktree is dirty, or when it is on another
   branch.
3. **Step worktrees** (§14.2): branch `epic-step/<epic>/<step>` from the `epic/<epic>` tip; one repo ⇒ the worktree is
   `<worktree_root>/<epic>-<step>`; several ⇒ the wave directory `<worktree_root>/<epic>-<step>/` with one worktree
   per repo at `<…>/<name>`. Idempotent: an existing worktree on the right branch is reused as is, dirty or not.
   Recorded in `state.worktrees`.
4. **The built-in sha check** (§14.4): parse the verdict's `<sha>` / `<name>=<sha>,…`, run `merge-base --is-ancestor`
   per repo in the integration worktree; all pass ⇒ the step's own check runs next (50-04's runner).
5. **Refusal conditions** returned as `worktree_refused` alarm conditions with the reason; nothing in this task
   resets, cleans, stashes, force-checks-out, removes a worktree, deletes a branch or pushes.
6. The session `--dir` for each worker step, for 50-06's spawn.

## Tests

Real git repositories in temporary directories (no network): a bare "remote", a primary clone left dirty and on
another branch, a second clone with no remote.

- `base_ref` resolution by branch, tag and sha; an unknown ref refused; the primary checkout's dirty state and branch
  have no effect.
- Integration worktree created, then reused; `epic/<epic>` checked out in the primary clone ⇒ refused and the
  primary clone's branch untouched; dirty integration worktree ⇒ refused.
- Step worktrees for one and for two repos (paths and basenames per §14.2); re-creation reuses a dirty worktree
  without losing its changes.
- The sha check: on the branch, not on the branch, a missing repo sha, a malformed sha.
- A repo without a remote works end to end (no push is attempted anywhere in this task).

## Done when

- No code path in the module can run `reset`, `clean`, `stash`, `checkout -f`, `worktree remove`, `branch -D` or
  `push` (a test greps the module's git argv literals).
- The operator's primary checkouts are byte-identical (status, HEAD, branch) before and after every test.
