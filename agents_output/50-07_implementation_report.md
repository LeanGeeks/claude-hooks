# Implementation Report - 50-07 Worker waves (tick side)

## Summary

Added the §14 worker-wave section to `epic_driver_lib.py`: base_ref resolution, a driver-owned integration worktree on `epic/<epic>`, step worktrees on `epic-step/<epic>/<step>`, the built-in sha check, and the `worker_dirs` result for 50-06's spawn. All git goes through `World.git`. No code path resets, cleans, stashes, force-checks-out, removes a worktree, deletes a branch or pushes.

## Files Created

- agents_output/50-07_implementation_report.md

## Files Modified

- .claude/hooks/epic_driver_lib.py
  - New section "Worker worktrees and the built-in sha check (§14, P0-7) — 50-07" at the end, and a row in the module docstring.
  - `_validate_repos` now delegates to a new shared `repo_problems(repos, git, verify_base_refs)`. The messages and fields are unchanged, and 50-04's tests are untouched.
  - `eligible_checks`: a worker step's own check is gated by `sha_gate_open` (§14.4, "in front").
  - `StepState` gains the `shas` and `sha_check` keys.
  - `World.git` / `RealWorld.git` already had the required discipline from 50-04 (timeout, `GIT_TERMINAL_PROMPT=0`, `-c core.hooksPath=/dev/null`, no stray `GIT_*`). It is unchanged and now covered by a test.
- tests/test_unit_epic_driver.py — 34 new tests (real git repos).

## Verification

- Compile/import: PASS (`py_compile` of both files, and `import epic_driver_lib`).
- Tests: `python3 tests/run_all_tests.py` gives 2895 tests, OK, skipped=2. The baseline was 2861, so +34. `--module unit_epic_driver` gives 354 OK. No pre-existing failures.
- Test fixtures: a bare remote, a primary clone that is dirty and on another branch (`operator-work`), and a remote-less clone. `HOME`, `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM` point into the temp dir.
- Primary checkouts: `tearDown` of every test asserts byte-identical status, HEAD, branch and files for both primary repos.
- Installed (install.sh --yes re-run): not applicable (not run).

## Public API added (contract for 50-08 / 50-09)

- **Names and layout:**
  - `integration_branch(epic)` returns `epic/<epic>`.
  - `step_branch(epic, step)` returns `epic-step/<epic>/<step>`.
  - `integration_dir(ledger, repo)` returns `<worktree_root>/integration/<name>`.
  - `step_worktree_paths(ledger, step)` returns `(session_dir, {name: path})`.
- **Repo validation:** `repo_problems(repos, git, verify_base_refs=True)` returns `[(index, field, message)]`. `resolve_base_ref(world, repo)` returns `(sha, None)` or `(None, why)`.
- **Worktrees:**
  - `ensure_integration(ledger, world, repo)` returns `WorktreeResult(path, refused, created)`.
  - `ensure_step_worktrees(ledger, state, world, step_id)` returns `StepWorktrees(step_id, dir, repos, refused, created, .ok)`. It is idempotent and writes `state["worktrees"]`.
  - `list_worktrees(world, repo_path)` parses `worktree list --porcelain`.
- **State shape:** `state["worktrees"] = {"integration": {name: path}, "steps": {step: {dir, repos, branch, refused}}}`.
- **Tick entry points:**
  - `prepare_worker_steps(ledger, state, world, step_ids) -> Dict[str, str]` runs `ensure_step_worktrees` for the worker steps in `step_ids` and returns `worker_dirs(state)`.
  - `worker_dirs(state) -> {step: session --dir}` lists only steps that are ready and not refused.
  - `worktree_conditions(ledger, state) -> [AlarmCondition("worktree_refused", step, reason)]` skips done steps, and the condition goes when the next ensure succeeds.
- **Sha check:**
  - `sha_check(ledger, world, step_id, shas) -> ShaCheck(repos=[RepoSha(repo, status, sha, detail)], .passed, .status, .detail)`.
  - Statuses are `SHA_PASS`, `SHA_NOT_ANCESTOR`, `SHA_INVALID` (missing or malformed, no git call) and `SHA_ERROR`.
  - `shas` is `steps[*].shas` from 50-05: `{BARE_SHA: sha}` or `{name: sha}`. A bare sha counts for a single-repo step.
  - `sha_gate_open(step, rec)`.
  - `run_sha_checks(ledger, state, world, now)` is called in the tick before `run_checks`. It covers worker steps in verifying or waiting status. It records `rec["sha_check"] = {passed, status, shas, detail, at}`. When it passes, the gate opens and the own check becomes eligible. A step with no own check becomes `done` with `by="check"`.

### How `--dir` reaches 50-06's spawn

50-06 already takes `worker_dirs: {step: dir}` (`plan_dispatch`, `step_due_items`, `session_dir_for`, `session_name_for`, `spawn_args`). When it is given, a step without a directory is not dispatched. 50-08 should call `worker_dirs = prepare_worker_steps(ledger, state, world, <dispatchable worker step ids>)` before `plan_dispatch(..., worker_dirs=worker_dirs)` and raise `worktree_conditions`. Nothing in 50-06 changed. A test checks that `--dir` equals the wave directory (several repos) and that the session name is `e1-w-e1-impl-w-g1`.

## Decisions

- `base_ref` is resolved and verified on every ensure, even when `epic/<epic>` already exists. An unresolvable base is a refusal (§14.2).
- A step worktree uses the existing step branch if it exists and is not checked out elsewhere. It is refused if the branch is checked out in another worktree, or if the path exists but is not a registered worktree. An existing worktree on the right branch is reused as is.
- Refusals leave anything already created in place. For example, with two repos where the second is refused, the first repo's worktrees stay, and the next tick retries.
- Repo names are used as directory names. A name outside `[A-Za-z0-9._-]`, or `.` or `..`, is refused at worktree time rather than changing 50-04's validator.
- A branch name that fails `git check-ref-format --branch` (a bad epic or step id) is refused.
- The worktree root is not forbidden under `/tmp`. The default is under the state dir (OQ-1), and tests need temp roots.
- The sha check runs every tick until it passes, since it is local and cheap. Git-level errors are recorded but do not raise a separate alarm (no §16.2 row).
- The git-argv test:
  - It checks the new section for forbidden literals: reset, clean, stash, push, checkout, restore, rebase, switch, remove, prune, gc, `-D`, `-d`, `-f`, `-F`, `--force*`, `--hard`, `--mirror`.
  - It checks that the verbs handed to the git runner across the module are exactly `{rev-parse, check-ref-format, worktree, status, merge-base}`, and that the `worktree` subverbs are `{list, add}`.
- The closing steps of §14.3 item 5 (commit, `merge --no-ff`) are driver or LLM actions and are not in this module. The tests perform them by hand to exercise the sha check.

## Blockers

None

## Questions for User

None
