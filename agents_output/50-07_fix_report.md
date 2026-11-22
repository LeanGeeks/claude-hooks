# Fix Report - 50-07 Worker waves

Files changed: `.claude/hooks/epic_driver_lib.py`, `tests/test_unit_epic_driver.py` (state.md untouched, nothing committed).

## Issue 1 (MEDIUM): runtime git allowlist
- Change: new `GIT_ALLOWED` table and `git_refusal(args)` in the lib. `RealWorld.git` calls it first and returns `CommandResult(rc=None, error="git refused: ...")`
  (the same shape as a timeout/OSError result) without running git. `FakeWorld.git` in the tests calls the same function.
- Final allowlist, as exact (verb, subverb) with the only flags allowed:
  - `rev-parse`: `--is-inside-work-tree`, `--verify`, `--quiet`, `--end-of-options`
  - `check-ref-format`: `--branch`
  - `worktree list`: `--porcelain`
  - `worktree add`: `-b`, `--end-of-options`
  - `status`: `--porcelain`
  - `merge-base`: `--is-ancestor`, `--end-of-options`
- Everything else is refused: a verb starting with `-` (so no `-c`/`-C`/`--git-dir`; the runner's own `-c core.hooksPath=/dev/null` is added after the check), any other verb
  (reset, clean, stash, push, update-ref, branch, checkout, switch, restore, rebase), any other `worktree` subverb (remove/prune/move), any flag not listed (`-B`, `-f`, `--force`,
  `--detach`, `--orphan`, `-D`, `-d`, `-M`, `-m`, `-c` anywhere). After `--end-of-options` any operand starting with `-` is refused too. `checkout` is not needed by the module, so it is not allowed in any form.
- Tests (class `TestGitAllowList`, `test_git_refusal_unit`): about 45 forbidden argvs, each refused by `RealWorld.git` against the temp repo with the full snapshot unchanged
  (and no `/tmp/pwned` or worktree created); `subprocess.run` is never called for them; the fake refuses the same list; allowed forms still run; a dynamic `"res"+"et"` argv is refused.
  Additionally `RecordingRealWorld` and `FakeWorld.refused` record refusals, and `_GitRepos.tearDown` / `_Base.tearDown` assert that the module itself never attempted a refused form
  (this is what makes a dynamic-argv mutation fail a test, since the refusal is otherwise harmless). The grep test is kept as a secondary guard with `-B -M -m -c --orphan --detach update-ref` added
  (`branch` is not in the grep list because the dict key `"branch"` is legitimately in the source; the behavioural tests cover `git branch`).

## Issue 2 (LOW): `_SHA_RE`
`_SHA_RE` is now `[0-9a-f]{7,40}` and all three uses (50-05's verdict parsing, two places) and 50-07's `sha_check` use `fullmatch`.
Tests: `test_trailing_newline_sha_is_invalid_and_the_regex_uses_fullmatch` (invalid, zero git calls), regex unit assertion.

## Issue 3 (LOW): refs and option injection
- `merge-base --is-ancestor --end-of-options <sha> refs/heads/epic/<epic>` (a tag cannot shadow it). `_branch_sha` already used `refs/heads/`.
- Git is 2.53.0, so `--end-of-options` (2.24+) is used in the base_ref `rev-parse --verify` (both `resolve_base_ref` and `repo_problems`), in `merge-base`, and in both `worktree add` forms
  (checked by hand that all four accept it). It is an allowed flag; a git older than 2.24 would give a failed call, never an unsafe one. `check-ref-format --branch` does not take it, but its branch always starts `epic/` or `epic-step/`.
- The ledger validator rejects a `base_ref` starting with `-` (`E_SCHEMA`, `steps.<id>...repos[i].base_ref`).
- Tests: a tag `epic/e6` pointing at a commit not on the branch gives `not_ancestor`; `--abbrev-ref=3` refused at validation; base ref resolved after `--end-of-options`;
  50-06/50-07 call-list assertions updated.

## Issue 4 (LOW): type-ignore comments
Both `# type: ignore[typeddict-unknown-key]` comments restored as at HEAD (`queue_request`, `_commit_event`); no added line has trailing whitespace (`git diff -U0 | grep` is empty).

## Issue 5 (LOW): snapshot coverage
`_GitRepos.snapshot` now also holds `for-each-ref` (name and sha) and `worktree list --porcelain` (path, HEAD, branch). `assert_unchanged` (tearDown) requires: status, HEAD, symbolic-ref and files equal;
every pre-existing ref has the same sha; every pre-existing worktree is unchanged; new refs must match `refs/heads/epic/<x>` or `refs/heads/epic-step/<x>/<y>`; new worktrees must be on such a branch.
Two existing tests create their own branches (`side`, `elsewhere`) with `git switch -c` in the driver's worktree; they register these in `self.test_made_refs`.
The `-b` to `-B` mutation: see below. A `-B` on a branch that does not exist yet is behaviourally identical to `-b`, so the snapshot cannot see it (nothing moves, only an allowed new ref appears). It is caught by the runtime
guard and the grep. When `-B` is applied to an existing branch (the move case) the tests fail through `test_an_existing_branch_is_attached` (`'second' != 'first'`).

## Issue 6 (LOW): sanitised step-id collisions
50-04's check already covers it: `_parse_step` sets `key = sanitise(id)` and the step loop in the validator rejects a second step with the same key (`E_NAMES`). I ran the reviewer's examples (`W`/`w`, `50-07`/`50.07`,
both worker steps) and both are rejected. The session suffix and `worker_dir_name` both use `sanitise(step)`, so they collide exactly when the key does. The reviewer's observation was about a path that
never reaches the worktrees (the ledger is invalid). Change: the message now names both ids: `step 'w' collides with step 'W' after sanitising (w)`. Test: both examples with worker steps, exactly one error, both ids in the message.

## Mutations (each backed up with `cp`, restored from the copy, `diff` empty)
- M1 `-b` to `-B` in `_add_worktree`: now fails (21 failures + 4 errors, including the allow list, `test_created_from_base_ref_then_reused`, all TestShaCheck/TestStepWorktrees). Previously survived.
- M2 `_v = "res"+"et"; ...; world.git([_v, *_x], real)` in `ensure_integration`: now fails (30 failures: refusal recorded; step-worktree tests). Previously only the snapshot tests caught it.
- Extra, with the runtime guard removed: `-B` on a fresh branch is caught by the grep and the allow-list tests only (see issue 5); `-B` on existing branches is caught by the attach test.

## Counts
- `py_compile` of both files: clean.
- Epic driver module (`--module unit_epic_driver`): Ran 367 tests, OK (354 before).
- Full suite `python3 tests/run_all_tests.py` (foreground): `Ran 2908 tests in 534.663s` / `OK (skipped=2)`, exit code 0 (baseline 2895; +13 tests).

## git status --short
```
 M .claude/hooks/epic_driver_lib.py
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
```

## Questions for User
None.
