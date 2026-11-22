# 50-06 Fix Report

Files changed: `.claude/hooks/epic_driver_lib.py`, `tests/test_unit_epic_driver.py` (no commit, no fixtures changed).

## Issue 1: launch retry backoff (manager decision 1)
- Change: constants `LAUNCH_RETRY_BASE_S = 600`, `LAUNCH_RETRY_MAX_S = 4*3600` in the constants block. New `launch_retry_wait_s(rec, now)`: the wait is `min(600 * 2**(launch_attempts-1), 4h)` from `launch_error.at` (10m, 20m, 40m, 1h20m, 2h40m, then 4h). `name_in_use` and "no error recorded" (a pass killed before the effect) are not gated. `_Dispatch.relaunch` returns early with a note while the wait is open; the commit-then-effect order of a re-issue is untouched. `_launch_result` resets `launch_attempts` to 1 on success. The `spawn_failed`/`resume_failed` alarm is unchanged (raised throughout).
- Tests: `test_the_launch_retry_schedule` (table incl. cap and 1 s before/at the boundary, name_in_use and no-error not gated); `test_a_failed_spawn_backs_off_doubling_to_the_cap_and_resets_on_success` (not re-issued 1 s before, re-issued at the interval, 7 attempts through the cap, alarm stays raised, reset on success); `test_name_in_use_keeps_its_every_tick_retry`; `test_a_failed_resume_backs_off_too`; `test_a_re_issued_launch_is_committed_before_it_runs` (invariant 5: killed after the commit, effect not run, `launch_attempts` and `sends` already recorded).

## Issue 2: refused dialog key retried (decision 2)
- Change: the `keyed` result mutation deletes the answer record when `outcome.ran` is False (guard refused, nothing sent). A key that ran keeps its record (`sent_at`, or `not_sent` text when the send command failed: outcome unknown). The record is still committed before the effect, so crash-after-commit stays recorded.
- Tests: `test_the_key_is_not_sent_once_the_dialog_is_gone` updated (record now cleared); new `test_a_refused_key_is_answered_on_the_next_pass_and_only_once` (copy-mode refusal, then answered once, never again); new `test_a_key_sent_before_a_crash_is_never_resent` (killed as the result is committed, key sent once, never again even if the dialog shows again); existing crash-between-commit-and-key test still covers "never sent". Crash matrix extended with scenario `dialog_refused` (first key guard sees copy-mode, killed at every commit in both modes), and the uncrashed check asserts exactly one key.

## Issue 3: retry without `line` (decision 3)
- Change: `retry_decisions` uses `e["line"]`, else `event_line(event, e["path"])`; with neither it sends nothing and keeps the entry (`continue`, no empty send).
- Tests: `test_a_retry_without_a_line_falls_back_to_the_event_line`, `test_a_retry_with_neither_line_nor_path_sends_nothing_and_keeps_the_entry`.

## Issue 4: re-rotation after `handoff_timeout` (decision 4)
- Change: `ROTATE_RETRY_AFTER_S = 3600` in the constants block; `_Dispatch.rotation_cooling_down(rec)` is true while `handoff_timeout.at` is younger than that, and the policy's rotate decision becomes reuse (same as `force_reuse`). After the cool-down (>= 1 h) rotation may happen again. `handoff_timeout` alarm and its clear rule untouched (the record goes with the old generation when a rotation completes or it is reaped).
- Tests: `test_inside_the_cool_down_after_a_handoff_timeout_the_session_is_reused` (49m59s: reuse, EVENT sent, alarm still raised), `test_after_the_cool_down_a_rotation_is_attempted_again` (exactly 1 h: ROTATE sent).

## Mutations (cp backups of lib and test file in /tmp, restored and confirmed with `diff`; test file identical to backup at the end)
| Mutation | Result |
|---|---|
| #1 backoff gate disabled in `relaunch` | killed: 9 failures (spawn schedule at every attempt, resume backoff) |
| #1 reset of `launch_attempts` on success removed | killed: the spawn schedule test |
| #2 refused key no longer clears the record | killed: refused-then-answered test, updated not-sent test, matrix `dialog_refused` uncrashed check |
| #2 record cleared even when the key ran | killed: `test_a_refused_key_is_answered...` (and the listed-key-once test errors) |
| #4 cool-down check removed | killed: `test_inside_the_cool_down...` |
| #3 fallback to `event_line` removed | killed: `test_a_retry_without_a_line...` |

## Runs
- `python3 -m py_compile` on both files: clean.
- Epic driver module (`-m unit_epic_driver`): `Ran 318 tests` / `OK` (was 307; +11).
- Full suite `python3 tests/run_all_tests.py` (the harness moved it to the background at 180 s; one copy only, awaited to the end): exit code 0, `Ran 2859 tests in 391.436s` / `OK (skipped=2)`. Baseline 2848 + 11 new.

## git status --short
```
 M .claude/hooks/epic_driver_lib.py
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
?? tests/fixtures/epic_driver/panes/cc-2.1.330-copy-mode-empty-box.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-input-empty-after-clear.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-input-event-line.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-input-multiline-paste.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-input-pasted-placeholder.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-input-typed.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-input-wrapped-40col.txt
?? tests/fixtures/epic_driver/panes/cc-2.1.330-shell-mode.txt
```
(`agents_output/` is gitignored, so this report does not appear; commit it with `git add -f`.)

## Questions for User
None.
