# 50-06 Fix Report 2

Files changed: `.claude/hooks/epic_driver_lib.py`, `tests/test_unit_epic_driver.py`. No commit, no git state changes.

## Decision 1: a reboot resets the launch backoff
Option chosen: reset the counter (not "skip the gate once"). A counter reset is persisted state, so it is
committed before the launch effect (invariant 5) and survives a crash. A skip flag would need extra state.
- New `_Dispatch.reset_launch_backoff()`, called in `plan_dispatch` right after `compute_resume_now()`. When
  `recon.reboot` is set (boot_id change, 50-05's reboot facts), every session with a `launch_error` and a
  non-zero `launch_attempts` gets `launch_attempts = 0` in one commit-only op ("reboot: the launch backoff starts over").
- `launch_retry_wait_s` treats `launch_attempts == 0` as "gate open". `relaunch` counts from 0, so the
  re-issue records attempt 1. A failure after the reboot therefore waits 10 m again (not 40 m).
- `launch_error` is not touched, so the `spawn_failed`/`resume_failed` alarm stays raised until a
  successful launch (§16.2).
- The reset op is planned before the session loop, so it is committed before the relaunch effect.

Test `test_a_reboot_resets_the_launch_backoff_and_the_next_pass_launches`: a failed spawn, the backoff
reaches attempt 2 (20 m), the pass 60 s later is gated, a reboot appears, a pass killed after its first commit
shows `launch_attempts == 0` with the effect not run and `launch_error` kept. The next pass launches at once,
fails again, and records `launch_attempts == 1` with the alarm raised. The ladder restarts at 10 m: nothing at 599 s,
a launch at 600 s (attempt 2), nothing again 1199 s later.

## Decision 2: dialog key that ran and failed
`DispatchWorld.key_error` was added to the test fake: `send_keys` returns rc=1. Test
`test_a_key_that_ran_and_failed_keeps_its_record_and_is_never_sent_again`: one key is sent, the record
stays with `not_sent` and no `sent_at`, and three later passes send no second key.

## Mutations (cp backups in /tmp/f2, restored after each; `diff` against the backup identical every time)
| Mutation | Result |
|---|---|
| reviewer's: clear the dialog record when `not outcome.ok` instead of `not outcome.ran` | killed: the new ran-and-failed test |
| `reset_launch_backoff` call removed | killed: the reboot test |
| `reset_launch_backoff` sets nothing (op without the reset) | killed: the reboot test |
| re-issue after the reset counts as attempt 2 (`max(1, n) + 1`) | killed: the reboot test (the ladder would start at 20 m) |
Final `diff` of both files against the pre-mutation backups: identical.

## Runs
- `python3 -m py_compile` on both files: clean.
- Epic driver module (`cd tests; python3 -m unittest test_unit_epic_driver`): `Ran 320 tests` / `OK` (was 318; +2).
- Full suite `python3 tests/run_all_tests.py` (foreground, one copy; the harness moved it to the background at 180 s and it was awaited): exit code 0, `Ran 2861 tests in 391.383s` / `OK (skipped=2)`. Baseline 2859, +2.

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
(`agents_output/` is gitignored; commit this report with `git add -f`.)

## Questions for User
None.
