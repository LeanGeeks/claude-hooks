# Fix Report 2 - Task 41-02 (review pass 2: MEDIUM 1, LOW 2, suite flake)

## Issue 1 (MEDIUM): cwd `/` loosened
- .claude/hooks/pretool_hook.py: `_is_workspace_binary` (~1118) and `_is_path_inside_workspace` (~1178) reverted to the 6772917 comparison (`root + os.sep`, no rstrip).
- `_realpath_inside_allowed_roots` (~1192): new `_base_roots()` (cwd, /tmp) keeps the exact prefix compare; only `self.extra_roots` use the `/`-aware `_path_within`. `_allowed_roots()` = base + extra (protected-path logic unchanged).
- `compute_write_roots`: a project root of `/` is not added (it is indistinguishable from an additionalDirectories root in the flat list, and the cwd covers it as before). A user-configured `/` in additionalDirectories still works.
- Tests (tests/test_unit_write_roots.py, new `TestCwdSlashIsNotARoot`, 7 tests): project root `/` not a root; with cwd `/`, redirect to /etc (with and without computed roots), `rm /etc/x`, `/usr/bin/curl`, `/bin/ls`, `_is_workspace_binary`, `_is_path_inside_workspace` all ask/False; `/tmp` rm still allowed; `/` extra root still allows from cwd `/`.
- Mutation proof (backup in /tmp/41-02-fix2-mut, diff-restored each time, git status identical):
  - M1 `_is_workspace_binary` rstrip back: test_workspace_binary_outside_still_asks FAILS (the survivor is now killed).
  - M2 `_is_path_inside_workspace` rstrip back: same test FAILS.
  - M3 base roots use `_path_within`: 3 FAIL (redirect x2, rm).
  - M4 project root `/` added: 2 FAIL.

## Issue 2 (LOW): RecursionError catch in settings_loader.py:97
Decision: keep, no code change. It matches the loader's existing JSONDecodeError/IOError posture; only the poisoned file's rules are lost, and HEAD crashed with no gate at all. Already documented by the earlier fix; covered by test_pathological_user_settings_fail_safe_and_the_gate_runs.

## Issue 3 (LOW): untested `_is_workspace_binary` rstrip
Closed by the Issue 1 tests (M1 above).

## Suite flake: test_case17
- It is `test_case17_confirm_log_respects_env_redirect` in tests/test_integration_pretool.py (`TestPermissionModeParity`), added by task 40-01 (commit cf2fe7b), not 41-01 or 41-02.
- Rewritten (same file, ~2493): the command carries a unique uuid marker; asserts the marker lands in the isolated CLAUDE_MANUAL_CONFIRM_LOG, and that the bytes the real ~/.claude/bash_manual_confirm.log gains during the run (read-only, from the pre-run size) do not contain it; also asserts the isolated path differs from the real one. Never writes the real log; concurrent live-session appends are irrelevant.
- Mutation: hook log path pointed at a different file (never the real log) -> test_case17 ERRORS/fails (as do case16/16b); restored, diff identical. Rerun of the module: 449 OK.

## Verification
py_compile clean. Full suite: `Ran 2129 tests, OK (skipped=2)` (/tmp/41-02-fix2-suite.log). `git status --short` unchanged vs. before (only pretool_hook.py, test_unit_write_roots.py, test_integration_pretool.py newly touched beyond the prior set). state.md and 41-05 report untouched.

## Questions for User
None
