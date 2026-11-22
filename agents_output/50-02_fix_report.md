# Fix Report - 50-02 (Issue 1 only)

## Test added
`tests/test_unit_claude_reducer.py`, class `TestReduceEventsSessionEnd`:
`test_turn_open_before_session_end_stays_terminated`. Events `[turn_start, session_end]`
(no stop); asserts `state_hint == "terminated"` and `terminated` is True.
Note: the pre-existing `test_terminated` uses the same event list, so the case was already
covered in effect; the new test names the intent explicitly. No production code changed.

## Mutation check
Backed up `.claude/hooks/claude_event_reducer.py` with `cp` to /tmp/reducer_backup.py.
Mutation: before the state-hint block, `if any(e.get("event") == EVENT_TURN_START for e in events): out["terminated"] = False`
(any turn_start anywhere clears the latch). Result: the new test FAILED
(`None != 'terminated'`); the run showed failures=3, errors=1 in total (the mutation also
broke other tests). Restored with `cp` from the backup; `diff` reported IDENTICAL.

## Counts
- Reducer module (`python3 tests/test_unit_claude_reducer.py`) after restore: Ran 50 tests, OK.
- Full suite `python3 tests/run_all_tests.py`: `Ran 2489 tests in 368.770s` / `OK (skipped=2)`, exit code 0.
  (Review baseline 2488, +1 new test.)

## git status --short
```
 M .claude/hooks/claude_event_reducer.py
 M docs/amux-spawn-fleet-supervision.md
 M tasks/50_epic_driver/50-05-session-reconcile-and-stalls_opus.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_amux_reads.py
 M tests/test_unit_claude_reducer.py
 M tests/test_unit_spawn_producer.py
```
