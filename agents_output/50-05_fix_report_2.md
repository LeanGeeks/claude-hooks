# Fix Report 2 - 50-05: interrupted event turn (silent stall)

## Change (`.claude/hooks/epic_driver_lib.py`)
- `_reconcile_verdicts`: the interrupted-turn skip (a later `turn_start` before any stop of the event turn) now also sets a local `interrupted` flag. Still no verdict, no limit read and nothing in `verdict.turns` from that turn; the operator turn stays exempt and its verdict line is not applied.
- New `_settled_idle(p, reading, starts, stops)`: reducer `state_hint == "idle"`, no `turn_open`, and the latest `turn_start` has a stop at/after it.
- `_verdict_followups(..., unanswered=False)`: when `unanswered` (interrupted and settled idle) and no usage limit was recorded (`limited_at` is None, so §12.4 still comes first), the event counts as "ended without a verdict". The existing rule then applies unchanged: one `VERDICT? E-<n>` reminder (`reminded_at` guard), then `verdict_missing` once the reminder turn (after `reminded_at`) ends without a valid verdict. A later turn naming `E-<n>` with a valid verdict settles the event and clears the alarm (existing path). While a turn is open, nothing is emitted.

## Tests (`TestReconcileVerdicts`)
- `test_an_interrupted_event_turn_does_not_borrow_the_operator_turns_stop` (kept; now via a shared helper).
- `test_interrupted_event_turn_then_idle_gets_one_reminder_and_the_reminder_settles_it`: interrupt, operator turn, idle -> exactly one `verdict_reminder` for E-1 (step still inflight, operator `ADVANCED: A` not applied); with `reminded_at` set, the `VERDICT? E-1` turn's `ADVANCED: A` -> step `verifying`, verdict complete, no actions, no alarm.
- `test_interrupted_event_turn_while_the_operator_turn_is_open_emits_nothing`.
- `test_interrupted_event_turn_without_a_verdict_after_the_reminder_raises_verdict_missing`: reminder turn says "Looking." -> `verdict_missing:E-1`, no second reminder; a later `E-1` turn with `ADVANCED: A` clears it and settles the step.

## Mutations (cp-backed copy /tmp/edl.bak, restored, `diff` clean each time)
| Mutation | Killed by |
|---|---|
| drop the `unanswered` term in `read_turns` | the reminder/settle test |
| `_settled_idle` always True (no idle/stop check) | the operator-turn-open test |
| drop only the reducer idle/turn_open check | survives: redundant with the "latest turn stopped" check (same condition in practice); both checks kept per spec |

## Counts
- `python3 -m py_compile` on epic_driver_lib.py, amux_spawn_lib.py, tests/test_unit_epic_driver.py: clean.
- Module `test_unit_epic_driver`: Ran 241 tests, OK (was 238).
- Full suite `python3 tests/run_all_tests.py`, foreground-equivalent, exit code 0: `Ran 2782 tests in 541.980s` / `OK (skipped=2)` (baseline 2779, +3).

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
