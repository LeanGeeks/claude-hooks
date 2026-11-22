# Fix Report 3 - 50-05: review 2 LOWs

## Changes (`.claude/hooks/epic_driver_lib.py`)
- Issue 1: `_settled_idle` keeps the reducer idle/turn_open check (it does the work: a stop with `background_tasks_count>0` leaves the latest turn stopped but the state `running`). The latest-stop check is normally implied by reducer idle but not provably in every case, so it stays as a cheap guard; the docstring says why.
- Issue 2: in `_reconcile_verdicts`, when the event is unanswered (interrupted and settled-idle), `reminded_at` is set and some `turn_start` is at or after `reminded_at` (settled or interrupted), `vstate["after_reminder"]` is set. `_verdict_followups` then raises `verdict_missing` and never sends a second reminder (the `reminded_at` guard is unchanged). A later valid `E-<n>` verdict still settles the event and clears the alarm.
- Issue 3: no code change (manager added the obligations to 50-06's task file).

## Tests (`tests/test_unit_epic_driver.py`, `TestReconcileVerdicts`)
- `test_interrupted_event_turn_with_background_tasks_running_emits_nothing`: interrupted event turn, operator turn stopped with `background_tasks_count=1` (reducer `running`) -> no action, no condition.
- `test_interrupted_reminder_turn_raises_verdict_missing_without_a_second_reminder`: reminder sent -> reminder turn interrupted -> operator turn -> idle: `verdict_missing:E-1`, no actions; then a later `E-1` turn with `ADVANCED: A` clears it, no actions, step `verifying`.

## Mutations (cp-backed /tmp/edl3.bak; restored and `diff` clean each time)
| Mutation | Result |
|---|---|
| `_settled_idle` reducer idle/turn_open check removed | killed by the background-tasks test (1 failure) |
| after-reminder promotion disabled (`and False`) | killed by the interrupted-reminder test (1 failure) |

## Counts
- `python3 -m py_compile` on epic_driver_lib.py, amux_spawn_lib.py, tests/test_unit_epic_driver.py: clean.
- Module `--module unit_epic_driver`: Ran 243 tests, OK (was 241).
- Full suite, foreground (`python3 tests/run_all_tests.py`), exit code 0: `Ran 2784 tests in 408.691s` / `OK (skipped=2)` (baseline 2782, +2).

## git status --short
```
 M .claude/hooks/amux_spawn_lib.py
 M .claude/hooks/epic_driver_lib.py
 M tasks/50_epic_driver/50-06-dispatch-policy-and-delivery_opus.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
?? tests/fixtures/epic_driver/
```
No commits, no git checkout/stash/reset/clean, no tmux interaction.

## Questions for User
None.
