# Review 50-13, second pass (after the F-3 operator decision)

## Verdict: CLEAN (no HIGH or MEDIUM; two LOW notes)

Compile OK (epic_driver_lib.py, bin/epic-driver). No mutation testing; `git diff --stat` unchanged by me.

## Checks
- Decision consistent: `trust_folders` (epic_driver_lib.py ~9157) returns only the workspace and the state root; gate 4 in
  `enable` (~9277) exits 3 via `Refused` with fix text. No per-repo gate, no `worktree_root` entry, no dead gate code.
  Removed tests are gone; the replacement asserts exactly `["/ws","/st"]`. docs/epic-driver.md §3/§4, architecture §18.1,
  the 50-12 runbook (open point + `startup_dialog` fallback) and state.md agree; grep for per-repo/"each repo" text finds none.
- First review LOW 1 (worktree_root over-requirement): resolved by removal. LOW 2 (abspath/realpath): resolved
  (`trusted_by` uses realpath only); worktree-own-key leniency left, reasonable and harmless now.
- F-1, F-2, F-4: the fix touched only `trust_folders`, `trusted_by` and their tests/docs; gate numbering (5 login, 6+ rest)
  and `--login-days` untouched; the epic-driver unit module passes (545 tests OK).

## Findings
1. LOW - epic_driver_lib.py `trusted_by` (~9138): `for p in (os.path.realpath(path),):` is a one-iteration loop left over
   from the abspath/realpath pair. Cosmetic. Fix: `p = os.path.realpath(path)` and dedent.
2. LOW - epic_driver_lib.py `trusted_by` / docs §4: the workspace is normally a git repo, and Claude Code's rule (kept here)
   ignores trust on a parent above the git root. The brief says "trusted itself or through a parent"; for the workspace a
   parent-only trust therefore refuses, which matches real Claude behaviour (no false pass) and is stated in docs/epic-driver.md:209-210.
   Not a defect; the lead may mention it to the operator. docs line 209-210 is one very long line (style only).

## Suite
`python3 tests/run_all_tests.py`: exit code 0 (the final summary line was cut off by my tail; the last lines were
questions-tool output). tests/test_unit_epic_driver.py alone: Ran 545 tests, OK. The fix report records 3100 tests OK (skipped=2).
