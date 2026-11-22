# Review Report - 50-07 Worker waves (second pass)

## Verdict: PASS

No BLOCKER, HIGH or MEDIUM issues. 2 LOW.

Baseline before any mutation: `python3 tests/run_all_tests.py` (foreground) gave `Ran 2908 tests in 534.499s` / `OK (skipped=2)`, exit 0. Both changed files byte-compile and import. Final tree: `git status` shows only `.claude/hooks/epic_driver_lib.py`, `tasks/50_epic_driver/state.md` and `tests/test_unit_epic_driver.py` modified. `git diff --stat` shows 3 files, 1142 insertions and 26 deletions. This matches the start. No added line has trailing whitespace.

## Completeness Check

1. **Allowlist completeness: met.** Every `world.git` call site was checked against `GIT_ALLOWED`:
   - `_git_cached` and `repo_problems` (`rev-parse --is-inside-work-tree`; `rev-parse --verify --quiet --end-of-options <ref>^{commit}`).
   - `resolve_base_ref`, `_branch_sha` (`refs/heads/<b>^{commit}`), `list_worktrees` (`worktree list --porcelain`).
   - `_add_worktree` (both `worktree add` forms), `_branch_name_ok` (`check-ref-format --branch`), `status --porcelain` (line 7053), and `merge-base --is-ancestor --end-of-options`.
   - All of them pass. The only `git(` callers are inside `epic_driver_lib.py`, and the one `subprocess.run` for git is in `RealWorld.git`.
2. **Allowlist strictness: met.** I called `git_refusal` directly with about 25 bypass attempts. All were refused except the legitimate forms.
   - Refused: `--git-dir=x reset`, `-C / reset`, `RESET`, `Reset`, `worktree ADD`, `worktree remove`, and `-fB` or `-B` anywhere.
   - Refused: `--force=1`, a `--` separator, and `-b -B`.
   - Refused: `status --ignored`, `status -uall`, `rev-parse --git-dir`, `--local-env-vars`, `-z`, and `--orphan` or `--detach` before or after `--end-of-options`.
   - Refused: a trailing-newline flag, a bare `worktree`, and a repeated `--end-of-options` operand.
   - The matching is exact and case-sensitive, so an abbreviated long option never matches either.
   - `RealWorld.git` (line 538) refuses before `subprocess.run` and returns `CommandResult(rc=None, error=...)`, the same shape as a timeout. Callers handle it:
     - `_branch_sha` treats `rc != 1` as an error with a message.
     - `list_worktrees`, `_add_worktree` and `resolve_base_ref` check `res.ok`.
     - `repo_problems` appends a problem.
   - The runner's `-c core.hooksPath=/dev/null` is added after the check, so a caller cannot inject `-c`.
   - Aliases cannot shadow builtins (`status`, `worktree` and the others are all builtins), and the verb is matched exactly, so an alias cannot be reached.
3. **Snapshot: met.** `snapshot` records `for-each-ref` (name and sha) and `worktree list --porcelain`. `assert_unchanged` runs from `_GitRepos.tearDown` for every repo. Pre-existing refs must keep their sha, and pre-existing worktrees must be unchanged. New refs and branches must match `refs/heads/epic/<x>` or `epic-step/<x>/<y>`, or be registered test-made refs. `tearDown` also asserts that `gw.refused` is empty unless the test sets `expects_refusals`.
4. **LOW fixes: met.**
   - `_SHA_RE` is `[0-9a-f]{7,40}`, and all uses are `fullmatch`: lines 3840 and 3843 in 50-05's verdict parsing, and line 7193 in 50-07.
   - `merge-base` targets `refs/heads/<integration_branch>` (line 7198).
   - The ledger validator rejects a leading `-` in `base_ref`.
   - `--end-of-options` is used everywhere git accepts it. Installed git is 2.53.0 (feature since 2.24), and a git older than 2.24 would give a failed call, never an unsafe one. `check-ref-format --branch` does not take it, but its branch always begins `epic/` or `epic-step/`.
   - Both `# type: ignore[typeddict-unknown-key]` comments are restored. `git diff HEAD` contains no `type: ignore` line, so they are identical to HEAD.
   - Sanitised step-id collisions are rejected at validation (line 1970, message names both ids).
5. **Regressions in 50-04 to 50-06: none found.** The full suite is green, and the epic module ran 367 tests OK before mutation.

## Mutation results

Each mutation was backed up with `cp` to `/tmp/lib.bak`, run with `--module unit_epic_driver` (367 tests), then restored from the copy and confirmed with `diff` (empty). All failed tests as required.

| Mutation | Result |
|---|---|
| M1: `-b` to `-B` in `_add_worktree` (reviewer's bypass) | FAILED (failures=34, errors=4) |
| M2: dynamic `"pru"+"ne"` argv before `worktree list` (like the reviewer's `"res"+"et"`, my own variant) | FAILED (failures=29) |
| M3: my own bypass, `status --porcelain -uall` | FAILED (failures=10) |
| M4: `fullmatch` weakened to `match` and `$` anchor | FAILED (failures=1) |
| M5: `merge-base` target changed from `refs/heads/<branch>` to the bare name | FAILED (failures=2) |
| M6: leading-`-` `base_ref` check disabled | FAILED (failures=2) |

My first M5 attempt used a sed pattern that did not match, so nothing was mutated and the run was OK. I redid it with a pattern that matched and confirmed the file differed before running.

## Issues Found

### Issue 1: [severity: LOW]
- **File:** `.claude/hooks/epic_driver_lib.py:461-470` (`_git_env`)
- **Problem:** The env scrub does not remove `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT` or `GIT_CONFIG_KEY_*`, `GIT_EXEC_PATH` or `GIT_CONFIG_GLOBAL`. The runner's own `-c core.hooksPath=/dev/null` still wins over them. Aliases cannot shadow the allowed builtins, so this is not exploitable through the argv gate. It only matters if the driver's own environment is already hostile.
- **Fix:** Optionally set `GIT_CONFIG_NOSYSTEM=1` and drop `GIT_CONFIG_*` and `GIT_EXEC_PATH`. Not required.

### Issue 2: [severity: LOW]
- **File:** `.claude/hooks/epic_driver_lib.py:325`
- **Problem:** The `rev-parse` allowlist permits `--verify` and `--quiet` in any combination, and `status` and `rev-parse` accept arbitrary non-dash operands before `--end-of-options`. This is harmless because these are read-only forms, but the "exact forms" claim in the comment is a flag allowlist, not an argv-shape check.
- **Fix:** None needed. Optionally note the limitation in the comment.

## Code Quality Notes
- The tearDown refused-call guard is what makes dynamic-argv mutations visible (the refusal itself is harmless). It worked for M2.
- `_ALLOWED_NEW_REF` accepts any `epic/<x>`, not only the current epic's branch. That is slightly loose but adequate.
- `-B` on a fresh branch is behaviourally identical to `-b`, so it is caught only by the runtime guard, the allowlist tests and the grep, not by the snapshot. The fixer documented this, and M1 confirmed it fails.

## Questions for User
None.
