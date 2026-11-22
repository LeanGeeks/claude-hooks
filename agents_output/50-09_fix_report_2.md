# Fix Report 2 - 50-09 (worker HANDOFF consistency)

## What the code did before
No bug. In `read_verdict` (`.claude/hooks/epic_driver_lib.py`) the `HANDOFF` branch runs before the
`not from_driver and verb not in (DONE, HALT)` check. A worker session's `HANDOFF: <abs path>` in a rotate turn
is therefore already valid (`INVALID_HANDOFF` otherwise). `reconcile` passes `rotate="rotate" in kinds` for every
session, workers included, and records `rotation["handoff"]`. `handoff_complete` and `choose_action` then give
`ACT_SPAWN_NEXT`. Discrepancy #5 holds only for non-rotate events: there a worker may end only DONE/HALT.

## Code change
None. `epic_driver_lib.py` is untouched, so `py_compile` was run only as a check and passed.

## Tests (tests/test_unit_epic_driver.py, +4, all pins)
- `test_worker_handoff_is_valid_only_in_a_rotate_turn` (parser): worker HANDOFF valid with rotate=True,
  `INVALID_HANDOFF` without it. ADVANCED and WAITING from a worker stay `INVALID_WORKER_VERB` even in a rotate turn.
- `test_worker_handoff_outside_a_rotate_turn_is_not_applied` (reconcile): invalid, no handoff recorded, not complete.
- `test_worker_handoff_in_a_rotate_turn_is_recorded` (reconcile): handoff recorded, `rotation_ready`, verdict complete.
- `test_a_worker_rotation_ends_with_handoff_and_spawns_the_next_generation` (TestRotation, full tick): rotate event to
  `impl:W` over the cap, worker ends `HANDOFF:` plus a newer handoff file, `ACT_SPAWN_NEXT`, gen 2 spawn
  (`t-impl-w-g2`), seed items `role, handoff`.
- The prompt test is renamed `test_worker_examples_are_done_halt_or_a_rotate_handoff`. It now allows DONE/HALT/HANDOFF
  examples, checks that each HANDOFF example is valid via `read_verdict(rotate=True, from_driver=False)`, and checks
  that the prompt scopes HANDOFF to a rotate turn.

## Prompt change (docs/prompts/epic-driver-worker.md)
- Section 3: "Workers end with `DONE` or `HALT`, and with `HANDOFF:` only in a `rotate` turn (section 5); any other
  verb, `HANDOFF` in any other turn, ... is invalid".
- Section 5: states that this is the one turn where HANDOFF is valid and that it needs no step id. Added a
  `verdict` block `HANDOFF: /abs/path/of/the/handoff-file.md`.
- Line counts: worker 95 (was 91), driver 159 (unchanged); both under 250 (the sum is 254).

## Other LOW
`test_no_push_without_dash_c` per-line heuristic: accepted as a known limit, no change.

## Verification
- `python3 -m unittest test_unit_epic_driver` (from tests/): Ran 501 tests, OK.
- `python3 tests/run_all_tests.py` (foreground, last): `Ran 3046 tests in 436.440s` / `OK (skipped=2)`, exit code 0
  (baseline 3042 + 4 new).

## git status --short
```
 M tasks/50_epic_driver/architecture.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
?? docs/prompts/epic-driver-driver.md
?? docs/prompts/epic-driver-worker.md
```
(plus this report under the gitignored `agents_output/`). Nothing committed.

## Questions for User

None.
