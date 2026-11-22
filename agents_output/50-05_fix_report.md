# Fix Report - 50-05 (review: PASS, 2 MEDIUM, 4 LOW)

## Issue 1 (MEDIUM): `delivery_provably_absent` must prove absence
Change (`.claude/hooks/epic_driver_lib.py`): returns False unless ALL hold: transcript path set; `transcript_first_at` known and <= first send (tail not truncated before the send); no `turn_start` at or after the send; no user entry with `E-<n>`; pane captured, non-blank, id not on it.
- New `SessionReading.transcript_first_at`, filled in `_read_transcript` via a new public helper `amux_spawn_lib.transcript_tail_first_epoch` (additive; a test forbids `asl._` private use in the lib).
- Tests (`TestDeliveryConfirmation`): True case; typed-pane, uncaptured pane, landed prompt False; no transcript path / no first_at False; tail starting after the send False; `turn_start` at +5 s and at the send instant False; reviewer reproduction (no transcript, turn_start +5 s) False; a turn_start before the send still True. Plus a recon test that the reading records `transcript_first_at`.

## Issue 2 (MEDIUM): interrupted event turn
Change (`_reconcile_verdicts`): for each event turn, if another `turn_start` follows it and the first stop at/after it is later than that next start (or there is no stop), the turn is treated as unsettled: skipped, no verdict read, no limit read, not recorded in `verdict.turns`. Operator turns stay exempt (they are never in `_event_turns` unless their prompt names the event).
Tests (`TestReconcileVerdicts`): interrupt-then-operator-turn (operator stop says `ADVANCED: A`) leaves step `inflight`, no verdicts, no actions, no `verdict_missing`, `turns == []`; a normal turn followed by an operator turn still settles to `verifying`; existing operator-turn and ping tests unchanged and green.

Downstream behaviour (existing rules only, nothing added): the outbox entry stays confirmed with `verdict.complete = False`. `_verdict_followups` returns early because no turn was read (`line` and `invalid` both None), so NO `VERDICT?` reminder is emitted and NO `verdict_missing` is raised, even once the session is idle. The step stays `inflight`. So the reminder path does NOT apply to this case; the entry sits silently until something else acts. If the manager wants a reminder or alarm for this, it is a new rule (candidate for 50-06 or a later task); I did not add one.

## Issue 3 (LOW): `classify_pane` false positive
Change: the pointer is `❯`/`›` only (`>` removed). A pointer row is a dialog only with a menu hint line (`Enter to confirm` / `Esc to cancel` / arrows) BELOW it, or when the pointer row itself is numbered (`❯ 1. Yes`). The "indented sibling" rule is removed. All real fixtures have the footer hint, so every existing classification is unchanged.
Tests/fixtures: new `synth-conversation-quote.txt` (reviewer's `> quote` + indented bullets, empty `>` input box) and `synth-conversation-quote-pointer.txt` (same with `❯` quote) classify as no dialog; `>`-glyph empty input box is no dialog; `> No, exit` with the hint is not a dialog; numbered pointer row is a dialog; the fixture-set assertion lists both new files.

## Issues 4 and 6 (LOW)
No code change, as decided (recorded in 50-06's task file).

## Issue 5 (LOW): fixture account details
`cc-2.1.330-startup-no-dialog.txt` and `synth-booting-splash.txt`: "Claude Ultra" -> "Claude"; login-expiry line -> "Example notice line". Grep of all fixtures for tier names, login/expire, `/home`, `/data`, `@`, user/project names: clean. `test_fixtures_are_scrubbed` now also asserts no `/home`, `@`, Ultra, Max, "Pro ", login, expires. Code-mode regex test still green.

## Mutations (cp-backed, restored from copy, `diff` clean each time)
| Mutation | Killed by |
|---|---|
| drop transcript-coverage check | `test_not_provable_when_the_tail_may_be_truncated_before_the_send`, `test_not_provable_without_a_readable_transcript` |
| drop `turn_start` check | `test_not_provable_when_a_turn_started_at_or_after_the_send` (both subtests) |
| drop interrupted-turn guard | `test_an_interrupted_event_turn_does_not_borrow_the_operator_turns_stop` |

## Counts
- `python3 -m py_compile` on epic_driver_lib.py, amux_spawn_lib.py, tests/test_unit_epic_driver.py: clean.
- Module `unit_epic_driver`: Ran 238 tests, OK (was 229).
- Full suite `python3 tests/run_all_tests.py` (foreground-equivalent, finished with exit code 0): `Ran 2779 tests in 537.800s` / `OK (skipped=2)` (baseline 2770, +9 new).

## git status --short
```
 M .claude/hooks/amux_spawn_lib.py
 M .claude/hooks/epic_driver_lib.py
 M tasks/50_epic_driver/50-06-dispatch-policy-and-delivery_opus.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
?? tests/fixtures/epic_driver/
```
(agents_output/ is gitignored.) No commits, no git checkout/stash/reset/clean, no tmux interaction.

## Questions for User
None.
