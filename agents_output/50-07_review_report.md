# Review Report - 50-07 Worker waves (tick side)

## Verdict: PASS

No BLOCKER or HIGH issues. 1 MEDIUM, 5 LOW.

Baseline: `python3 tests/run_all_tests.py` (foreground, before any mutation): `Ran 2895 tests in 390.377s` / `OK (skipped=2)`.
`py_compile` of `.claude/hooks/epic_driver_lib.py` and `tests/test_unit_epic_driver.py` is clean. The epic-driver module alone:
`-m unit_epic_driver` ran 354 tests, OK. End state: `git status` shows only the 3 originally-modified files (lib, state.md, test), `git diff --stat` unchanged
(870+/21-), and the lib and test file are byte-identical to the pre-mutation copies (`diff` against `/tmp` backups).

## Completeness Check

1. Repo validation (§5.4/§14.1): met. `repo_problems` (lib ~1683) is the factored-out 50-04 logic: absolute, unique realpath, unique name,
   work tree, `rev-parse --verify --quiet <ref>^{commit}`. `_validate_repos` is now a thin wrapper on the cached runner, so 50-04's uses are intact
   (suite green, TestBaseRef covers the E_REPOS path). Only `rev-parse` is run in the repo's own path; the primary checkout's branch and status are never read.
2. Integration worktree (§14.1, P0-7): met. `ensure_integration` (~6967): `check-ref-format --branch`, base sha resolved, `worktree list --porcelain`;
   refuses when `refs/heads/epic/<e>` is checked out in any other worktree (the operator's included), when the integration worktree is on another/detached
   branch, when dirty (`status --porcelain`), or when the path exists and is not a worktree. New branch: `worktree add -b <branch> <path> <base_sha>`.
   Existing branch: `worktree add <path> <branch>`, with no `-B`, `-f` or reset, so it cannot move the branch. If the branch is checked out elsewhere
   the refusal comes first (and git itself would also refuse without `-f`). Mutation M1 confirmed.
3. Step worktrees (§14.2): met. Branch `epic-step/<epic>/<step>` from the `epic/<epic>` tip. One repo gives `<root>/<epic>-<step>`; several give a wave
   dir with `<…>/<name>`; basename `worker_dir_name` (§3). A worktree already on the right branch is reused untouched, dirty or not (mutation M3
   confirmed). A worktree on the wrong branch is refused and left alone. `state.worktrees` records `{integration:{name:path}, steps:{step:{dir,repos,branch,refused}}}`.
4. Sha check (§14.4): met. `sha_check` accepts both verdict forms (bare sha for a single-repo step; `name=sha` per repo), runs
   `merge-base --is-ancestor <sha> epic/<epic>` with cwd the integration worktree, and treats a missing sha or a malformed sha as `invalid`
   without calling git (test asserts zero git calls for `--all`). rc 1 is `not_ancestor`; any other rc is `error`. `sha_gate_open` /
   `eligible_checks` hold the step's own check until the gate has passed for the current `shas`, and a newer ADVANCED closes it again.
   `run_sha_checks` marks done (`by=check`) a worker step with no check of its own. Mutation M2 confirmed.
5. `World.git` discipline: met. `RealWorld.git` (lib 490): `git -c core.hooksPath=/dev/null -C <cwd> …`, timeout, `_git_env()` with
   `GIT_TERMINAL_PROMPT=0` and `GIT_DIR` stripped (test_world_git_discipline), stdin DEVNULL.
6. Wiring to 50-06: met. `worker_dirs(state)` and `prepare_worker_steps` feed `session_dir_for` / `session_name_for` / `spawn_args`
   (`--dir`, tested). 50-06's code is unchanged; its tests are green. The tick that calls `prepare_worker_steps`, `run_sha_checks` and `worktree_conditions` is 50-08's.
7. `worktree_refused` condition (§16.2): met. `worktree_conditions` yields one per step whose last attempt was refused and which is not done.

## Git invocation audit (invariants 3, 4, 6)

Every call to `world.git` in the lib, including the 50-04 validator cache (`_git_cached`, `world.git(list(args), path)`), uses these verbs only:
`rev-parse`, `check-ref-format`, `worktree list`, `worktree add`, `status --porcelain`, `merge-base --is-ancestor`. The only variable-built argv is
`_add_worktree`'s two-branch list, which is `add -b <branch> <path> <sha>` or `add <path> <branch>`. There is no reset, clean, stash, checkout,
switch, restore, worktree remove or prune, branch delete, push or update-ref. The tests do `git switch`/`merge` only in the test's own worktrees.
The branch name goes through `check-ref-format --branch` and always starts with `epic/` or `epic-step/`, so it can't be option-like.
`path` is absolute.

## Issues Found

### Issue 1: [severity: MEDIUM]

- **File:** tests/test_unit_epic_driver.py `TestWorktreeDiscipline.test_no_git_argv_can_reset_clean_stash_force_remove_delete_or_push` (~line 868 of the diff); lib `World.git` (490)
- **Problem:** The invariant 3/4/6 guard is a source grep and is bypassable, and nothing at runtime enforces it. I verified:
  (a) `-B` is not in the forbidden list. Changing `"-b"` to `"-B"` in `_add_worktree` (which can reset an existing branch) left the whole epic-driver
  module green (354 OK);
  (b) a dynamically built argv passes. Inserting `_v = "res"+"et"; _x = ["--ha"+"rd"]; world.git([_v, *_x], real)` into `ensure_integration` left
  the grep test OK, because the verb regex only inspects calls whose first list element is a string literal and the forbidden list only matches quoted
  literals. (Literal `["reset","--hard"]` and `"--force"`/`"-f"` variants are caught.) In the dynamic case only the repo-snapshot tests caught the damage,
  and only because they run that code path against a dirty primary;
  (c) nothing in `RealWorld.git` itself refuses a non-allowlisted verb.
- **Fix:** Add `-B`, `--orphan`, `--detach`, `-c`, `--no-track`-style flags that move refs to the forbidden list. Better, enforce at runtime: an
  allowlist of `(verb, subverb)` pairs in `RealWorld.git`, and ideally also in the test fake, returning an error for anything else. The test can then
  assert behaviour rather than grep the source.

### Issue 2: [severity: LOW]

- **File:** lib `_SHA_RE` (3750; pre-existing, reused at ~7139)
- **Problem:** `^[0-9a-f]{7,40}$` accepts a trailing newline (`"abc1234\n"` matches). Such a sha would reach git as an argv element. Harmless (git
  rejects it with rc 128, giving `error`, never a pass) and a leading `-` is correctly excluded, but the `invalid` classification is wrong for this input.
- **Fix:** `fullmatch` or `\Z`.

### Issue 3: [severity: LOW]

- **File:** lib `sha_check` (~7143), `resolve_base_ref` (6906), `repo_problems` (1717)
- **Problem:** `merge-base --is-ancestor <sha> epic/<epic>` uses the unqualified name, so a tag called `epic/<epic>` (git prefers tags) would be checked
  instead of the branch. The `base_ref` is also passed to `rev-parse --verify` without `--end-of-options`; I confirmed a `base_ref` of `--abbrev-ref=3`
  is parsed as an option (read-only, so no damage, but it is option injection, and the validator doesn't reject a leading `-`).
- **Fix:** Use `refs/heads/epic/<epic>`; reject a leading `-` in `base_ref`, or pass `--end-of-options`.

### Issue 4: [severity: LOW]

- **File:** lib `queue_request` (~5379) and `_commit_event` (~5877)
- **Problem:** An unrelated edit removed two `# type: ignore[typeddict-unknown-key]` comments and left trailing whitespace on those lines (out of scope for 50-07).
- **Fix:** Revert those two lines, or strip the trailing whitespace if the removal is intentional.

### Issue 5: [severity: LOW]

- **File:** tests `_GitRepos.snapshot` (~line 65 of the test diff)
- **Problem:** The before/after snapshot (porcelain status, HEAD, `symbolic-ref`, working files) runs in `tearDown` for every `_GitRepos` test, so the check is
  universal for primary and solo, which is good. It does not capture the ref set (`for-each-ref`) or the worktree list, so a moved or created branch
  in the operator's repo that is not HEAD would go unnoticed (`worktree add -b` creating `epic/e1` is expected, but a `-B`-style move of an existing
  non-HEAD branch is not asserted). This is why issue 1(a) survived.
- **Fix:** Add `git for-each-ref` output, `worktree list`, and the `epic/e1` ref's old sha to the snapshot, with explicit allowed deltas.

### Issue 6: [severity: LOW]

- **File:** lib `_ensure_step_worktree` / `worker_dir_name`
- **Problem:** Directory names are sanitised (`W` and `w`, or `50-07` and `50.07`, give the same dir) while branch names use the raw step id. Colliding
  steps end up refused ("on another branch"), never overwritten, so this is safe. It is just an unexplained refusal.
- **Fix:** Optionally detect a duplicate sanitised step id at ledger validation.

## Code Quality Notes

- Test hygiene is good: real git in `self.tmp`, `HOME` / `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_NOSYSTEM` isolated, bare local remote. The only `push` is in test
  setup (`push origin main` to the temp bare remote); the no-remote repo (`solo`) is exercised end to end, and nothing in the lib pushes.
- Mutation results (module `unit_epic_driver`): M1 (drop the checked-out-elsewhere refusal) killed by `test_refused_when_the_operator_has_the_branch_checked_out`. M2 (rc 1
  counted as a pass) killed by `test_a_sha_not_on_the_branch_fails_for_that_repo_only`. M3 (disable reuse) killed by 2 tests (dirty reuse; wrong-branch).
  M4a (literal `reset --hard` in `ensure_integration`) killed by the grep test and by the snapshot tests. M4b (dynamic verb with a literal `--hard` arg) was
  caught by the grep test. A fully dynamic verb and args built with `+` were NOT caught (issue 1). M4c (`-b` to `-B`) survived (issue 1).
- `test_the_step_branch_starts_at_the_integration_tip` builds an unused `L2` ledger before `L3`, which is harmless.
- `run_sha_checks` only handles VERIFYING/WAITING, while `eligible_checks` gates all `CHECKED_STATUSES`. This is consistent, since WATCHING steps are non-worker.
- Installed hooks are unaffected, as this is the library only (no `install.sh` wiring needed until 50-08/50-10).

## Questions for User

None.
