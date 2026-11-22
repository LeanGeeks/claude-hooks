# Implementation Report - 50-02 Lifecycle: clear the `terminated` latch

## Summary

`reduce_events` now clears `terminated` on a `turn_start`, so a resumed session reads `running`/`idle` by the normal rules. A later `session_end` sets the latch again. The reducer is still pure stdlib. No new event type, hook or installer change. The producer and `_derive_claude_status` needed no code change; both are pinned by tests.

## Files Created

None.

## Files Modified

- `.claude/hooks/claude_event_reducer.py` — `turn_start` branch sets `terminated = False`. Module docstring updated: the table row now reads "session_end seen, no later turn_start", plus a paragraph saying the latch is not permanent.
- `tests/test_unit_claude_reducer.py` — replaced `test_terminated_dominates_open_turn` with 5 tests: session_end alone is terminated; `session_end, turn_start` is running; `session_end, turn_start, stop` is idle; a second `session_end` latches again; a partial tail after `session_end` does not clear.
- `tests/test_unit_spawn_producer.py` — `UserPromptSubmit` on a stored-`terminated` handle writes `running` and appends a `turn_start`.
- `tests/test_unit_amux_reads.py` — a handle stored `spawning` with a live pane and log `session_end, turn_start` reads `running` via `cli._derive_status`. `_derive_claude_status` only short-circuits on stored `terminated` or a dead pane, so no code change was needed.
- `docs/amux-spawn-fleet-supervision.md` — it had no "stays terminated" wording. I added a sentence in the diagnostic section saying `terminated` lasts only until the next `turn_start`.

## Verification

- Compile/import: PASS (`py_compile` on the reducer and the 3 changed test files; the suite imports them).
- Tests: `python3 tests/run_all_tests.py` ran 2488 tests, OK, 2 skipped. The baseline was 2482, so the delta is +6: 5 reducer tests replace 1, plus 1 producer and 1 derived-status.
- Installed (install.sh --yes re-run): not applicable.

## Decisions

- One existing test had to change, contrary to "existing suite passes untouched". `test_unit_claude_reducer.TestReduceEventsSessionEnd.test_terminated_dominates_open_turn` asserted `[turn_start, session_end, turn_start]` gives `terminated`, which is exactly the behaviour P0-4 reverses. I inverted it (`test_turn_start_after_session_end_clears_latch`) and left every other existing test untouched. The manager should confirm this is acceptable.
- The derived-status test is in `test_unit_amux_reads.py`, next to the other `_derive_status` tests, using the existing lifecycle-log helpers. It does not drive the CLI.
- The producer `state = "running"` behaviour is unchanged.

## Blockers

None.

## Questions for User

None.
